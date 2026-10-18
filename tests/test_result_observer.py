"""Fresh single-snapshot result observation, using injected replies and mock HTTP."""
import io
import json
from types import SimpleNamespace

from PIL import Image

import pytest

from agent.models import ImagePart, Message, ToolCall
from agent.result_observer import VisualResultObserver, build_result_observer


def snapshot():
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(buffer, format="PNG")
    return Message(role="tool", name="desktop_snapshot", created_at=100.0,
        content="[桌面截图] private tool text and window details",
        images=[ImagePart(data=buffer.getvalue(), mime="image/png", mime_type="image/png")],
        metadata={"task_origin_id": "task-A", "desktop_observation": {
            "snapshot_id": "desk-current", "image_size": [12, 8],
            "window": {"hwnd": 7, "foreground": True, "title": "private title"}}})


class FakeVision:
    supports_images = True
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
    def chat(self, messages):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def observe(reply, evidence=None, goal="Generate one landscape image"):
    client = FakeVision(reply)
    result = VisualResultObserver(client, clock=lambda: 100.0).observe(goal, evidence or snapshot(), "task-A")
    return result, client


def reply(status="completed", goal_match=True, result_count=1):
    return Message(role="assistant", content=json.dumps({
        "status": status, "goal_match": goal_match, "result_count": result_count}))


@pytest.mark.parametrize("status,match,count", [
    ("completed", True, 1), ("generating", True, 0), ("error", True, 0), ("unknown", False, 0)])
def test_valid_observation_binds_task_and_current_snapshot(status, match, count):
    value, client = observe(reply(status, match, count))
    assert value == {"status": status, "goal_match": match, "result_count": count,
                     "task_origin_id": "task-A", "snapshot_id": "desk-current", "source": "visual_observer"}
    assert len(client.calls) == 1


def test_payload_has_only_original_goal_and_one_canonical_image():
    evidence = snapshot()
    value, client = observe(reply(), evidence, "原始生成任务，不是内部阶段提示")
    messages = client.calls[0]
    assert [item.role for item in messages] == ["system", "user"]
    assert json.loads(messages[1].content) == {"task_goal": "原始生成任务，不是内部阶段提示"}
    assert len(messages[1].images) == 1 and messages[1].images[0].data == evidence.images[0].data
    assert messages[1].images[0] is not evidence.images[0]
    assert all(not item.tool_calls for item in messages)
    serialized = " ".join(item.content for item in messages)
    assert "private tool" not in serialized and "private title" not in serialized
    assert "task-A" not in serialized and "desk-current" not in serialized
    for rule in ("完整", "本次任务", "原始目标一致", "历史结果", "侧栏缩略图", "草稿", "提交动作", "加载", "unknown"):
        assert rule in messages[0].content
    assert value["status"] == "completed"


@pytest.mark.parametrize("content", [
    "not JSON", '```json\n{"status":"completed","goal_match":true,"result_count":1}\n```',
    '[]', '{"status":"completed","goal_match":true,"result_count":1,"extra":"private"}',
    '{"status":"completed","status":"unknown","goal_match":true,"result_count":1}',
    '{"status":"completed","goal_match":"true","result_count":1}',
    '{"status":"completed","goal_match":true,"result_count":true}',
    '{"status":"completed","goal_match":true,"result_count":1.0}',
    '{"status":"completed","goal_match":false,"result_count":1}',
    '{"status":"completed","goal_match":true,"result_count":0}',
    '{"status":"completed","goal_match":true,"result_count":-1}',
    '{"status":"completed","goal_match":true,"result_count":1001}',
    '{"status":"completed","goal_match":true,"result_count":NaN}',
    '{"status":"Completed","goal_match":true,"result_count":1}',
    '{"status":"generating","goal_match":false,"result_count":0}',
    '{"status":"unknown","goal_match":false,"result_count":1}',
])
def test_malformed_or_mismatched_reply_is_unknown(content):
    value, client = observe(Message(role="assistant", content=content))
    assert value["status"] == "unknown" and value["goal_match"] is False and value["result_count"] == 0
    assert len(client.calls) == 1


@pytest.mark.parametrize("mutation", [
    lambda e: setattr(e, "role", "user"),
    lambda e: setattr(e, "name", "desktop_type"),
    lambda e: setattr(e, "content", "[截图失败]"),
    lambda e: setattr(e, "created_at", 60.0),
    lambda e: setattr(e, "created_at", 103.0),
    lambda e: e.metadata.update(task_origin_id="task-B"),
    lambda e: e.metadata.pop("task_origin_id"),
    lambda e: e.metadata["desktop_observation"].update(snapshot_id=""),
    lambda e: e.metadata["desktop_observation"].update(snapshot_id=None),
    lambda e: e.metadata["desktop_observation"].update(task_origin_id="task-B"),
    lambda e: e.metadata["desktop_observation"]["window"].update(foreground=False),
    lambda e: e.metadata["desktop_observation"]["window"].update(hwnd=True),
    lambda e: e.metadata["desktop_observation"]["window"].update(hwnd=0),
    lambda e: e.metadata["desktop_observation"].update(image_size=[13, 8]),
    lambda e: e.metadata["desktop_observation"].update(image_size=[True, 8]),
    lambda e: e.images.clear(),
    lambda e: e.images.append(e.images[0]),
    lambda e: setattr(e.images[0], "data_base64", "not-base64"),
    lambda e: setattr(e.images[0], "data", b"conflicting-image"),
    lambda e: setattr(e, "tool_calls", [ToolCall(id="bad", name="file_read")]),
])
def test_foreign_stale_or_untrusted_evidence_never_reaches_model(mutation):
    evidence = snapshot()
    mutation(evidence)
    client = FakeVision(reply())
    value = VisualResultObserver(client, clock=lambda: 100.0).observe("one image", evidence, "task-A")
    assert value is None and client.calls == []


@pytest.mark.parametrize("value", [
    Message(role="assistant", tool_calls=[ToolCall(id="bad", name="desktop_type")]),
    Message(role="user", content='{"status":"completed","goal_match":true,"result_count":1}'),
    Message(role="assistant", content="x" * 4097),
])
def test_model_tool_calls_or_nonassistant_answers_cannot_confirm_results(value):
    result, client = observe(value)
    assert result["status"] == "unknown" and len(client.calls) == 1


def test_model_exception_keeps_unavailable_distinct_from_uncertain_screenshot():
    value, client = observe(RuntimeError("private error must stay out of result"))
    assert value is None and len(client.calls) == 1


def test_factory_is_explicit_and_conservative():
    assert build_result_observer(object()) is None
    assert build_result_observer(SimpleNamespace(result_observer_client=lambda: None)) is None
    assert build_result_observer(SimpleNamespace(result_observer_client=lambda: SimpleNamespace(supports_images=False))) is None
    client = SimpleNamespace(supports_images=True)
    built = build_result_observer(SimpleNamespace(result_observer_client=lambda: client))
    assert isinstance(built, VisualResultObserver) and built.llm is client
