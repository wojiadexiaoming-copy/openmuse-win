"""Resume LLM transport failures without replaying completed desktop actions."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
import json

import pytest

from agent.llm_router import RoutingUnavailable
from agent.models import Message, ToolCall, desktop_submission_progress
from agent.orchestrator import Orchestrator
from agent.planner import Plan, PlanStep
from agent.session_store import SessionStore
from agent.tracing import Tracer


BASELINE = {"existing.png": (1, 2, "synthetic-hash")}


def unavailable(category="transport", *, attempted=True):
    return RoutingUnavailable([{"ref": "offline-model@nvidia", "category": category,
                                "request_attempted": attempted}])


def typed_result(*, submitted=False):
    verification = ({"action": "press", "submitted": True} if submitted else
                    {"action": "type", "input_verified": True, "input_allowed": True})
    return Message(role="tool", name="desktop_type", content="[桌面输入已验证] private prompt",
                   metadata={"desktop_verification": verification})


class FakeRegistry:
    _tools = {"desktop_type": None, "desktop_press": None, "desktop_snapshot": None}

    def __init__(self, *, submitted=False):
        self.submitted = submitted
        self.executed = []
        self.scopes = []
        self.closed = 0

    def openai_schemas(self):
        return []

    def begin_turn(self, scope):
        self.scopes.append(scope)

    def end_turn(self):
        self.closed += 1

    def execute(self, name, args):
        self.executed.append((name, dict(args)))
        if name == "desktop_type":
            return typed_result(submitted=self.submitted)
        return Message(role="tool", name=name, content="observed")


def context():
    return SimpleNamespace(build=lambda conversation, tools=None: list(conversation))


def type_call():
    return Message(role="assistant", tool_calls=[ToolCall(id="typed-once", name="desktop_type",
                   arguments={"text": "private prompt", "press_enter": False})])


@pytest.fixture
def verification(monkeypatch):
    capture = MagicMock(return_value=BASELINE)
    verify = MagicMock(return_value=(False, ["缺少本轮图片产物"]))
    monkeypatch.setattr("agent.verifier.capture_artifacts", capture)
    monkeypatch.setattr("agent.verifier.verify_outcome", verify)
    return capture, verify


@pytest.mark.parametrize("category", ["transport", "rate_limit", "service_unavailable"])
def test_transient_llm_failure_preserves_verified_input_and_protocol(verification, category):
    store = SessionStore(":memory:")
    sid = store.new_session("offline")
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [type_call(), unavailable(category)]
    registry = FakeRegistry()
    checkpoints = []
    events = []
    def persist(history):
        checkpoints.append(deepcopy(history))
        store.save_history(sid, history)
    orch = Orchestrator(llm, registry, context(), on_history=persist,
                        on_event=lambda name, data: events.append((name, data)), session_id=sid)
    try:
        reply = orch.handle("生成图片并下载到本地")
        assert orch.last_turn_status == "incomplete"
        assert "输入已读回验证，但尚未提交" in reply and "未发送 Enter" in reply
        assert "已保存的操作记录会用于恢复" in reply and "继续" in reply
        assert llm.chat.call_count == 2
        assert [name for name, _ in registry.executed] == ["desktop_type"]
        assert registry.closed == 1
        assert [message.role for message in orch.history] == ["user", "assistant", "tool", "assistant"]
        tool = orch.history[2]
        assert tool.tool_call_id == "typed-once"
        assert tool.metadata["desktop_verification"]["input_verified"] is True
        assert orch.history[-1].metadata["retryable"] is True
        assert orch.history[-1].metadata["desktop_progress"]["submitted"] is False
        assert orch.history[0].metadata["task_status"] == "incomplete"
        assert orch.history[0].metadata["retryable"] is True
        assert orch.history[0].metadata["artifact_baseline"]["files"] == BASELINE
        assert "缺少本轮图片产物" in orch.history[-1].metadata["failed_criteria"]
        assert verification[0].call_count == verification[1].call_count == 1
        assert any(snapshot[-1].role == "tool" for snapshot in checkpoints)
        assert not any(name == "agent_state" and data["state"] == "error" for name, data in events)
        restored = store.load_history(sid)
        assert restored[2].tool_call_id == "typed-once" and restored[-1].metadata["retryable"] is True
    finally:
        store.db.close()


def test_continue_and_cooldown_pause_keep_original_goal_and_never_replay_input(verification):
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [type_call(), unavailable()]
    registry = FakeRegistry()
    orch = Orchestrator(llm, registry, context(), session_id="same-session")
    goal = "生成图片并下载到本地"
    orch.handle(goal)
    original = orch.history[0]
    cooldown = unavailable("cooldown", attempted=False)
    assert cooldown.retryable is True
    llm.chat.side_effect = [cooldown]
    second = orch.handle("继续")
    assert "输入已读回验证，但尚未提交" in second
    assert orch.last_turn_status == "incomplete" and orch.history[-1].metadata["retryable"] is True
    llm.chat.side_effect = [Message(role="assistant", tool_calls=[ToolCall(id="fresh-shot", name="desktop_snapshot")]),
                            Message(role="assistant", content="完成")]
    verification[1].return_value = (True, [])
    assert orch.handle("继续") == "完成"
    assert [name for name, _ in registry.executed] == ["desktop_type", "desktop_snapshot"]
    assert registry.scopes == ["same-session"] * 3 and registry.closed == 3
    assert verification[0].call_count == 1 and verification[1].call_count == 3
    assert all(call.args[0] == goal and call.kwargs["baseline"] == BASELINE for call in verification[1].call_args_list)
    resumes = [message for message in orch.history if message.role == "user" and message.content == "继续"]
    assert len(resumes) == 2 and all(message.metadata["task_origin_id"] == original.id for message in resumes)
    assert orch.last_turn_status == "done" and "retryable" not in original.metadata
    messages = llm.chat.call_args_list[-2].args[0]
    assert any(message.role == "tool" and message.tool_call_id == "typed-once" for message in messages)


def test_history_reload_can_resume_same_progress_and_baseline(verification):
    store = SessionStore(":memory:")
    sid = store.new_session("offline")
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [type_call(), unavailable()]
    first = Orchestrator(llm, FakeRegistry(), context(), session_id=sid,
                         on_history=lambda history: store.save_history(sid, history))
    try:
        first.handle("生成图片并下载到本地")
        original_id = first.history[0].id
        registry = FakeRegistry()
        resumed_llm = MagicMock(model="offline")
        resumed_llm.chat.side_effect = [Message(role="assistant", tool_calls=[ToolCall(id="fresh", name="desktop_snapshot")]),
                                        Message(role="assistant", content="完成")]
        resumed = Orchestrator(resumed_llm, registry, context(), session_id=sid)
        resumed.history = store.load_history(sid)
        verification[1].return_value = (True, [])
        assert resumed.handle("继续") == "完成"
        assert [name for name, _ in registry.executed] == ["desktop_snapshot"]
        assert verification[0].call_count == 1
        user = next(message for message in resumed.history if message.role == "user" and message.content == "继续")
        assert user.metadata["task_origin_id"] == original_id
    finally:
        store.db.close()


def test_submitted_progress_never_claims_unsubmitted_or_reissues_enter(verification):
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [type_call(), unavailable()]
    registry = FakeRegistry(submitted=True)
    orch = Orchestrator(llm, registry, context())
    reply = orch.handle("生成图片")
    assert "提交已发送，结果尚未确认" in reply
    assert "尚未提交" not in reply
    assert [name for name, _ in registry.executed] == ["desktop_type"]
    assert orch.history[-1].metadata["desktop_progress"]["submitted"] is True


def test_new_task_does_not_report_previous_tasks_desktop_progress(verification):
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [unavailable()]
    orch = Orchestrator(llm, FakeRegistry(), context())
    orch.history = [Message(role="user", content="old task", metadata={"task_status": "done"}),
                    typed_result(), Message(role="assistant", content="old completed")]
    reply = orch.handle("整理新的任务")
    assert "输入已读回验证" not in reply and "提交已发送" not in reply
    assert "desktop_progress" not in orch.history[-1].metadata


def test_planner_transport_failure_returns_retryable_pause_without_tools(verification, monkeypatch):
    monkeypatch.setattr("agent.planner.should_plan", lambda _: True)
    planner = MagicMock()
    planner.make_plan.side_effect = unavailable()
    llm = MagicMock(model="offline")
    registry = FakeRegistry()
    orch = Orchestrator(llm, registry, context(), planner=planner)
    reply = orch.handle("生成图片然后下载")
    assert "当前进度已保存" in reply
    assert orch.last_turn_status == "incomplete" and orch.history[-1].metadata["retryable"] is True
    llm.chat.assert_not_called()
    assert registry.executed == []
    assert verification[1].call_count == 1


def test_mid_plan_failure_does_not_advance_completed_or_pending_steps(verification, monkeypatch):
    monkeypatch.setattr("agent.planner.should_plan", lambda _: True)
    plan = Plan(goal="draw", steps=[PlanStep(desc="查看窗口"), PlanStep(desc="输入提示词"), PlanStep(desc="下载图片")])
    planner = SimpleNamespace(make_plan=lambda _: plan)
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [Message(role="assistant", content="第一步完成"), type_call(), unavailable()]
    registry = FakeRegistry()
    orch = Orchestrator(llm, registry, context(), planner=planner)
    orch.handle("查看窗口然后生成图片并下载")
    assert [step.status for step in plan.steps] == ["done", "incomplete", "pending"]
    assert llm.chat.call_count == 3 and len(registry.executed) == 1
    assert orch.last_turn_status == "incomplete" and verification[1].call_count == 1


@pytest.mark.parametrize("category", ["authentication", "configuration", "missing_key", "protocol", "unexpected"])
def test_nontransient_routing_error_keeps_error_path(verification, category):
    error = unavailable(category)
    assert error.retryable is False
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [type_call(), error]
    registry = FakeRegistry()
    orch = Orchestrator(llm, registry, context())
    with pytest.raises(RoutingUnavailable):
        orch.handle("生成图片")
    assert orch.last_turn_status == "error"
    assert orch.history[2].tool_call_id == "typed-once"
    assert [name for name, _ in registry.executed] == ["desktop_type"]
    assert verification[1].call_count == 0 and registry.closed == 1


@pytest.mark.parametrize("error", [
    RoutingUnavailable([{"ref": "old@nvidia", "category": "transport"}]),
    RuntimeError("RoutingUnavailable retryable timeout"), TimeoutError("transport"),
])
def test_legacy_or_unrelated_exceptions_are_not_silently_recovered(verification, error):
    llm = MagicMock(model="offline")
    llm.chat.side_effect = error
    orch = Orchestrator(llm, FakeRegistry(), context())
    with pytest.raises(type(error)):
        orch.handle("查看状态")
    assert orch.last_turn_status == "error"


@pytest.mark.parametrize("message", [
    Message(role="tool", name="desktop_type", content="input_verified=True", metadata={}),
    Message(role="tool", name="desktop_type", metadata={"desktop_verification": {"action": "type", "input_verified": True}}),
    Message(role="tool", name="desktop_type", metadata={"desktop_verification": {"action": "type", "input_verified": "true", "input_allowed": True}}),
    Message(role="tool", name="desktop_type", metadata={"desktop_verification": {"action": "type", "input_verified": True, "input_allowed": False}}),
])
def test_unverified_or_untyped_input_does_not_claim_ready_to_submit(message):
    assert desktop_submission_progress([message]) is None


def test_later_uncertain_action_cannot_claim_that_input_was_not_submitted():
    uncertain = Message(role="tool", name="desktop_press", content="[桌面验证失败] 后续证据不可取得")
    assert desktop_submission_progress([typed_result(), uncertain]) is None
    focus_false = Message(role="tool", name="desktop_inspect_focus", metadata={"desktop_verification": {"focus_verified": False}})
    assert desktop_submission_progress([typed_result(), focus_false]) is None


def test_pause_audit_is_safe_and_does_not_expose_prompt_or_tool_arguments(verification):
    tracer = Tracer(":memory:")
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [type_call(), unavailable()]
    orch = Orchestrator(llm, FakeRegistry(), context(), tracer=tracer)
    try:
        orch.handle("private task text")
        rows = tracer.db.execute("SELECT data FROM events WHERE event='llm_progress_paused'").fetchall()
        assert len(rows) == 1
        data = json.loads(rows[0][0])
        assert data == {"error_type": "RoutingUnavailable", "failure_category": "transient_llm_failure",
                        "phase": "progress_preserved", "status": "incomplete"}
        assert "private" not in rows[0][0]
    finally:
        tracer.db.close()


def test_transport_failure_during_artifact_repair_stops_further_repair_calls(verification):
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [Message(role="assistant", content="阶段处理结束"), unavailable()]
    orch = Orchestrator(llm, FakeRegistry(), context())
    reply = orch.handle("生成图片并下载到本地")
    assert "当前进度已保存" in reply
    assert llm.chat.call_count == 2
    assert orch.last_turn_status == "incomplete" and orch.history[-1].metadata["retryable"] is True
    assert len([message for message in orch.history if message.name == "verification_feedback"]) == 1
    assert verification[1].call_count == 2


@pytest.mark.parametrize("tool", ["shell_exec", "unknown_action"])
def test_later_unclassified_action_cannot_preserve_unsubmitted_claim(tool):
    later = Message(role="tool", name=tool, content="action completed")
    assert desktop_submission_progress([typed_result(), later]) is None


@pytest.mark.parametrize("a_submitted", [True, False])
def test_resuming_older_task_ignores_newer_completed_tasks_opposite_progress(verification, a_submitted):
    llm = MagicMock(model="offline")
    llm.chat.side_effect = [type_call(), unavailable()]
    registry = FakeRegistry(submitted=a_submitted)
    orch = Orchestrator(llm, registry, context(), session_id="same-session")
    goal_a = "生成第一张图片并下载"
    orch.handle(goal_a)
    original = orch.history[0]
    original_baseline = deepcopy(original.metadata["artifact_baseline"])

    registry.submitted = not a_submitted
    llm.chat.side_effect = [type_call(), Message(role="assistant", content="第二个任务已完成")]
    verification[1].return_value = (True, [])
    orch.handle("创建另一份新的草稿文件")
    task_b = next(message for message in orch.history
                  if message.role == "user" and message.content == "创建另一份新的草稿文件")
    assert task_b.metadata["task_status"] == "done"
    assert original.metadata["task_status"] == "incomplete"

    llm.chat.side_effect = [unavailable()]
    verification[1].return_value = (False, ["缺少第一张图片"])
    reply = orch.handle("继续")
    assert orch.history[-2].metadata["task_origin_id"] == original.id
    assert orch.history[-1].metadata["desktop_progress"]["submitted"] is a_submitted
    assert ("提交已发送，结果尚未确认" in reply) is a_submitted
    assert ("输入已读回验证，但尚未提交" in reply) is (not a_submitted)
    assert orch.last_turn_status == "incomplete"
    assert original.metadata["artifact_baseline"] == original_baseline
    assert verification[1].call_args.args[0] == goal_a
    assert [name for name, _ in registry.executed] == ["desktop_type", "desktop_type"]
    assert llm.chat.call_count == 5


@pytest.mark.parametrize("internal_name", ["plan_step", "verification_feedback", "tool_observation", "context_task_goal"])
def test_internal_users_do_not_change_legacy_tool_task_ownership(internal_name):
    origin = Message(role="user", content="task A")
    other = Message(role="user", content="task B")
    resume = Message(role="user", content="继续", metadata={"task_origin_id": origin.id})
    internal = Message(role="user", name=internal_name, content="internal context")
    history = [origin, typed_result(submitted=True), other, typed_result(), resume,
               internal, typed_result(submitted=True)]
    assert desktop_submission_progress(history, task_origin_id=origin.id)["submitted"] is True
    assert desktop_submission_progress(history, task_origin_id=other.id)["submitted"] is False


def test_explicit_tool_origin_overrides_surrounding_other_task_segment():
    origin = Message(role="user", content="task A")
    other = Message(role="user", content="task B")
    result_a = typed_result(submitted=True)
    result_a.metadata["task_origin_id"] = origin.id
    history = [origin, other, typed_result(), result_a]
    assert desktop_submission_progress(history, task_origin_id=origin.id)["submitted"] is True
    assert desktop_submission_progress(history, task_origin_id=other.id)["submitted"] is False


def test_missing_original_task_keeps_recovery_progress_generic():
    missing_origin = "missing-original-task"
    resume = Message(role="user", content="继续", metadata={"task_origin_id": missing_origin})
    result = typed_result()
    result.metadata["task_origin_id"] = missing_origin
    llm = MagicMock(model="offline")
    orch = Orchestrator(llm, FakeRegistry(), context())
    orch.history = [resume, result]
    orch._active_task_origin_id = missing_origin
    reply = orch._pause_for_llm_failure(unavailable())
    assert "desktop_progress" not in reply.metadata
    assert "输入已读回验证" not in reply.content and "提交已发送" not in reply.content
    assert "当前进度已保存" in reply.content and orch.last_turn_status == "incomplete"
    llm.chat.assert_not_called()
