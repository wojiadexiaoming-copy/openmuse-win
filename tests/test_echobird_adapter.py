"""EchoBird adapter tests use synthetic keys and offline transports only."""
import json

import httpx
from openai import OpenAI
import pytest

from agent import echobird_adapter as adapter_mod
from agent.echobird_adapter import (
    EchoBirdAdapter,
    ProbeResult,
    RouteObservation,
    adapt_payload_for_error,
    build_echobird_payload,
    clear_probe_cache,
    clear_profile_cache,
    is_echobird_target,
    load_models_catalog,
    parse_routed_via,
    probe_echobird,
    resolve_profile,
)
from agent.llm import LLMClient
from agent.models import Message


CATALOG = {
    "m-a9ca5e": {"provider": "qoder", "model_id": "DeepSeek-Flash"},
    "m-7f3e97": {"provider": "qoderCn", "model_id": "DeepSeek-Flash"},
}


@pytest.fixture(autouse=True)
def _clear_adapter_caches():
    clear_probe_cache()
    clear_profile_cache()
    yield
    clear_probe_cache()
    clear_profile_cache()


def test_target_detection_does_not_treat_every_auto_as_echobird():
    assert is_echobird_target("http://127.0.0.1:53683/v1", "auto", "synthetic")
    assert is_echobird_target("https://example.invalid/v1", "auto", "eb-local-synthetic")
    assert is_echobird_target("https://echobird.example/v1", "some-model", "")
    assert not is_echobird_target("https://example.invalid/v1", "auto", "sk-synthetic")


def test_parse_route_header_maps_candidate_to_vendor_model():
    route = parse_routed_via("m-a9ca5e/DeepSeek-Flash", None, CATALOG)
    assert route.route_key == "m-a9ca5e/DeepSeek-Flash"
    assert route.provider == "qoder"
    assert route.model_id == "DeepSeek-Flash"
    assert route.display_name == "Qoder / DeepSeek-Flash"
    assert route.source == "header"


def test_parse_route_header_without_slash_uses_catalog_and_body():
    route = parse_routed_via("m-7f3e97", "DeepSeek-Flash", CATALOG)
    assert route.provider == "qoderCn"
    assert route.route_key == "m-7f3e97/DeepSeek-Flash"


def test_parse_route_keeps_slashes_inside_model_id():
    route = parse_routed_via("m-a9ca5e/qwen/max", None, CATALOG)
    assert route.candidate_id == "m-a9ca5e"
    assert route.model_id == "qwen/max"
    assert route.route_key == "m-a9ca5e/qwen/max"


def test_parse_route_without_header_degrades_to_body_model():
    route = parse_routed_via(None, "DeepSeek-Flash", CATALOG)
    assert route.route_key == "DeepSeek-Flash"
    assert route.provider is None
    assert route.source == "body_model"


def test_catalog_reader_does_not_expose_personal_account_name(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps([{
        "internalId": "m-a9ca5e",
        "name": "personal-account-label-not-for-logs",
        "modelId": "DeepSeek-Flash",
        "accountSource": {"provider": "qoder"},
        "type": "openai",
        "scope": "global",
    }]), encoding="utf-8")
    catalog = load_models_catalog(path)
    assert catalog["m-a9ca5e"]["provider"] == "qoder"
    assert "name" not in catalog["m-a9ca5e"]
    assert "personal-account-label" not in json.dumps(catalog)


def test_profile_precedence_starts_with_generic_then_vendor_then_model():
    generic = resolve_profile(None, None)
    qoder = resolve_profile("qoder", "DeepSeek-Flash")
    assert generic["temperature"] == "unknown"
    assert qoder["temperature"] == "unsupported"
    assert qoder["tools"] == "supported"
    assert qoder["reasoning"] == "supported"
    assert qoder["profile_key"] == "qoder/DeepSeek-Flash"


def test_safe_baseline_removes_temperature_and_optional_fields():
    payload = build_echobird_payload({
        "model": "auto",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 64,
        "tools": [{"type": "function"}],
        "tool_choice": "auto",
        "response_format": {"type": "json_object"},
    }, resolve_profile("qoder", "DeepSeek-Flash"))
    assert payload["model"] == "auto"
    assert payload["max_tokens"] == 64
    assert payload["tools"] == [{"type": "function"}]
    assert payload["tool_choice"] == "auto"
    for key in ("temperature", "top_p", "response_format"):
        assert key not in payload


def test_profile_can_map_max_tokens_field_without_vendor_branching():
    profile = dict(resolve_profile("futureVendor", "future-model"))
    profile["max_tokens_field"] = "max_completion_tokens"
    payload = build_echobird_payload(
        {"model": "auto", "messages": [], "max_tokens": 32},
        profile,
    )
    assert payload["max_completion_tokens"] == 32
    assert "max_tokens" not in payload


def test_400_adaptation_removes_temperature_only_when_present():
    class BadRequest(Exception):
        status_code = 400
        body = '{"error":{"message":"Unsupported parameter: temperature"}}'

    adapted = adapt_payload_for_error(
        {"model": "auto", "messages": [], "temperature": 0.2, "max_tokens": 8},
        BadRequest("bad request"),
    )
    assert adapted is not None
    assert "temperature" not in adapted
    assert adapted["max_tokens"] == 8


