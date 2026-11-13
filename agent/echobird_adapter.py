"""EchoBird SmartRouter 适配层。

EchoBird 对外只暴露一个固定模型 ID：``auto``。真实厂商/模型由
SmartRouter 在服务端决定，并通过响应头 ``x-echobird-routed-via``
返回（格式通常为 ``候选ID/模型ID``）。

本模块刻意把“厂商能力”做成可扩展数据，而不是在请求路径里写
``if qoder``：

* 探测（probe）只作为“建议”，真实请求仍会重新读取路由头；
* 首次真实请求使用兼容性安全基线，默认不携带 ``temperature`` 等可选参数；
* 真实响应确认路由后，才允许后续按该路由的 profile 调整请求格式；
* 未来新增厂商只需补充 ``echobird_profiles.json``，不需要改调用方。

注意：不要在这里读取或记录 EchoBird 的加密配置；API Key 只从调用方
传入，且任何日志都不允许包含密钥。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import httpx

logger = logging.getLogger(__name__)

ROUTE_HEADER = "x-echobird-routed-via"
PROBE_TTL_SECONDS = 10 * 60
PROBE_FAILURE_TTL_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 10.0
PROBE_MAX_TOKENS = 16
PROBE_PROMPT = "ping"

# 这些参数在能力未确认时一律不发送。EchoBird 背后的部分厂商（例如 Qoder）
# 会对未声明支持的 OpenAI 可选参数直接返回 400。
_OPTIONAL_PARAMETERS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "seed",
    "extra_body",
)

_DEFAULT_GENERIC_PROFILE: dict[str, Any] = {
    "protocol": "openai_chat",
    "temperature": "unknown",
    "top_p": "unknown",
    "tools": "unknown",
    "stream": "unknown",
    "vision": "unknown",
    "response_format": "unknown",
    "reasoning": "unknown",
    "max_tokens_field": "max_tokens",
    "tool_choice": "auto",
    "drop_optional_params": True,
}


def _default_profiles() -> dict[str, Any]:
    return {
        "version": 1,
        "generic": dict(_DEFAULT_GENERIC_PROFILE),
        "vendors": {
            "qoder": {
                "temperature": "unsupported",
                "tools": "supported",
                "stream": "supported",
                "vision": "unknown",
                "max_tokens_field": "max_tokens",
            },
            "qoderCn": {
                "temperature": "unsupported",
                "tools": "supported",
                "stream": "supported",
                "vision": "unknown",
                "max_tokens_field": "max_tokens",
            },
        },
        "models": {},
    }


_PROFILE_CACHE: tuple[float, dict[str, Any]] | None = None
_PROFILE_CACHE_LOCK = threading.Lock()

_PROBE_CACHE: dict[str, tuple[float, "ProbeResult"]] = {}
_PROBE_CACHE_LOCK = threading.Lock()
_PROBE_LOCKS: dict[str, threading.Lock] = {}
_PROBE_LOCKS_LOCK = threading.Lock()


@dataclass(frozen=True)
class RouteObservation:
    """一次实际路由观察结果（来自真实响应或 probe 响应）。"""

    route_key: str | None = None
    candidate_id: str | None = None
    provider: str | None = None
    model_id: str | None = None
    display_name: str | None = None
    raw_header: str | None = None
    source: str = "unknown"
    observed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_key": self.route_key,
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "source": self.source,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ProbeResult:
    """轻量探测结果。探测失败不应阻止真实请求，只作为诊断信息。"""

    ok: bool
    route: RouteObservation | None = None
    error_category: str | None = None
    error_message: str | None = None
    observed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "route": self.route.to_dict() if self.route else None,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "observed_at": self.observed_at,
        }


def is_echobird_target(base_url: str, model: str = "", api_key: str = "") -> bool:
    """判断目标是否是 EchoBird。不要仅凭 model=auto 判断，避免误伤其他网关。"""
    url = (base_url or "").lower()
    key = api_key or ""
    return (
        "echobird" in url
        or "53683" in url
        or key.startswith("eb-local-")
    )


def _echobird_home() -> Path:
    return Path.home() / ".echobird"


def load_models_catalog(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """读取 EchoBird 的 models.json，返回按 internalId 索引的脱敏元数据。

    只保留路由识别需要的字段。``name`` 是用户的个人账号标签，不写入
    日志、不返回给 UI。
    """
    catalog_path = path or (_echobird_home() / "config" / "models.json")
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug("读取 EchoBird models.json 失败 (%s)", type(exc).__name__)
        return {}

    if isinstance(data, dict):
        data = data.get("models") or data.get("candidates") or []
    if not isinstance(data, list):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        internal_id = str(item.get("internalId") or "").strip()
        if not internal_id:
            continue
        account = item.get("accountSource")
        account = account if isinstance(account, dict) else {}
        result[internal_id] = {
            "internal_id": internal_id,
            "model_id": str(item.get("modelId") or "").strip(),
            "provider": str(account.get("provider") or "").strip(),
            "type": str(item.get("type") or "").strip(),
            "scope": str(item.get("scope") or "").strip(),
        }
    return result


def _provider_label(provider: str | None) -> str:
    if not provider:
        return "未知厂商"
    labels = {"qoder": "Qoder", "qodercn": "Qoder CN"}
    return labels.get(provider.lower(), provider)


def _make_display_name(provider: str | None, model_id: str | None) -> str | None:
    if provider and model_id:
        return f"{_provider_label(provider)} / {model_id}"
    if model_id:
        return model_id
    if provider:
        return _provider_label(provider)
    return None


def parse_routed_via(
    raw_header: str | None,
    body_model: str | None = None,
    catalog: Mapping[str, Mapping[str, Any]] | None = None,
) -> RouteObservation:
    """解析 ``候选ID/模型ID`` 路由头；缺失时退化为响应体 model。"""
    raw = (raw_header or "").strip() or None
    candidate_id: str | None = None
    model_id: str | None = None

    if raw:
        if "/" in raw:
            candidate_id, model_id = raw.split("/", 1)
        else:
            candidate_id = raw
        candidate_id = candidate_id.strip() or None
        model_id = (model_id or "").strip() or None

    entry = catalog.get(candidate_id) if (candidate_id and catalog) else None
    provider = str(entry.get("provider") or "") or None if entry else None
    if entry and not model_id:
        model_id = str(entry.get("model_id") or "") or None
    if not model_id:
        model_id = (body_model or "").strip() or None

    if candidate_id and model_id:
        route_key = f"{candidate_id}/{model_id}"
    else:
        route_key = model_id or candidate_id

    return RouteObservation(
        route_key=route_key,
        candidate_id=candidate_id,
        provider=provider,
        model_id=model_id,
        display_name=_make_display_name(provider, model_id),
        raw_header=raw,
        source="header" if raw else ("body_model" if model_id else "unknown"),
    )


def _profiles_path() -> Path:
    return Path(__file__).with_name("echobird_profiles.json")


def load_profiles(force: bool = False) -> dict[str, Any]:
    """读取厂商能力数据；文件缺失时使用内置默认值。"""
    global _PROFILE_CACHE
    path = _profiles_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    with _PROFILE_CACHE_LOCK:
        if not force and _PROFILE_CACHE is not None and _PROFILE_CACHE[0] == mtime:
            return _PROFILE_CACHE[1]

        data: dict[str, Any] = _default_profiles()
        if mtime:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    generic = loaded.get("generic")
                    if isinstance(generic, dict):
                        data["generic"].update(generic)
                    vendors = loaded.get("vendors")
                    if isinstance(vendors, dict):
                        for name, profile in vendors.items():
                            if isinstance(profile, dict):
                                data["vendors"][str(name)] = dict(profile)
                    models = loaded.get("models")
                    if isinstance(models, dict):
                        data["models"] = {
                            str(name): dict(profile)
                            for name, profile in models.items()
                            if isinstance(profile, dict)
                        }
            except Exception as exc:
                logger.warning("读取 EchoBird 能力配置失败，使用内置默认值 (%s)", type(exc).__name__)

        _PROFILE_CACHE = (mtime, data)
        return data


def resolve_profile(provider: str | None = None, model_id: str | None = None) -> dict[str, Any]:
    """按 ``模型 override → 厂商默认 → 通用兜底`` 解析能力。"""
    profiles = load_profiles()
    result = dict(profiles.get("generic") or _DEFAULT_GENERIC_PROFILE)

    vendors = profiles.get("vendors") or {}
    vendor_profile = None
    if provider:
        for name, profile in vendors.items():
            if str(name).lower() == provider.lower():
                vendor_profile = profile
                break
    if isinstance(vendor_profile, dict):
        result.update(vendor_profile)

    models = profiles.get("models") or {}
    model_key = f"{provider}/{model_id}" if provider and model_id else ""
    if model_key:
        override = models.get(model_key)
        if not override:
            for name, profile in models.items():
                if str(name).lower() == model_key.lower():
                    override = profile
                    break
        if isinstance(override, dict):
            result.update(override)

    result["profile_key"] = model_key or (provider or "generic")
    return result


def build_echobird_payload(
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any] | None = None,
    *,
    include_optional: bool = False,
) -> dict[str, Any]:
    """根据不同路由能力构造实际发送的请求体。

    默认采用安全基线：移除所有可选参数。``include_optional`` 预留给
    真实路由确认后的后续版本；当前调用方不会开启。
    """
    out = dict(kwargs or {})
    active_profile = dict(profile or _DEFAULT_GENERIC_PROFILE)

    max_tokens_field = str(active_profile.get("max_tokens_field") or "max_tokens")
    if max_tokens_field != "max_tokens" and "max_tokens" in out:
        out[max_tokens_field] = out.pop("max_tokens")

    drop_optional = bool(active_profile.get("drop_optional_params", True))
    for key in _OPTIONAL_PARAMETERS:
        if drop_optional or not include_optional:
            out.pop(key, None)
        elif active_profile.get(key) == "unsupported":
            out.pop(key, None)

    # 没有工具时不要留下孤立的 tool_choice。
    if not out.get("tools"):
        out.pop("tool_choice", None)

    # 不在适配层补 model；调用方传入的固定 auto 必须原样保留。
    return out


def _chat_completions_url(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    # 没有路径时按 OpenAI 兼容惯例补 /v1；已有路径（如 /api）则只补
    # /chat/completions，避免重复拼接。
    without_scheme = url.split("://", 1)[-1]
    if "/" not in without_scheme:
        return url + "/v1/chat/completions"
    return url + "/chat/completions"


def _cache_key(base_url: str, api_key: str) -> str:
    digest = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]
    return f"{(base_url or '').rstrip('/')}|{digest}"


def _lock_for(key: str) -> threading.Lock:
    with _PROBE_LOCKS_LOCK:
        lock = _PROBE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROBE_LOCKS[key] = lock
        return lock


def _probe_impl(base_url: str, api_key: str, timeout: float) -> ProbeResult:
    if not api_key:
        return ProbeResult(False, error_category="missing_key",
                           error_message="EchoBird API Key 未配置")

    url = _chat_completions_url(base_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
    }

    try:
        with httpx.Client(
            trust_env=False,
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=timeout, pool=timeout),
        ) as client:
            response = client.post(url, headers=headers, json=body)
    except Exception as exc:
        return ProbeResult(False, error_category="transport",
                           error_message=f"探测请求失败 ({type(exc).__name__})")

    if response.status_code in (401, 403):
        return ProbeResult(False, error_category="auth",
                           error_message="EchoBird API Key 认证失败 (401/403)")
    if response.status_code >= 400:
        return ProbeResult(False, error_category="http",
                           error_message=f"探测返回 HTTP {response.status_code}")

    try:
        payload = response.json()
    except Exception:
        payload = None
    body_model = payload.get("model") if isinstance(payload, dict) else None
    route = parse_routed_via(
        response.headers.get(ROUTE_HEADER),
        body_model,
        load_models_catalog(),
    )
    return ProbeResult(True, route=route)


def probe_echobird(
    base_url: str,
    api_key: str,
    *,
    force: bool = False,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """轻量探测实际路由，带 TTL 与 single-flight 缓存。"""
    key = _cache_key(base_url, api_key)
    now = time.monotonic()

    with _PROBE_CACHE_LOCK:
        cached = _PROBE_CACHE.get(key)
        if not force and cached and cached[0] > now:
            return cached[1]

    with _lock_for(key):
        now = time.monotonic()
        with _PROBE_CACHE_LOCK:
            cached = _PROBE_CACHE.get(key)
            if not force and cached and cached[0] > now:
                return cached[1]

        result = _probe_impl(base_url, api_key, timeout)
        ttl = PROBE_TTL_SECONDS if result.ok else PROBE_FAILURE_TTL_SECONDS
        with _PROBE_CACHE_LOCK:
            _PROBE_CACHE[key] = (time.monotonic() + ttl, result)
        return result


def clear_probe_cache() -> None:
    """测试辅助：清空探测缓存。"""
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE.clear()


def clear_profile_cache() -> None:
    """测试辅助：清空能力配置缓存。"""
    global _PROFILE_CACHE
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE = None


def _header_value(headers: Any, name: str) -> str | None:
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is not None:
            return str(value)
    except Exception:
        pass
    try:
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value)
    except Exception:
        pass
    return None


def adapt_payload_for_error(payload: Mapping[str, Any], exc: BaseException) -> dict[str, Any] | None:
    """真实请求返回 400 时，尝试移除不被当前路由支持的字段。

    只做“安全字段”级别的降级；工具/图片能力问题不在这里静默删除，
    避免把用户明确要求的能力降级成文本请求。
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    if status != 400:
        return None

    text = str(exc).lower()
    body = getattr(exc, "body", None)
    if body:
        text += " " + str(body).lower()

    # 工具/视觉能力错误必须上抛，由上层决定是否切换备用模型。
    if any(token in text for token in ("tool", "function", "image", "vision")):
        return None

    out = dict(payload or {})
    changed = False

    if "max_completion_tokens" in text and "max_tokens" in out:
        out["max_completion_tokens"] = out.pop("max_tokens")
        changed = True
    elif "max_tokens" in text and "max_completion_tokens" in out:
        out["max_tokens"] = out.pop("max_completion_tokens")
        changed = True

    for key in _OPTIONAL_PARAMETERS:
        if key in out and key in text:
            out.pop(key, None)
            changed = True

    return out if changed else None


