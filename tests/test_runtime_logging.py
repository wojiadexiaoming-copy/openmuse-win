"""Runtime diagnostics: temporary databases and mocks only."""
from __future__ import annotations

import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bottle import Bottle

from agent.models import Message, ToolCall
from agent.session_store import SessionStore
from agent.tracing import Tracer, new_trace, trace_id_var
from channels.weixin import WeixinChannel
from ui.server import AgentServer, EventBus


def _call_api(app, method, path, body=None):
    raw = json.dumps(body or {}).encode("utf-8")
    status = []
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path,
        "wsgi.input": io.BytesIO(raw), "wsgi.errors": io.StringIO(),
        "CONTENT_LENGTH": str(len(raw)), "CONTENT_TYPE": "application/json",
        "HTTP_AUTHORIZATION": "Bearer test-runtime-token",
    }
    data = b"".join(app(environ, lambda value, headers, exc_info=None: status.append(value)))
    return status[0], json.loads(data.decode("utf-8"))


class _ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def runtime_server(tmp_path, monkeypatch):
    """Bypass production initialization: no real config, vault, scheduler or tools."""
    server = AgentServer.__new__(AgentServer)
    server.app = Bottle()
    server.api_token = "test-runtime-token"
    server.llm = SimpleNamespace(model="test-model")
    server.orch = SimpleNamespace(history=[], last_turn_tool_records=[], last_turn_status="running")
    server.memory = SimpleNamespace(maybe_remember=lambda value: None)
    server.vault = SimpleNamespace(scrub=lambda value: value)
    server.store = SessionStore(str(tmp_path / "conversations.db"))
    server.tracer = Tracer(str(tmp_path / "spans.db"))
    server.event_bus = EventBus()
    server.state_snapshot = {
        "agent_state": {"state": "idle"}, "plan": [], "activity": [], "approvals": [],
        "status": {"current_model": "test-model", "token_usage": {}, "memory_writes": []},
    }
    server._setup_routes()
    monkeypatch.setattr("ui.server.run_memory_distill", lambda **kwargs: {})
    monkeypatch.setattr("ui.server.threading.Thread", _ImmediateThread)
    yield server
    server.tracer.close()
    server.store.db.close()


def _append_failed_tool(orch):
    orch.history.extend([
        Message(role="assistant", tool_calls=[
            ToolCall(id="tc-failed", name="desktop_press", arguments={"key": "enter"}),
        ]),
        Message(role="tool", name="desktop_press", tool_call_id="tc-failed",
                content="[工具执行失败] 已保存的中间步骤"),
    ])


def test_tracer_concurrent_spans_events_and_export_are_complete(tmp_path):
    tracer = Tracer(str(tmp_path / "trace.db"))
    tid = new_trace()

    def write(index):
        trace_id_var.set(tid)
        span = tracer.start("tool", "desktop_press")
        log = tracer.log if index % 2 else tracer.log_event
        log("runtime_error", {
            "error_type": "RuntimeError", "status": "error", "channel": "ui",
            "input_chars": index, "args": {"key": "private-key"},
            "api_key": "sk-private", "error": "Bearer private-secret",
        })
        tracer.finish(span, status="error", tokens_in=index, tokens_out=1)
        if index % 10 == 0:
            tracer.export_trace(tid, str(tmp_path / f"partial-{index}.zip"))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(60)))
    out = tracer.export_trace(tid, str(tmp_path / "trace.zip"))
    tracer.close()
    with zipfile.ZipFile(out) as bundle:
        spans = json.loads(bundle.read("spans.json"))
        raw_events = bundle.read("events.json").decode("utf-8")
        events = json.loads(raw_events)
    assert len(spans) == len(events) == 60
    assert all(row["ended_at"] and row["status"] == "error" for row in spans)
    assert sum(row["tokens_in"] for row in spans) == sum(range(60))
    assert all(row["data"]["error_type"] == "RuntimeError" for row in events)
    assert "private" not in raw_events and '"args"' not in raw_events


def test_ui_failure_persists_partial_history_and_visible_error(runtime_server, caplog):
    server = runtime_server
    sid = server.store.new_session("test")
    events = server.event_bus.subscribe()

    def fail(message):
        _append_failed_tool(server.orch)
        raise RuntimeError("sk-private-token raw tool arguments")

    server.orch.handle = fail
    status, body = _call_api(server.app, "POST", "/api/chat", {
        "session_id": sid, "content": "执行测试任务",
    })
    assert status.startswith("500")
    saved = server.store.load_history(sid)
    assert [m.role for m in saved] == ["user", "assistant", "tool", "assistant"]
    assert saved[-1].content == body["message"]["content"]
    assert "RuntimeError" in saved[-1].content
    assert "未完成" in saved[-1].content
    assert server.state_snapshot["agent_state"]["state"] == "error"
    assert server.orch.last_turn_status == "error"
    row = server.tracer.db.execute(
        "SELECT data FROM events WHERE event='chat_error'").fetchone()
    assert json.loads(row[0])["session_id"] == sid
    assert "private" not in row[0] and "private" not in caplog.text
    messages = []
    while not events.empty():
        messages.append(events.get_nowait())
    assert any("event: message" in event and saved[-1].id in event for event in messages)


