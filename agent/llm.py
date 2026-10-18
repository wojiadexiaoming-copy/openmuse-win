"""§4.1 LLM 适配层：OpenAI 兼容接口。

M1 只实现 chat()（非流式）。流式 / 多模态 / 重试退避是 M4（spec §32.5 前不用）。
"""
from __future__ import annotations

import contextvars
from copy import deepcopy
import json
import logging
import math
import os
import time
import threading
import uuid

from openai import OpenAI

# Keep the real SDK type even when tests inject a constructor factory.
_SDK_OPENAI_TYPE = OpenAI

from .models import Message, ToolCall
from .vision_diagnostics import collect_request_metadata

logger = logging.getLogger(__name__)
_request_context = contextvars.ContextVar("llm_request_context", default=None)
_routing_runtime_lock = threading.Lock()


def _routing_runtime_kwargs(cfg, vault):
    """Share only health state across role factories for this config/Vault owner."""
    from .llm_router import RoutingHealthState
    with _routing_runtime_lock:
        runtime = cfg.get("_routing_runtime")
        if (not isinstance(runtime, dict) or runtime.get("_vault_owner") is not vault
                or not isinstance(runtime.get("health_state"), RoutingHealthState)
                or not runtime.get("owner_scope")):
            runtime = {"health_state": RoutingHealthState(), "owner_scope": uuid.uuid4().hex,
                       "_vault_owner": vault}
            cfg["_routing_runtime"] = runtime
        return {"health_state": runtime["health_state"], "owner_scope": runtime["owner_scope"]}



_ALLOWED_REQUEST_OPTIONS = {
    "extra_body", "top_p", "presence_penalty", "frequency_penalty", "response_format", "seed",
}
_RESERVED_BODY_FIELDS = {
    "model", "messages", "tools", "tool_choice", "functions", "function_call",
    "api_key", "api_key_env", "base_url", "headers", "extra_headers", "authorization",
    "auth", "authentication", "credentials", "access_token", "password", "secret",
    "provider", "organization", "project", "stream",
}


def _validate_extra_body(value, ancestors=None):
    """Keep provider extensions from overriding payload identity or credentials."""
    if ancestors is None:
        ancestors = set()
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in ancestors:
            raise ValueError("extra_body must contain acyclic JSON data")
        ancestors.add(identity)
        try:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if not isinstance(key, str) or key.casefold() in _RESERVED_BODY_FIELDS:
                        raise ValueError("extra_body cannot override identity, messages, tools or authentication")
                    _validate_extra_body(nested, ancestors)
            else:
                for nested in value:
                    _validate_extra_body(nested, ancestors)
        finally:
            ancestors.remove(identity)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError("extra_body must contain JSON data")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("extra_body cannot contain non-finite numbers")


def _request_options_snapshot(options):
    if options is None:
        return {}
    if not isinstance(options, dict) or set(options) - _ALLOWED_REQUEST_OPTIONS:
        raise ValueError("Unsupported model request option")
    if "extra_body" in options:
        if not isinstance(options["extra_body"], dict):
            raise ValueError("extra_body must be a JSON object")
        _validate_extra_body(options["extra_body"])
    snapshot = deepcopy(options)
    if "extra_body" in snapshot:
        _validate_extra_body(snapshot["extra_body"])
    return snapshot


def _resolve_role_cfg(cfg: dict, role: str, _depth: int = 0) -> dict:
    """取角色的模型配置，支持 ref 间接引用，防循环。"""
    if _depth > 5:
        raise RuntimeError(f"models.roles 引用循环：{role}")
    models = cfg.get("models", {})
    roles = models.get("roles", {})
    if role in roles:
        rc = roles[role]
        if isinstance(rc, dict) and "ref" in rc:
            return _resolve_role_cfg(cfg, rc["ref"], _depth + 1)
        return rc
    # 向后兼容：没有 models 表时，main/planner 回落到顶层 llm
    if role in ("main", "planner"):
        return cfg["llm"]
    raise KeyError(f"models.roles 缺角色配置：{role}")


