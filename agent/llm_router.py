"""Provider-isolated, capability-aware model routing from an injected plan.

This module never loads a configuration or secrets file. Each provider uses only
its own Vault name or configured environment variable. Legacy LLMClient routing
remains in agent.llm and can dispatch here only when the application enables it.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit

import httpx

from .llm import LLMClient, _request_context, _validate_extra_body, _response_usage
from .models import Message
from .vision_diagnostics import collect_request_metadata

logger = logging.getLogger(__name__)

DEFAULT_PROVIDERS = {
    "nvidia": {"base_url": "https://integrate.api.nvidia.com/v1", "api_key_env": "NVIDIA_API_KEY"},
    "modelscope": {"base_url": "https://api-inference.modelscope.cn/v1", "api_key_env": "MODELSCOPE_API_KEY"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key_env": "ZHIPU_API_KEY"},
    "agnes": {"base_url": "https://apihub.agnes-ai.com/v1", "api_key_env": "AGNES_API_KEY"},
}
ROLE_ALIASES = {
    "main": "main_brain", "planner": "main_brain", "worker": "main_brain",
    "subagent": "sub_agent", "sub_agent": "sub_agent",
}
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:+-]{0,199}\Z")
_GENERIC_KEY_ENVS = {"ALTEREGO_LLM_API_KEY", "ALTEREGO_BACKUP_LLM_API_KEY", "OPENAI_API_KEY"}
_MODEL_ERROR_CODES = {"invalid_model", "model_not_found", "unsupported_model", "model_not_supported"}


class RoutingConfigurationError(ValueError):
    pass


class ProviderKeyUnavailable(RuntimeError):
    pass


class ToolProtocolError(ValueError):
    pass


class EmbeddingResponseError(ValueError):
    pass


class RoutingUnavailable(RuntimeError):
    """Safe routing error: failure records never contain provider response bodies."""
    def __init__(self, failures):
        self.failures = tuple(dict(item) for item in failures)
        # Explicit attempt markers distinguish HTTP failures from eligibility
        # skips. A cooldown/probe skip comes only from transient health records;
        # it remains resumable even if the user continues before cooldown expiry.
        transient = {"transport", "rate_limit", "service_unavailable"}
        waiting = {"cooldown", "recovery_probe_inflight"}
        eligible_skips = {
            "provider_disabled", "chat_capability_blocked", "vision_capability_blocked",
            "tool_capability_blocked", "embedding_capability_blocked",
        }
        def recoverable(item):
            attempted = item.get("request_attempted")
            category = item.get("category")
            return ((attempted is True and category in transient)
                    or (attempted is False and category in waiting | eligible_skips))
        self.retryable = bool(self.failures) and all(recoverable(item) for item in self.failures) and any(
            (item.get("request_attempted") is True and item.get("category") in transient)
            or (item.get("request_attempted") is False and item.get("category") in waiting)
            for item in self.failures
        )
        self.status_code = next((item["status_code"] for item in reversed(self.failures)
                                 if isinstance(item.get("status_code"), int)), None)
        summary = "; ".join(f'{item["ref"]}:{item["category"]}' for item in self.failures)
        super().__init__("没有可用模型：" + (summary or "empty_chain"))


@dataclass(frozen=True)
class ModelTarget:
    model: str
    provider: str

    @property
    def ref(self):
        return f"{self.model}@{self.provider}"


def _target(value):
    if not isinstance(value, str) or value.count("@") != 1:
        raise RoutingConfigurationError("模型须使用 model@provider 格式")
    model, provider = value.rsplit("@", 1)
    if not _MODEL_RE.fullmatch(model) or provider not in DEFAULT_PROVIDERS:
        raise RoutingConfigurationError("模型ID或provider无效")
    return ModelTarget(model, provider)


def _chain(value):
    if isinstance(value, dict):
        value = value.get("chain", [])
    if not isinstance(value, list) or len(value) > 64:
        raise RoutingConfigurationError("角色chain须为最多64项的列表")
    result = []
    seen = set()
    for item in value:
        target = _target(item)
        if target.ref not in seen:
            seen.add(target.ref)
            result.append(target)
    return result


def _safe_json_options(value):
    try:
        _validate_extra_body(value)
    except ValueError:
        raise RoutingConfigurationError("extra_body必须为安全JSON扩展，禁止覆盖协议或认证字段") from None


def classify_failure(exc):
    """Classify by exception/status types, never provider error text."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        status = None
    if isinstance(exc, RoutingConfigurationError):
        return "configuration", None
    if isinstance(exc, ProviderKeyUnavailable):
        return "missing_key", None
    if isinstance(exc, (ToolProtocolError, EmbeddingResponseError)):
        return "protocol", status
    if status in (401, 403):
        code = getattr(exc, "code", None)
        if code is None:
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                detail = body.get("error", body)
                code = detail.get("code") if isinstance(detail, dict) else None
        if isinstance(code, str) and code.casefold() in _MODEL_ERROR_CODES:
            return "configuration", status
        return "authentication", status
    if status == 429:
        return "rate_limit", status
    if status in (400, 404, 405, 415, 422):
        return "configuration", status
    if status in (408, 409) or (status is not None and 500 <= status <= 599):
        return "service_unavailable", status
    if isinstance(exc, (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.TransportError)):
        return "transport", status
    if type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}:
        return "transport", status
    return "unexpected", status


