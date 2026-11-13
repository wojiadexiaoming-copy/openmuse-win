"""v3.1 回归：世界状态指纹、无变化动作、wait_subagent、防丢提醒、超时清退。

对应实跑事故：截图与 snapshot 交替观察被当成持续前进；点击无变化仍续杯；
子 Agent 完成后只能靠 poll 查到、主 Agent 反复 wait/poll 空转、超时不取消。
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.context import ContextBuilder
from agent.models import Message, ToolCall
from agent.orchestrator import Orchestrator
from agent.subagent import SubagentResult, SubagentTask
from agent.tools_registry import ToolDefinition, ToolRegistry


def _reg():
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="noop", description="t",
                                parameters={"type": "object", "properties": {}},
                                func=lambda _: "ok"))
    reg.set_approval(SimpleNamespace(check=lambda *a, **k: True))
    return reg


def _orch(llm=None, **kw):
    llm = llm or MagicMock()
    llm.model = "offline"
    return Orchestrator(llm, _reg(), ContextBuilder(max_tokens=4000), **kw)


def _result(task_id="t1", summary="查到 K1622", status="done"):
    return SubagentResult(
        task_id=task_id, status=status, summary=summary,
        evidence=[{"type": "tool_record", "tool": "browser_evaluate",
                   "args": {"script": "tickets()"},
                   "result": "K1622 天津→深圳东 22:16→06:00"}],
        confidence="high")


class _FakeSubMgr:
    def __init__(self, wait_result=None, poll_result=None):
        self.wait_result = wait_result
        self.poll_result = poll_result
        self.calls = []

    def poll(self, task_id):
        self.calls.append(("poll", task_id))
        return self.poll_result

    def wait(self, task_id, timeout_sec=60.0):
        self.calls.append(("wait", task_id, timeout_sec))
        return self.wait_result

    def cancel(self, task_id):
        self.calls.append(("cancel", task_id))


def _pending_task(tid="t1"):
    return {tid: {"brief": "查天津→深圳车次", "spawn_step": 0,
                  "last_poll_step": 0, "reminded": False,
                  "task": SubagentTask(brief="查天津→深圳车次")}}


def test_world_fingerprint_is_tool_neutral():
    page = ("当前页面: https://example.com/list 标题: 车次列表\n"
            "[ref=e12] K1622 天津→深圳东 22:16")
    snapshot = Orchestrator._world_state_fingerprint("browser_snapshot", page)
    evaluate = Orchestrator._world_state_fingerprint("browser_evaluate", page)
    assert snapshot == evaluate, "同一页面换工具观察不得算成新进展"

    changed = Orchestrator._world_state_fingerprint(
        "browser_snapshot", page.replace("K1622", "K1623"))
    assert changed != snapshot
    assert Orchestrator._digest_family(snapshot) == "worldt"
    assert Orchestrator._digest_family("not-a-world-digest") == "data"


def test_world_state_first_frame_is_baseline_only():
    """第一次看到页面只是建立基线；同族内容变化才算前进一步。"""
    first = Orchestrator._world_state_fingerprint(
        "browser_snapshot", "当前页面: https://a.com 标题: A\n列表为空")
    same = Orchestrator._world_state_fingerprint(
        "browser_snapshot", "当前页面: https://a.com 标题: A\n列表为空")
    moved = Orchestrator._world_state_fingerprint(
        "browser_snapshot", "当前页面: https://a.com 标题: A\n出现 K1622")
    data = Orchestrator._result_fingerprint("接口返回 1 趟车")
    assert Orchestrator._count_new_info_steps([{first}]) == 0
    assert Orchestrator._count_new_info_steps([{first}, {same}]) == 0
    assert Orchestrator._count_new_info_steps([{first}, {moved}]) == 1
    assert Orchestrator._count_new_info_steps([{data}]) == 1

    # 文本观察切到截图观察：截图族首帧同样只建基线，不能冒充进展
    shot = Orchestrator._world_state_fingerprint(
        "browser_screenshot", "当前页面: https://a.com 标题: A")
    assert Orchestrator._digest_family(shot) == "worldi"
    assert Orchestrator._count_new_info_steps([{first}, {shot}]) == 0


def test_click_without_page_change_does_not_count_as_progress():
    content = "[点击成功] 点击前后页面 URL 与内容均无任何变化"
    assert Orchestrator._result_blocks_progress(content, "browser_click")
    tc = ToolCall(id="c1", name="browser_click", arguments={"ref": "e1"})
    result = SimpleNamespace(tool_call_id="c1", content=content)
    assert Orchestrator._progress_signature([tc], [result]) == ()
    orch = _orch()
    assert orch._progress_allows_extension(
        [()], [0], probe_streak=0, new_info_steps=0) is False


def test_wait_subagent_returns_evidence_and_clears_pending():
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[ToolCall(
            id="w1", name="wait_subagent",
            arguments={"task_id": "t1", "timeout_sec": 30})]),
        Message(role="assistant", content="已拿到子任务结果"),
    ]
    orch = _orch(llm)
    fake = _FakeSubMgr(wait_result=_result())
    orch.sub_mgr = fake
    orch._pending_subagents = _pending_task()

    orch.run_turn(Message(role="user", content="等一下子任务"))

    tool_msgs = [m for m in orch.history
                 if m.role == "tool" and m.name == "wait_subagent"]
    assert tool_msgs, "wait_subagent 必须走主循环特殊路径"
    assert "K1622" in tool_msgs[0].content, "结果必须带证据原文"
    assert "通过验收" in tool_msgs[0].content
    assert ("wait", "t1", 30.0) in fake.calls
    assert "t1" not in orch._pending_subagents, "完成即移出防丢追踪"


def test_poll_after_auto_return_only_returns_receipt():
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[ToolCall(
            id="p1", name="poll_subagent", arguments={"task_id": "t1"})]),
        Message(role="assistant", content="收到"),
    ]
    orch = _orch(llm)
    orch.sub_mgr = _FakeSubMgr(poll_result=_result())
    orch._pending_subagents = _pending_task()

    orch.run_turn(Message(role="user", content="查一下子任务"))

    tool_msgs = [m for m in orch.history
                 if m.role == "tool" and m.name == "poll_subagent"]
    assert tool_msgs
    assert "结果已自动回传" in tool_msgs[0].content
    assert "K1622" not in tool_msgs[0].content, "自动回传过的结果不得重复灌全文"


def test_stale_reminder_resets_after_poll():
    orch = _orch()
    orch.sub_mgr = None  # 只测超期提醒，不触发自动 poll
    orch._pending_subagents = _pending_task()
    orch._turn_step = 5
    orch._mark_subagent_polled("t1")
    assert orch._check_stale_subagents(9) == ""
    reminder = orch._check_stale_subagents(10)
    assert "防丢提醒" in reminder and "t1" in reminder
    assert orch._check_stale_subagents(11) == "", "同一超期只提醒一次"


def test_exhaustion_cancels_unfinished_subagents():
    orch = _orch()
    fake = _FakeSubMgr()
    orch.sub_mgr = fake
    orch._pending_subagents = _pending_task()
    orch._partial_delivery = lambda *a, **k: ""

    orch._exhausted_with_partial("步骤到", [], reason="steps")

    assert ("cancel", "t1") in fake.calls
    assert orch._pending_subagents == {}


def test_wait_is_blocked_when_less_than_20pct_budget_left():
    orch = _orch()
    orch._turn_deadline = time.time() + 10
    orch._turn_budget_total = 100.0
    blocked = orch._wait_blocked_by_deadline("wait_subagent")
    assert "禁止阻塞等待" in blocked

    orch._turn_deadline = time.time() + 90
    assert orch._wait_blocked_by_deadline("wait_subagent") == ""

    orch._turn_deadline = None
    assert orch._wait_blocked_by_deadline("wait_subagent") == ""


def test_pending_subagent_blocks_normal_turn_end_with_nudge():
    """主 Agent 不能丢下运行中的子任务直接收尾（fire-and-forget 防早退）。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", content="已经查好了"),
        Message(role="assistant", tool_calls=[ToolCall(
            id="w1", name="wait_subagent",
            arguments={"task_id": "t1", "timeout_sec": 5})]),
        Message(role="assistant", content="完成"),
    ]
    orch = _orch(llm)
    fake = _FakeSubMgr(wait_result=_result())
    orch.sub_mgr = fake
    orch._pending_subagents = _pending_task()

    orch.run_turn(Message(role="user", content="查一下车次"))

    nudges = [m for m in orch.history
              if getattr(m, "name", "") == "anti_early_exit"]
    assert nudges and "未收尾" in nudges[0].content
    assert ("wait", "t1", 5.0) in fake.calls


def test_execute_tool_refuses_wait_near_deadline_before_touching_submgr():
    orch = _orch()
    orch._turn_deadline = time.time() + 5
    orch._turn_budget_total = 100.0
    fake = _FakeSubMgr(wait_result=_result())
    orch.sub_mgr = fake

    msg = orch._execute_tool("wait_subagent", {"task_id": "t1"})
    assert "禁止阻塞等待" in msg.content
    assert fake.calls == []
