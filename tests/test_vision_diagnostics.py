"""Offline vision diagnostics; every model API is mocked."""
from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from agent.llm import LLMClient
from agent.models import ImagePart, Message
from agent.vision_diagnostics import (
    collect_request_metadata, configured_diagnostic_client, main, parse_diagnosis,
    run_vision_probe, score_bbox, validate_bbox,
)


def make_image(size=(64, 32)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(20, 40, 60)).save(buffer, format="PNG")
    return ImagePart(data=buffer.getvalue(), mime="image/png", mime_type="image/png")


def client(model="main-model", host="main.example"):
    llm = LLMClient(base_url=f"https://{host}/v1", model=model, api_key="fake-test-key")
    llm.client = MagicMock()
    llm.client.chat.completions.create.return_value = response("plain reply")
    return llm


def response(content):
    return SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[])),
    ])


def diagnosis(box=None, confidence=0.9, description="窗口中有一个聊天输入框"):
    if box is None:
        box = {"x1": 0.1, "y1": 0.6, "x2": 0.8, "y2": 0.9}
    return json.dumps({"description": description, "input_box": box,
                       "confidence": confidence}, ensure_ascii=False)


def test_request_metadata_uses_actual_serialized_images_and_no_private_content():
    image = make_image()
    payload = [
        LLMClient._to_openai(Message(role="tool", content="private tool result", images=[image])),
        LLMClient._to_openai(Message(role="user", content="private prompt", images=[image])),
    ]
    data = collect_request_metadata(
        "main-model", "https://user:sk-private@vision.example/v1?api_key=private",
        payload, [{"function": {"description": "private tool schema"}}])
    assert data["base_url_host"] == "vision.example"
    assert data["image_count"] == data["tool_count"] == 1
    assert data["images"][0]["dimensions"] == [64, 32]
    assert data["images"][0]["sha256"] == hashlib.sha256(image.data).hexdigest()
    rendered = json.dumps(data)
    assert "private" not in rendered and image.data_base64 not in rendered
    assert "messages" not in data and "tools" not in data


def test_invalid_inline_image_is_counted_without_leaking_data():
    data = collect_request_metadata("model", "https://example.com", [{
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,@@@@private"}}],
    }])
    assert data["image_count"] == 1
    assert data["images"][0]["dimensions"] is None
    assert data["images"][0]["metadata_error"] == "Error"
    assert "private" not in json.dumps(data)


def test_request_observer_reports_success_and_payload_dimensions():
    llm = client()
    events = []
    llm.set_request_observer(events.append)
    reply = llm.chat([Message(role="user", content="describe", images=[make_image()])],
                     tools=[{"type": "function", "function": {"name": "test"}}])
    assert reply.content == "plain reply"
    assert len(events) == 1
    event = events[0]
    assert event["success"] is True
    assert event["requested_model"] == event["actual_model"] == "main-model"
    assert event["is_fallback"] is False and event["fallback_depth"] == 0
    assert event["image_count"] == event["tool_count"] == 1
    assert event["elapsed_ms"] >= 0
    assert len(event["request_id"]) == 32


def test_fallback_observer_reports_actual_model_and_resets_context():
    primary, backup = client(), client("backup-model", "backup.example")
    backup.supports_images = True
    primary.fallback = backup
    primary.client.chat.completions.create.side_effect = RuntimeError("sk-private")
    events = []
    primary.set_request_observer(events.append)
    primary.chat([Message(role="user", content="test", images=[make_image()])])
    assert len(events) == 2
    assert events[0]["success"] is False and events[0]["error_type"] == "RuntimeError"
    assert events[1]["success"] is True
    assert events[1]["actual_model"] == "backup-model"
    assert events[1]["base_url_host"] == "backup.example"
    assert events[1]["is_fallback"] is True and events[1]["fallback_depth"] == 1
    assert events[0]["request_id"] == events[1]["request_id"]
    assert events[1]["requested_model"] == "main-model"
    assert events[0]["images"] == events[1]["images"]
    backup.chat([Message(role="user", content="standalone backup")])
    assert events[-1]["is_fallback"] is False
    assert events[-1]["requested_model"] == "backup-model"
    assert events[-1]["request_id"] != events[0]["request_id"]


