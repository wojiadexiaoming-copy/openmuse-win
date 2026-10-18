"""Terminal desktop errors: fake tools/models, pure state, and isolated storage."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
import json
import threading

import pytest

from agent.models import Message, ToolCall, terminal_tool_control, tool_result_is_error
from agent.orchestrator import Orchestrator
from agent.planner import Plan, PlanStep
from agent.session_store import SessionStore
from agent.state_text import result_summary
from agent.tools_registry import ToolDefinition, ToolRegistry
from agent.tracing import Tracer


CONTROL = {"retryable": False, "requires_user": True, "reason": "desktop_failsafe"}


def stop_result(name="desktop_click"):
    return Message(role="tool", name=name, content="[桌面安全停止] 四角停止保护已触发。",
                   metadata={"tool_control": dict(CONTROL)})


class FakeRegistry:
    def __init__(self, execute=None):
        self._tools = {name: None for name in ("desktop_click", "desktop_type", "desktop_snapshot", "spawn_subagent")}
        self.execute_fn = execute or (lambda name, args: stop_result(name))
        self.executed = []
        self.scopes = []
        self.closed = 0

    def openai_schemas(self):
        return []

    def execute(self, name, args):
        self.executed.append(name)
        return self.execute_fn(name, args)

    def begin_turn(self, scope):
        self.scopes.append(scope)

    def end_turn(self):
        self.closed += 1


@pytest.fixture
def verification(monkeypatch):
    capture = MagicMock(return_value={})
    verify = MagicMock(return_value=(False, ["缺少本轮图片产物"]))
    monkeypatch.setattr("agent.verifier.capture_artifacts", capture)
    monkeypatch.setattr("agent.verifier.verify_outcome", verify)
    return capture, verify


def fake_context():
    return SimpleNamespace(build=lambda conversation, tools=None: list(conversation))


@pytest.mark.parametrize("parallel", [False, True])
def test_terminal_result_cancels_remaining_batch_and_checkpoints_before_return(verification, parallel):
    calls = [ToolCall(id="click", name="desktop_click"), ToolCall(id="type", name="desktop_type"),
             ToolCall(id="spawn", name="spawn_subagent")]
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.return_value = Message(role="assistant", tool_calls=calls)
    registry = FakeRegistry()
    checkpoints = []
    sub_mgr = MagicMock()
    orch = Orchestrator(llm, registry, fake_context(), max_iters=25, parallel_tools=parallel,
                        sub_mgr=sub_mgr, on_history=lambda history: checkpoints.append(deepcopy(history)))

    reply = orch.handle("生成图片并下载到本地")

    assert llm.chat.call_count == 1
    assert registry.executed == ["desktop_click"]
    sub_mgr.spawn.assert_not_called()
    assert orch.last_turn_status == "waiting_user"
    assert "四角停止保护" in reply and "重新截图" in reply and "尚未完成" in reply
    results = [message for message in orch.history if message.role == "tool"]
    assert [message.tool_call_id for message in results] == ["click", "type", "spawn"]
    assert [message.metadata.get("tool_cancelled", False) for message in results] == [False, True, True]
    assert any([message.tool_call_id for message in snapshot if message.role == "tool"] == ["click", "type", "spawn"]
               and snapshot[-1].role == "tool" for snapshot in checkpoints)
    assert terminal_tool_control(orch.history[-1]) == CONTROL
    assert "缺少本轮图片产物" in orch.history[-1].metadata["failed_criteria"]
    assert verification[1].call_count == 1
    assert registry.closed == 1


def test_plan_does_not_advance_or_auto_repair_after_terminal_result(verification, monkeypatch):
    monkeypatch.setattr("agent.planner.should_plan", lambda _: True)
    plan = Plan(goal="draw", steps=[PlanStep(desc="操作桌面", done_when="输入完成"),
                                    PlanStep(desc="下载图片", done_when="文件落地")])
    planner = SimpleNamespace(make_plan=lambda _: plan)
    llm = MagicMock()
    llm.chat.return_value = Message(role="assistant", tool_calls=[ToolCall(id="click", name="desktop_click")])
    orch = Orchestrator(llm, FakeRegistry(), fake_context(), planner=planner)

    orch.handle("生成图片然后下载")

    assert llm.chat.call_count == 1
    assert [step.status for step in plan.steps] == ["waiting_user", "pending"]
    assert orch.last_turn_status == "waiting_user"
    assert verification[1].call_count == 1


def test_continue_starts_new_turn_and_restores_original_goal_baseline(verification):
    llm = MagicMock()
    llm.chat.return_value = Message(role="assistant", tool_calls=[ToolCall(id="first", name="desktop_click")])
    registry = FakeRegistry()
    orch = Orchestrator(llm, registry, fake_context(), session_id="same-session")
    orch.handle("生成图片并下载到本地")
    original = orch.history[0]
    registry.execute_fn = lambda name, args: Message(role="tool", name=name, content="observed")
    verification[1].return_value = (True, [])
    llm.chat.side_effect = [Message(role="assistant", tool_calls=[ToolCall(id="new-shot", name="desktop_snapshot")]),
                            Message(role="assistant", content="完成")]

    assert orch.handle("继续") == "完成"

    assert registry.executed == ["desktop_click", "desktop_snapshot"]
    assert orch.last_turn_status == "done"
    assert registry.scopes == ["same-session", "same-session"]
    assert verification[0].call_count == 1
    assert verification[1].call_args.args[0] == original.content
    resume = next(message for message in orch.history if message.role == "user" and message.content == "继续")
    assert resume.metadata["task_origin_id"] == original.id


def test_parallel_gate_does_not_hold_lock_during_running_tool():
    entered = threading.Event()
    release = threading.Event()
    def execute(name, args):
        if name == "file_read":
            entered.set()
            assert release.wait(3)
            return Message(role="tool", name=name, content="read completed")
        return stop_result(name)
    registry = FakeRegistry(execute)
    orch = Orchestrator(SimpleNamespace(), registry, fake_context())
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(orch._execute_tool, "file_read", {})
        assert entered.wait(2)
        try:
            stopped = pool.submit(orch._execute_tool, "desktop_click", {}).result(2)
            assert terminal_tool_control(stopped) == CONTROL
            cancelled = orch._execute_tool("desktop_type", {})
            assert cancelled.metadata["tool_cancelled"] is True
        finally:
            release.set()
        assert read.result(2).content == "read completed"
    assert registry.executed == ["file_read", "desktop_click"]


@pytest.mark.parametrize("text", ["[桌面点击失败] FailSafeException", "[录制已停止] saved.mp4", "正常正文提到 FailSafeException"])
def test_plain_text_never_creates_terminal_control(text):
    registry = FakeRegistry(lambda name, args: Message(role="tool", name=name, content=text))
    llm = MagicMock()
    llm.chat.side_effect = [Message(role="assistant", tool_calls=[ToolCall(id="one", name="desktop_click")]),
                            Message(role="assistant", content="普通后续回复")]
    orch = Orchestrator(llm, registry, fake_context())
    assert orch.run_turn(Message(role="user", content="test")).content == "普通后续回复"
    assert llm.chat.call_count == 2
    assert orch._get_terminal_control() is None
    assert terminal_tool_control(Message(role="tool", content=text)) is None
    assert tool_result_is_error("[录制已停止] saved.mp4") is False


def test_terminal_control_requires_explicit_boolean_false():
    for value in (None, True, 0, "false"):
        message = Message(role="tool", metadata={"tool_control": {"retryable": value, "requires_user": True}})
        assert terminal_tool_control(message) is None


def test_terminal_without_manual_dependency_stays_incomplete(verification):
    message = stop_result()
    message.metadata["tool_control"]["requires_user"] = False
    registry = FakeRegistry(lambda name, args: message)
    llm = MagicMock()
    llm.chat.return_value = Message(role="assistant", tool_calls=[ToolCall(id="one", name="desktop_click")])
    orch = Orchestrator(llm, registry, fake_context())
    orch.handle("执行任务")
    assert orch.last_turn_status == "incomplete"
    assert llm.chat.call_count == 1
    assert verification[1].call_count == 1


def test_focus_false_remains_authoritative_over_editable_and_keyboard_focus():
    message = Message(role="tool", name="desktop_inspect_focus", content="[桌面焦点检查] editable=true",
                      metadata={"desktop_verification": {"focus_verified": False,
                                 "focus": {"editable": True, "has_keyboard_focus": True}}})
    assert tool_result_is_error(message) is True
    assert "未验证" in result_summary(message) and "不能输入" in result_summary(message)
    assert "未验证" in result_summary("[桌面焦点验证失败] editable=true")


def test_registry_preserves_control_and_never_caches_terminal_result():
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="desktop_click", description="offline", parameters={}, func=lambda _: stop_result()))
    cache = MagicMock()
    cache.get.return_value = None
    registry.set_cache(cache)
    message = registry.execute("desktop_click", {})
    assert terminal_tool_control(message) == CONTROL
    cache.put.assert_not_called()


def test_registry_normalizes_only_trusted_exception_type_and_invalidates_state():
    from tools.desktop_runtime import begin_turn, clear_scope, current_state, end_turn
    failsafe = type("FailSafeException", (Exception,), {"__module__": "pyautogui"})
    token = begin_turn("registry-bare-failsafe")
    registry = ToolRegistry()
    def fail(args):
        raise failsafe("private exception message")
    registry.register(ToolDefinition(name="desktop_click", description="offline", parameters={}, func=fail))
    try:
        state = current_state()
        state.observe({"hwnd": 1}, [100, 100], [100, 100])
        message = registry.execute("desktop_click", {})
        assert terminal_tool_control(message) == CONTROL
        assert state.halted is True and state.snapshot == {} and state.focus == {}
        assert "private exception message" not in message.content
    finally:
        end_turn(token)
        clear_scope("registry-bare-failsafe")
    assert registry._desktop_safety_stop("desktop_click", RuntimeError("FailSafeException")) is None
    assert registry._desktop_safety_stop("file_read", failsafe()) is None


def test_registry_preserves_desktop_halted_precheck_semantics():
    from tools.desktop_runtime import begin_turn, clear_scope, current_state, end_turn
    token = begin_turn("halted-precheck")
    registry = ToolRegistry()
    def no_native_action(args):
        pytest.fail("halted desktop action executed")
    no_native_action.__module__ = "tools.desktop"
    registry.register(ToolDefinition(name="desktop_scroll", description="offline", parameters={}, func=no_native_action))
    try:
        current_state().halt("desktop_failsafe")
        assert terminal_tool_control(registry.execute("desktop_scroll", {})) == CONTROL
    finally:
        end_turn(token)
        clear_scope("halted-precheck")


def test_safe_stop_event_and_checkpoint_metadata_are_durable(verification):
    tracer = Tracer(":memory:")
    store = SessionStore(":memory:")
    sid = store.new_session("offline")
    llm = MagicMock()
    llm.chat.return_value = Message(role="assistant", tool_calls=[ToolCall(id="one", name="desktop_click", arguments={"text": "private tool argument"})])
    orch = Orchestrator(llm, FakeRegistry(), fake_context(), tracer=tracer,
                        on_history=lambda history: store.save_history(sid, history))
    try:
        orch.handle("private task text")
        rows = tracer.db.execute("SELECT data FROM events WHERE event='tool_control_stop'").fetchall()
        assert len(rows) == 1
        data = json.loads(rows[0][0])
        assert data["tool"] == "desktop_click" and data["failure_category"] == "desktop_failsafe"
        assert data["error_type"] == "FailSafeException" and data["phase"] == "execution_stopped"
        assert "private" not in rows[0][0]
        restored = store.load_history(sid)
        assert terminal_tool_control(next(message for message in restored if message.role == "tool")) == CONTROL
        assert restored[-1].metadata["task_status"] == "waiting_user"
    finally:
        tracer.db.close()
        store.db.close()


@pytest.mark.parametrize("parallel", [False, True])
def test_cancelled_repeat_of_same_tool_does_not_hide_original_failure(verification, parallel):
    verification[1].return_value = (True, [])
    llm = MagicMock()
    llm.chat.return_value = Message(role="assistant", tool_calls=[
        ToolCall(id="first", name="desktop_click"), ToolCall(id="cancelled", name="desktop_click")])
    registry = FakeRegistry()
    orch = Orchestrator(llm, registry, fake_context(), parallel_tools=parallel)
    orch.handle("操作桌面")
    assert registry.executed == ["desktop_click"]
    assert orch._unresolved_tool_failures() == ["desktop_click"]
    # §39.1.4：验收只认业务目标。工具失败仍可查询（上一行），但不再注入
    # failed_criteria 当修复项；terminal 熔断照常走 waiting_user。
    assert not any("desktop_click" in criterion
                   for criterion in orch.history[-1].metadata["failed_criteria"])
    assert orch.last_turn_status == "waiting_user"
