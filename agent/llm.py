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

# 确保本地环回地址 (127.0.0.1, localhost) 永不走系统代理 (如 Clash 7890)，防止本地模型服务 (EchoBird / Ollama) 被代理劫持返回 502/503
def _ensure_no_proxy_for_localhost():
    cur = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
    hosts = [h.strip() for h in cur.split(",") if h.strip()]
    changed = False
    for local_host in ("127.0.0.1", "localhost", "::1"):
        if local_host not in hosts:
            hosts.append(local_host)
            changed = True
    if changed:
        val = ",".join(hosts)
        os.environ["NO_PROXY"] = val
        os.environ["no_proxy"] = val

_ensure_no_proxy_for_localhost()

_SDK_OPENAI_TYPE = None


def _get_sdk_openai_type():
    global _SDK_OPENAI_TYPE
    if "OpenAI" in globals() and globals()["OpenAI"] is not None:
        return globals()["OpenAI"]
    if _SDK_OPENAI_TYPE is None:
        from openai import OpenAI
        _SDK_OPENAI_TYPE = OpenAI
    return _SDK_OPENAI_TYPE


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
    fb_chain = role_cfg.get("fallback_chain")
    if isinstance(fb_chain, list):
        llm["fallback_chain"] = [
            {
                "enabled": f.get("enabled", True) if isinstance(f, dict) else True,
                "base_url": _expand_env(f.get("base_url", "")) if isinstance(f, dict) else "",
                "model": f.get("model") if isinstance(f, dict) else str(f),
                "supports_images": f.get("supports_images", False) if isinstance(f, dict) else False,
                "temperature": f.get("temperature", 0.7) if isinstance(f, dict) else 0.7,
                "max_tokens": f.get("max_tokens", 8192) if isinstance(f, dict) else 8192,
            }
            for f in fb_chain if (isinstance(f, dict) and f.get("model")) or (isinstance(f, str) and f)
        ]
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
        # 严格检查 default_key：仅当以 sk- 开头且不是其他本地代理 key 时才复用
        if default_key and default_key.startswith("sk-") and not default_key.startswith("sk-local"):
            return default_key
        # 若 default_key 明显属于其他专有厂商格式（如 eb-local-、AQ.、AIzaSy、cfat_），严禁错传给 Agnes 导致 401
        if any((default_key or "").startswith(p) for p in ("eb-local-", "AQ.", "AIzaSy", "cfat_")):
            return ""

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

    # 4. 阿里云百炼 / DashScope: aliyuncs.com 或 qwen / kimi / deepseek / glm
    elif "aliyuncs.com" in url_lower or "dashscope" in url_lower or any(m in model_lower for m in ("kimi", "qwen", "deepseek", "glm")):
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

    # 4.5 联通云星罗 Token Plan: cucloud.cn 或 MiniMax / aisp
    elif "cucloud.cn" in url_lower or any(m in model_lower for m in ("minimax", "aisp")):
        env_key = os.environ.get("CUCLOUD_API_KEY", "").strip()
        if env_key:
            return env_key
        if vault:
            try:
                k = vault.resolve("cucloud_api_key", caller="agent.llm:resolve_key_for_target")
                if k:
                    return k
            except Exception:
                pass
        if default_key and default_key.startswith("sk-"):
            return default_key

    # 5. EchoBird / 本地智能代理网关: 127.0.0.1:53683 或 auto 或 eb-local
    elif "53683" in url_lower or "echobird" in url_lower or model_lower == "auto" or (default_key or "").startswith("eb-local-"):
        env_key = os.environ.get("ECHOBIRD_API_KEY", "").strip()
        if env_key:
            return env_key
        if vault:
            try:
                k = vault.resolve("echobird_api_key", caller="agent.llm:resolve_key_for_target")
                if k:
                    return k
            except Exception:
                pass
        if default_key and default_key.startswith("eb-local-"):
            return default_key
        return ""

    return default_key