def test_failed_primary_and_backup_do_not_log_exception_secrets(caplog):
    primary, backup = client(), client("backup", "backup.example")
    primary.fallback = backup
    primary.client.chat.completions.create.side_effect = RuntimeError("Bearer private-key")
    backup.client.chat.completions.create.side_effect = ValueError("sk-private-key")
    events = []
    primary.set_request_observer(events.append)
    with pytest.raises(ValueError):
        primary.chat([Message(role="user", content="test")])
    assert [event["success"] for event in events] == [False, False]
    assert [event["error_type"] for event in events] == ["RuntimeError", "ValueError"]
    assert "private-key" not in json.dumps(events) + caplog.text


def test_observer_failure_cannot_change_reply_or_trigger_fallback():
    primary, backup = client(), client("backup")
    primary.fallback = backup
    primary.set_request_observer(lambda _: (_ for _ in ()).throw(RuntimeError("observer failed")))
    assert primary.chat([Message(role="user", content="test")]).content == "plain reply"
    backup.client.chat.completions.create.assert_not_called()


def test_observer_is_inherited_by_fallback_attached_after_setter():
    primary = client()
    events = []
    primary.set_request_observer(events.append)
    backup = client("backup")
    primary.fallback = backup
    primary.client.chat.completions.create.side_effect = RuntimeError("failure")
    primary.chat([Message(role="user", content="test")])
    assert events[-1]["actual_model"] == "backup"
    assert events[-1]["is_fallback"] is True


@pytest.mark.parametrize("raw", [
    '[]', '{}', '{"description":"x","input_box":null,"confidence":1,"extra":1}',
    '{"description":"x","input_box":null,"confidence":true}',
    '{"description":"x","input_box":null,"confidence":NaN}',
    '{"description":"x","input_box":null,"confidence":2}',
    '{"description":"","input_box":null,"confidence":1}',
    '{"description":"x","input_box":{"x1":0,"y1":0,"x2":1},"confidence":1}',
    '{"description":"x","input_box":{"x1":-0.1,"y1":0,"x2":1,"y2":1},"confidence":1}',
    '{"description":"x","input_box":{"x1":0.8,"y1":0,"x2":0.2,"y2":1},"confidence":1}',
    '{"description":"x","input_box":{"x1":0,"y1":0,"x2":1,"y2":Infinity},"confidence":1}',
    '{"description":"x","input_box":{"x1":false,"y1":0,"x2":1,"y2":1},"confidence":1}',
    '{"description":"x","description":"other","input_box":null,"confidence":1}',
    '```json\n{"description":"x","input_box":null,"confidence":1}\n```',
    '{"description":"x","input_box":null,"confidence":1} trailing',
])
def test_diagnosis_rejects_invalid_schema_and_coordinates(raw):
    with pytest.raises((ValueError, TypeError)):
        parse_diagnosis(raw)


def test_diagnosis_accepts_bounded_json_or_explicitly_missing_input():
    parsed = parse_diagnosis(diagnosis())
    assert parsed.input_box == (0.1, 0.6, 0.8, 0.9)
    assert parsed.to_dict()["confidence"] == 0.9
    missing = parse_diagnosis('{"description":"未找到输入框","input_box":null,"confidence":0.1}')
    assert missing.input_box is None


def test_bbox_scores_use_manual_ground_truth():
    exact = score_bbox((0.1, 0.6, 0.8, 0.9), (0.1, 0.6, 0.8, 0.9))
    assert exact == {"iou": 1.0, "center_distance": 0.0, "center_inside": True,
                     "mean_coordinate_error": 0.0}
    disjoint = score_bbox((0, 0, 0.2, 0.2), (0.8, 0.8, 1, 1))
    assert disjoint["iou"] == 0 and disjoint["center_inside"] is False
    assert score_bbox(None, (0.1, 0.1, 0.2, 0.2))["iou"] == 0
    with pytest.raises(ValueError):
        validate_bbox((0, 0, 2, 1))


def test_isolated_probe_has_one_image_no_tools_and_restores_observer():
    llm = client()
    llm.client.chat.completions.create.return_value = response(diagnosis())
    previous = []
    llm.set_request_observer(previous.append)
    saved_observer = llm._request_observer
    report = run_vision_probe(llm, make_image(), (0.1, 0.6, 0.8, 0.9))
    kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert len(kwargs["messages"]) == 2
    assert "tools" not in kwargs and "tool_choice" not in kwargs
    assert report["requests"][0]["image_count"] == 1
    assert report["score"]["iou"] == 1
    assert llm._request_observer == saved_observer and len(previous) == 1