@pytest.mark.parametrize("turn_status", ["waiting_user", "incomplete", "error"])
def test_ui_preserves_non_done_terminal_status(runtime_server, turn_status):
    server = runtime_server

    def respond(message):
        server.orch.last_turn_status = turn_status
        server.emit_event("agent_state", {"state": turn_status, "detail": "尚未完成"})
        reply = Message(role="assistant", content="尚未完成，请查看任务状态。")
        server.orch.history.append(reply)
        return reply.content

    server.orch.handle = respond
    status, body = _call_api(server.app, "POST", "/api/chat", {"content": "测试"})
    assert status.startswith("200")
    assert body["message"]["status"] == turn_status
    assert server.state_snapshot["agent_state"]["state"] == turn_status


@pytest.mark.parametrize("result", [
    "[审批未通过] 用户拒绝或超时", "[工具不存在] unknown",
    "[工具未启用] disabled", "[资源冲突] occupied",
    "[工具执行失败] failed", "[拒绝] denied",
])
def test_ui_history_reports_all_tool_failures(runtime_server, result):
    server = runtime_server
    sid = server.store.new_session("test")
    server.store.save_history(sid, [
        Message(role="assistant", tool_calls=[ToolCall(id="tc1", name="desktop_press")]),
        Message(role="tool", tool_call_id="tc1", name="desktop_press", content=result),
    ])
    status, body = _call_api(server.app, "GET", f"/api/sessions/{sid}/messages")
    assert status.startswith("200")
    assert body[0]["tool_calls"][0]["status"] == "error"


def test_weixin_failure_persists_partial_history_and_diagnostic(tmp_path, monkeypatch, caplog):
    store = SessionStore(str(tmp_path / "conversations.db"))
    tracer = Tracer(str(tmp_path / "trace.db"))
    sid = store.new_session("test")
    events = []
    orch = SimpleNamespace(history=[], last_turn_tool_records=[], last_turn_status="running",
                           llm=SimpleNamespace(model="test-model"))
    channel = WeixinChannel(
        data_dir=tmp_path / "channel", orch=orch, session_store=store,
        tracer=tracer, on_event=lambda event, data: events.append((event, data)))
    channel.client = MagicMock()
    monkeypatch.setattr("channels.weixin.threading.Thread", _ImmediateThread)

    def fail(text):
        _append_failed_tool(orch)
        raise ValueError("sk-private-token")

    orch.handle = fail
    channel._dispatch_agent_task(
        "执行测试任务", "fake-token", "user", "bot", "https://example.invalid", "fake-context")
    saved = store.load_history(sid)
    assert [m.role for m in saved] == ["user", "assistant", "tool", "assistant"]
    assert "ValueError" in saved[-1].content and "未完成" in saved[-1].content
    assert orch.last_turn_status == "error"
    assert any(event == "agent_state" and data["state"] == "error" for event, data in events)
    assert any(event == "message" and data.get("status") == "error" for event, data in events)
    channel.client.send_message.assert_called_once()
    row = tracer.db.execute("SELECT data FROM events WHERE event='weixin_task_error'").fetchone()
    assert json.loads(row[0])["error_type"] == "ValueError"
    assert "private" not in row[0] and "private" not in caplog.text
    tracer.close()
    store.db.close()


def test_ui_hides_internal_observations_and_verification_feedback(runtime_server):
    server = runtime_server
    sid = server.store.new_session("test")
    server.store.save_history(sid, [
        Message(role="user", content="真实请求"),
        Message(role="user", name="tool_observation", content="AUTO_OBSERVATION"),
        Message(role="user", name="verification_feedback", content="AUTO_FEEDBACK"),
        Message(role="user", name="plan_step", content="AUTO_PLAN_STEP"),
        Message(role="assistant", content="真实回复"),
    ])
    status, body = _call_api(server.app, "GET", f"/api/sessions/{sid}/messages")
    assert status.startswith("200")
    assert [message["content"] for message in body] == ["真实请求", "真实回复"]
    assert len(server.store.load_history(sid)) == 5


@pytest.mark.parametrize("scope", ["session", "hourly", "nightly"])
def test_memory_worker_does_not_treat_internal_feedback_as_user_material(tmp_path, monkeypatch, scope):
    from agent.llm import LLMClient
    from agent.memory_manager import MemoryManager
    from agent.memory_pipeline import run_memory_distill

    store = SessionStore(str(tmp_path / "memory-sessions.db"))
    sid = store.new_session("test")
    store.save_history(sid, [
        Message(role="user", content="我喜欢简洁回复"),
        Message(role="user", name="tool_observation", content="AUTO_OBSERVATION"),
        Message(role="user", name="verification_feedback", content="AUTO_FEEDBACK"),
        Message(role="user", name="plan_step", content="AUTO_PLAN_STEP"),
    ])
    worker = MagicMock()
    worker.chat.return_value = Message(role="assistant", content="")
    monkeypatch.setattr(LLMClient, "for_role", lambda *args, **kwargs: worker)
    result = run_memory_distill(
        memory=MemoryManager(tmp_path / "memory"), store=store,
        scope=scope, session_id=sid, cfg={"models": {}})
    materials = worker.chat.call_args.args[0][-1].content
    assert "我喜欢简洁回复" in materials
    assert all(marker not in materials for marker in (
        "AUTO_OBSERVATION", "AUTO_FEEDBACK", "AUTO_PLAN_STEP"))
    assert result["extracted_count"] == 0
    store.db.close()
