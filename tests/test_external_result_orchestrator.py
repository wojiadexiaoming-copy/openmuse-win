"""Offline integration of submitted desktop work and independent visual confirmation."""
import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from agent.context import ContextBuilder
from agent.llm_router import RoutingUnavailable
from agent.models import ImagePart, Message, ToolCall
from agent.orchestrator import Orchestrator
from agent.planner import Plan, PlanStep
from agent.result_observer import VisualResultObserver
from agent.tools_registry import ToolDefinition, ToolRegistry

GOAL = "用豆包桌面程序生成一张心理学的海报"


def shot(snapshot_id="desk-current", **overrides):
    buf = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(buf, format="PNG")
    fields = dict(role="tool", name="desktop_snapshot", images=[ImagePart(data=buf.getvalue())],
                  metadata={"desktop_observation": {"snapshot_id": snapshot_id,
                            "image_size": [12, 8], "window": {"hwnd": 7, "foreground": True, "title": "豆包"}}})
    fields.update(overrides)
    return Message(**fields)


def submission(submitted=True):
    return Message(role="tool", name="desktop_type", content="[输入已提交]",
                   metadata={"desktop_verification": {"submitted": submitted,
                             "generation_verified": False, "action": "press"}})


def observed(status="completed", count=4):
    import json
    return Message(role="assistant", content=json.dumps({
        "status": status, "goal_match": status != "unknown", "result_count": count}))


def harness(tmp_path, monkeypatch, responses=None, main_responses=None, snapshot=None):
    monkeypatch.chdir(tmp_path)
    events, waits, shots = [], [], []
    tools = ToolRegistry()
    tools.set_approval(SimpleNamespace(check=lambda *a, **k: True))

    def register(name, func):
        tools.register(ToolDefinition(name=name, description=name,
                       parameters={"type": "object", "properties": {}}, func=func))

    def capture(args):
        shots.append(dict(args))
        return snapshot(args) if snapshot else shot("desk-" + str(len(shots)))

    register("desktop_type", lambda _: submission())
    register("desktop_wait", lambda args: waits.append(args) or "[等待结束]")
    register("desktop_snapshot", capture)
    main = MagicMock()
    main.model = "offline"
    main.chat.side_effect = main_responses or [
        Message(role="assistant", tool_calls=[ToolCall(id="submit", name="desktop_type",
                                                     arguments={"press_enter": True})]),
    ]
    vision = MagicMock()
    vision.chat.side_effect = responses or [observed()]
    orch = Orchestrator(main, tools, ContextBuilder(), session_id="session-A",
                        result_observer=VisualResultObserver(vision),
                        on_event=lambda name, data: events.append((name, data)))
    return SimpleNamespace(orch=orch, main=main, vision=vision, events=events, waits=waits, shots=shots)


def assert_protocol(history):
    calls, results = [], []
    for message in history:
        calls.extend(call.id for call in message.tool_calls)
        if message.role == "tool":
            results.append(message.tool_call_id)
    assert sorted(calls) == sorted(results)