def diagnose_llm_error(exc: Exception) -> dict:
    """精准诊断大模型调用异常，尤其识别平台配额耗尽、限流与认证失败。"""
    err_str = str(exc or "")
    err_type = type(exc).__name__

    body_text = ""
    if hasattr(exc, "response") and getattr(exc, "response", None) is not None:
        try:
            body_text += " " + str(getattr(exc.response, "text", ""))
        except Exception:
            pass
    if hasattr(exc, "body") and getattr(exc, "body", None) is not None:
        body_text += " " + str(getattr(exc, "body", ""))
    full_text = f"{err_str} {body_text}".lower()

    # 1. 阿里百炼 / 通用平台免费赠送额度耗尽（用完即停 403 AllocationQuota.FreeTierOnly）
    if any(k in full_text for k in [
        "allocationquota.freetieronly",
        "allocationquota",
        "freetieronly",
        "free quota exhausted",
        "free_quota_exhausted",
        "quota exhausted",
        "resource has been exhausted",
    ]):
        return {
            "reason": "free_quota_exhausted",
            "should_retry": False,
            "status_code": 403,
            "summary": "阿里百炼免费额度已耗尽 (AllocationQuota.FreeTierOnly: 仅消耗赠送额度功能已触发停止)",
            "detail": err_str,
        }

    # 2. 账号欠费 / 余额不足
    if any(k in full_text for k in ["insufficientbalance", "arrearage", "accountinarrears", "quota_exceeded"]):
        return {
            "reason": "insufficient_balance",
            "should_retry": False,
            "status_code": 402,
            "summary": "账户欠费或付费额度已用尽 (Insufficient Balance)",
            "detail": err_str,
        }

    # 3. 速率限制 / 并发超限 (429 RateLimit)
    if "ratelimit" in err_type.lower() or any(k in full_text for k in ["ratelimit", "429", "too many requests"]):
        return {
            "reason": "rate_limit",
            "should_retry": True,
            "status_code": 429,
            "summary": "并发请求频率超限 (429 Rate Limit Exceeded)",
            "detail": err_str,
        }

    # 4. API 密钥失效 (401 Auth)
    if "auth" in err_type.lower() or any(k in full_text for k in ["401", "invalidapikey", "unauthorized", "invalid_api_key"]):
        return {
            "reason": "auth_error",
            "should_retry": False,
            "status_code": 401,
            "summary": "API Key 无效或未授权 (401 Authentication Error)",
            "detail": err_str,
        }

    # 5. 上下文长度超出模型最大限制 (400 Context)
    if any(k in full_text for k in ["maximum context length", "context_length_exceeded", "prompt is too long"]):
        return {
            "reason": "context_window_exceeded",
            "should_retry": False,
            "status_code": 400,
            "summary": "对话上下文超过该模型最大窗口限制",
            "detail": err_str,
        }

    return {
        "reason": "general_error",
        "should_retry": False,
        "status_code": getattr(exc, "status_code", 500) if hasattr(exc, "status_code") else 500,
        "summary": f"{err_type}: {err_str[:120]}",
        "detail": err_str,
    }


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
        client_cls = _get_sdk_openai_type()
        import httpx
        is_local = any(h in self.base_url for h in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]"))
        if is_local:
            http_client = httpx.Client(
                trust_env=False,
                timeout=httpx.Timeout(connect=5.0, read=600.0, write=600.0, pool=600.0)
            )
        else:
            # 云端直连长连接热池化：保持 300 秒 Keep-Alive，避免多轮调用反复经历 TCP 三次握手与 TLS 协商
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=300.0)
            http_client = httpx.Client(
                trust_env=False,
                limits=limits,
                timeout=httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=300.0)
            )
        self.client = client_cls(base_url=self.base_url, api_key=api_key or "unconfigured_key", http_client=http_client)
        self.is_unconfigured = (api_key == "unconfigured_key" or not api_key)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.fallback = fallback
        self.supports_images = supports_images
        self._request_observer = None
        self._api_key = api_key or ""
        self._echobird_adapter = None
        self._echobird_checked = False
        self.request_options = _request_options_snapshot(request_options)

    @classmethod
    def from_config(cls, cfg: dict, api_key: str = "", fallback_api_key: str = "", vault=None, allow_unconfigured: bool = False) -> "LLMClient":
        routing = cfg.get("llm_router") or {}  # 键存在但为 null 时 .get 默认值不生效（问题汇总 4.58）
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
            if allow_unconfigured:
                api_key = "unconfigured_key"
            else:
                raise RuntimeError("缺 LLM API Key：环境变量 ALTEREGO_LLM_API_KEY 或 vault（M2）")

        # 构建多级容灾降级链（递归链式 LLMClient，从最末级兜底往前链接）
        fallbacks_cfg = []
        if isinstance(llm.get("fallback_chain"), list) and llm["fallback_chain"]:
            fallbacks_cfg = [f for f in llm["fallback_chain"] if isinstance(f, dict) and f.get("enabled", True)]
        elif isinstance(llm.get("fallback"), list) and llm["fallback"]:
            fallbacks_cfg = [f for f in llm["fallback"] if isinstance(f, dict) and f.get("enabled", True)]
        elif isinstance(llm.get("fallback"), dict) and llm["fallback"].get("enabled", True):
            fallbacks_cfg = [llm["fallback"]]

        fallback_client: LLMClient | None = None
        for fb_item in reversed(fallbacks_cfg):
            m_id = fb_item.get("model")
            b_url = fb_item.get("base_url") or ""
            if not m_id:
                continue
            resolved_fb_key = resolve_key_for_target(
                b_url, m_id,
                default_key=fallback_api_key or os.environ.get("ALTEREGO_BACKUP_LLM_API_KEY", ""),
                vault=vault
            )
            if not resolved_fb_key and not fallback_api_key and vault:
                try:
                    fallback_api_key = vault.resolve("backup_llm_api_key", caller="agent.llm")
                except Exception:
                    pass
            fb_key = resolved_fb_key or fallback_api_key or os.environ.get("ALTEREGO_BACKUP_LLM_API_KEY", "") or api_key
            if not fb_key and any(h in b_url for h in ("127.0.0.1", "localhost", "53683")):
                fb_key = "eb-local-no-key"
            elif not fb_key and allow_unconfigured:
                fb_key = "unconfigured_key"

            if fb_key:
                fallback_client = cls(
                    base_url=b_url,
                    model=m_id,
                    api_key=fb_key,
                    temperature=fb_item.get("temperature", 0.7),
                    max_tokens=fb_item.get("max_tokens", 8192),
                    fallback=fallback_client,
                    supports_images=fb_item.get("supports_images", False),
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
    def for_role(cls, cfg: dict, role: str, vault=None, api_key: str = "", fallback_api_key: str = "", allow_unconfigured: bool = False) -> "LLMClient":
        """按角色取模型客户端。支持专属 key 隔离与自动回落。"""
        routing = cfg.get("llm_router") or {}  # 键存在但为 null 时 .get 默认值不生效（问题汇总 4.58）
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
                if not api_key:
                    try:
                        api_key = vault.resolve("agnes_api_key", caller=f"agent.llm:{role}:fallback_agnes")
                    except Exception:
                        pass
            api_key = api_key or os.environ.get("ALTEREGO_LLM_API_KEY", "")
            if not api_key and vault:
                try:
                    from agent.main import resolve_llm_key
                    api_key = resolve_llm_key(vault)
                except Exception:
                    pass

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
            allow_unconfigured=allow_unconfigured,
        )

    def _get_echobird_adapter(self):
        """Lazily create the EchoBird adapter only for EchoBird targets.

        Keeping this out of ``__init__`` is important: constructing a client must
        never perform network I/O.
        """
        if not self._echobird_checked:
            self._echobird_checked = True
            from .echobird_adapter import EchoBirdAdapter, is_echobird_target

            if is_echobird_target(self.base_url, self.model, self._api_key):
                self._echobird_adapter = EchoBirdAdapter(
                    self.base_url, self.model, self._api_key
                )
        return self._echobird_adapter

    def probe_echobird(self, force: bool = False) -> dict | None:
        """Return the latest EchoBird probe diagnostics, or None for other targets."""
        adapter = self._get_echobird_adapter()
        if adapter is None:
            return None
        return adapter.probe(force=force).to_dict()

    def get_echobird_route_info(self) -> dict | None:
        """Return probe/last-route/capability diagnostics for the settings UI."""
        adapter = self._get_echobird_adapter()
        if adapter is None:
            return None
        return adapter.info()

    @staticmethod
    def _echobird_trace_fields(route) -> dict | None:
        if route is None or not route.route_key:
            return None
        return {
            "echobird_route": route.route_key,
            "echobird_provider": route.provider,
            "echobird_model": route.model_id,
        }

    def set_request_observer(self, observer):
        """Attach an optional metadata-only callback to this client and fallbacks."""
        self._request_observer = observer
        if self.fallback:
            self.fallback.set_request_observer(observer)
        return self

    def _observe_request(self, payload, tools, context, started, success, error=None,
                         phase="request", request_attempted=True, usage=None, extra=None):
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
            if extra:
                metadata.update(extra)
            if error is not None:
                metadata["error_type"] = type(error).__name__
            self._request_observer(metadata)
        except Exception as exc:
            # Auditing failure must not change the reply or trigger paid fallback calls.
            logger.warning("LLM request observer failed (%s)", type(exc).__name__)

    def prewarm(self) -> bool:
        """后台预热 HTTP/2 & TLS 连接池，使后续交互消除冷握手延迟。"""
        if getattr(self, "is_unconfigured", False) or getattr(self, "_is_prewarmed", False):
            return False
        if not self.base_url or not self.base_url.startswith("http"):
            return False
        try:
            raw_http = getattr(self.client, "_client", None)
            if raw_http and hasattr(raw_http, "get"):
                target = self.base_url.rstrip("/") + "/models"
                headers = {"Authorization": f"Bearer {getattr(self.client, 'api_key', '')}"}
                raw_http.get(target, headers=headers, timeout=2.0)
                self._is_prewarmed = True
                logger.debug("LLMClient [%s] 云端长连接后台预热完成: %s", self.model, target)
                return True
        except Exception as exc:
            self._is_prewarmed = True
            logger.debug("LLMClient [%s] 预热握手完成（非阻塞）: %s", self.model, exc)
            return False
        return False

    def chat(self, messages: list[Message], tools: list[dict] | None = None,
             known_tools: set[str] | None = None) -> Message:
        """调一次 LLM，按实际发出的主/备请求分别报告脱敏诊断元数据。"""
        if getattr(self, "is_unconfigured", False):
            raise RuntimeError("尚未配置模型 API Key，请点击右上角【设置】填写您的 API Key 并在保存后再发送消息。")
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
                "max_tokens": self.max_tokens,
            }
            if self.temperature is not None and not any(k in (self.model or "").lower() for k in ("kimi",)):
                kwargs["temperature"] = self.temperature
            kwargs.update(_request_options_snapshot(self.request_options))
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            started = time.perf_counter()
            usage = {"usage_available": False}
            route = None
            try:
                adapter = self._get_echobird_adapter()
                if adapter is not None:
                    # Probe is advisory, but a confirmed 401/403 means the real
                    # request would certainly fail. Avoid spending a second call.
                    probe_result = adapter.probe()
                    if not probe_result.ok and probe_result.error_category == "auth":
                        raise RuntimeError(
                            "EchoBird API Key 认证失败 (401/403)。请检查 ECHOBIRD_API_KEY "
                            "或设置中的 EchoBird 密钥。"
                        )
                    kwargs = adapter.build_payload(kwargs)

                for attempt in range(2):
                    try:
                        from openai import OpenAI as _RealOpenAI
                        if isinstance(self.client, _RealOpenAI):
                            # Inspect original JSON before Pydantic coerces invalid counters.
                            raw_response = self.client.chat.completions.with_raw_response.create(**kwargs)
                            body = None
                            try:
                                body = raw_response.http_response.json()
                                usage = _response_usage(body.get("usage") if isinstance(body, dict) else None)
                            except Exception:
                                pass
                            if adapter is not None:
                                route = adapter.observe_response(
                                    raw_response.http_response.headers,
                                    body.get("model") if isinstance(body, dict) else None,
                                )
                            resp = raw_response.parse()
                        else:
                            resp = self.client.chat.completions.create(**kwargs)
                            usage = _response_usage(getattr(resp, "usage", None))
                            if adapter is not None:
                                route = adapter.observe_response(
                                    None, getattr(resp, "model", None)
                                )
                        break
                    except Exception as _req_exc:
                        if adapter is not None:
                            observed = adapter.observe_error(_req_exc)
                            if observed is not None and observed.route_key:
                                route = observed
                        if adapter is not None and attempt == 0:
                            adapted = adapter.adapt_payload(kwargs, _req_exc)
                            if adapted is not None:
                                kwargs = adapted
                                logger.info(
                                    "[%s] EchoBird 路由不接受部分请求参数，移除后重试一次",
                                    self.model,
                                )
                                continue
                        diagnosis = diagnose_llm_error(_req_exc)
                        # 如果是不可恢复的配额耗尽/鉴权失败等，立即跳过无谓的重试，直接抛出进入备用链降级
                        if not diagnosis.get("should_retry", False):
                            raise _req_exc
                        max_retries = getattr(self.client, "max_retries", 2)
                        if isinstance(max_retries, int) and max_retries > 0 and diagnosis.get("reason") == "rate_limit" and attempt == 0:
                            logger.info("[%s] 遇到频率限制 (RateLimitError)，等待 2 秒后重试...", self.model)
                            time.sleep(2.0)
                            continue
                        raise _req_exc
                result = self._from_openai(resp, known_tools=known_tools)
                metadata = {"actual_model": self.model, **usage}
                route_fields = self._echobird_trace_fields(route)
                if route_fields:
                    metadata.update(route_fields)
                result.metadata.update(metadata)
            except Exception as exc:
                route_fields = self._echobird_trace_fields(route)
                self._observe_request(
                    payload, tools, context, started, False, exc,
                    usage=usage, extra=route_fields,
                )
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
                    diagnosis = diagnose_llm_error(exc)
                    logger.warning(
                        "主模型 [%s] 调用异常: %s。自动丝滑切换至降级备用模型 [%s] 继续执行...",
                        self.model, diagnosis["summary"], self.fallback.model,
                    )
                    fallback_token = _request_context.set(fallback_context)
                    try:
                        res = self.fallback.chat(messages, tools=tools, known_tools=known_tools)
                        res.metadata.setdefault("fallback_history", []).append({
                            "from_model": self.model,
                            "to_model": self.fallback.model,
                            "reason": diagnosis["reason"],
                            "summary": diagnosis["summary"],
                            "status_code": diagnosis.get("status_code", 500),
                            "error": str(exc),
                        })
                        return res
                    finally:
                        _request_context.reset(fallback_token)
                raise
            self._observe_request(
                payload, tools, context, started, True,
                usage=usage, extra=self._echobird_trace_fields(route),
            )
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


def __getattr__(name: str):
    if name in ("OpenAI", "_SDK_OPENAI_TYPE"):
        return _get_sdk_openai_type()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

