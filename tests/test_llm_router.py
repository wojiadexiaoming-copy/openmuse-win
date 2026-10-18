"""Injected provider plans and fake HTTP only; never read real credentials."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from openai import OpenAI as RealOpenAI
import pytest
from PIL import Image

from agent import llm as llm_module
from agent.llm import _request_context
from agent.llm_router import (
    DEFAULT_PROVIDERS, RoutedLLMClient, RoutingConfigurationError, RoutingUnavailable, RoutingHealthState,
    build_embedding_client, build_routed_client,
)
from agent.models import ImagePart, Message, ToolCall


def plan():
    return {
        "enabled": True,
        "providers": deepcopy(DEFAULT_PROVIDERS),
        "roles": {
            "main_brain": ["model-a@nvidia", "model-b@modelscope", "model-c@zhipu"],
            "sub_agent": {"chain": ["model-b@modelscope"]},
            "code": ["model-c@zhipu"],
            "vision": ["vision-b@modelscope", "agnes-3.0-flash@agnes"],
            "memory_embedding": ["embed-a@nvidia", "embed-b@modelscope"],
        },
        "capabilities": {
            "model-a@nvidia": {"chat": True, "tools": True, "vision": False},
            "model-b@modelscope": {"chat": True, "tools": True, "vision": False},
            "model-c@zhipu": {"chat": True, "tools": True, "vision": False},
            "vision-b@modelscope": {"chat": True, "tools": True, "vision": True},
            "agnes-3.0-flash@agnes": {"chat": True, "tools": True, "vision": True},
            "embed-a@nvidia": {"chat": False, "embedding": True, "dimensions": 2},
            "embed-b@modelscope": {"chat": False, "embedding": True, "dimensions": 3},
        },
        "options": {"timeout_seconds": 60, "max_retries": 0},
    }


ENV = {config["api_key_env"]: f"fake-{provider}-credential"
       for provider, config in DEFAULT_PROVIDERS.items()}
TOOLS = [{"type": "function", "function": {
    "name": "file_read", "description": "read", "parameters": {"type": "object", "properties": {}},
}}]


def chat_response(model, content=None, calls=None):
    return {
        "id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": content if content is not None else "reply:" + model,
            "tool_calls": calls or [],
        }}],
    }


def embedding_response(model, vectors):
    return {"object": "list", "model": model, "usage": {"prompt_tokens": 1, "total_tokens": 1},
            "data": [{"object": "embedding", "index": index, "embedding": vector}
                     for index, vector in enumerate(vectors)]}


def image_message():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), "red").save(buffer, format="PNG")
    return Message(role="user", content="private screenshot prompt",
                   images=[ImagePart(data=buffer.getvalue(), mime="image/png", mime_type="image/png")])


@pytest.fixture
def fake_http(monkeypatch):
    calls = []
    outcomes = {}
    created = []
    http_clients = []

    def handler(request):
        body = json.loads(request.content)
        calls.append({"url": str(request.url), "body": body,
                      "authorization": request.headers.get("authorization")})
        model = body["model"]
        outcome = outcomes.get(model)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome(body)
        if outcome is None:
            if request.url.path.endswith("/embeddings"):
                inputs = body["input"]
                count = 1 if isinstance(inputs, str) else len(inputs)
                dims = 2 if model == "embed-a" else 3
                outcome = (200, embedding_response(model, [[float(i)] * dims for i in range(count)]))
            else:
                outcome = (200, chat_response(model))
        status, payload = outcome
        return httpx.Response(status, content=json.dumps(payload, allow_nan=True).encode("utf-8"),
                              headers={"Content-Type": "application/json"}, request=request)

    def factory(**kwargs):
        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        http_clients.append(http_client)
        sdk = RealOpenAI(**kwargs, http_client=http_client, max_retries=0)
        created.append(sdk)
        return sdk

    monkeypatch.setattr(llm_module, "OpenAI", factory)
    fixture = SimpleNamespace(calls=calls, outcomes=outcomes, created=created)
    yield fixture
    for client in http_clients:
        client.close()


def api_error(code=None):
    return {"error": {"message": "private-server-detail sk-do-not-leak",
                      "type": "provider_error", "code": code}}


@pytest.mark.parametrize("role,model", [
    ("main", "model-a"), ("planner", "model-a"), ("worker", "model-a"),
    ("main_brain", "model-a"), ("subagent", "model-b"), ("sub_agent", "model-b"),
    ("code", "model-c"), ("vision", "vision-b"),
])
def test_legacy_role_aliases_are_lazy_and_keep_ui_attributes(fake_http, role, model):
    client = build_routed_client(plan(), role=role, env={})

    assert client.model == model
    assert client.base_url.startswith("https://")
    assert isinstance(client.supports_images, bool)
    assert fake_http.calls == fake_http.created == []
    if role in {"main", "main_brain", "planner", "worker"}:
        assert client.fallback.model == "model-b"


def test_three_layer_failover_keeps_provider_credentials_and_observer(fake_http):
    fake_http.outcomes["model-a"] = (429, api_error("rate_limited"))
    fake_http.outcomes["model-b"] = (503, api_error("unavailable"))
    observer = []
    client = build_routed_client(plan(), env=ENV).set_request_observer(observer.append)

    result = client.chat([Message(role="user", content="private prompt")])

    assert result.content == "reply:model-c"
    assert [call["body"]["model"] for call in fake_http.calls] == ["model-a", "model-b", "model-c"]
    assert [call["authorization"] for call in fake_http.calls] == [
        "Bearer fake-nvidia-credential", "Bearer fake-modelscope-credential", "Bearer fake-zhipu-credential",
    ]
    assert [event["fallback_depth"] for event in observer] == [0, 1, 2]
    assert [event["success"] for event in observer] == [False, False, True]
    assert [event["provider"] for event in observer] == ["nvidia", "modelscope", "zhipu"]
    assert all(event["role"] == "main_brain" and event["request_attempted"] for event in observer)
    assert len({event["request_id"] for event in observer}) == 1
    assert observer[-1]["actual_model"] == client.model == "model-c"
    assert all(sdk.max_retries == 0 for sdk in fake_http.created)
    assert _request_context.get() is None
    rendered = json.dumps(observer)
    assert "private prompt" not in rendered and "sk-do-not-leak" not in rendered


def test_vault_and_env_lookup_are_exact_and_never_use_generic_key(fake_http):
    vault_names = []

    def resolve(name, caller):
        vault_names.append(name)
        if name == "nvidia_api_key":
            return "fake-nvidia-vault"
        raise KeyError("not configured")

    fake_http.outcomes["model-a"] = (429, api_error())
    client = build_routed_client(plan(), vault=SimpleNamespace(resolve=resolve), env=ENV)

    assert client.chat([Message(role="user", content="hi")]).content == "reply:model-b"
    assert vault_names == ["nvidia_api_key", "modelscope_api_key"]
    assert fake_http.calls[0]["authorization"] == "Bearer fake-nvidia-vault"
    assert fake_http.calls[1]["authorization"] == "Bearer fake-modelscope-credential"

    only_generic = {"ALTEREGO_LLM_API_KEY": "private-generic", "OPENAI_API_KEY": "private-generic"}
    with pytest.raises(RoutingUnavailable) as caught:
        build_routed_client(plan(), env=only_generic).chat([Message(role="user", content="hi")])
    assert all(item["category"] == "missing_key" for item in caught.value.failures)
    assert "private-generic" not in str(caught.value)
    assert len(fake_http.calls) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_skips_other_models_with_same_provider(fake_http, status):
    config = plan()
    config["roles"]["main_brain"] = ["model-a@nvidia", "another@nvidia", "model-b@modelscope"]
    config["capabilities"]["another@nvidia"] = {"chat": True, "tools": True}
    fake_http.outcomes["model-a"] = (status, api_error("invalid_api_key"))
    observer = []
    client = build_routed_client(config, env=ENV).set_request_observer(observer.append)

    assert client.chat([Message(role="user", content="hi")]).content == "reply:model-b"
    assert [call["body"]["model"] for call in fake_http.calls] == ["model-a", "model-b"]
    assert observer[0]["failure_category"] == "authentication"
    assert observer[1]["failure_category"] == "provider_auth_blocked"
    assert observer[1]["request_attempted"] is False


def test_model_specific_401_does_not_disable_other_models_with_same_key(fake_http):
    config = plan()
    config["roles"]["main_brain"] = ["model-a@nvidia", "another@nvidia"]
    config["capabilities"]["another@nvidia"] = {"chat": True, "tools": True}
    fake_http.outcomes["model-a"] = (401, api_error("invalid_model"))
    observer = []
    client = build_routed_client(config, env=ENV).set_request_observer(observer.append)

    assert client.chat([Message(role="user", content="hi")]).content == "reply:another"
    assert [call["body"]["model"] for call in fake_http.calls] == ["model-a", "another"]
    assert observer[0]["failure_category"] == "configuration"


@pytest.mark.parametrize("status", [400, 404, 422])
def test_configuration_failures_are_not_recorded_as_rate_limits(fake_http, status):
    fake_http.outcomes["model-a"] = (status, api_error("invalid_model"))
    events = []
    client = build_routed_client(plan(), env=ENV).set_request_observer(events.append)

    client.chat([Message(role="user", content="hi")])

    assert events[0]["failure_category"] == "configuration"
    assert events[0]["status_code"] == status
    assert events[0]["request_attempted"] is True


def test_transport_timeout_can_fall_back_without_leaking_error(fake_http):
    fake_http.outcomes["model-a"] = httpx.ReadTimeout("private timeout detail")
    events = []
    client = build_routed_client(plan(), env=ENV).set_request_observer(events.append)

    assert client.chat([Message(role="user", content="hi")]).content == "reply:model-b"
    assert events[0]["failure_category"] == "transport"
    assert "private timeout detail" not in json.dumps(events)


def test_all_failures_raise_safe_summary_and_reset_request_context(fake_http):
    for model in ("model-a", "model-b", "model-c"):
        fake_http.outcomes[model] = (429, api_error("limited"))
    client = build_routed_client(plan(), env=ENV)

    with pytest.raises(RoutingUnavailable) as caught:
        client.chat([Message(role="user", content="private prompt")])

    assert caught.value.status_code == 429
    assert all(failure["category"] == "rate_limit" for failure in caught.value.failures)
    assert "private" not in str(caught.value) and "sk-do-not-leak" not in str(caught.value)
    assert _request_context.get() is None


@pytest.mark.parametrize("history", [False, True])
def test_tools_are_filtered_for_schemas_and_prior_protocol_history(fake_http, history):
    config = plan()
    config["capabilities"]["model-a@nvidia"]["tools"] = False
    messages = [Message(role="user", content="hi")]
    if history:
        messages.extend([
            Message(role="assistant", tool_calls=[ToolCall(id="call-1", name="file_read")]),
            Message(role="tool", name="file_read", tool_call_id="call-1", content="result"),
        ])
    client = build_routed_client(config, env=ENV)

    client.chat(messages, tools=None if history else TOOLS, known_tools={"file_read"})

    assert [call["body"]["model"] for call in fake_http.calls] == ["model-b"]
    if history:
        assert [message["role"] for message in fake_http.calls[0]["body"]["messages"]] == [
            "user", "assistant", "tool",
        ]
        assert fake_http.calls[0]["body"]["messages"][-1]["tool_call_id"] == "call-1"


def test_image_and_tools_use_only_verified_vision_candidates(fake_http):
    fake_http.outcomes["vision-b"] = (503, api_error())
    messages = [image_message()]
    events = []
    client = build_routed_client(plan(), env=ENV).set_request_observer(events.append)

    assert client.chat(messages, tools=TOOLS, known_tools={"file_read"}).content == "reply:agnes-3.0-flash"
    assert [call["body"]["model"] for call in fake_http.calls] == ["vision-b", "agnes-3.0-flash"]
    image_payloads = [call["body"]["messages"][0]["content"][1] for call in fake_http.calls]
    assert image_payloads[0] == image_payloads[1]
    assert all(event["image_count"] == 1 for event in events)
    assert events[-1]["images"][0]["dimensions"] == [8, 6]
    assert client.supports_images is True


def test_text_only_chain_cannot_receive_images_even_with_no_backup(fake_http):
    config = plan()
    config["roles"]["vision"] = []
    config["capabilities"]["agnes-3.0-flash@agnes"]["vision"] = False
    events = []
    client = build_routed_client(config, env=ENV).set_request_observer(events.append)

    with pytest.raises(RoutingUnavailable):
        client.chat([image_message()])

    assert fake_http.calls == []
    assert all(event["request_attempted"] is False for event in events)
    assert all(event["failure_category"] == "vision_capability_blocked" for event in events)


def test_native_unknown_tool_is_rejected_before_dispatch_and_can_fall_back(fake_http):
    bad = [{"id": "bad", "type": "function", "function": {"name": "unknown_tool", "arguments": "{}"}}]
    good = [{"id": "good", "type": "function", "function": {"name": "file_read", "arguments": '{"path":"a"}'}}]
    fake_http.outcomes["model-a"] = (200, chat_response("model-a", calls=bad))
    fake_http.outcomes["model-b"] = (200, chat_response("model-b", calls=good))
    events = []
    client = build_routed_client(plan(), env=ENV).set_request_observer(events.append)

    result = client.chat([Message(role="user", content="read")], tools=TOOLS, known_tools={"file_read"})

    assert [call.name for call in result.tool_calls] == ["file_read"]
    assert events[0]["success"] is False and events[0]["failure_category"] == "protocol"
    assert events[0]["phase"] == "response_protocol_rejected"


def test_observer_failure_does_not_trigger_paid_failover(fake_http):
    client = build_routed_client(plan(), env=ENV).set_request_observer(
        lambda _: (_ for _ in ()).throw(RuntimeError("offline observer failure")))

    assert client.chat([Message(role="user", content="hi")]).content == "reply:model-a"
    assert len(fake_http.calls) == 1


def test_per_model_thinking_options_and_sdk_retry_timeout(fake_http):
    config = plan()
    config["model_options"] = {"model-a@nvidia": {
        "temperature": 0.2, "max_tokens": 512, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }}
    client = build_routed_client(config, env=ENV)

    client.chat([Message(role="user", content="hi")])

    body = fake_http.calls[0]["body"]
    assert body["temperature"] == 0.2 and body["max_tokens"] == 512
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert client.client.max_retries == 0
    assert client.client.timeout == 60


@pytest.mark.parametrize("extra", [
    {"model": "spoof"}, {"messages": []}, {"nested": {"api_key": "private"}},
    {"tools": []}, {"extra_headers": {"Authorization": "private"}},
    {"functions": []}, {"provider": "spoof"}, {"nested": {"ACCESS_TOKEN": "private"}},
    {"secret": "private"}, {"project": "spoof"},
])
def test_model_options_cannot_override_protocol_or_authentication(fake_http, extra):
    config = plan()
    config["roles"]["main_brain"] = ["model-a@nvidia"]
    config["model_options"] = {"model-a@nvidia": {"extra_body": extra}}

    with pytest.raises(RoutingUnavailable) as caught:
        build_routed_client(config, env=ENV).chat([Message(role="user", content="hi")])

    assert fake_http.calls == fake_http.created == []
    assert caught.value.failures[0]["category"] == "configuration"


@pytest.mark.parametrize("url", [
    "https://other.example/v1", "https://integrate.api.nvidia.com:444/v1",
    "https://fake-user@integrate.api.nvidia.com/v1", "http://integrate.api.nvidia.com/v1",
    "https://integrate.api.nvidia.com/v1?api_key=private",
])
def test_inline_plan_cannot_redirect_provider_credentials(fake_http, url):
    config = plan()
    config["providers"]["nvidia"]["base_url"] = url

    with pytest.raises(RoutingConfigurationError):
        build_routed_client(config, env=ENV)

    assert fake_http.calls == fake_http.created == []


def test_generic_or_shared_key_env_names_are_rejected(fake_http):
    config = plan()
    config["providers"]["nvidia"]["api_key_env"] = "ALTEREGO_LLM_API_KEY"
    with pytest.raises(RoutingConfigurationError):
        build_routed_client(config, env=ENV)
    config = plan()
    config["providers"]["modelscope"]["api_key_env"] = "NVIDIA_API_KEY"
    with pytest.raises(RoutingConfigurationError):
        build_routed_client(config, env=ENV)


def test_disabling_fallback_pins_diagnostic_model_and_prevents_vision_switch(fake_http):
    config = plan()
    config["capabilities"]["model-a@nvidia"]["vision"] = True
    fake_http.outcomes["model-a"] = (429, api_error())
    client = build_routed_client(config, env=ENV)
    saved = client.fallback
    client.fallback = None

    with pytest.raises(RoutingUnavailable):
        client.chat([image_message()])

    assert [call["body"]["model"] for call in fake_http.calls] == ["model-a"]
    client.fallback = saved
    assert client.fallback.model == "model-b"


@pytest.mark.parametrize("input_type", ["query", "passage"])
def test_nvidia_embeddings_use_only_embedding_endpoint_and_validate_order(fake_http, input_type):
    body = embedding_response("embed-a", [[1, 2], [3, 4]])
    body["data"].reverse()
    fake_http.outcomes["embed-a"] = (200, body)
    events = []
    client = build_embedding_client(plan(), env=ENV).set_request_observer(events.append)

    assert client.embed(["first", "second"], input_type=input_type) == [[1.0, 2.0], [3.0, 4.0]]
    request = fake_http.calls[0]
    assert request["url"].endswith("/embeddings")
    assert request["body"]["input_type"] == input_type
    assert request["body"]["truncate"] == "END"
    assert request["body"]["encoding_format"] == "float"
    assert client.dimensions == 2
    assert client.vector_space_id == "nvidia/embed-a/2"
    assert events[0]["role"] == "memory_embedding" and events[0]["request_attempted"]
    with pytest.raises(RoutingConfigurationError):
        client.chat([Message(role="user", content="not chat")])
    assert len(fake_http.calls) == 1


@pytest.mark.parametrize("problem", ["duplicate_index", "wrong_count", "wrong_dimensions", "nan", "infinity", "boolean", "empty"])
def test_invalid_embedding_evidence_falls_back_to_distinct_vector_space(fake_http, problem):
    payload = embedding_response("embed-a", [[1, 2], [3, 4]])
    if problem == "duplicate_index":
        payload["data"][1]["index"] = 0
    elif problem == "wrong_count":
        payload["data"].pop()
    elif problem == "wrong_dimensions":
        payload["data"][1]["embedding"] = [1]
    elif problem == "nan":
        payload["data"][0]["embedding"] = [float("nan"), 1]
    elif problem == "infinity":
        payload["data"][0]["embedding"] = [float("inf"), 1]
    elif problem == "boolean":
        payload["data"][0]["embedding"] = [True, 1]
    elif problem == "empty":
        payload["data"][0]["embedding"] = []
    fake_http.outcomes["embed-a"] = (200, payload)
    events = []
    client = build_embedding_client(plan(), env=ENV).set_request_observer(events.append)

    vectors = client.embed(["a", "b"])

    assert len(vectors) == 2 and all(len(vector) == 3 for vector in vectors)
    assert [call["body"]["model"] for call in fake_http.calls] == ["embed-a", "embed-b"]
    assert all(call["url"].endswith("/embeddings") for call in fake_http.calls)
    assert "input_type" not in fake_http.calls[1]["body"]
    assert client.vector_space_id == "modelscope/embed-b/3"
    assert events[0]["failure_category"] == "protocol"


@pytest.mark.parametrize("inputs,input_type", [
    ([], "query"), ("", "query"), (["a", ""], "query"), ([1], "query"),
    ([Message(role="user", content="x")], "query"), ("a", "invalid"),
])
def test_embedding_rejects_invalid_input_before_any_request(fake_http, inputs, input_type):
    with pytest.raises(ValueError):
        build_embedding_client(plan(), env=ENV).embed(inputs, input_type=input_type)
    assert fake_http.calls == fake_http.created == []


def test_embedding_capabilities_and_disabled_provider_are_enforced(fake_http):
    config = plan()
    config["providers"]["nvidia"]["enabled"] = False
    config["capabilities"]["embed-b@modelscope"]["embedding"] = False

    with pytest.raises(RoutingUnavailable):
        build_embedding_client(config, env=ENV).embed("text")

    assert fake_http.calls == []
    with pytest.raises(RoutingConfigurationError):
        build_routed_client(config, role="memory_embedding", env=ENV)


@pytest.mark.parametrize("kind", ["rate_limit", "transport", "service_unavailable"])
def test_model_cooldown_spans_calls_and_expiry_restores_primary(fake_http, monkeypatch, kind):
    clock = [100.0]
    monkeypatch.setattr("agent.llm_router.time.monotonic", lambda: clock[0])
    if kind == "transport":
        fake_http.outcomes["model-a"] = httpx.ReadTimeout("private transport detail")
    else:
        fake_http.outcomes["model-a"] = (429 if kind == "rate_limit" else 503, api_error())
    config = plan()
    config["options"]["failure_cooldown_seconds"] = 30
    events = []
    client = build_routed_client(config, env=ENV).set_request_observer(events.append)
    messages = [Message(role="user", content="hi")]

    assert client.chat(messages).content == "reply:model-b"
    fake_http.outcomes.pop("model-a")
    clock[0] += 29
    assert client.chat(messages).content == "reply:model-b"
    assert [call["body"]["model"] for call in fake_http.calls] == ["model-a", "model-b", "model-b"]
    skipped = [event for event in events if event.get("failure_category") == "cooldown"]
    assert len(skipped) == 1 and skipped[0]["request_attempted"] is False
    clock[0] += 1
    assert client.chat(messages).content == "reply:model-a"
    assert client.model == "model-a"


def test_pinned_diagnostic_ignores_cooldown_and_keeps_capability_guards(fake_http, monkeypatch):
    monkeypatch.setattr("agent.llm_router.time.monotonic", lambda: 100.0)
    fake_http.outcomes["model-a"] = (429, api_error())
    client = build_routed_client(plan(), env=ENV)
    client.chat([Message(role="user", content="hi")])
    fake_http.outcomes.pop("model-a")
    client.fallback = None
    assert client.chat([Message(role="user", content="hi")]).content == "reply:model-b"
    pinned = client.pin("model-a@nvidia")
    assert pinned.chat([Message(role="user", content="hi")]).content == "reply:model-a"
    count = len(fake_http.calls)
    with pytest.raises(RoutingUnavailable) as caught:
        pinned.chat([image_message()])
    assert len(fake_http.calls) == count
    assert caught.value.failures[0]["category"] == "vision_capability_blocked"


@pytest.mark.parametrize("content", ["", "   "])
def test_empty_reply_without_tool_calls_is_protocol_failure(fake_http, content):
    payload = chat_response("model-a", content=content)
    payload["choices"][0]["message"]["reasoning_content"] = "private reasoning only"
    fake_http.outcomes["model-a"] = (200, payload)
    events = []
    reply = build_routed_client(plan(), env=ENV).set_request_observer(events.append).chat(
        [Message(role="user", content="hi")])
    assert reply.content == "reply:model-b"
    assert events[0]["failure_category"] == "protocol"
    assert events[0]["success"] is False
    assert "private reasoning" not in json.dumps(events)


def test_embedding_cooldown_skips_failed_primary_across_calls(fake_http, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agent.llm_router.time.monotonic", lambda: clock[0])
    fake_http.outcomes["embed-a"] = (429, api_error())
    events = []
    client = build_embedding_client(plan(), env=ENV).set_request_observer(events.append)
    assert len(client.embed("a")[0]) == 3
    fake_http.outcomes.pop("embed-a")
    assert len(client.embed("a")[0]) == 3
    assert [call["body"]["model"] for call in fake_http.calls] == ["embed-a", "embed-b", "embed-b"]
    assert any(event.get("failure_category") == "cooldown" and event["request_attempted"] is False for event in events)
    clock[0] += 30
    assert len(client.embed("a")[0]) == 2


@pytest.mark.parametrize("cooldown", [-1, True, float("nan"), 601, "30"])
def test_invalid_cooldown_configuration_rejected(fake_http, cooldown):
    config = plan()
    config["options"]["failure_cooldown_seconds"] = cooldown
    with pytest.raises(RoutingConfigurationError):
        build_routed_client(config, env=ENV)
    assert fake_http.calls == []


def test_swapping_provider_env_owners_cannot_exfiltrate_keys(fake_http):
    config = plan()
    config["providers"]["nvidia"]["api_key_env"] = "MODELSCOPE_API_KEY"
    config["providers"]["modelscope"]["api_key_env"] = "NVIDIA_API_KEY"
    with pytest.raises(RoutingConfigurationError):
        build_routed_client(config, env=ENV)
    assert fake_http.calls == []



def test_cyclic_model_extension_is_rejected_before_credentials_or_network(fake_http):
    config = plan()
    config["roles"]["main_brain"] = ["model-a@nvidia"]
    extra = {"extension": []}
    extra["extension"].append(extra)
    config["model_options"] = {"model-a@nvidia": {"extra_body": extra}}
    vault = MagicMock()
    with pytest.raises(RoutingUnavailable) as caught:
        build_routed_client(config, env=ENV, vault=vault).chat([Message(role="user", content="hi")])
    assert caught.value.failures[0]["category"] == "configuration"
    vault.resolve.assert_not_called()
    assert fake_http.calls == fake_http.created == []



def test_shared_health_skips_failed_model_across_roles_without_sharing_sdks(fake_http):
    state = RoutingHealthState(clock=lambda: 100.0)
    scope = object()
    fake_http.outcomes["model-a"] = (429, api_error())
    main = build_routed_client(plan(), env=ENV, health_state=state, owner_scope=scope)
    assert main.chat([Message(role="user", content="main")]).content == "reply:model-b"
    for role in ("planner", "worker", "subagent"):
        config = plan()
        config["roles"]["sub_agent"] = ["model-a@nvidia", "model-b@modelscope"]
        config["roles"]["unused"] = ["model-c@zhipu"]
        events = []
        client = build_routed_client(config, role=role, env=ENV, health_state=state,
                                     owner_scope=scope).set_request_observer(events.append)
        assert client.chat([Message(role="user", content=role)]).content == "reply:model-b"
        assert events[0]["failure_category"] == "cooldown" and events[0]["request_attempted"] is False
        assert events[0]["status_code"] == 429
        assert main._clients["model-b@modelscope"] is not client._clients["model-b@modelscope"]
        assert main._clients["model-b@modelscope"].client is not client._clients["model-b@modelscope"].client
    assert [call["body"]["model"] for call in fake_http.calls] == ["model-a", "model-b", "model-b", "model-b", "model-b"]


@pytest.mark.parametrize("shared_state", [False, True])
def test_health_does_not_share_by_default_without_explicit_owner(fake_http, shared_state):
    state = RoutingHealthState(clock=lambda: 100.0) if shared_state else None
    fake_http.outcomes["model-a"] = (429, api_error())
    for _ in range(2):
        build_routed_client(plan(), env=ENV, health_state=state).chat([Message(role="user", content="hi")])
    assert [call["body"]["model"] for call in fake_http.calls] == ["model-a", "model-b", "model-a", "model-b"]


@pytest.mark.parametrize("difference", ["owner", "credential", "settings", "capabilities", "provider_path"])
def test_shared_health_isolated_by_owner_credential_and_configuration(fake_http, difference):
    state = RoutingHealthState(clock=lambda: 100.0)
    scope = object()
    fake_http.outcomes["model-a"] = (429, api_error())
    build_routed_client(plan(), env=ENV, health_state=state, owner_scope=scope).chat(
        [Message(role="user", content="first")])
    fake_http.outcomes.pop("model-a")
    config = plan()
    env = dict(ENV)
    if difference == "owner":
        scope = object()
    elif difference == "credential":
        env["NVIDIA_API_KEY"] = "fake-rotated-nvidia-credential"
    elif difference == "settings":
        config["model_options"] = {"model-a@nvidia": {"temperature": 0.1}}
    elif difference == "capabilities":
        config["capabilities"]["model-a@nvidia"]["tools"] = False
    elif difference == "provider_path":
        config["providers"]["nvidia"]["base_url"] += "/alternate"
    client = build_routed_client(config, env=env, health_state=state, owner_scope=scope)
    assert client.chat([Message(role="user", content="second")]).content == "reply:model-a"
    assert [call["body"]["model"] for call in fake_http.calls] == ["model-a", "model-b", "model-a"]


def test_shared_health_profile_is_canonical_not_mapping_order(fake_http):
    state = RoutingHealthState(clock=lambda: 100.0)
    scope = object()
    fake_http.outcomes["model-a"] = (429, api_error())
    first = plan()
    build_routed_client(first, env=ENV, health_state=state, owner_scope=scope).chat([Message(role="user", content="one")])
    second = plan()
    second["providers"] = dict(reversed(list(second["providers"].items())))
    second["capabilities"] = dict(reversed(list(second["capabilities"].items())))
    build_routed_client(second, env=ENV, health_state=state, owner_scope=scope).chat([Message(role="user", content="two")])
    assert [call["body"]["model"] for call in fake_http.calls] == ["model-a", "model-b", "model-b"]


def test_credential_rotation_rebuilds_cached_sdk_and_cannot_reuse_old_health(fake_http):
    env = dict(ENV)
    state = RoutingHealthState(clock=lambda: 100.0)
    client = build_routed_client(plan(), env=env, health_state=state, owner_scope=object())
    fake_http.outcomes["model-a"] = (429, api_error())
    client.chat([Message(role="user", content="one")])
    old_sdk = client._clients["model-a@nvidia"].client
    env["NVIDIA_API_KEY"] = "fake-new-nvidia-key"
    fake_http.outcomes.pop("model-a")
    assert client.chat([Message(role="user", content="two")]).content == "reply:model-a"
    assert client._clients["model-a@nvidia"].client is not old_sdk
    assert old_sdk.is_closed()
    assert fake_http.calls[-1]["authorization"] == "Bearer fake-new-nvidia-key"


def test_pinned_and_fallback_views_keep_owner_but_diagnostics_bypass_health(fake_http):
    state = RoutingHealthState(clock=lambda: 100.0)
    scope = object()
    config = plan()
    config["roles"]["main_brain"] = ["model-a@nvidia", "model-b@modelscope", "model-c@zhipu"]
    fake_http.outcomes["model-a"] = (429, api_error())
    fake_http.outcomes["model-b"] = (429, api_error())
    client = build_routed_client(config, env=ENV, health_state=state, owner_scope=scope)
    client.chat([Message(role="user", content="one")])
    view = client.fallback
    assert view.health_state is state and view.owner_scope is scope
    assert view.chat([Message(role="user", content="two")]).content == "reply:model-c"
    pinned = client.pin("model-a@nvidia")
    assert pinned.health_state is state and pinned.owner_scope is scope
    fake_http.outcomes.pop("model-a")
    assert pinned.chat([Message(role="user", content="diagnostic")]).content == "reply:model-a"
    # An explicit diagnostic cannot erase another active route's failure record.
    assert client.chat([Message(role="user", content="three")]).content == "reply:model-c"


def test_health_recovery_is_single_probe_and_does_not_hold_lock_during_http(fake_http):
    now = [100.0]
    state = RoutingHealthState(clock=lambda: now[0])
    scope = object()
    fake_http.outcomes["model-a"] = (429, api_error())
    build_routed_client(plan(), env=ENV, health_state=state, owner_scope=scope).chat([Message(role="user", content="one")])
    now[0] += 30
    entered = threading.Event()
    release = threading.Event()
    def recovery(body):
        entered.set()
        assert release.wait(3)
        return 200, chat_response("model-a")
    fake_http.outcomes["model-a"] = recovery
    first = build_routed_client(plan(), env=ENV, health_state=state, owner_scope=scope)
    second_events = []
    second = build_routed_client(plan(), env=ENV, health_state=state, owner_scope=scope).set_request_observer(second_events.append)
    with ThreadPoolExecutor(max_workers=2) as pool:
        probe = pool.submit(first.chat, [Message(role="user", content="probe")])
        assert entered.wait(3)
        try:
            fallback = pool.submit(second.chat, [Message(role="user", content="parallel")])
            assert fallback.result(2).content == "reply:model-b"
            assert second_events[0]["failure_category"] == "recovery_probe_inflight"
            assert second_events[0]["request_attempted"] is False
        finally:
            release.set()
        assert probe.result(3).content == "reply:model-a"
    fake_http.outcomes.pop("model-a")
    assert second.chat([Message(role="user", content="recovered")]).content == "reply:model-a"
    assert [call["body"]["model"] for call in fake_http.calls].count("model-a") == 3


def test_stale_success_cannot_erase_newer_failure_and_health_cache_is_bounded():
    now = [100.0]
    state = RoutingHealthState(clock=lambda: now[0], max_entries=2)
    key = (object(), "synthetic-model")
    old_success, _ = state.acquire(key)
    failure, _ = state.acquire(key)
    state.finish(failure, success=False, category="transport", cooldown_seconds=30)
    state.finish(old_success, success=True)
    lease, skip = state.acquire(key)
    assert lease is None and skip["category"] == "cooldown"
    for _ in range(4):
        lease, _ = state.acquire((object(), "other"))
        state.finish(lease, success=True)
    assert len(state._records) <= 2


@pytest.mark.parametrize("endpoint", ["chat", "embedding"])
def test_one_router_serializes_sdk_cache_and_observer_per_request(fake_http, endpoint):
    first_entered = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    events = []
    model = "model-a" if endpoint == "chat" else "embed-a"
    def slow(body):
        content = body["messages"][-1]["content"] if endpoint == "chat" else body["input"]
        if content == "A":
            first_entered.set()
            assert release.wait(3)
        else:
            second_entered.set()
        return 200, chat_response(model, content=content) if endpoint == "chat" else embedding_response(model, [[1, 2]])
    fake_http.outcomes[model] = slow
    factory = build_routed_client if endpoint == "chat" else build_embedding_client
    client = factory(plan(), env=ENV).set_request_observer(events.append)
    def invoke(tag):
        if tag == "B":
            second_started.set()
        if endpoint == "chat":
            return client.chat([Message(role="user", content=tag)]).content
        return client.embed(tag)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, "A")
        assert first_entered.wait(3)
        second = pool.submit(invoke, "B")
        assert second_started.wait(3)
        try:
            assert not second_entered.wait(0.1)
        finally:
            release.set()
        first.result(3)
        second.result(3)
    assert second_entered.is_set()
    assert len(fake_http.created) == 1
    assert len(events) == 2 and len({event["request_id"] for event in events}) == 2
    assert all(event["request_attempted"] and event["success"] for event in events)


def test_early_configuration_error_releases_instance_lock_for_other_thread(fake_http):
    config = plan()
    config["roles"]["main_brain"] = ["model-a@nvidia"]
    config["model_options"] = {"model-a@nvidia": {"max_tokens": False}}
    client = build_routed_client(config, env=ENV)
    with pytest.raises(RoutingUnavailable):
        client.chat([Message(role="user", content="invalid")])
    client.model_options.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(client.chat, [Message(role="user", content="valid")]).result(2).content == "reply:model-a"
    assert len(fake_http.calls) == 1


def test_shared_health_metadata_never_exposes_owner_or_credential_tags(fake_http):
    scope = "private-owner-identifier"
    state = RoutingHealthState(clock=lambda: 100.0)
    fake_http.outcomes["model-a"] = (429, api_error())
    events = []
    for _ in range(2):
        build_routed_client(plan(), env=ENV, health_state=state, owner_scope=scope).set_request_observer(events.append).chat(
            [Message(role="user", content="private user text")])
    rendered = json.dumps(events)
    assert scope not in rendered and "private user text" not in rendered
    assert all(credential not in rendered for credential in ENV.values())
    assert all("credential_tag" not in event and "owner_scope" not in event for event in events)


def test_embedding_shared_health_obeys_the_same_owner_and_credential_isolation(fake_http):
    scope = object()
    state = RoutingHealthState(clock=lambda: 100.0)
    fake_http.outcomes["embed-a"] = (503, api_error())
    for _ in range(2):
        client = build_embedding_client(plan(), env=ENV, health_state=state, owner_scope=scope)
        assert len(client.embed("text")[0]) == 3
    assert [call["body"]["model"] for call in fake_http.calls] == ["embed-a", "embed-b", "embed-b"]



@pytest.mark.parametrize("endpoint", ["chat", "embedding"])
def test_interrupted_construction_releases_health_lease_and_instance_lock(fake_http, endpoint):
    state = RoutingHealthState(clock=lambda: 100.0)
    attempts = [0]
    def interrupt_once(**kwargs):
        attempts[0] += 1
        if attempts[0] == 1:
            raise KeyboardInterrupt()
        return llm_module.LLMClient(**kwargs)
    factory = build_routed_client if endpoint == "chat" else build_embedding_client
    client = factory(plan(), env=ENV, health_state=state, owner_scope=object(), client_factory=interrupt_once)
    def invoke():
        return client.chat([Message(role="user", content="hi")]) if endpoint == "chat" else client.embed("text")
    with pytest.raises(KeyboardInterrupt):
        invoke()
    assert all(record["inflight"] == 0 and record["healthy"] is False for record in state._records.values())
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(invoke).result(2)
    assert len(fake_http.calls) == 1



@pytest.mark.parametrize("prompt,total", [(21, 21), (0, 0), (2**63 - 1, 2**63 - 1)])
def test_embedding_usage_records_original_input_and_total_tokens(fake_http, prompt, total):
    payload = embedding_response("embed-a", [[1, 2]])
    payload["usage"] = {"prompt_tokens": prompt, "total_tokens": total,
                        "completion_tokens": "ignored-endpoint-extension", "private_details": "must-not-leak"}
    fake_http.outcomes["embed-a"] = (200, payload)
    events = []
    client = build_embedding_client(plan(), env=ENV).set_request_observer(events.append)
    assert client.embed("text") == [[1.0, 2.0]]
    assert len(events) == 1
    event = events[0]
    assert event["request_attempted"] is True and event["usage_available"] is True
    assert event["prompt_tokens"] == prompt and event["total_tokens"] == total
    assert event["completion_tokens"] == 0
    assert "must-not-leak" not in json.dumps(event)


@pytest.mark.parametrize("usage", [
    None, {}, {"prompt_tokens": 1}, {"total_tokens": 1},
    {"prompt_tokens": True, "total_tokens": 1}, {"prompt_tokens": "1", "total_tokens": 1},
    {"prompt_tokens": 1.0, "total_tokens": 1}, {"prompt_tokens": -1, "total_tokens": 1},
    {"prompt_tokens": 1, "total_tokens": False}, {"prompt_tokens": 1, "total_tokens": "1"},
    {"prompt_tokens": 1, "total_tokens": 2**63}, {"prompt_tokens": float("nan"), "total_tokens": 1},
    {"prompt_tokens": 1, "total_tokens": float("inf")}, [],
])
def test_embedding_missing_or_invalid_usage_is_unknown_without_rejecting_vectors(fake_http, usage):
    payload = embedding_response("embed-a", [[1, 2]])
    payload["usage"] = usage
    fake_http.outcomes["embed-a"] = (200, payload)
    events = []
    assert build_embedding_client(plan(), env=ENV).set_request_observer(events.append).embed("text") == [[1.0, 2.0]]
    assert events[0]["success"] is True and events[0]["usage_available"] is False
    assert not {"prompt_tokens", "completion_tokens", "total_tokens"} & events[0].keys()
    assert len(fake_http.calls) == 1


def test_embedding_protocol_failure_preserves_each_provider_actual_usage(fake_http):
    invalid = embedding_response("embed-a", [[True, 2]])
    invalid["usage"] = {"prompt_tokens": 17, "total_tokens": 17}
    valid = embedding_response("embed-b", [[1, 2, 3]])
    valid["usage"] = {"prompt_tokens": 9, "total_tokens": 9}
    fake_http.outcomes["embed-a"] = (200, invalid)
    fake_http.outcomes["embed-b"] = (200, valid)
    events = []
    assert build_embedding_client(plan(), env=ENV).set_request_observer(events.append).embed("text") == [[1.0, 2.0, 3.0]]
    assert [event["provider"] for event in events] == ["nvidia", "modelscope"]
    assert [event["request_attempted"] for event in events] == [True, True]
    assert [event["success"] for event in events] == [False, True]
    assert [event["usage_available"] for event in events] == [True, True]
    assert [event["prompt_tokens"] for event in events] == [17, 9]
    assert [event["total_tokens"] for event in events] == [17, 9]
    assert [event["completion_tokens"] for event in events] == [0, 0]
    assert events[0]["failure_category"] == "protocol"
    assert len({event["request_id"] for event in events}) == 1


def test_embedding_failed_http_and_skipped_candidates_never_inherit_usage(fake_http):
    state = RoutingHealthState(clock=lambda: 100.0)
    fake_http.outcomes["embed-a"] = (429, api_error())
    payload = embedding_response("embed-b", [[1, 2, 3]])
    payload["usage"] = {"prompt_tokens": 8, "total_tokens": 8}
    fake_http.outcomes["embed-b"] = (200, payload)
    events = []
    client = build_embedding_client(plan(), env=ENV, health_state=state, owner_scope=object()).set_request_observer(events.append)
    client.embed("one")
    client.embed("two")
    assert [event["request_attempted"] for event in events] == [True, True, False, True]
    assert [event.get("usage_available", False) for event in events] == [False, True, False, True]
    assert not {"prompt_tokens", "completion_tokens", "total_tokens"} & events[0].keys()
    assert not {"prompt_tokens", "completion_tokens", "total_tokens"} & events[2].keys()
    assert events[2]["failure_category"] == "cooldown"
    assert len(fake_http.calls) == 3


def test_embedding_sdk_object_response_usage_is_observed_without_chat(fake_http):
    sdk = SimpleNamespace(embeddings=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[1.0, 2.0])],
        usage=SimpleNamespace(prompt_tokens=6, total_tokens=6))))
    def factory(**kwargs):
        return SimpleNamespace(client=sdk)
    events = []
    client = build_embedding_client(plan(), env=ENV, client_factory=factory).set_request_observer(events.append)
    assert client.embed("text") == [[1.0, 2.0]]
    assert events[0]["usage_available"] is True
    assert events[0]["prompt_tokens"] == events[0]["total_tokens"] == 6
    assert events[0]["completion_tokens"] == 0
    assert fake_http.calls == []


@pytest.mark.parametrize("failures,expected", [
    ([], False),
    ([{"category": "transport"}], False),
    ([{"category": "transport", "request_attempted": 1}], False),
    ([{"category": "transport", "request_attempted": True}], True),
    ([{"category": "rate_limit", "request_attempted": True}], True),
    ([{"category": "service_unavailable", "request_attempted": True}], True),
    ([{"category": "cooldown", "request_attempted": False}], True),
    ([{"category": "recovery_probe_inflight", "request_attempted": False}], True),
    ([{"category": "vision_capability_blocked", "request_attempted": False}], False),
    ([{"category": "provider_disabled", "request_attempted": False}], False),
    ([{"category": "cooldown", "request_attempted": True}], False),
    ([{"category": "transport", "request_attempted": False}], False),
    ([{"category": "transport", "request_attempted": True},
      {"category": "tool_capability_blocked", "request_attempted": False}], True),
    ([{"category": "cooldown", "request_attempted": False},
      {"category": "provider_disabled", "request_attempted": False}], True),
])
def test_retryable_routing_error_requires_explicit_transient_evidence(failures, expected):
    records = [{"ref": "model@provider", **record} for record in failures]
    error = RoutingUnavailable(records)
    assert error.retryable is expected
    # The caller's records are not altered by exception construction.
    assert records == [{"ref": "model@provider", **record} for record in failures]


@pytest.mark.parametrize("category", [
    "authentication", "configuration", "missing_key", "protocol", "unexpected",
    "provider_auth_blocked", "unknown",
])
@pytest.mark.parametrize("attempted", [True, False])
def test_permanent_or_unknown_failure_never_becomes_a_retryable_pause(category, attempted):
    error = RoutingUnavailable([
        {"ref": "a@nvidia", "category": "transport", "request_attempted": True},
        {"ref": "b@modelscope", "category": category, "request_attempted": attempted},
    ])
    assert error.retryable is False


def test_all_timeouts_with_capability_skips_pause_without_claiming_skips_were_requests(fake_http):
    config = plan()
    config["options"]["failure_cooldown_seconds"] = 90
    for model in ("vision-b", "agnes-3.0-flash"):
        fake_http.outcomes[model] = httpx.ReadTimeout("private transient outage")
    events = []
    client = build_routed_client(config, env=ENV).set_request_observer(events.append)

    with pytest.raises(RoutingUnavailable) as first:
        client.chat([image_message()], tools=TOOLS)
    assert first.value.retryable is True
    assert [item["request_attempted"] for item in first.value.failures] == [True, True, False, False, False]
    assert len(fake_http.calls) == 2
    assert len([event for event in events if event["request_attempted"]]) == 2
    assert "private transient outage" not in str(first.value)

    events.clear()
    with pytest.raises(RoutingUnavailable) as second:
        client.chat([image_message()], tools=TOOLS)
    assert second.value.retryable is True
    assert [item["category"] for item in second.value.failures][:2] == ["cooldown", "cooldown"]
    assert all(item["request_attempted"] is False for item in second.value.failures)
    assert len(fake_http.calls) == 2
    assert all(event["request_attempted"] is False for event in events)


def test_transient_embedding_outage_uses_the_same_retryable_contract(fake_http):
    for model in ("embed-a", "embed-b"):
        fake_http.outcomes[model] = (503, api_error("unavailable"))
    client = build_embedding_client(plan(), env=ENV)

    with pytest.raises(RoutingUnavailable) as caught:
        client.embed("offline test")
    assert caught.value.retryable is True
    assert all(item["request_attempted"] is True for item in caught.value.failures)


def test_missing_credentials_are_not_mistaken_for_a_transient_api_outage(fake_http):
    with pytest.raises(RoutingUnavailable) as caught:
        build_routed_client(plan(), env={}).chat([Message(role="user", content="offline test")])
    assert caught.value.retryable is False
    assert all(item["request_attempted"] is False for item in caught.value.failures)
    assert fake_http.calls == []



def observation_plan():
    config = plan()
    config["roles"]["vision"] = ["vision-a@nvidia", "vision-b@modelscope", "vision-readonly@zhipu"]
    for ref in config["roles"]["vision"]:
        config["capabilities"][ref] = {"chat": True, "vision": True, "tools": True}
    config["capabilities"]["vision-readonly@zhipu"]["tools"] = False
    config["options"].update(failure_cooldown_seconds=120, agnes_vision_fallback=False)
    return config


def test_result_observer_respects_main_cooldowns_and_reaches_tool_free_vision(fake_http):
    config = observation_plan()
    events = []
    health = RoutingHealthState()
    main = RoutedLLMClient(config, env=ENV, health_state=health)
    main.set_request_observer(events.append)
    for model in ("vision-a", "vision-b"):
        fake_http.outcomes[model] = httpx.ReadTimeout("offline timeout")
    with pytest.raises(RoutingUnavailable):
        main.chat([image_message()], tools=TOOLS)
    assert [call["body"]["model"] for call in fake_http.calls] == ["vision-a", "vision-b"]
    original = deepcopy(main.plan)
    observer = main.result_observer_client()
    assert observer is not main and observer.health_state is health
    assert observer.owner_scope == main.owner_scope
    assert observer.plan == main.plan == original
    assert observer.timeout_seconds == 12
    fake_http.calls.clear()
    events.clear()
    reply = observer.chat([image_message()])
    assert reply.metadata["actual_model"] == "vision-readonly"
    assert len(fake_http.calls) == 1
    body = fake_http.calls[0]["body"]
    assert body["temperature"] == 0 and body["max_tokens"] == 512
    assert "tools" not in body
    assert [event["failure_category"] for event in events[:-1]] == ["cooldown", "cooldown"]
    assert [event["request_attempted"] for event in events] == [False, False, True]
    assert main.model != observer.model
    assert observer._clients["vision-readonly@zhipu"].client.timeout == 12


def test_result_observer_budget_counts_actual_requests_only(fake_http):
    config = observation_plan()
    main = RoutedLLMClient(config, env=ENV)
    observer = main.result_observer_client()
    for model in ("vision-a", "vision-b"):
        fake_http.outcomes[model] = httpx.ReadTimeout("offline timeout")
    with pytest.raises(RoutingUnavailable):
        observer.chat([image_message()])
    assert [call["body"]["model"] for call in fake_http.calls] == ["vision-a", "vision-b"]
    assert main._clients == {}


@pytest.mark.parametrize("messages,kwargs", [
    ([image_message()], {"tools": TOOLS}),
    ([image_message()], {"known_tools": {"desktop_type"}}),
    ([Message(role="tool", name="desktop_snapshot", content="old history")], {}),
    ([Message(role="assistant", tool_calls=[ToolCall(id="old", name="desktop_type")])], {}),
])
def test_result_observer_never_accepts_tools_or_tool_history(fake_http, messages, kwargs):
    observer = RoutedLLMClient(observation_plan(), env=ENV).result_observer_client()
    with pytest.raises(RoutingConfigurationError):
        observer.chat(messages, **kwargs)
    assert fake_http.calls == []


def test_result_observer_does_not_invent_unconfigured_vision_route(fake_http):
    config = plan()
    config["roles"]["vision"] = []
    main = RoutedLLMClient(config, env=ENV)
    assert main.result_observer_client() is None
    assert fake_http.calls == []
