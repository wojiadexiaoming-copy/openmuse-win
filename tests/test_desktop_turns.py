"""Desktop turn isolation and durable checkpoints; no native desktop or network."""
from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bottle import Bottle
from PIL import Image

from agent.context import ContextBuilder
from agent.models import Message, ToolCall
from agent.orchestrator import Orchestrator
from agent.session_store import SessionStore
from channels.weixin import WeixinChannel
from ui.server import AgentServer, EventBus


class ScopedRegistry:
    def __init__(self, execute=None):
        self._tools = {"file_read": None, "file_write": None}
        self.execute_fn = execute or (lambda name, arguments: Message(
            role="tool", name=name, content="tool completed"))
        self.scopes = []
        self.closed = 0
        self.scope = ContextVar("test_desktop_scope", default=None)
        self.tokens = []

    def openai_schemas(self):
        return []

    def begin_turn(self, scope_id):
        self.scopes.append(scope_id)
        self.tokens.append(self.scope.set(scope_id))

    def end_turn(self):
        self.closed += 1
        self.scope.reset(self.tokens.pop())

    def execute(self, name, arguments):
        return self.execute_fn(name, arguments)


def _call_api(app, method, path, body=None):
    raw = json.dumps(body or {}).encode("utf-8")
    statuses = []
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path,
        "wsgi.input": io.BytesIO(raw), "wsgi.errors": io.StringIO(),
        "CONTENT_LENGTH": str(len(raw)), "CONTENT_TYPE": "application/json",
        "HTTP_AUTHORIZATION": "Bearer offline-turn-token",
    }
    data = b"".join(app(environ, lambda status, headers, exc_info=None: statuses.append(status)))
    return statuses[0], json.loads(data.decode("utf-8"))


@pytest.fixture
def turn_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ui.server.run_memory_distill", lambda **kwargs: {})
    server = AgentServer.__new__(AgentServer)
    server.app = Bottle()
    server.api_token = "offline-turn-token"
    server.llm = SimpleNamespace(model="offline")
    server.orch = Orchestrator(server.llm, ScopedRegistry(), ContextBuilder())
    server.memory = SimpleNamespace(maybe_remember=lambda value: None)
    server.vault = SimpleNamespace(scrub=lambda value: value)
    server.store = SessionStore(str(tmp_path / "conversations.db"))
    server.tracer = None
    server.tracker = None
    server.event_bus = EventBus()
    server.state_snapshot = {
        "agent_state": {"state": "idle"}, "plan": [], "activity": [], "approvals": [],
        "status": {"current_model": "offline", "token_usage": {}, "memory_writes": []},
    }
    server._setup_routes()
    yield server
    server.store.db.close()


def test_handle_binds_session_scope_and_always_closes_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = ScopedRegistry()
    llm = SimpleNamespace(chat=lambda *args, **kwargs: Message(
        role="assistant", content="完成"), model="offline")
    orch = Orchestrator(llm, registry, ContextBuilder(), session_id="session-one")
    orch.handle("随便聊聊")
    orch.session_id = "session-two"
    orch.history = []
    orch.handle("你好")

    assert registry.scopes == ["session-one", "session-two"]
    assert registry.closed == 2
    assert registry.scope.get() is None

    def fail(*args, **kwargs):
        raise RuntimeError("offline failure")

    llm.chat = fail
    with pytest.raises(RuntimeError):
        orch.handle("测试异常")

    assert registry.closed == 3
    assert registry.scope.get() is None


def test_parallel_tool_threads_keep_session_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = ScopedRegistry()
    observed = []

    def execute(name, arguments):
        observed.append(registry.scope.get())
        return Message(role="tool", name=name, content="observed")

    registry.execute_fn = execute
    replies = iter([
        Message(role="assistant", tool_calls=[ToolCall(id="read", name="file_read")]),
        Message(role="assistant", content="完成"),
    ])
    orch = Orchestrator(
        SimpleNamespace(chat=lambda *args, **kwargs: next(replies)),
        registry, ContextBuilder(), parallel_tools=True, session_id="parallel-session",
    )

    orch.handle("查看状态")

    assert observed == ["parallel-session"]


