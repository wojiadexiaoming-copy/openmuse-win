"""v3.2 回归：子任务结果跨进程持久化、未知 task_id 快速失败、历史校验、异常清退。

对应实跑事故：UI 重启后主 Agent 拿着上个进程的 task_id 反复 wait/poll，
poll 对未知 id 返回 None 被当成"执行中"，白等 150 秒后只能靠旧数据作答。
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.context import ContextBuilder
from agent.llm_router import RoutingUnavailable
from agent.models import Message
from agent.orchestrator import Orchestrator
from agent.subagent import (
    SubagentManager, SubagentResult, SubagentStore, SubagentTask,
    make_subagent_tools,
)
from agent.tools_registry import ToolDefinition, ToolRegistry


class _QuickOrch:
    def __init__(self, task):
        self.task = task
        self.last_turn_status = "done"
        self._turn_exhausted = False
        self.last_turn_tool_records = [{
            "tool": "browser_evaluate", "args": {"script": "tickets()"},
            "status": "ok", "result": "K1622 天津→深圳东 22:16",
        }]

    def handle(self, prompt):
        return "查到 K1622"


class _CancelFake:
    def __init__(self):
        self.calls = []

    def cancel(self, task_id):
        self.calls.append(("cancel", task_id))

    def knows(self, task_id):
        return True

    def is_stale(self, task_id):
        return False

    def poll(self, task_id):
        return None


def _mgr(tmp_path=None, **kw):
    return SubagentManager(
        make_orchestrator=_QuickOrch,
        enforce_min_memory=False,
        store_path=(tmp_path / "subagents.db") if tmp_path else None,
        **kw,
    )


def _orch(llm=None):
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="noop", description="t",
                                parameters={"type": "object", "properties": {}},
                                func=lambda _: "ok"))
    reg.set_approval(SimpleNamespace(check=lambda *a, **k: True))
    llm = llm or MagicMock()
    llm.model = "offline"
    return Orchestrator(llm, reg, ContextBuilder(max_tokens=4000))


def test_done_result_survives_manager_restart(tmp_path):
    mgr = _mgr(tmp_path)
    try:
        tid = mgr.spawn(SubagentTask(brief="查 K1622", use_process=False))
        res = mgr.wait(tid, 10)
        assert res is not None and res.status == "done"
    finally:
        mgr.shutdown()

    mgr2 = _mgr(tmp_path)
    try:
        restored = mgr2.poll(tid)
        assert restored is not None, "重启后不得把已完成任务当成未知/执行中"
        assert restored.status == "done"
        assert "K1622" in restored.summary
        assert restored.evidence, "证据也要一起恢复"
        assert mgr2.knows(tid)
        assert not mgr2.is_stale(tid)
    finally:
        mgr2.shutdown()


def test_unfinished_task_from_previous_process_is_interrupted(tmp_path):
    store = SubagentStore(tmp_path / "subagents.db")
    store.create("sub-aaaaaaaaaa", "昨天的查价任务", "browser")
    store.mark_running("sub-aaaaaaaaaa")
    store.close()

    mgr = _mgr(tmp_path)
    try:
        assert mgr.knows("sub-aaaaaaaaaa")
        assert mgr.is_stale("sub-aaaaaaaaaa")
        res = mgr.poll("sub-aaaaaaaaaa")
        assert res is not None and res.status == "failed"
        assert "进程重启" in res.summary
    finally:
        mgr.shutdown()

    # 再次重启：中断标记必须仍然有效，不能因为已写成 failed 就丢掉 stale 语义
    mgr2 = _mgr(tmp_path)
    try:
        assert mgr2.is_stale("sub-aaaaaaaaaa")
        assert mgr2.poll("sub-aaaaaaaaaa").status == "failed"
    finally:
        mgr2.shutdown()


def test_unknown_task_id_fails_fast_instead_of_fake_running():
    mgr = _mgr()
    try:
        start = time.time()
        res = mgr.wait("sub-ffffffffff", timeout_sec=30)
        assert res is not None
        assert res.status == "failed"
        assert "任务不存在或已随进程重启失效" in res.summary
        assert time.time() - start < 2, "未知 id 不得空等到超时"

        polled = mgr.poll("sub-ffffffffff")
        assert polled is not None and polled.status == "failed"
        assert not mgr.knows("sub-ffffffffff")

        wait_tool = next(t for t in make_subagent_tools(mgr)
                         if t.name == "wait_subagent")
        text = wait_tool.func({"task_id": "sub-ffffffffff", "timeout_sec": 30})
        assert "任务不存在或已随进程重启失效" in text
    finally:
        mgr.shutdown()


def test_history_referencing_unknown_task_id_gets_system_notice():
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.return_value = Message(role="assistant", content="我会重新派发")
    orch = _orch(llm)
    orch.sub_mgr = _CancelFake()
    orch.sub_mgr.knows = lambda _tid: False
    orch._pending_subagents = {"sub-deadbeef00": {
        "brief": "旧任务", "spawn_step": 0, "last_poll_step": 0, "reminded": False}}
    orch.history.append(Message(
        role="assistant", content="正在等待 sub-deadbeef00 的结果"))

    orch.run_turn(Message(role="user", content="继续"))

    notices = [m for m in orch.history
               if m.role == "system" and "[子任务失效]" in (m.content or "")]
    assert notices, "历史中的失效 task_id 必须提示模型重新派发"
    assert "sub-deadbeef00" in notices[0].content
    assert "sub-deadbeef00" not in orch._pending_subagents
    assert "spawn_subagent" in notices[0].content


def test_history_with_live_task_id_is_not_flagged():
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.return_value = Message(role="assistant", content="好")
    orch = _orch(llm)
    orch.sub_mgr = _CancelFake()
    orch.history.append(Message(
        role="assistant", content="正在等待 sub-abcdef1234 的结果"))

    orch.run_turn(Message(role="user", content="继续"))

    assert not [m for m in orch.history
                if m.role == "system" and "[子任务失效]" in (m.content or "")]


def test_handle_exception_cancels_pending_subagents():
    orch = _orch()
    fake = _CancelFake()
    orch.sub_mgr = fake
    orch._pending_subagents = {"t1": {
        "brief": "查车次", "spawn_step": 0, "last_poll_step": 0, "reminded": False}}
    orch.run_turn = MagicMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        orch.handle("hi")

    assert ("cancel", "t1") in fake.calls
    assert orch._pending_subagents == {}


def test_llm_failure_pause_cancels_pending_subagents():
    orch = _orch()
    fake = _CancelFake()
    orch.sub_mgr = fake
    orch._pending_subagents = {"t1": {
        "brief": "查车次", "spawn_step": 0, "last_poll_step": 0, "reminded": False}}
    truncated = SubagentResult(task_id="t1", status="partial", summary="部分结果")
    fake.poll = lambda _tid: truncated

    error = RoutingUnavailable([{
        "ref": "m@p", "category": "transport", "request_attempted": True}])
    assert error.retryable is True
    reply = orch._pause_for_llm_failure(error)

    assert "暂时无法响应" in reply.content
    assert ("cancel", "t1") in fake.calls
    assert orch._pending_subagents == {}
