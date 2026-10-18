"""Count serialized request text without retaining prompts, pixels or credentials."""
import base64
from copy import deepcopy
import json

import pytest

from agent.models import ImagePart, Message
from agent.llm import LLMClient
from agent.tracing import Tracer, make_llm_request_observer
from agent.vision_diagnostics import collect_request_metadata


def test_counts_content_strings_and_multimodal_text_only():
    system = "private system text"
    tool_text = "private tool result"
    first = "private user text"
    second = "\u4e2d\u6587\U0001f642"
    payload = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": first},
            {"type": "image_url", "image_url": {"url": "https://private.invalid/pixels"}},
            {"type": "text", "text": second},
            {"type": "text", "text": None},
            {"type": "other", "text": "private unsupported part"},
            None,
        ]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "function": {"arguments": "private tool arguments must not count"},
        }]},
        {"role": "tool", "content": tool_text},
        {"role": "assistant", "content": ""},
    ]
    before = deepcopy(payload)
    data = collect_request_metadata("model", "https://gateway.invalid/v1", payload)
    assert data["message_count"] == 5
    assert data["text_characters"] == len(system) + len(tool_text) + len(first) + len(second)
    assert data["image_count"] == 1
    assert payload == before
    assert "private" not in json.dumps(data)


def test_empty_request_has_explicit_zero_counts():
    data = collect_request_metadata("model", "https://gateway.invalid/v1", [])
    assert data["message_count"] == data["text_characters"] == 0


@pytest.mark.parametrize("pixel_bytes", [b"pixels", b"pixels" * 20000], ids=["small", "large"])
def test_inline_image_size_never_contributes_to_text_count(pixel_bytes):
    encoded = base64.b64encode(pixel_bytes).decode("ascii")
    payload = [{"role": "user", "content": [
        {"type": "text", "text": "text only"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
    ]}]
    data = collect_request_metadata("model", "https://gateway.invalid/v1", payload)
    assert data["message_count"] == 1 and data["text_characters"] == len("text only")
    assert data["image_count"] == 1
    assert encoded not in json.dumps(data)


def test_exact_llm_serialization_counts_reach_audit_without_payload(tmp_path):
    source_text = "private prompt and synthetic credential marker"
    image = ImagePart(data=b"private-pixel-bytes")
    payload = [LLMClient._to_openai(Message(role="user", content=source_text, images=[image])),
               LLMClient._to_openai(Message(role="tool", content="private tool result", images=[image]))]
    metadata = collect_request_metadata(
        "model", "https://user:private-credential@gateway.invalid/v1?token=private-token", payload,
        [{"function": {"description": "private tool description"}}],
    )
    tracer = Tracer(str(tmp_path / "audit.db"))
    observer = make_llm_request_observer(tracer)
    observer({**metadata, "messages": payload, "api_key": "private-credential",
              "text": source_text, "data_base64": image.data_base64})
    raw = tracer.db.execute("SELECT data FROM events WHERE event='llm_request'").fetchone()[0]
    audit = json.loads(raw)
    assert audit["message_count"] == 2
    assert audit["text_characters"] == len(source_text) + len("private tool result")
    assert audit["image_count"] == 1  # Tool images are not sent by LLMClient._to_openai.
    assert audit["base_url_host"] == "gateway.invalid"
    assert isinstance(audit["message_count"], int) and isinstance(audit["text_characters"], int)
    assert "private" not in raw and image.data_base64 not in raw
    assert not {"messages", "text", "api_key", "data_base64"} & audit.keys()
    tracer.close()


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "2", None, 2**63])
def test_size_audit_accepts_only_safe_nonnegative_integer_counts(tmp_path, invalid):
    tracer = Tracer(str(tmp_path / "audit.db"))
    tracer.log_event("llm_request", {"message_count": invalid, "text_characters": invalid})
    audit = json.loads(tracer.db.execute("SELECT data FROM events").fetchone()[0])
    assert "message_count" not in audit and "text_characters" not in audit
    tracer.close()
