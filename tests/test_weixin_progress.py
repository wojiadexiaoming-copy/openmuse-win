"""Offline Weixin stage receipts: scoped, once per turn, before the final reply."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agent.models import Message
from agent.session_store import SessionStore
from channels.weixin import WeixinChannel, WeixinClient


RECEIPT = "已提交给目标应用，正在确认生成结果。"
FINAL = "提交已发送；结果仍待确认，当前进度已保存。"


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(WeixinChannel, "load_credentials", lambda self: None)
    monkeypatch.setattr("channels.weixin.threading.Thread", ImmediateThread)


def make_channel(tmp_path, *, store=None, sid="wx-session", callback=None):
    ui_events = []
    logs = []
    previous_event = callback or (lambda name, data: ui_events.append((name, data)))
    previous_history = lambda history: None
    orch = SimpleNamespace(
        history=[], session_id="ui-session", on_event=previous_event,
        on_history=previous_history, last_turn_tool_records=[],
        last_turn_status="running", llm=SimpleNamespace(model="offline-model"),
    )
    channel = WeixinChannel(
        tmp_path / "channel", orch=orch, session_store=store,
        tracer=SimpleNamespace(log_event=lambda name, data: logs.append((name, data))),
    )
    channel.weixin_session_id = sid
    channel.client = MagicMock()
    channel.client.send_message.return_value = {"ret": 0}
    return channel, orch, ui_events, logs, previous_event, previous_history


def progress(orch, **overrides):
    user = orch.history[-1]
    data = {
        "stage": "submitted", "text": "private request must not be forwarded",
        "session_id": orch.session_id,
        "task_origin_id": user.metadata.get("task_origin_id") or user.id,
    }
    data.update(overrides)
    return data


def finish(orch, text=FINAL, status="incomplete"):
    orch.last_turn_status = status
    orch.history[-1].metadata["task_status"] = status
    orch.history.append(Message(role="assistant", content=text, metadata={"task_status": status}))
    return text


def dispatch(channel, text="private request"):
    channel._dispatch_agent_task(
        text, "offline-token", "user", "bot", "https://example.invalid", "offline-context")


def test_submitted_is_scoped_deduplicated_and_delivered_before_final(tmp_path):
    channel, orch, ui_events, logs, original_event, original_history = make_channel(tmp_path)
    captured = []

    def handle(text):
        assert orch.session_id == "wx-session"
        captured.append(orch.on_event)
        for overrides in (
            {"session_id": "other-session"}, {"task_origin_id": "old-task"},
            {"stage": "result_pending"}, {"stage": "result_verified"}, {}, {},
        ):
            orch.on_event("task_progress", progress(orch, **overrides))
        orch.on_event("agent_state", {"state": "running"})
        assert channel.client.send_message.call_count == 1
        return finish(orch)

    orch.handle = handle
    dispatch(channel)
    calls = channel.client.send_message.call_args_list
    assert [call.kwargs["text"] for call in calls] == [RECEIPT, FINAL]
    assert calls[0].kwargs["timeout_sec"] == 3.0
    assert "timeout_sec" not in calls[1].kwargs
    assert all(call.kwargs["to_user_id"] == "user" for call in calls)
    assert len(ui_events) == 7
    assert logs == []
    assert orch.on_event is original_event
    assert orch.on_history is original_history
    assert orch.session_id == "ui-session"
    # A retained callback from an old turn cannot enqueue a receipt after its final.
    captured[0]("task_progress", {
        "stage": "submitted", "session_id": "wx-session",
        "task_origin_id": orch.history[0].id,
    })
    assert channel.client.send_message.call_count == 2


def test_resume_uses_origin_assigned_by_handle_and_preserves_session_history(tmp_path):
    store = SessionStore(str(tmp_path / "conversations.db"))
    try:
        sid = store.new_session("paused task")
        origin = Message(role="user", content="original goal", metadata={"task_status": "incomplete"})
        store.save_history(sid, [origin])
        channel, orch, _, _, original_event, _ = make_channel(tmp_path, store=store, sid=sid)

        def handle(text):
            assert text == "继续"
            user = orch.history[-1]
            user.metadata["task_origin_id"] = origin.id
            orch.on_event("task_progress", progress(orch, task_origin_id=user.id))
            assert channel.client.send_message.call_count == 0
            orch.on_event("task_progress", progress(orch))
            return finish(orch)

        orch.handle = handle
        dispatch(channel, "继续")
        assert [call.kwargs["text"] for call in channel.client.send_message.call_args_list] == [RECEIPT, FINAL]
        saved = store.load_history(sid)
        assert [message.content for message in saved] == ["original goal", "继续", FINAL]
        assert saved[1].metadata["task_origin_id"] == origin.id
        assert saved[2].metadata["task_status"] == "incomplete"
        assert orch.on_event is original_event and orch.session_id == "ui-session"
    finally:
        store.db.close()


@pytest.mark.parametrize("failure", ["timeout", "rejected"])
def test_progress_failure_does_not_change_task_or_block_final(tmp_path, caplog, failure):
    channel, orch, _, logs, original_event, original_history = make_channel(tmp_path)

    def send(**kwargs):
        if kwargs["text"] == RECEIPT:
            if failure == "timeout":
                raise httpx.ReadTimeout("private token must not enter logs")
            return {"ret": -1, "private": "private response must not enter logs"}
        return {"ret": 0}

    channel.client.send_message.side_effect = send

    def handle(text):
        event = progress(orch)
        orch.on_event("task_progress", event)
        orch.on_event("task_progress", event)
        return finish(orch)

    orch.handle = handle
    dispatch(channel)
    assert [call.kwargs["text"] for call in channel.client.send_message.call_args_list] == [RECEIPT, FINAL]
    assert orch.last_turn_status == "incomplete"
    assert orch.history[-1].content == FINAL
    assert [name for name, data in logs] == ["weixin_progress_delivery_error"]
    assert logs[0][1] == {
        "error_type": "ReadTimeout" if failure == "timeout" else "RuntimeError",
        "channel": "weixin", "session_id": "wx-session",
    }
    assert "private" not in caplog.text
    assert orch.on_event is original_event and orch.on_history is original_history
    assert orch.session_id == "ui-session"


def test_callback_restored_and_receipts_closed_when_task_raises(tmp_path):
    channel, orch, _, logs, original_event, original_history = make_channel(tmp_path)
    captured = []

    def handle(text):
        captured.append(orch.on_event)
        orch.on_event("task_progress", progress(orch))
        raise ValueError("private error detail")

    orch.handle = handle
    dispatch(channel)
    calls = channel.client.send_message.call_args_list
    assert len(calls) == 2 and calls[0].kwargs["text"] == RECEIPT
    assert "ValueError" in calls[1].kwargs["text"] and "未完成" in calls[1].kwargs["text"]
    assert orch.last_turn_status == "error"
    assert orch.on_event is original_event and orch.on_history is original_history
    assert orch.session_id == "ui-session"
    assert [name for name, data in logs] == ["weixin_task_error"]
    captured[0]("task_progress", {
        "stage": "submitted", "session_id": "wx-session", "task_origin_id": orch.history[0].id,
    })
    assert channel.client.send_message.call_count == 2


@pytest.mark.parametrize("stage", ["result_verified", "result_pending"])
def test_other_stages_only_send_authoritative_final(tmp_path, stage):
    channel, orch, ui_events, _, _, _ = make_channel(tmp_path)

    def handle(text):
        orch.on_event("task_progress", progress(orch, stage=stage))
        return finish(orch, text="准确的任务结果正文", status="done" if stage == "result_verified" else "incomplete")

    orch.handle = handle
    dispatch(channel)
    channel.client.send_message.assert_called_once()
    assert channel.client.send_message.call_args.kwargs["text"] == "准确的任务结果正文"
    assert ui_events[0][0] == "task_progress"


def test_receipt_deduplication_is_turn_local_and_ui_failure_is_optional(tmp_path):
    ui_events = []

    def broken_ui(name, data):
        ui_events.append((name, data))
        raise RuntimeError("UI unavailable")

    channel, orch, _, logs, original_event, _ = make_channel(tmp_path, callback=broken_ui)

    def handle(text):
        orch.on_event("task_progress", progress(orch))
        orch.on_event("task_progress", progress(orch))
        return finish(orch)

    orch.handle = handle
    dispatch(channel, "first task")
    dispatch(channel, "second task")
    assert [call.kwargs["text"] for call in channel.client.send_message.call_args_list] == [RECEIPT, FINAL, RECEIPT, FINAL]
    assert len(ui_events) == 4 and logs == []
    assert orch.on_event is original_event


@pytest.mark.parametrize("timeout", [None, 3.0])
def test_client_uses_selected_io_timeout_without_real_network(timeout):
    client = WeixinClient()
    with patch("channels.weixin.httpx.Client") as http:
        response = http.return_value.__enter__.return_value.post.return_value
        response.json.return_value = {"ret": 0}
        kwargs = {} if timeout is None else {"timeout_sec": timeout}
        client.send_message(token="offline", to_user_id="user", text=RECEIPT, **kwargs)
        http.assert_called_once_with(timeout=15.0 if timeout is None else 3.0)
        body = http.return_value.__enter__.return_value.post.call_args.kwargs["json"]
        assert body["msg"]["item_list"] == [{"type": 1, "text_item": {"text": RECEIPT}}]