def test_checkpoint_is_saved_before_next_model_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SessionStore(str(tmp_path / "conversations.db"))
    sid = store.new_session("checkpoint")
    checkpoints = []
    calls = 0

    def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Message(role="assistant", tool_calls=[ToolCall(id="read", name="file_read")])
        saved = store.load_history(sid)
        assert [message.role for message in saved] == ["user", "assistant", "tool"]
        assert saved[-1].tool_call_id == "read"
        assert saved[-1].metadata["snapshot_id"] == "actual-evidence"
        return Message(role="assistant", content="已读取")

    def persist(history):
        checkpoints.append([message.role for message in history])
        store.save_history(sid, history)

    registry = ScopedRegistry(lambda name, args: Message(
        role="tool", name=name, content="evidence",
        metadata={"snapshot_id": "actual-evidence"},
    ))
    orch = Orchestrator(SimpleNamespace(chat=chat), registry, ContextBuilder(), on_history=persist)

    try:
        orch.handle("查看窗口")
        assert ["user", "assistant", "tool"] in checkpoints
        assert store.load_history(sid)[0].metadata["task_status"] == "done"
    finally:
        store.db.close()


def test_interrupted_batch_checkpoint_pairs_all_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshots = []

    def execute(name, arguments):
        if name == "file_write":
            raise RuntimeError("interrupt second call")
        return Message(role="tool", name=name, content="first result")

    llm = SimpleNamespace(chat=lambda *args, **kwargs: Message(
        role="assistant", tool_calls=[
            ToolCall(id="first", name="file_read"), ToolCall(id="second", name="file_write"),
        ]))
    orch = Orchestrator(
        llm, ScopedRegistry(execute), ContextBuilder(),
        on_history=lambda history: snapshots.append([
            (message.role, message.tool_call_id, message.content) for message in history
        ]),
    )

    with pytest.raises(RuntimeError):
        orch.handle("执行两步")

    paired = [snapshot for snapshot in snapshots if [row[0] for row in snapshot]
              == ["user", "assistant", "tool", "tool"]]
    assert paired
    assert [row[1] for row in paired[-1][-2:]] == ["first", "second"]
    assert paired[-1][-2][2] == "first result"
    assert "未完成" in paired[-1][-1][2]