@dataclass(frozen=True)
class _HealthLease:
    key: tuple
    generation: int


class RoutingHealthState:
    """Caller-owned, bounded health records; never shares SDKs or stores raw keys."""
    def __init__(self, *, clock=None, max_entries=2048):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self._clock = clock or time.monotonic
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._salt = secrets.token_bytes(32)
        self._records = {}

    def credential_tag(self, api_key):
        return hmac.new(self._salt, api_key.encode("utf-8"), hashlib.sha256).digest()

    def _prune(self):
        # In-flight leases remain valid; inactive records are evicted oldest first.
        excess = len(self._records) - self._max_entries
        if excess > 0:
            inactive = sorted(((record["touched"], key) for key, record in self._records.items()
                               if record["inflight"] == 0), key=lambda item: item[0])
            for _, key in inactive[:excess]:
                self._records.pop(key, None)

    def acquire(self, key):
        with self._lock:
            now = self._clock()
            record = self._records.setdefault(key, {
                "until": 0.0, "category": None, "status_code": None, "generation": 0,
                "inflight": 0, "failed": False, "healthy": False, "touched": now,
            })
            record["touched"] = now
            remaining = max(0.0, record["until"] - now)
            if remaining:
                return None, {"category": "cooldown", "cooldown_remaining_seconds": round(remaining, 3),
                              "status_code": record["status_code"]}
            # Only one recovery probe after an expired failure, without blocking HTTP.
            if record["failed"] and not record["healthy"] and record["inflight"]:
                return None, {"category": "recovery_probe_inflight"}
            record["inflight"] += 1
            lease = _HealthLease(key, record["generation"])
            self._prune()
            return lease, None

    def finish(self, lease, *, success, category=None, status_code=None, cooldown_seconds=0):
        if lease is None:
            return
        with self._lock:
            record = self._records.get(lease.key)
            if record is None:
                return
            now = self._clock()
            record["inflight"] = max(0, record["inflight"] - 1)
            record["touched"] = now
            if success and record["generation"] == lease.generation:
                record.update(until=0.0, category=None, status_code=None, healthy=True)
            elif not success and category in {"rate_limit", "transport", "service_unavailable"}:
                record.update(until=now + cooldown_seconds, category=category,
                              status_code=status_code, healthy=False, failed=True)
                record["generation"] += 1
            self._prune()


def _serialized(method):
    @wraps(method)
    def serialized(self, *args, **kwargs):
        with self._call_lock:
            return method(self, *args, **kwargs)
    return serialized