def test_pure_generation_finishes_without_download_or_another_planner_call(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    reply = h.orch.handle(GOAL)
    assert "已核验" in reply and "4张" in reply
    assert h.orch.last_turn_status == "done"
    assert h.main.chat.call_count == h.vision.chat.call_count == 1
    assert h.waits == [{"seconds": 8}] and len(h.shots) == 1
    assert list(tmp_path.iterdir()) == []
    origin = h.orch.history[0]
    assert origin.metadata["external_result"]["task_origin_id"] == origin.id
    assert origin.metadata["failed_criteria"] == []
    progress = [data for name, data in h.events if name == "task_progress"]
    assert [p["stage"] for p in progress] == ["submitted", "result_verified"]
    assert all(p["task_origin_id"] == origin.id and p["session_id"] == "session-A" for p in progress)
    for msg in h.orch.history:
        if msg.role == "tool":
            assert msg.metadata["task_origin_id"] == origin.id
    payload = h.vision.chat.call_args.args[0]
    assert len(payload) == 2 and len(payload[1].images) == 1
    assert GOAL in payload[1].content
    assert_protocol(h.orch.history)


def test_generation_polls_fresh_evidence_until_completed(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, responses=[observed("generating", 0), observed()])
    assert "已核验" in h.orch.handle(GOAL)
    assert h.main.chat.call_count == 1 and h.vision.chat.call_count == 2
    assert h.waits == [{"seconds": 8}] * 2 and len(h.shots) == 2
    assert h.orch.history[0].metadata["external_result"]["snapshot_id"] == "desk-2"


@pytest.mark.parametrize("response,phrase,probes", [
    (observed("unknown", 0), "尚不能可靠证明", 1),
    (observed("error", 0), "生成出错", 1),
    (RuntimeError("private failure"), "结果确认模型暂不可用", 1),
    (observed("generating", 0), "仍在生成", 4),
])
def test_pending_result_is_incomplete_retryable_without_resubmission(tmp_path, monkeypatch, response, phrase, probes):
    h = harness(tmp_path, monkeypatch, responses=[response] * probes)
    reply = h.orch.handle(GOAL)
    assert phrase in reply and "不会自动重复提交" in reply
    assert h.orch.last_turn_status == "incomplete"
    assert h.main.chat.call_count == 1 and h.vision.chat.call_count == probes
    assert len(h.shots) == len(h.waits) == probes
    assert sum(r["tool"] == "desktop_type" for r in h.orch.last_turn_tool_records) == 1
    assert h.orch.history[0].metadata["retryable"] is True
    assert h.orch.history[-1].metadata["result_control"]["reason"] == "result_confirmation_pending"
    assert_protocol(h.orch.history)


def test_confirmed_generation_does_not_finish_requested_download(tmp_path, monkeypatch):
    transient = RoutingUnavailable([{"ref": "offline@nvidia", "category": "transport",
                                      "request_attempted": True}])
    h = harness(tmp_path, monkeypatch, main_responses=[
        Message(role="assistant", tool_calls=[ToolCall(id="submit", name="desktop_type")]), transient])
    reply = h.orch.handle(GOAL + "并下载到本地")
    assert "已核验目标应用中的生成结果" in reply and "本地保存或后续交付尚未验收" in reply
    assert "本地控制模型" in reply
    assert h.orch.last_turn_status == "incomplete" and h.main.chat.call_count == 2
    assert h.vision.chat.call_count == 1
    assert "本轮" in h.orch.history[0].metadata["failed_criteria"][0]


@pytest.mark.parametrize("change", [
    {"snapshot_id": "old"}, {"task_origin_id": "other-task"}, {"source": "screenshot"},
    {"goal_match": "true"}, {"result_count": True}, {"result_count": 0},
])
def test_foreign_or_malformed_observation_never_marks_done(tmp_path, monkeypatch, change):
    h = harness(tmp_path, monkeypatch)

    def observe(goal, evidence, origin):
        result = dict(status="completed", goal_match=True, result_count=1,
                      snapshot_id=evidence.metadata["desktop_observation"]["snapshot_id"],
                      task_origin_id=origin, source="visual_observer")
        result.update(change)
        return result

    h.orch.result_observer = SimpleNamespace(observe=observe)
    assert "重新观察" in h.orch.handle(GOAL)
    assert h.orch.last_turn_status == "incomplete" and h.main.chat.call_count == 1


def test_resume_captures_current_result_without_replaying_submission(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, responses=[observed("unknown", 0), observed()])
    assert "重新观察" in h.orch.handle(GOAL)
    origin = h.orch.history[0]
    saved = h.orch.history
    # Simulate reloading persisted history into a new engine.
    restarted = Orchestrator(h.main, h.orch.tools, ContextBuilder(), session_id="session-A",
                             result_observer=VisualResultObserver(h.vision))
    restarted.history = saved
    assert "已核验" in restarted.handle("继续")
    assert restarted.last_turn_status == "done" and h.main.chat.call_count == 1
    assert h.shots[-1] == {"window_title": "豆包"}
    assert len(h.waits) == 1
    assert origin.metadata["task_status"] == "done"
    assert_protocol(restarted.history)


def test_terminal_snapshot_stops_confirmation_and_preserves_tool_protocol(tmp_path, monkeypatch):
    def stopped(args):
        return Message(role="tool", content="[桌面停止]", metadata={
            "tool_control": {"reason": "desktop_failsafe", "retryable": False, "requires_user": True}})
    h = harness(tmp_path, monkeypatch, snapshot=stopped)
    assert "四角停止保护" in h.orch.handle(GOAL)
    assert h.orch.last_turn_status == "waiting_user"
    h.vision.chat.assert_not_called()
    assert h.main.chat.call_count == 1
    assert_protocol(h.orch.history)


def test_verified_pure_generation_skips_unsolicited_save_plan(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    plan = Plan(goal=GOAL, steps=[
        PlanStep(desc="生成海报", done_when="已生成"),
        PlanStep(desc="下载到本地", done_when="文件存在"),
    ])
    h.orch.planner = SimpleNamespace(make_plan=lambda _: plan)
    monkeypatch.setattr("agent.planner.should_plan", lambda _: True)
    assert "已核验" in h.orch.handle(GOAL)
    assert [step.status for step in plan.steps] == ["done", "skipped"]


def test_unsubmitted_input_does_not_invoke_confirmation(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    h.orch.tools._tools["desktop_type"].func = lambda _: submission(False)
    h.main.chat.side_effect = [
        Message(role="assistant", tool_calls=[ToolCall(id="type", name="desktop_type")]),
        Message(role="assistant", content="请手动协助确认窗口"),
    ]
    h.orch.handle(GOAL)
    h.vision.chat.assert_not_called()
    assert h.shots == h.waits == []
    assert not any(n == "task_progress" for n, _ in h.events)



def test_last_planner_iteration_can_still_confirm_completed_generation(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    h.orch.max_iters = 1
    assert "已核验" in h.orch.handle(GOAL)
    assert h.orch.last_turn_status == "done" and h.main.chat.call_count == 1


def test_missing_snapshot_is_reported_separately_from_model_failure(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, snapshot=lambda _: "[截图失败] offline")
    reply = h.orch.handle(GOAL)
    assert "尚未取得可用于核验的目标窗口截图" in reply
    assert "模型暂不可用" not in reply and h.orch.last_turn_status == "incomplete"
    h.vision.chat.assert_not_called()
    assert h.main.chat.call_count == 1


def test_missing_snapshot_id_cannot_be_accepted_by_equality_of_none(tmp_path, monkeypatch):
    def no_id(args):
        evidence = shot()
        evidence.metadata["desktop_observation"].pop("snapshot_id")
        return evidence
    h = harness(tmp_path, monkeypatch, snapshot=no_id)
    h.orch.result_observer = SimpleNamespace(observe=lambda goal, evidence, origin: {
        "status": "completed", "goal_match": True, "result_count": 4,
        "snapshot_id": None, "task_origin_id": origin, "source": "visual_observer"})
    reply = h.orch.handle(GOAL)
    assert "尚不能可靠证明" in reply and h.orch.last_turn_status == "incomplete"


def test_confirmation_uses_original_goal_inside_plan_and_keeps_download_pending(tmp_path, monkeypatch):
    transient = RoutingUnavailable([{"ref": "offline@nvidia", "category": "transport",
                                      "request_attempted": True}])
    h = harness(tmp_path, monkeypatch, main_responses=[
        Message(role="assistant", tool_calls=[ToolCall(id="submit", name="desktop_type")]), transient])
    goal = GOAL + "并下载到本地"
    plan = Plan(goal=goal, steps=[PlanStep(desc="生成图片", done_when="可见"),
                                PlanStep(desc="下载", done_when="本地文件")])
    h.orch.planner = SimpleNamespace(make_plan=lambda _: plan)
    monkeypatch.setattr("agent.planner.should_plan", lambda _: True)
    assert "本地保存或后续交付尚未验收" in h.orch.handle(goal)
    assert [s.status for s in plan.steps] == ["incomplete", "pending"]
    payload = h.vision.chat.call_args.args[0]
    assert "[当前执行阶段" not in payload[1].content and goal in payload[1].content



def test_resume_after_snapshot_failure_retries_observation_without_new_submission(tmp_path, monkeypatch):
    attempts = []

    def flaky(args):
        attempts.append(args)
        return "[截图失败] offline" if len(attempts) == 1 else shot()

    h = harness(tmp_path, monkeypatch, snapshot=flaky)

    def submitted(args):
        msg = submission()
        msg.metadata["desktop_observation"] = {"window": {"title": "豆包"}}
        return msg

    h.orch.tools._tools["desktop_type"].func = submitted
    assert "尚未取得" in h.orch.handle(GOAL)
    assert "已核验" in h.orch.handle("继续")
    assert attempts == [{}, {"window_title": "豆包"}]
    assert h.main.chat.call_count == 1 and h.vision.chat.call_count == 1
    assert h.orch.last_turn_status == "done"


def test_resume_confirmation_precedes_any_new_plan_request(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, responses=[observed("unknown", 0), observed()])
    h.orch.handle(GOAL)
    planner = MagicMock()
    planner.make_plan.side_effect = AssertionError("must not replan an already submitted pure generation")
    h.orch.planner = planner
    monkeypatch.setattr("agent.planner.should_plan", lambda _: True)
    assert "已核验" in h.orch.handle("继续")
    planner.make_plan.assert_not_called()
    assert h.main.chat.call_count == 1