def test_manual_dependency_still_records_failed_artifact_criteria(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checked = []

    def verify(goal, **kwargs):
        checked.append(goal)
        return False, ["缺少本轮有效图片"]

    monkeypatch.setattr("agent.verifier.verify_outcome", verify)
    orch = Orchestrator(
        SimpleNamespace(chat=lambda *args, **kwargs: Message(
            role="assistant", content="需要你协助确认窗口后才能继续")),
        ScopedRegistry(), ContextBuilder(),
    )

    orch.handle("生图并下载")

    assert checked == ["生图并下载"]
    assert orch.last_turn_status == "waiting_user"
    assert orch.history[0].metadata["failed_criteria"] == ["缺少本轮有效图片"]
    assert orch.history[-1].metadata["failed_criteria"] == ["缺少本轮有效图片"]


def test_correction_restores_original_goal_and_baseline_after_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake-user")
    store = SessionStore(str(tmp_path / "conversations.db"))
    sid = store.new_session("resume")
    goal = "打开豆包生成3张图片并下载"

    def first_reply(*args, **kwargs):
        Image.new("RGB", (8, 8), "red").save(tmp_path / "first.png")
        Image.new("RGB", (8, 8), "blue").save(tmp_path / "second.png")
        return Message(role="assistant", content="需要你协助确认窗口后才能继续")

    first = Orchestrator(SimpleNamespace(chat=first_reply), ScopedRegistry(), ContextBuilder())
    first.handle(goal)
    saved_baseline = first.history[0].metadata["artifact_baseline"]
    store.save_history(sid, first.history)

    def last_reply(*args, **kwargs):
        Image.new("RGB", (8, 8), "green").save(tmp_path / "third.png")
        return Message(role="assistant", content="三张图片已生成并下载")

    second = Orchestrator(SimpleNamespace(chat=last_reply), ScopedRegistry(), ContextBuilder())
    second.history = store.load_history(sid)

    try:
        second.handle("你没有点到正确按钮，继续生图下载")
        assert second.last_turn_status == "done"
        assert second.history[0].metadata["task_goal"] == goal
        assert second.history[0].metadata["artifact_baseline"] == json.loads(json.dumps(saved_baseline))
        correction = next(message for message in second.history
                          if message.content.startswith("你没有"))
        assert correction.metadata["task_origin_id"] == second.history[0].id
        assert correction.metadata["task_goal"] == goal
    finally:
        store.db.close()


def test_ui_and_weixin_turns_share_lock_and_do_not_mix_histories(turn_server, tmp_path):
    server = turn_server
    sid_ui = server.store.new_session("ui")
    sid_wx = server.store.new_session("weixin")
    entered_ui = threading.Event()
    release_ui = threading.Event()
    entered_wx = threading.Event()
    wx_done = threading.Event()
    seen = []

    def handle(message):
        content = message.content if isinstance(message, Message) else message
        sid = server.orch.session_id
        seen.append((sid, content))
        if sid == sid_ui:
            entered_ui.set()
            assert release_ui.wait(3)
        else:
            entered_wx.set()
        reply = Message(role="assistant", content=f"reply:{content}")
        server.orch.history.append(reply)
        server.orch.on_history(list(server.orch.history))
        server.orch.last_turn_status = "done"
        return reply.content

    server.orch.handle = handle
    channel = WeixinChannel(
        tmp_path / "weixin", orch=server.orch, session_store=server.store,
        turn_lock=server.turn_lock,
    )
    channel.weixin_session_id = sid_wx
    channel.client = MagicMock()
    channel.client.send_message.side_effect = lambda **kwargs: wx_done.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        ui_result = pool.submit(_call_api, server.app, "POST", "/api/chat", {
            "session_id": sid_ui, "content": "UI task",
        })
        assert entered_ui.wait(3)
        channel._dispatch_agent_task(
            "微信 task", "offline", "user", "bot", "https://example.invalid", None)
        try:
            assert not entered_wx.wait(0.1)
        finally:
            release_ui.set()
        status, _ = ui_result.result(timeout=3)
        assert status.startswith("200")
        assert wx_done.wait(3)

    assert channel.turn_lock is server.turn_lock is server.orch.turn_lock
    assert seen == [(sid_ui, "UI task"), (sid_wx, "微信 task")]
    assert [message.content for message in server.store.load_history(sid_ui)] == [
        "UI task", "reply:UI task",
    ]
    assert [message.content for message in server.store.load_history(sid_wx)] == [
        "微信 task", "reply:微信 task",
    ]


def test_approval_api_is_not_blocked_by_chat_turn_lock(turn_server):
    server = turn_server
    waiting = threading.Event()
    resolved = threading.Event()
    approvals = []

    def resolve(aid, approved, scope):
        approvals.append((aid, approved, scope))
        resolved.set()
        return True

    server.approval = SimpleNamespace(resolve_card=resolve)

    def handle(message):
        # Re-entering the same lock in a turn must remain safe.
        with server.orch.turn_lock:
            waiting.set()
            assert resolved.wait(3)
            reply = Message(role="assistant", content="操作完成")
            server.orch.history.append(reply)
            server.orch.last_turn_status = "done"
            return reply.content

    server.orch.handle = handle

    with ThreadPoolExecutor(max_workers=1) as pool:
        chat = pool.submit(_call_api, server.app, "POST", "/api/chat", {"content": "需要审批的操作"})
        assert waiting.wait(3)
        status, body = _call_api(
            server.app, "POST", "/api/approvals/one/resolve",
            {"approved": True, "scope": "once"},
        )
        assert status.startswith("200")
        assert body["approved"] is True
        assert chat.result(timeout=3)[0].startswith("200")

    assert approvals == [("one", True, "once")]


def test_retry_without_session_reuses_original_tool_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = ScopedRegistry()
    orch = Orchestrator(
        SimpleNamespace(chat=lambda *args, **kwargs: Message(
            role="assistant", content="需要你协助确认窗口后才能继续")),
        registry, ContextBuilder(),
    )

    orch.handle("点击窗口按钮")
    origin_id = orch.history[0].id
    orch.handle("继续")

    assert registry.scopes == [origin_id, origin_id]
    assert registry.closed == 2


@pytest.mark.parametrize("reply", [
    "好了", "可以了", "已登录", "可以继续", "验证码填好了", "验证码已经输入完了",
    "我已经登录了", "我已登录成功", "我登录好了", "登录完成了", "已经登录完成",
    "我已完成登录", "我完成了登录", "我已经输入完验证码了", "我已完成验证码输入",
])
def test_short_manual_resume_keeps_pending_goal_and_baseline(tmp_path, monkeypatch, reply):
    monkeypatch.chdir(tmp_path)
    checked = []
    capture = MagicMock(return_value={})
    monkeypatch.setattr("agent.verifier.capture_artifacts", capture)

    def verify(goal, **kwargs):
        checked.append(goal)
        return False, ["缺少本轮图片"]

    monkeypatch.setattr("agent.verifier.verify_outcome", verify)
    orch = Orchestrator(
        SimpleNamespace(chat=lambda *args, **kwargs: Message(
            role="assistant", content="需要你协助确认窗口后才能继续")),
        ScopedRegistry(), ContextBuilder(),
    )
    goal = "生成3张图片"
    orch.handle(goal)
    origin_id = orch.history[0].id
    baseline = orch.history[0].metadata["artifact_baseline"]
    orch.handle(reply)

    assert checked == [goal, goal]
    assert orch.last_turn_status == "waiting_user"
    assert orch.history[0].metadata["artifact_baseline"] == baseline
    assert next(message for message in orch.history if message.content == reply).metadata[
        "task_origin_id"] == origin_id
    capture.assert_called_once()


@pytest.mark.parametrize("new_task", [
    "可以了，请生成2个文件",
    "我已经登录了，请生成2个文件",
    "我已完成登录，请解释新的问题",
])
def test_manual_acknowledgement_with_new_task_does_not_reuse_pending_goal(tmp_path, monkeypatch, new_task):
    monkeypatch.chdir(tmp_path)
    checked = []
    goal = "生成3张图片"

    def verify(current_goal, **kwargs):
        checked.append(current_goal)
        return (current_goal == new_task, [] if current_goal == new_task else ["缺少图片"])

    monkeypatch.setattr("agent.verifier.verify_outcome", verify)
    replies = iter([
        Message(role="assistant", content="需要你协助确认窗口后才能继续"),
        Message(role="assistant", content="新任务已完成"),
    ])
    orch = Orchestrator(SimpleNamespace(chat=lambda *a, **k: next(replies)),
                        ScopedRegistry(), ContextBuilder())
    orch.handle(goal)
    orch.handle(new_task)

    assert checked == [goal, new_task]
    assert orch.history[0].metadata["task_status"] == "waiting_user"
    current = next(message for message in orch.history if message.content == new_task)
    assert "task_origin_id" not in current.metadata
    assert current.metadata["task_goal"] == new_task


def test_manual_acknowledgement_does_not_resume_completed_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checked = []
    monkeypatch.setattr("agent.verifier.verify_outcome", lambda goal, **kwargs: (
        checked.append(goal) or True, []))
    orch = Orchestrator(
        SimpleNamespace(chat=lambda *a, **k: Message(role="assistant", content="好的")),
        ScopedRegistry(), ContextBuilder(),
    )
    orch.history = [Message(role="user", content="之前的任务",
                            metadata={"task_status": "done"})]

    orch.handle("好了")

    assert checked == ["好了"]
    assert "task_origin_id" not in orch.history[-2].metadata


def test_early_capture_failure_marks_error_and_releases_turn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = ScopedRegistry()
    checkpoints = []
    events = []
    orch = Orchestrator(
        SimpleNamespace(chat=lambda *a, **k: Message(role="assistant", content="unused")),
        registry, ContextBuilder(),
        on_history=lambda history: checkpoints.append([dict(message.metadata) for message in history]),
        on_event=lambda name, data: events.append((name, data)),
    )
    monkeypatch.setattr("agent.verifier.capture_artifacts", lambda *a, **k: (
        _ for _ in ()).throw(RuntimeError("offline metadata failure")))

    with pytest.raises(RuntimeError):
        orch.handle("查看窗口")

    assert orch.last_turn_status == "error"
    assert orch.history[0].metadata["task_status"] == "error"
    assert checkpoints[-1][0]["task_status"] == "error"
    assert registry.closed == 1 and registry.scope.get() is None
    assert any(name == "agent_state" and data["state"] == "error" for name, data in events)

    def can_acquire():
        acquired = orch.turn_lock.acquire(blocking=False)
        if acquired:
            orch.turn_lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(can_acquire).result(timeout=3)


@pytest.mark.parametrize("phase", ["lookup", "load"])
def test_weixin_history_failure_never_persists_previous_session(turn_server, tmp_path, monkeypatch, phase):
    server = turn_server
    sid = server.store.new_session("session B")
    existing = Message(role="user", content="B existing history")
    server.store.save_history(sid, [existing])
    real_load = server.store.load_history
    server.orch.history = [Message(role="user", content="A private history")]
    server.orch.handle = MagicMock(return_value="should not execute")
    trace_events = []
    events = []
    channel = WeixinChannel(
        tmp_path / "weixin-failed-load", orch=server.orch, session_store=server.store,
        turn_lock=server.turn_lock,
        tracer=SimpleNamespace(log_event=lambda name, data: trace_events.append((name, data))),
        on_event=lambda name, data: events.append((name, data)),
    )
    channel.weixin_session_id = sid if phase == "load" else None
    channel.client = MagicMock()

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("channels.weixin.threading.Thread", ImmediateThread)
    if phase == "load":
        monkeypatch.setattr(server.store, "load_history", MagicMock(side_effect=RuntimeError("load failed")))
    else:
        monkeypatch.setattr(server.store, "list_sessions", MagicMock(side_effect=RuntimeError("lookup failed")))
    save = MagicMock(wraps=server.store.save_history)
    monkeypatch.setattr(server.store, "save_history", save)

    channel._dispatch_agent_task(
        "B new task", "offline", "user", "bot", "https://example.invalid", None)

    save.assert_not_called()
    server.orch.handle.assert_not_called()
    assert [message.content for message in real_load(sid)] == ["B existing history"]
    assert all("A private history" not in message.content for message in server.orch.history)
    assert server.orch.last_turn_status == "error"
    assert any(name == "weixin_task_error" for name, data in trace_events)
    assert any(name == "message" and data.get("status") == "error" for name, data in events)
    channel.client.send_message.assert_called_once()


def test_session_delete_waits_for_turn_and_clears_scope(turn_server):
    from tools.desktop_runtime import begin_turn, clear_scope, current_state, end_turn, _states

    server = turn_server
    sid = server.store.new_session("delete safely")
    server.weixin_channel = SimpleNamespace(weixin_session_id=sid)
    token = begin_turn(sid)
    current_state()
    end_turn(token)
    entered = threading.Event()
    release = threading.Event()
    delete_started = threading.Event()

    def handle(message):
        entered.set()
        assert release.wait(3)
        reply = Message(role="assistant", content="完成")
        server.orch.history.append(reply)
        server.orch.last_turn_status = "done"
        return reply.content

    server.orch.handle = handle

    def delete():
        delete_started.set()
        return _call_api(server.app, "DELETE", f"/api/sessions/{sid}")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            chat = pool.submit(_call_api, server.app, "POST", "/api/chat", {
                "session_id": sid, "content": "执行中的任务",
            })
            assert entered.wait(3)
            deletion = pool.submit(delete)
            try:
                assert delete_started.wait(3)
                assert not deletion.done()
            finally:
                release.set()
            assert chat.result(timeout=3)[0].startswith("200")
            status, body = deletion.result(timeout=3)
            assert status.startswith("200") and body["deleted"] == sid
        assert server.store.load_history(sid) == []
        assert all(row["session_id"] != sid for row in server.store.list_sessions())
        assert sid not in _states
        assert server.weixin_channel.weixin_session_id is None
    finally:
        clear_scope(sid)


def test_ui_key_reload_reattaches_request_observer(turn_server, monkeypatch):
    server = turn_server
    observed = []
    request_events = []
    new_client = SimpleNamespace(set_request_observer=observed.append)
    server.cfg = {}
    server.tracer = SimpleNamespace(
        log_event=lambda name, data: request_events.append((name, data)))
    server.vault = SimpleNamespace(
        ALLOWLIST={"llm_api_key"}, set=MagicMock(), scrub=lambda value: value)
    monkeypatch.setattr("ui.server.resolve_llm_key", lambda vault: "offline")
    monkeypatch.setattr("ui.server.resolve_backup_llm_key", lambda vault: "")
    monkeypatch.setattr("ui.server.LLMClient.from_config", lambda *args, **kwargs: new_client)

    status, body = _call_api(server.app, "POST", "/api/settings/vault_key", {
        "name": "llm_api_key", "key": "offline-test-value",
    })

    assert status.startswith("200") and body["status"] == "saved"
    assert server.orch.llm is new_client
    assert len(observed) == 1
    observed[0]({"success": True})
    assert request_events == [("llm_request", {"success": True, "channel": "ui"})]


def test_weixin_delivery_failure_preserves_completed_task_and_saved_result(turn_server, tmp_path, monkeypatch):
    server = turn_server
    sid = server.store.new_session("completed computer task")
    server.orch.llm.chat = lambda *args, **kwargs: Message(
        role="assistant", content="电脑任务已经完成")
    log_events = []
    emitted = []
    channel = WeixinChannel(
        tmp_path / "weixin-delivery", orch=server.orch, session_store=server.store,
        turn_lock=server.turn_lock,
        tracer=SimpleNamespace(log_event=lambda name, data: log_events.append((name, data))),
        on_event=lambda name, data: emitted.append((name, data)),
    )
    channel.weixin_session_id = sid
    channel.client = MagicMock()
    channel.client.send_message.side_effect = RuntimeError("offline message delivery failure")

    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    with monkeypatch.context() as dispatch_patch:
        dispatch_patch.setattr("channels.weixin.threading.Thread", ImmediateThread)
        channel._dispatch_agent_task(
            "执行电脑操作", "offline", "user", "bot", "https://example.invalid", None)

    history = server.store.load_history(sid)
    assert server.orch.last_turn_status == "done"
    assert history[0].metadata["task_status"] == "done"
    assert [message.content for message in history] == ["执行电脑操作", "电脑任务已经完成"]
    assert history[-1].metadata["task_status"] == "done"
    channel.client.send_message.assert_called_once()
    assert [name for name, data in log_events] == ["weixin_delivery_error"]
    assert any(name == "message" and data.get("status") == "done" for name, data in emitted)
    assert not any(name == "message" and data.get("status") == "error" for name, data in emitted)
    assert server.orch.on_history is None and server.orch.session_id is None

    def can_acquire():
        acquired = server.turn_lock.acquire(blocking=False)
        if acquired:
            server.turn_lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(can_acquire).result(timeout=3)


@pytest.mark.parametrize("reply", [
    "我还没登录", "我尚未登录", "没有登录成功", "我已经登录失败了", "我还没填好验证码",
    "完成登录", "我已经登录了，请生成新的文件",
])
def test_manual_resume_does_not_accept_negation_or_new_instructions(reply):
    assert not Orchestrator._is_manual_resume(reply)


def test_natural_login_confirmation_cannot_skip_original_artifact_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checked = []

    def verify(goal, **kwargs):
        checked.append(goal)
        return False, ["缺少三张图片"]

    monkeypatch.setattr("agent.verifier.verify_outcome", verify)
    replies = iter([
        Message(role="assistant", content="需要你协助登录账号后才能继续"),
        Message(role="assistant", content="登录已完成"),
        Message(role="assistant", content="正在继续"),
        Message(role="assistant", content="仍未生成"),
    ])
    orch = Orchestrator(SimpleNamespace(chat=lambda *args, **kwargs: next(replies)),
                        ScopedRegistry(), ContextBuilder())
    orch.handle("生成3张图片")
    assert orch.last_turn_status == "waiting_user"
    origin_id = orch.history[0].id

    orch.handle("我已经登录了")

    assert checked == ["生成3张图片"] * 4
    assert orch.last_turn_status == "incomplete"
    confirmation = next(message for message in orch.history if message.content == "我已经登录了")
    assert confirmation.metadata["task_origin_id"] == origin_id
    assert confirmation.metadata["task_goal"] == "生成3张图片"