class _RoutingBase:
    def __init__(self, plan, role, vault=None, env=None, client_factory=None, *, _targets=None,
                 health_state=None, owner_scope=None):
        if not isinstance(plan, dict):
            raise RoutingConfigurationError("路由plan须为字典")
        self._call_lock = threading.RLock()
        if health_state is not None and not isinstance(health_state, RoutingHealthState):
            raise RoutingConfigurationError("health_state须为RoutingHealthState")
        self.health_state = health_state if health_state is not None else RoutingHealthState()
        self.owner_scope = object() if owner_scope is None else owner_scope
        try:
            hash(self.owner_scope)
        except TypeError:
            raise RoutingConfigurationError("owner_scope须为不可变身份标识") from None
        self._health_profile = None
        self._client_tags = {}
        self.plan = deepcopy(plan)
        self.role = ROLE_ALIASES.get(role, role)
        self.vault = vault
        self.env = os.environ if env is None else env
        self._factory = client_factory or LLMClient
        self._clients = {}
        self._request_observer = None
        self._fallback_enabled = True
        options = self.plan.get("options") or {}
        if not isinstance(options, dict):
            raise RoutingConfigurationError("options须为字典")
        self.options = options
        timeout = options.get("timeout_seconds", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise RoutingConfigurationError("timeout_seconds须在0到600秒之间")
        if options.get("max_retries", 0) != 0:
            raise RoutingConfigurationError("路由模式max_retries必须为0，避免每层SDK重复重试")
        self.timeout_seconds = float(timeout)
        cooldown = options.get("failure_cooldown_seconds", 30)
        if (isinstance(cooldown, bool) or not isinstance(cooldown, (int, float))
                or not math.isfinite(cooldown) or not 0 <= cooldown <= 600):
            raise RoutingConfigurationError("failure_cooldown_seconds须在0到600秒之间")
        self.failure_cooldown_seconds = float(cooldown)
        providers = self.plan.get("providers") or {}
        if not isinstance(providers, dict):
            raise RoutingConfigurationError("providers须为字典")
        self.providers = {}
        used_envs = {}
        for provider, default in DEFAULT_PROVIDERS.items():
            configured = providers.get(provider) or {}
            if not isinstance(configured, dict):
                raise RoutingConfigurationError("provider配置须为字典")
            config = {**default, **configured}
            url = config.get("base_url", "")
            try:
                parsed = urlsplit(url)
                port = parsed.port
            except (ValueError, TypeError):
                raise RoutingConfigurationError("provider base_url无效") from None
            expected_host = urlsplit(default["base_url"]).hostname
            if (parsed.scheme != "https" or parsed.hostname != expected_host or port not in (None, 443)
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise RoutingConfigurationError("provider base_url须使用固定provider域名和HTTPS/443")
            env_name = config.get("api_key_env", "")
            if (not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name)
                    or env_name != default["api_key_env"]
                    or env_name in _GENERIC_KEY_ENVS or env_name in used_envs):
                raise RoutingConfigurationError("每个provider须使用独立的专用api_key_env")
            used_envs[env_name] = provider
            self.providers[provider] = config
        roles = self.plan.get("roles") or {}
        if not isinstance(roles, dict):
            raise RoutingConfigurationError("roles须为字典")
        self.targets = list(_targets) if _targets is not None else _chain(roles.get(self.role, []))
        if not self.targets:
            raise RoutingConfigurationError("角色没有配置模型chain")
        self.capabilities = self.plan.get("capabilities") or {}
        if not isinstance(self.capabilities, dict):
            raise RoutingConfigurationError("capabilities须为字典")
        self.model_options = self.plan.get("model_options") or {}
        if not isinstance(self.model_options, dict):
            raise RoutingConfigurationError("model_options须为字典")
        self._last_target = self.targets[0]
        self._dimensions = None

    def _caps(self, target):
        value = self.capabilities.get(target.ref, {})
        return value if isinstance(value, dict) else {}

    @property
    def model(self):
        return self._last_target.model

    @property
    def provider(self):
        return self._last_target.provider

    @property
    def base_url(self):
        return self.providers[self.provider]["base_url"].rstrip("/")

    @property
    def supports_images(self):
        return self._caps(self._last_target).get("vision") is True

    @property
    def fallback(self):
        if not self._fallback_enabled or len(self.targets) < 2:
            return None
        return type(self)(self.plan, self.role, self.vault, self.env, self._factory,
                          _targets=self.targets[1:], health_state=self.health_state,
                          owner_scope=self.owner_scope)

    @fallback.setter
    @_serialized
    def fallback(self, value):
        self._fallback_enabled = value is not None

    @_serialized
    def set_request_observer(self, observer):
        self._request_observer = observer
        return self

    def _resolve_key(self, provider):
        if self.vault is not None:
            try:
                key = self.vault.resolve(f"{provider}_api_key", caller="agent.llm_router:" + provider)
                if isinstance(key, str) and key.strip():
                    return key.strip()
            except Exception:
                pass
        key = self.env.get(self.providers[provider]["api_key_env"], "")
        if isinstance(key, str) and key.strip():
            return key.strip()
        raise ProviderKeyUnavailable(f"provider {provider} 缺少专用API Key")

    def _settings(self, target):
        value = self.model_options.get(target.ref) or {}
        if not isinstance(value, dict) or set(value) - {"temperature", "max_tokens", "extra_body"}:
            raise RoutingConfigurationError("model_options仅支持temperature/max_tokens/extra_body")
        temperature = value.get("temperature", self.options.get("temperature", 0.7))
        max_tokens = value.get("max_tokens", self.options.get("max_tokens", 8192))
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise RoutingConfigurationError("temperature须在0到2之间")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise RoutingConfigurationError("max_tokens须为正整数")
        extra = value.get("extra_body")
        if extra is not None:
            if not isinstance(extra, dict):
                raise RoutingConfigurationError("extra_body须为字典")
            _safe_json_options(extra)
        return temperature, max_tokens, ({"extra_body": deepcopy(extra)} if extra is not None else {})

    @_serialized
    def _single(self, target, *, api_key=None, credential_tag=None):
        temperature, max_tokens, request_options = self._settings(target)
        if api_key is None:
            api_key = self._resolve_key(target.provider)
        if credential_tag is None:
            credential_tag = self.health_state.credential_tag(api_key)
        if target.ref not in self._clients or self._client_tags.get(target.ref) != credential_tag:
            previous = self._clients.pop(target.ref, None)
            close = getattr(getattr(previous, "client", None), "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    logger.warning("Retired routing client close failed (%s)", type(exc).__name__)
            client = self._factory(
                base_url=self.providers[target.provider]["base_url"], model=target.model,
                api_key=api_key, temperature=temperature,
                max_tokens=max_tokens, fallback=None,
                supports_images=self._caps(target).get("vision") is True,
            )
            client.fallback = None
            client.request_options = request_options
            sdk = getattr(client, "client", None)
            with_options = getattr(sdk, "with_options", None)
            if callable(with_options):
                client.client = with_options(timeout=self.timeout_seconds, max_retries=0)
            self._clients[target.ref] = client
            self._client_tags[target.ref] = credential_tag
        return self._clients[target.ref]

    @property
    def client(self):
        return self._single(self._last_target).client

    @client.setter
    @_serialized
    def client(self, value):
        self._single(self._last_target).client = value

    def pin(self, model_ref=None):
        """Return one capability-checked target with no automatic failover."""
        target = _target(model_ref) if model_ref is not None else self._last_target
        client = type(self)(self.plan, self.role, self.vault, self.env, self._factory,
                            _targets=[target], health_state=self.health_state,
                            owner_scope=self.owner_scope)
        client.fallback = None
        client.set_request_observer(self._request_observer)
        return client

    def _emit(self, metadata):
        if self._request_observer is not None:
            try:
                self._request_observer(dict(metadata))
            except Exception as exc:
                logger.warning("Routing observer failed (%s)", type(exc).__name__)

    def _metadata(self, target, context, depth, started, *, attempted, success, phase,
                  error=None, payload=None, tools=None, **extra):
        metadata = collect_request_metadata(target.model, self.providers[target.provider]["base_url"],
                                            payload or [], tools)
        metadata.update({
            "request_id": context["request_id"], "requested_model": context["requested_model"],
            "provider": target.provider, "role": self.role,
            "fallback_depth": depth, "is_fallback": depth > 0,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "success": success, "request_attempted": attempted, "phase": phase, **extra,
        })
        if error is not None:
            category, status = classify_failure(error)
            metadata.update(error_type=type(error).__name__, failure_category=category)
            if status is not None:
                metadata["status_code"] = status
        return metadata

    def _record_skip(self, target, context, depth, category, failures, payload=None, tools=None, **extra):
        failure = {"ref": target.ref, "category": category, "request_attempted": False}
        if isinstance(extra.get("status_code"), int):
            failure["status_code"] = extra["status_code"]
        failures.append(failure)
        self._emit(self._metadata(target, context, depth, time.perf_counter(),
                                 attempted=False, success=False, phase="routing_" + category,
                                 payload=payload, tools=tools, failure_category=category, **extra))

    def _health_key(self, target, credential_tag):
        if self._health_profile is None:
            profile = {"providers": self.providers, "capabilities": self.capabilities,
                       "model_options": self.model_options, "options": self.options}
            try:
                encoded = json.dumps(profile, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                raise RoutingConfigurationError("健康配置profile须为有效JSON数据") from None
            self._health_profile = hashlib.sha256(encoded).digest()
        endpoint_kind = "embedding" if isinstance(self, RoutedEmbeddingClient) else "chat"
        return (self.owner_scope, self._health_profile, endpoint_kind, target.provider,
                self.providers[target.provider]["base_url"].rstrip("/"), target.model, credential_tag)

    def _acquire_health(self, target):
        # Validate settings before resolving credentials or constructing a network client.
        self._settings(target)
        api_key = self._resolve_key(target.provider)
        credential_tag = self.health_state.credential_tag(api_key)
        lease, skip = None, None
        if self._fallback_enabled and self.failure_cooldown_seconds:
            lease, skip = self.health_state.acquire(self._health_key(target, credential_tag))
        return api_key, credential_tag, lease, skip

    def _finish_health(self, lease, error, completed):
        category, status = classify_failure(error) if error is not None else (None, None)
        self.health_state.finish(lease, success=completed and error is None, category=category,
                                 status_code=status, cooldown_seconds=self.failure_cooldown_seconds)

    def _forget_provider(self, provider):
        for ref in list(self._clients):
            if ref.rsplit("@", 1)[1] == provider:
                retired = self._clients.pop(ref, None)
                self._client_tags.pop(ref, None)
                close = getattr(getattr(retired, "client", None), "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as exc:
                        logger.warning("Retired routing client close failed (%s)", type(exc).__name__)


class RoutedLLMClient(_RoutingBase):
    def __init__(self, plan, role="main", vault=None, env=None, client_factory=None, *, _targets=None,
                 health_state=None, owner_scope=None):
        super().__init__(plan, role, vault, env, client_factory, _targets=_targets,
                         health_state=health_state, owner_scope=owner_scope)
        if self.role == "memory_embedding":
            raise RoutingConfigurationError("embedding角色须使用build_embedding_client，不可调用chat")

    @_serialized
    def result_observer_client(self):
        """A separate, tool-free vision client with the same health ownership.

        Keep the plan and its health profile intact. Runtime request limits do
        not erase failures already learned by the normal planning/tool route.
        """
        targets = [target for target in _chain((self.plan.get("roles") or {}).get("vision", []))
                   if self.providers[target.provider].get("enabled", True)
                   and self._caps(target).get("vision") is True
                   and self._caps(target).get("chat", not self._caps(target).get("embedding", False)) is True]
        if not targets:
            return None
        client = _ResultObservationClient(
            self.plan, "vision", self.vault, self.env, self._factory, _targets=targets,
            health_state=self.health_state, owner_scope=self.owner_scope)
        client.timeout_seconds = min(12.0, self.timeout_seconds)
        client._health_profile = self._health_profile
        client.set_request_observer(self._request_observer)
        return client

    def _candidates(self, has_images):
        if not self._fallback_enabled:
            return [self._last_target]
        if has_images and self.options.get("route_images_to_vision", True):
            vision = _chain((self.plan.get("roles") or {}).get("vision", []))
            candidates = vision + self.targets
        else:
            candidates = list(self.targets)
        verified_agnes = "agnes-3.0-flash@agnes"
        if (has_images and self._caps(_target(verified_agnes)).get("vision") is True
                and self.options.get("agnes_vision_fallback", True)):
            candidates.append(_target(verified_agnes))
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _validate_reply(reply, tools, known_tools):
        if not isinstance(reply, Message):
            raise ToolProtocolError("模型响应不是Message")
        if not reply.tool_calls and (not isinstance(reply.content, str) or not reply.content.strip()):
            raise ToolProtocolError("模型没有返回可用内容或工具调用")
        allowed = known_tools
        if allowed is None and tools:
            allowed = {item.get("function", {}).get("name") for item in tools if isinstance(item, dict)}
        if reply.tool_calls and not tools and not allowed:
            raise ToolProtocolError("未提供工具时模型不得请求执行工具")
        ids = set()
        for call in reply.tool_calls:
            if (not call.id or call.id in ids or not isinstance(call.arguments, dict)
                    or (allowed is not None and call.name not in allowed)):
                raise ToolProtocolError("模型返回无效或未授权的工具调用")
            ids.add(call.id)

    @_serialized
    def chat(self, messages, tools=None, known_tools=None):
        payload = [LLMClient._to_openai(message) for message in messages]
        has_images = any(isinstance(message.get("content"), list) and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in message["content"]) for message in payload)
        needs_tools = bool(tools) or any(message.role == "tool" or message.tool_calls for message in messages)
        targets = self._candidates(has_images)
        context = _request_context.get() or {
            "request_id": uuid.uuid4().hex, "requested_model": targets[0].model, "fallback_depth": 0,
        }
        failures = []
        blocked_providers = set()
        attempted_count = 0
        max_attempts = getattr(self, "_max_network_attempts", None)
        for depth, target in enumerate(targets):
            if max_attempts is not None and attempted_count >= max_attempts:
                break
            caps = self._caps(target)
            category = None
            if not self.providers[target.provider].get("enabled", True):
                category = "provider_disabled"
            elif target.provider in blocked_providers:
                category = "provider_auth_blocked"
            elif caps.get("chat", not caps.get("embedding", False)) is not True:
                category = "chat_capability_blocked"
            elif has_images and caps.get("vision") is not True:
                category = "vision_capability_blocked"
            elif needs_tools and caps.get("tools") is not True:
                category = "tool_capability_blocked"
            if category:
                self._record_skip(target, context, depth, category, failures, payload, tools)
                continue
            started = time.perf_counter()
            observations = []
            attempted = False
            error = None
            completed = False
            lease = None
            token = _request_context.set({**context, "fallback_depth": depth})
            try:
                api_key, credential_tag, lease, skip = self._acquire_health(target)
                if skip is not None:
                    skip = dict(skip)
                    category = skip.pop("category")
                    self._record_skip(target, context, depth, category, failures, payload, tools, **skip)
                    continue
                client = self._single(target, api_key=api_key, credential_tag=credential_tag)
                hook = getattr(client, "set_request_observer", None)
                if callable(hook):
                    hook(observations.append)
                attempted = True
                attempted_count += 1
                reply = client.chat(messages, tools=tools, known_tools=known_tools)
                self._validate_reply(reply, tools, known_tools)
                completed = True
            except Exception as exc:
                error = exc
                category, status = classify_failure(exc)
                failure = {"ref": target.ref, "category": category, "request_attempted": attempted}
                if status is not None:
                    failure["status_code"] = status
                failures.append(failure)
                if category == "authentication":
                    blocked_providers.add(target.provider)
                    self._forget_provider(target.provider)
            finally:
                self._finish_health(lease, error, completed)
                _request_context.reset(token)
            metadata = observations[-1] if observations else self._metadata(
                target, context, depth, started, attempted=attempted, success=error is None,
                phase="request", payload=payload, tools=tools)
            metadata = {**metadata, "provider": target.provider, "role": self.role,
                        "request_attempted": attempted, "success": error is None}
            if error is not None:
                category, status = classify_failure(error)
                metadata.update(error_type=type(error).__name__, failure_category=category)
                if status is not None:
                    metadata["status_code"] = status
                if isinstance(error, ToolProtocolError):
                    metadata["phase"] = "response_protocol_rejected"
            self._emit(metadata)
            if error is None:
                self._last_target = target
                reply.metadata["actual_model"] = target.model
                return reply
            if classify_failure(error)[0] == "unexpected":
                raise RoutingUnavailable(failures) from None
        raise RoutingUnavailable(failures) from None


class _ResultObservationClient(RoutedLLMClient):
    """Small read-only profile; eligibility/cooldown skips use no HTTP budget."""
    _max_network_attempts = 2

    def _settings(self, target):
        _, max_tokens, request_options = super()._settings(target)
        return 0.0, min(max_tokens, 512), request_options

    def _candidates(self, has_images):
        # Only the configured vision chain, without hidden global additions.
        return list(self.targets)

    def chat(self, messages, tools=None, known_tools=None):
        if tools or known_tools or any(message.role == "tool" or message.tool_calls for message in messages):
            raise RoutingConfigurationError("只读结果观察不接受工具或工具调用历史")
        return super().chat(messages, tools=None, known_tools=None)


def _embedding_usage(response):
    """Embedding has no generated text; validate original input/total counters."""
    try:
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        prompt = usage.get("prompt_tokens") if isinstance(usage, dict) else getattr(usage, "prompt_tokens", None)
        total = usage.get("total_tokens") if isinstance(usage, dict) else getattr(usage, "total_tokens", None)
        return _response_usage({"prompt_tokens": prompt, "completion_tokens": 0, "total_tokens": total})
    except Exception:
        return {"usage_available": False}


class RoutedEmbeddingClient(_RoutingBase):
    def __init__(self, plan, role="memory_embedding", vault=None, env=None, client_factory=None, *, _targets=None,
                 health_state=None, owner_scope=None):
        super().__init__(plan, role, vault, env, client_factory, _targets=_targets,
                         health_state=health_state, owner_scope=owner_scope)

    def chat(self, *args, **kwargs):
        raise RoutingConfigurationError("embedding模型只能调用embed，不能发送chat请求")

    @property
    def dimensions(self):
        return self._dimensions

    @property
    def vector_space_id(self):
        return f"{self.provider}/{self.model}/{self._dimensions or 'unknown'}"

    @staticmethod
    def _validate_vectors(response, count, expected_dimensions=None):
        data = response.get("data") if isinstance(response, dict) else getattr(response, "data", None)
        if not isinstance(data, list) or len(data) != count:
            raise EmbeddingResponseError("embedding数量不匹配")
        vectors = [None] * count
        dimensions = expected_dimensions
        if dimensions is not None and (isinstance(dimensions, bool)
                or not isinstance(dimensions, int) or dimensions <= 0):
            raise EmbeddingResponseError("embedding预期维度无效")
        for item in data:
            index = item.get("index") if isinstance(item, dict) else getattr(item, "index", None)
            vector = item.get("embedding") if isinstance(item, dict) else getattr(item, "embedding", None)
            if (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count
                    or vectors[index] is not None or not isinstance(vector, list) or not vector):
                raise EmbeddingResponseError("embedding索引或向量无效")
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in vector):
                raise EmbeddingResponseError("embedding包含非有限或非数字值")
            if dimensions is None:
                dimensions = len(vector)
            if len(vector) != dimensions:
                raise EmbeddingResponseError("embedding维度不匹配")
            vectors[index] = [float(value) for value in vector]
        return vectors, dimensions

    @_serialized
    def embed(self, inputs, input_type="query"):
        if input_type not in {"query", "passage"}:
            raise ValueError("input_type须为query或passage")
        texts = [inputs] if isinstance(inputs, str) else inputs
        if not isinstance(texts, list) or not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("embedding输入须为非空文本或非空文本列表")
        targets = self.targets if self._fallback_enabled else [self._last_target]
        context = {"request_id": uuid.uuid4().hex, "requested_model": targets[0].model, "fallback_depth": 0}
        failures = []
        blocked = set()
        for depth, target in enumerate(targets):
            if not self.providers[target.provider].get("enabled", True):
                self._record_skip(target, context, depth, "provider_disabled", failures)
                continue
            if target.provider in blocked:
                self._record_skip(target, context, depth, "provider_auth_blocked", failures)
                continue
            if self._caps(target).get("embedding") is not True:
                self._record_skip(target, context, depth, "embedding_capability_blocked", failures)
                continue
            started = time.perf_counter()
            attempted = False
            error = None
            completed = False
            lease = None
            usage = {"usage_available": False}
            try:
                api_key, credential_tag, lease, skip = self._acquire_health(target)
                if skip is not None:
                    skip = dict(skip)
                    category = skip.pop("category")
                    self._record_skip(target, context, depth, category, failures, **skip)
                    continue
                sdk = self._single(target, api_key=api_key, credential_tag=credential_tag).client
                extra = {"input_type": input_type, "truncate": "END"} if target.provider == "nvidia" else {}
                kwargs = {"model": target.model, "input": inputs, "encoding_format": "float"}
                if extra:
                    kwargs["extra_body"] = extra
                attempted = True
                raw_resource = getattr(sdk.embeddings, "with_raw_response", None)
                raw_create = getattr(raw_resource, "create", None)
                if callable(raw_create):
                    # Validate original JSON before SDK coercion turns booleans into floats.
                    raw_response = raw_create(**kwargs)
                    try:
                        response = raw_response.http_response.json()
                    except ValueError:
                        raise EmbeddingResponseError("embedding响应不是有效JSON") from None
                else:
                    response = sdk.embeddings.create(**kwargs)
                usage = _embedding_usage(response)
                vectors, dimensions = self._validate_vectors(
                    response, len(texts), self._caps(target).get("dimensions"))
                completed = True
            except Exception as exc:
                error = exc
                category, status = classify_failure(exc)
                failure = {"ref": target.ref, "category": category, "request_attempted": attempted}
                if status is not None:
                    failure["status_code"] = status
                failures.append(failure)
                if category == "authentication":
                    blocked.add(target.provider)
                    self._forget_provider(target.provider)
            finally:
                self._finish_health(lease, error, completed)
            self._emit(self._metadata(
                target, context, depth, started, attempted=attempted, success=error is None,
                phase="embedding", error=error, input_count=len(texts),
                **usage, **({"dimensions": dimensions} if error is None else {}),
            ))
            if error is None:
                self._last_target = target
                self._dimensions = dimensions
                return vectors
            if classify_failure(error)[0] == "unexpected":
                raise RoutingUnavailable(failures) from None
        raise RoutingUnavailable(failures) from None


def build_routed_client(plan, role="main", vault=None, env=None, client_factory=None, *,
                        health_state=None, owner_scope=None):
    return RoutedLLMClient(plan, role, vault, env, client_factory,
                           health_state=health_state, owner_scope=owner_scope)


def build_embedding_client(plan, role="memory_embedding", vault=None, env=None, client_factory=None, *,
                           health_state=None, owner_scope=None):
    return RoutedEmbeddingClient(plan, role, vault, env, client_factory,
                                 health_state=health_state, owner_scope=owner_scope)