class EchoBirdAdapter:
    """单个 ``LLMClient`` 的 EchoBird 适配状态。"""

    def __init__(self, base_url: str, model: str, api_key: str):
        self.base_url = base_url
        self.model = model
        self._api_key = api_key or ""
        self._probe: ProbeResult | None = None
        self._last_route: RouteObservation | None = None
        self._profile: dict[str, Any] = dict(_DEFAULT_GENERIC_PROFILE)
        self.probe_enabled = os.environ.get("ALTEREGO_ECHOBIRD_PROBE", "1").strip().lower() not in (
            "0", "false", "off", "no",
        )

    def probe(self, force: bool = False) -> ProbeResult:
        if not self.probe_enabled:
            result = ProbeResult(False, error_category="disabled",
                                 error_message="EchoBird 探测已通过环境变量关闭")
            self._probe = result
            return result
        result = probe_echobird(self.base_url, self._api_key, force=force)
        self._probe = result
        if result.ok and result.route:
            self._profile = resolve_profile(result.route.provider, result.route.model_id)
        return result

    def build_payload(
        self,
        kwargs: Mapping[str, Any],
        *,
        include_optional: bool = False,
    ) -> dict[str, Any]:
        route = self._last_route or (self._probe.route if self._probe else None)
        provider = route.provider if route else None
        model_id = route.model_id if route else None
        self._profile = resolve_profile(provider, model_id)
        return build_echobird_payload(kwargs, self._profile, include_optional=include_optional)

    def observe_response(self, headers: Any, body_model: str | None = None) -> RouteObservation:
        route = parse_routed_via(
            _header_value(headers, ROUTE_HEADER),
            body_model,
            load_models_catalog(),
        )
        self._last_route = route
        if route.provider or route.model_id:
            self._profile = resolve_profile(route.provider, route.model_id)
        return route

    def observe_error(self, exc: BaseException) -> RouteObservation | None:
        """Read the routed-via header from a failed HTTP response when available."""
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            return None
        body = getattr(exc, "body", None)
        body_model = body.get("model") if isinstance(body, dict) else None
        return self.observe_response(headers, body_model)

    def adapt_payload(self, payload: Mapping[str, Any], exc: BaseException) -> dict[str, Any] | None:
        # Only parameter-format 400s are retried. A 401/429/503 must not be
        # turned into an accidental extra request by a profile rebuild.
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        if status != 400:
            return None

        # A failed response may already contain the actual route. Rebuild with
        # that route's data profile before falling back to text-based removal.
        rebuilt = build_echobird_payload(payload, self._profile)
        if rebuilt != dict(payload or {}):
            return rebuilt
        return adapt_payload_for_error(payload, exc)

    def info(self) -> dict[str, Any]:
        return {
            "enabled": self.probe_enabled,
            "probe": self._probe.to_dict() if self._probe else None,
            "last_route": self._last_route.to_dict() if self._last_route else None,
            "capabilities": dict(self._profile),
        }