def _expand_env(value: str) -> str:
    """展开 ${VAR} 环境变量（如 CF_ACCOUNT_ID，避免账号 ID 进仓库）。"""
    import re
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), value)


def _adapt_cfg(role_cfg: dict) -> dict:
    """把角色配置适配成 from_config 能够解析的形状。"""
    llm = {
        "base_url": _expand_env(role_cfg["base_url"]),
        "model": role_cfg["model"],
        "supports_images": role_cfg.get("supports_images"),
        "temperature": role_cfg.get("temperature", 0.7),
        "max_tokens": role_cfg.get("max_tokens", 8192),
    }
    fb = role_cfg.get("fallback")
    if fb and fb.get("enabled", True):
        llm["fallback"] = {
            "enabled": True,
            "base_url": _expand_env(fb["base_url"]),
            "model": fb["model"],
            "supports_images": fb.get("supports_images", False),
            "temperature": fb.get("temperature", 0.7),
            "max_tokens": fb.get("max_tokens", 8192),
        }
    return {"llm": llm}


def _response_usage(usage) -> dict:
    """Keep only complete, uncoerced token counters supplied by this response."""
    try:
        fields = ("prompt_tokens", "completion_tokens", "total_tokens")
        values = {field: (usage.get(field) if isinstance(usage, dict)
                          else getattr(usage, field, None)) for field in fields}
        if not all(type(value) is int and 0 <= value <= 2**63 - 1
                   for value in values.values()):
            return {"usage_available": False}
        return {"usage_available": True, **values}
    except Exception:
        return {"usage_available": False}


class VisionFallbackUnavailable(RuntimeError):
    """The selected fallback is not explicitly allowed to receive image payloads."""