def test_offline_cli_crops_and_scores_without_instantiating_model(tmp_path, monkeypatch, capsys):
    image_path, answer_path, output_path = tmp_path / "source.png", tmp_path / "answer.json", tmp_path / "report.json"
    Image.new("RGB", (100, 80)).save(image_path)
    answer_path.write_text(diagnosis(), encoding="utf-8")
    monkeypatch.setattr(
        "agent.vision_diagnostics.configured_diagnostic_client",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("offline must not load credentials")))
    result = main([str(image_path), "--crop", "20", "30", "60", "70",
                   "--response-json", str(answer_path), "--ground-truth",
                   "0.1", "0.6", "0.8", "0.9", "--output", str(output_path)])
    assert result == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["mode"] == "offline"
    assert report["image"]["dimensions"] == [40, 40]
    assert report["preparation"]["source_dimensions"] == [100, 80]
    assert report["score"]["iou"] == 1
    assert "base64" not in capsys.readouterr().out


def test_configured_primary_and_fallback_diagnostics_cannot_silently_switch(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("llm: {}\n", encoding="utf-8")
    original = config.read_bytes()

    def configured(*args, **kwargs):
        primary = client()
        primary.fallback = client("backup", "backup.example")
        return primary

    monkeypatch.setattr(LLMClient, "for_role", configured)
    primary = configured_diagnostic_client(config, "main", "primary")
    backup = configured_diagnostic_client(config, "main", "fallback")
    assert primary.model == "main-model" and primary.fallback is None
    assert backup.model == "backup" and backup.fallback is None
    assert config.read_bytes() == original


def test_cli_errors_never_print_private_exception_text(tmp_path, monkeypatch, capsys):
    screenshot = tmp_path / "window.png"
    Image.new("RGB", (10, 10)).save(screenshot)
    monkeypatch.setattr(
        "agent.vision_diagnostics.configured_diagnostic_client",
        lambda *args: (_ for _ in ()).throw(RuntimeError("sk-private-key")))
    assert main([str(screenshot), "--run-model"]) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["diagnostic_error"] == "RuntimeError"
    assert "private" not in output


def test_tracer_observer_persists_defined_schema_only(tmp_path):
    from agent.tracing import Tracer, make_llm_request_observer, new_trace

    tracer = Tracer(str(tmp_path / "audit.db"))
    new_trace()
    llm = client()
    llm.set_request_observer(make_llm_request_observer(tracer, channel="cli"))
    llm.chat([Message(role="user", content="private prompt", images=[make_image()])])
    raw = tracer.db.execute("SELECT data FROM events WHERE event='llm_request'").fetchone()[0]
    data = json.loads(raw)
    assert data["actual_model"] == "main-model"
    assert data["base_url_host"] == "main.example"
    assert data["image_count"] == 1 and data["tool_count"] == 0
    assert data["success"] is True and data["is_fallback"] is False
    assert data["channel"] == "cli" and data["elapsed_ms"] >= 0
    assert data["images"][0]["dimensions"] == [64, 32]
    assert len(data["images"][0]["sha256"]) == 64
    assert "private" not in raw and "base64" not in raw
    tracer.close()


def test_tracer_observer_drops_unrecognized_payload_and_image_fields(tmp_path):
    from agent.tracing import Tracer, make_llm_request_observer

    tracer = Tracer(str(tmp_path / "audit.db"))
    observer = make_llm_request_observer(tracer)
    observer({
        "model": "model", "image_count": 1, "success": False,
        "error_type": "RuntimeError", "api_key": "private-key",
        "messages": [{"content": "private prompt"}], "elapsed_ms": float("nan"),
        "images": [{"dimensions": [64, 32], "sha256": "a" * 64,
                    "data_base64": "private-pixels", "url": "https://private.example"}],
    })
    raw = tracer.db.execute("SELECT data FROM events").fetchone()[0]
    data = json.loads(raw)
    assert "private" not in raw and "elapsed_ms" not in data
    assert data["images"] == [{"dimensions": [64, 32], "sha256": "a" * 64}]
    tracer.close()

def api_error(message, status=400, code="invalid_request_error"):
    import httpx
    from openai import BadRequestError, InternalServerError

    request = httpx.Request("POST", "https://gateway.example/v1?api_key=private-key")
    body = {"error": {"code": code, "message": message}}
    error_class = BadRequestError if status < 500 else InternalServerError
    return error_class(message, response=httpx.Response(status, request=request), body=body)


def test_failed_probe_retains_requests_and_selected_model_metadata():
    llm = client("text-model", "gateway.example")
    image = make_image()
    llm.client.chat.completions.create.side_effect = api_error(
        "This model does not support image input. Bearer private-key")
    report = run_vision_probe(llm, image)
    assert report["success"] is False and report["diagnostic_error"] == "BadRequestError"
    assert len(report["requests"]) == 1 and report["requests"][0]["success"] is False
    assert report["request_summary"]["actual_model"] == "text-model"
    assert report["request_summary"]["base_url_host"] == "gateway.example"
    assert report["request_summary"]["images"][0]["sha256"] == hashlib.sha256(image.data).hexdigest()
    assert report["error"]["status_code"] == 400
    assert report["error"]["code"] == "invalid_request_error"
    assert report["error"]["category"] == "vision_unsupported"
    assert "private-key" not in json.dumps(report)
    assert image.data_base64 not in json.dumps(report)


def test_probe_disables_fallback_even_for_direct_call_and_restores_clients():
    primary, backup = client(), client("backup")
    primary.fallback = backup
    primary.client.chat.completions.create.side_effect = api_error("Bad request")
    old_observer = lambda metadata: None
    primary.set_request_observer(old_observer)
    report = run_vision_probe(primary, make_image())
    assert report["success"] is False
    assert len(report["requests"]) == 1
    backup.client.chat.completions.create.assert_not_called()
    assert primary.fallback is backup and primary._request_observer is old_observer
    assert backup._request_observer is old_observer


@pytest.mark.parametrize("message,status,category", [
    ("This model does not support image input", 400, "vision_unsupported"),
    ("Image input is unsupported by this model", 400, "vision_unsupported"),
    ("Invalid image_url format", 400, "request_format_error"),
    ("messages content expected string but found array", 400, "request_format_error"),
    ("upstream gateway failure", 503, "gateway_error"),
    ("access denied", 403, "gateway_error"),
    ("bad request without a documented reason", 400, "unclassified_bad_request"),
])
def test_safe_error_classifies_only_supported_evidence(message, status, category):
    from agent.vision_diagnostics import safe_diagnostic_error

    data = safe_diagnostic_error(api_error(message + " sk-private-key", status))
    assert data["category"] == category
    assert data["evidence"]
    assert "private" not in json.dumps(data)


def test_safe_error_rejects_secret_like_provider_code():
    from agent.vision_diagnostics import safe_diagnostic_error

    data = safe_diagnostic_error(api_error("bad request", code="sk-private-key"))
    assert data["code"] is None
    assert "private" not in json.dumps(data)


def test_schema_rejection_preserves_successful_request_audit():
    llm = client()
    llm.client.chat.completions.create.return_value = response("not strict diagnostic JSON")
    report = run_vision_probe(llm, make_image())
    assert report["success"] is False
    assert report["requests"][0]["success"] is True
    assert report["error"]["category"] == "response_schema_error"
    assert report["error"]["stage"] == "response"


def test_cli_failure_writes_safe_report_and_uses_one_bounded_request(tmp_path, monkeypatch, capsys):
    screenshot, output_path = tmp_path / "window.png", tmp_path / "failed.json"
    image = make_image()
    screenshot.write_bytes(image.data)
    llm = client("configured-fallback", "fallback.example")
    llm.client.with_options.return_value = llm.client
    llm.client.chat.completions.create.side_effect = api_error(
        "This model does not support image input; private-key")
    monkeypatch.setattr(
        "agent.vision_diagnostics.configured_diagnostic_client", lambda *args: llm)
    result = main([str(screenshot), "--run-model", "--target", "fallback",
                   "--timeout-seconds", "60", "--output", str(output_path)])
    assert result == 2
    llm.client.with_options.assert_called_once_with(timeout=60, max_retries=0)
    llm.client.chat.completions.create.assert_called_once()
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["target"] == "fallback" and report["success"] is False
    assert report["request_summary"]["actual_model"] == "configured-fallback"
    assert len(report["requests"]) == 1
    assert report["image"]["sha256"] == report["requests"][0]["images"][0]["sha256"]
    assert report["error"]["category"] == "vision_unsupported"
    assert "private-key" not in capsys.readouterr().out + output_path.read_text(encoding="utf-8")

def test_explicit_diagnostic_can_probe_selected_text_fallback_without_changing_capability():
    llm = client("selected-text-fallback", "fallback.example")
    llm.supports_images = False
    llm.client.chat.completions.create.side_effect = api_error("bad request")
    report = run_vision_probe(llm, make_image())
    llm.client.chat.completions.create.assert_called_once()
    assert report["diagnostic_error"] == "BadRequestError"
    assert report["request_summary"]["actual_model"] == "selected-text-fallback"
    assert llm.supports_images is False
