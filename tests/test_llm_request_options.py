"""Request extensions use real SDK serialization with an offline transport."""
import json
from unittest.mock import MagicMock

import httpx
from openai import OpenAI
import pytest

from agent.llm import LLMClient
from agent.models import Message


RESERVED = ["model", "messages", "tools", "tool_choice", "api_key", "base_url",
            "extra_headers", "Authorization", "provider", "stream"]


def make_client(options=None):
    return LLMClient(base_url="https://offline.example/v1", model="intended-model",
                     api_key="synthetic-only", request_options=options)


@pytest.mark.parametrize("field", RESERVED)
def test_reserved_extra_body_fields_rejected_at_construction(field):
    with pytest.raises(ValueError, match="cannot override") as error:
        make_client({"extra_body": {"nested": [{field: "private-marker"}]}})
    assert "private-marker" not in str(error.value)


@pytest.mark.parametrize("field", RESERVED)
def test_mutated_extensions_rejected_before_sdk_or_fallback(field):
    client = make_client({"extra_body": {"thinking": {"type": "disabled"}}})
    client.client = MagicMock()
    client.fallback = MagicMock()
    client.request_options["extra_body"]["thinking"][field] = "private-marker"
    with pytest.raises(ValueError, match="cannot override"):
        client.chat([Message(role="user", content="intended-content")])
    client.client.chat.completions.create.assert_not_called()
    client.fallback.chat.assert_not_called()


def test_constructor_detaches_caller_owned_nested_options():
    options = {"extra_body": {"thinking": {"type": "disabled"}}}
    client = make_client(options)
    options["extra_body"]["thinking"]["model"] = "injected-model"
    assert client.request_options == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_allowed_extensions_preserve_real_payload_and_observer_identity():
    sent, observed = [], []

    def transport(request):
        sent.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer synthetic-only"
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": "intended-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "offline"},
                "finish_reason": "stop"}],
        })

    client = make_client({"extra_body": {"thinking": {"type": "disabled"}, "seed": 42},
                          "seed": 12, "top_p": 0.9})
    client.client = OpenAI(api_key="synthetic-only", base_url=client.base_url,
                          http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    client.set_request_observer(observed.append)
    try:
        reply = client.chat([Message(role="user", content="intended-content")])
    finally:
        client.client.close()
    assert reply.content == "offline"
    assert sent[0]["model"] == "intended-model"
    assert sent[0]["messages"] == [{"role": "user", "content": "intended-content"}]
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert sent[0]["seed"] == 42 and sent[0]["top_p"] == 0.9
    assert observed[0]["actual_model"] == sent[0]["model"]
    assert observed[0]["success"] is True


@pytest.mark.parametrize("extra", [[], {"value": float("nan")}, {"value": object()}])
def test_non_json_extension_values_rejected(extra):
    with pytest.raises(ValueError):
        make_client({"extra_body": extra})


def test_cyclic_extension_values_rejected():
    cyclic = {}
    cyclic["nested"] = cyclic
    with pytest.raises(ValueError, match="acyclic"):
        make_client({"extra_body": cyclic})