def resolve_key_for_target(base_url: str, model: str, default_key: str = "", vault=None) -> str:
    """按目标服务商智能匹配最合适的 API Key，防止多服务商切换/回退时 key 错配。"""
    url_lower = (base_url or "").lower()
    model_lower = (model or "").lower()

    # 1. Agnes: apihub.agnes-ai.com 或 agnes-3.0-*
    if "agnes-ai.com" in url_lower or "agnes" in model_lower:
        env_key = os.environ.get("ALTEREGO_AGNES_API_KEY", "").strip()
        if env_key:
            return env_key
        if vault:
            try:
                k = vault.resolve("agnes_api_key", caller="agent.llm:resolve_key_for_target")
                if k:
                    return k
            except Exception:
                pass
        if default_key and default_key.startswith("sk-"):
            return default_key

    # 2. Cloudflare Workers AI: api.cloudflare.com 或 @cf/
    elif "cloudflare.com" in url_lower or model_lower.startswith("@cf/"):
        env_key = (os.environ.get("ALTEREGO_CLOUDFLARE_API_KEY", "") or os.environ.get("ALTEREGO_BACKUP_LLM_API_KEY", "")).strip()
        if env_key:
            return env_key
        if default_key and default_key.startswith("cfat_"):
            return default_key
        if vault:
            try:
                k = vault.resolve("backup_llm_api_key", caller="agent.llm:resolve_key_for_target")
                if k:
                    return k
            except Exception:
                pass

    # 3. Gemini: generativelanguage.googleapis.com 或 gemini-
    elif "generativelanguage.googleapis.com" in url_lower or "gemini" in model_lower:
        env_key = (os.environ.get("ALTEREGO_GEMINI_API_KEY", "") or os.environ.get("ALTEREGO_LLM_API_KEY", "")).strip()
        if env_key:
            return env_key
        if default_key and (default_key.startswith("AIzaSy") or default_key.startswith("AQ.")):
            return default_key
        if vault:
            try:
                k = vault.resolve("gemini_api_key", caller="agent.llm:resolve_key_for_target")
                if k:
                    return k
            except Exception:
                pass
            try:
                k = vault.resolve("llm_api_key", caller="agent.llm:resolve_key_for_target")
                if k:
                    return k
            except Exception:
                pass

    # 4. 阿里云百炼 / DashScope: aliyuncs.com 或 kimi-k3 / qwen3.8-*
    elif "aliyuncs.com" in url_lower or "dashscope" in url_lower or any(m in model_lower for m in ("kimi-k3", "qwen3.8-omni-flash", "qwen3.8-max")):
        env_key = (os.environ.get("DASHSCOPE_API_KEY", "") or os.environ.get("ALTEREGO_DASHSCOPE_API_KEY", "") or os.environ.get("BAILIAN_API_KEY", "")).strip()
        if env_key:
            return env_key
        if vault:
            for k_name in ("dashscope_api_key", "bailian_api_key"):
                try:
                    k = vault.resolve(k_name, caller="agent.llm:resolve_key_for_target")
                    if k:
                        return k
                except Exception:
                    pass
        if default_key and default_key.startswith("sk-"):
            return default_key

    return default_key


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str,
                 temperature: float = 0.7, max_tokens: int = 8192,
                 fallback: LLMClient | None = None,
                 supports_images: bool | None = None, request_options: dict | None = None):
        # [M2-§11 完成] api_key 优先从 Vault 取（vault://llm_api_key），环境变量作为 fallback。
        clean_url = (base_url or "").rstrip("/")
        if clean_url.endswith("/chat/completions"):
            clean_url = clean_url[:-len("/chat/completions")].rstrip("/")
        self.base_url = clean_url
        self.client = OpenAI(base_url=self.base_url, api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.fallback = fallback
        self.supports_images = supports_images
        self._request_observer = None
        self.request_options = _request_options_snapshot(request_options)

    @classmethod
    def from_config(cls, cfg: dict, api_key: str = "", fallback_api_key: str = "", vault=None) -> "LLMClient":
        routing = cfg.get("llm_router", {})
        if routing.get("enabled") is True:
            from .llm_router import build_routed_client
            return build_routed_client(routing, role="main", vault=vault, **_routing_runtime_kwargs(cfg, vault))
        llm = cfg["llm"]
        if not api_key and vault:
            try:
                api_key = vault.resolve("llm_api_key", caller="agent.llm")
            except Exception:
                pass
        api_key = api_key or os.environ.get("ALTEREGO_LLM_API_KEY", "")
        # 按主模型 provider 修正 key
        api_key = resolve_key_for_target(
            llm.get("base_url", ""), llm.get("model", ""), default_key=api_key, vault=vault
        )
        if not api_key:
            raise RuntimeError("缺 LLM API Key：环境变量 ALTEREGO_LLM_API_KEY 或 vault（M2）")

        fallback_client: LLMClient | None = None
        fb_cfg = llm.get("fallback")
        if fb_cfg and fb_cfg.get("enabled", True):
            resolved_fb_key = resolve_key_for_target(
                fb_cfg.get("base_url", ""), fb_cfg.get("model", ""),
                default_key=fallback_api_key or os.environ.get("ALTEREGO_BACKUP_LLM_API_KEY", ""),
                vault=vault
            )
            if not resolved_fb_key and not fallback_api_key and vault:
                try:
                    fallback_api_key = vault.resolve("backup_llm_api_key", caller="agent.llm")
                except Exception:
                    pass
            fb_key = resolved_fb_key or fallback_api_key or os.environ.get("ALTEREGO_BACKUP_LLM_API_KEY", "") or api_key

            if fb_key:
                fallback_client = cls(
                    base_url=fb_cfg["base_url"],
                    model=fb_cfg["model"],
                    api_key=fb_key,
                    temperature=fb_cfg.get("temperature", 0.7),
                    max_tokens=fb_cfg.get("max_tokens", 8192),
                    fallback=None,
                    supports_images=fb_cfg.get("supports_images", False),
                )

        return cls(
            base_url=llm["base_url"],
            model=llm["model"],
            api_key=api_key,
            temperature=llm.get("temperature", 0.7),
            max_tokens=llm.get("max_tokens", 8192),
            fallback=fallback_client,
            supports_images=llm.get("supports_images"),
        )

    @classmethod
    def for_role(cls, cfg: dict, role: str, vault=None, api_key: str = "", fallback_api_key: str = "") -> "LLMClient":
        """按角色取模型客户端。支持专属 key 隔离与自动回落。"""
        routing = cfg.get("llm_router", {})
        if routing.get("enabled") is True:
            from .llm_router import build_routed_client, build_embedding_client
            if role == "memory_embedding":
                return build_embedding_client(routing, role=role, vault=vault, **_routing_runtime_kwargs(cfg, vault))
            return build_routed_client(routing, role=role, vault=vault, **_routing_runtime_kwargs(cfg, vault))
        role_cfg = _resolve_role_cfg(cfg, role)
        key_name = {"main": "llm_api_key"}.get(role, f"{role}_api_key")
        if not api_key and vault:
            try:
                api_key = vault.resolve(key_name, caller=f"agent.llm:{role}")
            except Exception:
                pass
        api_key = api_key or os.environ.get(f"ALTEREGO_{role.upper()}_API_KEY", "")
        # 非 main 角色若未配专属 key，自动回落主 key
        if not api_key:
            if vault:
                try:
                    api_key = vault.resolve("llm_api_key", caller=f"agent.llm:{role}:fallback_main")
                except Exception:
                    pass
            api_key = api_key or os.environ.get("ALTEREGO_LLM_API_KEY", "")

        # 智能匹配角色主 key 与 fallback key
        target_url = (role_cfg.get("base_url") or cfg.get("llm", {}).get("base_url", ""))
        target_model = (role_cfg.get("model") or cfg.get("llm", {}).get("model", ""))
        api_key = resolve_key_for_target(target_url, target_model, default_key=api_key, vault=vault)

        fb_cfg = role_cfg.get("fallback")
        if fb_cfg and fb_cfg.get("enabled", True):
            fallback_api_key = resolve_key_for_target(
                fb_cfg.get("base_url", ""), fb_cfg.get("model", ""),
                default_key=fallback_api_key or os.environ.get("ALTEREGO_BACKUP_LLM_API_KEY", ""),
                vault=vault
            )
        if not fallback_api_key and vault:
            try:
                fallback_api_key = vault.resolve("backup_llm_api_key", caller=f"agent.llm:{role}:fallback")
            except Exception:
                pass
        fallback_api_key = fallback_api_key or os.environ.get("ALTEREGO_BACKUP_LLM_API_KEY", "")

        return cls.from_config(
            _adapt_cfg(role_cfg),
            api_key=api_key,
            fallback_api_key=fallback_api_key,
            vault=vault,
        )

    def set_request_observer(self, observer):
        """Attach an optional metadata-only callback to this client and fallbacks."""
        self._request_observer = observer
        if self.fallback:
            self.fallback.set_request_observer(observer)
        return self

    def _observe_request(self, payload, tools, context, started, success, error=None,
                         phase="request", request_attempted=True, usage=None):
        if self._request_observer is None:
            return
        try:
            metadata = collect_request_metadata(self.model, self.base_url, payload, tools)
            metadata.update({
                "request_id": context["request_id"],
                "requested_model": context["requested_model"],
                "is_fallback": context["fallback_depth"] > 0,
                "fallback_depth": context["fallback_depth"],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "success": success,
                "phase": phase,
                "request_attempted": request_attempted,
            })
            metadata.update(usage or {"usage_available": False})
            if error is not None:
                metadata["error_type"] = type(error).__name__
            self._request_observer(metadata)
        except Exception as exc:
            # Auditing failure must not change the reply or trigger paid fallback calls.
            logger.warning("LLM request observer failed (%s)", type(exc).__name__)

    def chat(self, messages: list[Message], tools: list[dict] | None = None,
             known_tools: set[str] | None = None) -> Message:
        """调一次 LLM，按实际发出的主/备请求分别报告脱敏诊断元数据。"""
        context = _request_context.get()
        root_token = None
        if context is None:
            context = {"request_id": uuid.uuid4().hex, "requested_model": self.model,
                       "fallback_depth": 0}
            root_token = _request_context.set(context)
        try:
            payload = [self._to_openai(m) for m in messages]
            kwargs: dict = {
                "model": self.model, "messages": payload,
                "temperature": self.temperature, "max_tokens": self.max_tokens,
            }
            kwargs.update(_request_options_snapshot(self.request_options))
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            started = time.perf_counter()
            usage = {"usage_available": False}
            try:
                for attempt in range(2):
                    try:
                        if isinstance(self.client, _SDK_OPENAI_TYPE):
                            # Inspect original JSON before Pydantic coerces invalid counters.
                            raw_response = self.client.chat.completions.with_raw_response.create(**kwargs)
                            try:
                                body = raw_response.http_response.json()
                                usage = _response_usage(body.get("usage") if isinstance(body, dict) else None)
                            except Exception:
                                pass
                            resp = raw_response.parse()
                        else:
                            resp = self.client.chat.completions.create(**kwargs)
                            usage = _response_usage(getattr(resp, "usage", None))
                        break
                    except Exception as _req_exc:
                        max_retries = getattr(self.client, "max_retries", 2)
                        if max_retries > 0 and type(_req_exc).__name__ == "RateLimitError" and attempt == 0:
                            logger.info("[%s] 遇到频率限制 (RateLimitError)，等待 2 秒后重试...", self.model)
                            time.sleep(2.0)
                            continue
                        raise _req_exc
                result = self._from_openai(resp, known_tools=known_tools)
                result.metadata.update({"actual_model": self.model, **usage})
            except Exception as exc:
                self._observe_request(payload, tools, context, started, False, exc, usage=usage)
                if self.fallback:
                    self.fallback.set_request_observer(self._request_observer)
                    fallback_context = {
                        **context, "fallback_depth": context["fallback_depth"] + 1,
                    }
                    has_images = any(
                        isinstance(message.get("content"), list)
                        and any(isinstance(part, dict) and part.get("type") == "image_url"
                                for part in message["content"])
                        for message in payload
                    )
                    if has_images and self.fallback.supports_images is not True:
                        unavailable = VisionFallbackUnavailable(
                            "主模型视觉请求失败，备用模型未明确支持图像；"
                            "已停止视觉任务，未向备用模型发送截图。")
                        self.fallback._observe_request(
                            payload, tools, fallback_context, time.perf_counter(), False,
                            unavailable, phase="fallback_capability_blocked",
                            request_attempted=False,
                        )
                        raise unavailable from None
                    logger.warning(
                        "主模型 [%s] 调用失败 (%s)，自动故障转移至 [%s]",
                        self.model, type(exc).__name__, self.fallback.model,
                    )
                    fallback_token = _request_context.set(fallback_context)
                    try:
                        return self.fallback.chat(messages, tools=tools, known_tools=known_tools)
                    finally:
                        _request_context.reset(fallback_token)
                raise
            self._observe_request(payload, tools, context, started, True, usage=usage)
            return result
        finally:
            if root_token is not None:
                _request_context.reset(root_token)

    @staticmethod
    def _to_openai(m: Message) -> dict:
        d: dict = {"role": m.role}
        if m.images and m.role != "tool":
            parts = [{"type": "text", "text": m.content or ""}]
            for img in m.images:
                b64 = img.data_base64
                if not b64 and img.data:
                    import base64
                    b64 = base64.b64encode(img.data).decode("ascii")
                mime = img.mime_type or img.mime or "image/jpeg"
                parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{b64}"
                    }
                })
            d["content"] = parts
        else:
            d["content"] = m.content

        if m.tool_calls:
            calls = []
            for tc in m.tool_calls:
                if isinstance(tc, dict):
                    tc_id = tc.get("id")
                    tc_name = tc.get("name")
                    tc_args = tc.get("arguments", {})
                    extra_content = tc.get("extra_content")
                else:
                    tc_id = tc.id
                    tc_name = tc.name
                    tc_args = tc.arguments
                    extra_content = getattr(tc, "extra_content", None)
                args_str = json.dumps(tc_args, ensure_ascii=False) if isinstance(tc_args, dict) else str(tc_args)
                item = {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tc_name,
                        "arguments": args_str,
                    }
                }
                if extra_content:
                    item["extra_content"] = extra_content
                calls.append(item)
            d["tool_calls"] = calls
        if m.role == "tool":
            d["tool_call_id"] = m.tool_call_id
            d["name"] = m.name
        return d

    @staticmethod
    def _from_openai(resp, known_tools: set[str] | None = None) -> Message:
        import ast
        import re
        import uuid
        choice = resp.choices[0].message
        tcs = []
        for tc in (choice.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {"raw": tc.function.arguments}
            extra_content = getattr(tc, "extra_content", None)
            if extra_content is None and isinstance(tc, dict):
                extra_content = tc.get("extra_content")
            tcs.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args, extra_content=extra_content))

        # 适配 Cloudflare Workers AI / 开源网关将 tool_call 放在 message.content 中的情况
        content_val = choice.content
        if not content_val and not _request_context.get() and getattr(choice, "reasoning_content", None):
            content_val = choice.reasoning_content
        if not tcs and content_val:
            def _normalize_tool_dict(item):
                if not isinstance(item, dict):
                    return None
                # 严格信封优先：支持 {"tool_call": ...} 或 {"tool_calls": [...]}
                if "tool_call" in item and isinstance(item["tool_call"], dict):
                    item = item["tool_call"]
                elif "tool_calls" in item and isinstance(item["tool_calls"], list) and len(item["tool_calls"]) > 0:
                    if isinstance(item["tool_calls"][0], dict):
                        item = item["tool_calls"][0]

                # 名字只认 name（删掉 tool/function/action 别名以杜绝误触发）
                name = item.get("name")
                if not name or not isinstance(name, str):
                    return None

                # 校验工具名是否在注册表（known_tools）中
                if known_tools is not None and name not in known_tools:
                    try:
                        from agent.tracing import Tracer
                        Tracer().log("toolcall_rejected", {"name": name})
                    except Exception:
                        pass
                    return None

                # 参数认 arguments，兼容 parameters
                args = item.get("arguments")
                if args is None:
                    args = item.get("parameters")
                if args is None:
                    args = {}

                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"raw": args}
                elif not isinstance(args, dict):
                    args = {"value": args}
                return name, args

            if isinstance(content_val, dict):
                norm = _normalize_tool_dict(content_val)
                if norm:
                    tcs.append(ToolCall(
                        id=f"cf_call_{uuid.uuid4().hex[:8]}",
                        name=norm[0],
                        arguments=norm[1],
                    ))
                    content_val = ""
            elif isinstance(content_val, str):
                trimmed = content_val.strip()
                # 1. 尝试直接整段 JSON 解析
                if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
                    try:
                        parsed_obj = None
                        try:
                            parsed_obj = json.loads(trimmed)
                        except Exception:
                            parsed_obj = ast.literal_eval(trimmed)
                        if isinstance(parsed_obj, dict):
                            norm = _normalize_tool_dict(parsed_obj)
                            if norm:
                                tcs.append(ToolCall(
                                    id=f"cf_call_{uuid.uuid4().hex[:8]}",
                                    name=norm[0],
                                    arguments=norm[1],
                                ))
                                content_val = ""
                        elif isinstance(parsed_obj, list):
                            for item in parsed_obj:
                                norm = _normalize_tool_dict(item)
                                if norm:
                                    tcs.append(ToolCall(
                                        id=f"cf_call_{uuid.uuid4().hex[:8]}",
                                        name=norm[0],
                                        arguments=norm[1],
                                    ))
                            if tcs:
                                content_val = ""
                    except Exception:
                        pass

                # 2. 扫描 Markdown 代码块中的 ```json ... ```
                if not tcs:
                    blocks = list(re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", content_val))
                    if blocks:
                        clean_text = content_val
                        any_matched = False
                        for b in blocks:
                            block_content = b.group(1).strip()
                            try:
                                parsed = json.loads(block_content)
                                extracted = []
                                if isinstance(parsed, dict):
                                    norm = _normalize_tool_dict(parsed)
                                    if norm:
                                        extracted.append(norm)
                                elif isinstance(parsed, list):
                                    for item in parsed:
                                        norm = _normalize_tool_dict(item)
                                        if norm:
                                            extracted.append(norm)
                                if extracted:
                                    any_matched = True
                                    for name, args in extracted:
                                        tcs.append(ToolCall(
                                            id=f"cf_call_{uuid.uuid4().hex[:8]}",
                                            name=name,
                                            arguments=args,
                                        ))
                                    clean_text = clean_text.replace(b.group(0), "")
                            except Exception:
                                pass
                        if any_matched:
                            content_val = clean_text.strip()

                # 3. 扫描位于消息末尾的工具调用 JSON（适配部分模型在正文末尾直接输出工具调用）
                if not tcs:
                    decoder = json.JSONDecoder()
                    idx = 0
                    while idx < len(content_val):
                        pos = content_val.find('{"name"', idx)
                        pos_tc = content_val.find('{"tool_call"', idx)
                        if pos == -1 and pos_tc == -1:
                            break
                        if pos != -1 and (pos_tc == -1 or pos <= pos_tc):
                            cand_idx = pos
                        else:
                            cand_idx = pos_tc
                        try:
                            obj, end = decoder.raw_decode(content_val[cand_idx:])
                            if isinstance(obj, dict):
                                norm = _normalize_tool_dict(obj)
                                if norm:
                                    preceding = content_val[:cand_idx]
                                    raw_trailing = content_val[cand_idx + end:]
                                    # 检查是否为句子中间举例文本（如："你可以输入 {"name": "demo"}，注意保持格式"）
                                    is_inline_example = (
                                        not preceding.endswith(("\n", "\r", "```"))
                                        and any(raw_trailing.lstrip(" `").startswith(p) for p in ("，", ",", "。", "！", "!", "；", ";", "、"))
                                    )
                                    if not is_inline_example:
                                        tcs.append(ToolCall(
                                            id=f"cf_call_{uuid.uuid4().hex[:8]}",
                                            name=norm[0],
                                            arguments=norm[1],
                                        ))
                                        content_val = preceding.strip()
                                        break
                            idx = cand_idx + max(end, 1)
                        except Exception:
                            idx = cand_idx + 1

                # 4. 适配代码模型（如 Qwen 2.5 Coder）直接输出 Python 函数调用语法：tool_name(param="value")
                if not tcs and content_val:
                    pattern = r'([a-zA-Z_]\w*)\s*\(([\s\S]*?)\)'
                    for m in re.finditer(pattern, content_val):
                        func_name = m.group(1)
                        if known_tools is not None and func_name not in known_tools:
                            continue
                        call_expr = m.group(0)
                        try:
                            tree = ast.parse(call_expr.strip())
                            if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Call):
                                call_node = tree.body[0].value
                                kw_args = {}
                                for kw in call_node.keywords:
                                    try:
                                        kw_args[kw.arg] = ast.literal_eval(kw.value)
                                    except Exception:
                                        kw_args[kw.arg] = str(getattr(kw.value, "id", getattr(kw.value, "value", "")))
                                # 支持位置参数转换
                                if call_node.args and not kw_args:
                                    try:
                                        first_val = ast.literal_eval(call_node.args[0])
                                    except Exception:
                                        first_val = str(getattr(call_node.args[0], "id", getattr(call_node.args[0], "value", "")))
                                    if func_name in ("desktop_open_app", "desktop_open"):
                                        kw_args["app_name"] = str(first_val)
                                    elif func_name in ("file_read", "file_write"):
                                        kw_args["path"] = str(first_val)
                                    elif func_name == "shell_exec":
                                        kw_args["command"] = str(first_val)
                                    else:
                                        kw_args["target"] = str(first_val)

                                tcs.append(ToolCall(
                                    id=f"cf_call_{uuid.uuid4().hex[:8]}",
                                    name=func_name,
                                    arguments=kw_args,
                                ))
                                content_val = content_val.replace(call_expr, "").strip()
                                break
                        except Exception:
                            pass

                if tcs:
                    content_val = re.sub(r"```(?:json)?\s*```?", "", content_val).strip()

        return Message(role="assistant", content=content_val or "", tool_calls=tcs)