def test_non_400_errors_are_not_adapted_into_extra_requests():
    class Unauthorized(Exception):
        status_code = 401

    adapter = EchoBirdAdapter("http://127.0.0.1:53683/v1", "auto", "eb-local-test-only")
    adapter._profile["max_tokens_field"] = "max_completion_tokens"
    assert adapter.adapt_payload(
        {"model": "auto", "messages": [], "max_tokens": 8},
        Unauthorized("unauthorized"),
    ) is None


def test_400_can_rebuild_payload_from_observed_route_profile():
    class BadRequest(Exception):
        status_code = 400

    adapter = EchoBirdAdapter("http://127.0.0.1:53683/v1", "auto", "eb-local-test-only")
    adapter._profile["max_tokens_field"] = "max_completion_tokens"
    adapted = adapter.adapt_payload(
        {"model": "auto", "messages": [], "max_tokens": 8},
        BadRequest("bad request"),
    )
    assert adapted is not None
    assert adapted["max_completion_tokens"] == 8
    assert "max_tokens" not in adapted


def test_400_tool_error_is_not_silently_downgraded():
    class BadRequest(Exception):
        status_code = 400
        body = '{"error":{"message":"tools are not supported by this route"}}'

    assert adapt_payload_for_error(
        {"model": "auto", "messages": [], "tools": [{"type": "function"}]},
        BadRequest("bad request"),
    ) is None


def test_probe_cache_is_single_flight_and_force_refreshes(monkeypatch):
    calls = []

    def fake_probe(base_url, api_key, timeout):
        calls.append((base_url, api_key))
        return ProbeResult(True, RouteObservation(
            route_key="m-a9ca5e/DeepSeek-Flash",
            candidate_id="m-a9ca5e",
            provider="qoder",
            model_id="DeepSeek-Flash",
        ))

    monkeypatch.setattr(adapter_mod, "_probe_impl", fake_probe)
    first = probe_echobird("http://127.0.0.1:53683/v1", "synthetic-only")
    second = probe_echobird("http://127.0.0.1:53683/v1", "synthetic-only")
    assert first.ok and second.ok
    assert len(calls) == 1

    probe_echobird("http://127.0.0.1:53683/v1", "synthetic-only", force=True)
    assert len(calls) == 2


def test_error_response_header_updates_route_for_retry(monkeypatch):
    monkeypatch.setattr(adapter_mod, "load_models_catalog", lambda path=None: CATALOG)

    class BadRequest(Exception):
        status_code = 400
        body = {"error": {"message": "unsupported parameter"}}
        response = type("Response", (), {
            "headers": {"x-echobird-routed-via": "m-7f3e97/DeepSeek-Flash"},
        })()

    adapter = EchoBirdAdapter("http://127.0.0.1:53683/v1", "auto", "eb-local-test-only")
    route = adapter.observe_error(BadRequest("bad request"))
    assert route is not None
    assert route.provider == "qoderCn"
    assert adapter.info()["last_route"]["model_id"] == "DeepSeek-Flash"


def test_llmclient_uses_auto_and_reads_actual_route(monkeypatch):
    monkeypatch.setenv("ALTEREGO_ECHOBIRD_PROBE", "0")
    monkeypatch.setattr(adapter_mod, "load_models_catalog", lambda path=None: CATALOG)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(body)
        return httpx.Response(
            200,
            headers={"x-echobird-routed-via": "m-a9ca5e/DeepSeek-Flash"},
            json={
                "id": "chatcmpl-offline",
                "object": "chat.completion",
                "created": 0,
                "model": "DeepSeek-Flash",
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "offline reply"},
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    client = LLMClient(
        base_url="http://127.0.0.1:53683/v1",
        model="auto",
        api_key="eb-local-test-only",
        temperature=0.2,
        max_tokens=64,
    )
    client.client = OpenAI(
        base_url="http://127.0.0.1:53683/v1",
        api_key="eb-local-test-only",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.chat([Message(role="user", content="ping")])
    assert result.content == "offline reply"
    assert seen["model"] == "auto"
    assert "temperature" not in seen
    assert seen["max_tokens"] == 64
    assert result.metadata["echobird_route"] == "m-a9ca5e/DeepSeek-Flash"
    assert result.metadata["echobird_provider"] == "qoder"
    assert result.metadata["echobird_model"] == "DeepSeek-Flash"

    info = client.get_echobird_route_info()
    assert info["last_route"]["provider"] == "qoder"
    assert "api_key" not in json.dumps(info).lower()


def test_adapter_info_never_contains_key():
    adapter = EchoBirdAdapter("http://127.0.0.1:53683/v1", "auto", "eb-local-secret-sentinel")
    adapter._last_route = RouteObservation(route_key="m-a9ca5e/DeepSeek-Flash")
    serialized = json.dumps(adapter.info())
    assert "secret-sentinel" not in serialized
