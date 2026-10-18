"""Tests for Milestone 4: Orchestrator Governance, Subagent Quotas, and Browser Lease Arbitration."""
import threading
import time
from unittest.mock import MagicMock

import pytest

from agent.context import ContextBuilder
from agent.models import Message, ToolCall
from agent.orchestrator import Orchestrator
from agent.subagent import SubagentManager, SubagentResult, SubagentTask, make_subagent_tools
from agent.tools_registry import ToolRegistry
from tools.browser.worker import BrowserWorker


def test_orchestrator_cancellation():
    mock_llm = MagicMock()
    mock_llm.model = "test-model"
    # LLM keeps returning tool calls
    mock_llm.generate.return_value = Message(
        role="assistant",
        content="I will call a tool",
        tool_calls=[ToolCall(id="c1", name="dummy_tool", arguments={})]
    )

    tools = ToolRegistry()
    called = []
    def dummy_tool(args):
        called.append(True)
        return "tool ok"

    from agent.tools_registry import ToolDefinition
    tools.register(ToolDefinition(
        name="dummy_tool",
        description="test dummy",
        parameters={"type": "object", "properties": {}},
        func=dummy_tool,
    ))

    orch = Orchestrator(
        llm=mock_llm,
        tools=tools,
        ctx=ContextBuilder(),
    )

    # Cancel before running
    orch.cancel()
    assert orch.is_cancelled() is True

    # run_turn should immediately exit with cancelled reply
    reply = orch.run_turn(Message(role="user", content="hello"))
    assert "[任务已取消]" in reply.content
    assert orch.last_turn_status == "cancelled"

    # Reset cancel
    orch.reset_cancel()
    assert orch.is_cancelled() is False


def test_subagent_depth_quota():
    mgr = SubagentManager(make_orchestrator=lambda task: MagicMock())

    # Depth 0 and 1 allowed
    t0 = SubagentTask(brief="task 0", depth=0)
    id0 = mgr.spawn(t0)
    assert id0.startswith("sub-")

    t1 = SubagentTask(brief="task 1", depth=1)
    id1 = mgr.spawn(t1)
    assert id1.startswith("sub-")

    # Depth 2 should be rejected
    t2 = SubagentTask(brief="task 2", depth=2)
    with pytest.raises(ValueError, match="嵌套深度达到上限"):
        mgr.spawn(t2)

    # Test via make_subagent_tools
    sub_tools = make_subagent_tools(mgr)
    spawn_tool = next(t for t in sub_tools if t.name == "spawn_subagent")
    res = spawn_tool.func({"brief": "deep task", "depth": 2})
    assert "嵌套深度达到上限" in res


def test_subagent_cancellation_and_cleanup():
    cancelled_event = threading.Event()

    class StoppableOrch:
        def __init__(self, task):
            self.task = task
            self._cancel = threading.Event()

        def cancel(self):
            self._cancel.set()
            cancelled_event.set()

        def cleanup(self):
            self.cancel()

        def handle(self, prompt):
            # Sleep wait for cancel
            for _ in range(50):
                if self._cancel.is_set():
                    return "[已取消]"
                time.sleep(0.05)
            return "timeout waiting"

    mgr = SubagentManager(make_orchestrator=StoppableOrch)
    task = SubagentTask(brief="long sub task", depth=0)
    tid = mgr.spawn(task)

    time.sleep(0.1)
    # Cancel the task
    mgr.cancel(tid)
    assert cancelled_event.is_set()

    res = mgr.poll(tid)
    assert res is not None
    assert res.status == "failed"
    assert "已主动取消" in res.summary


def test_browser_lease_arbitration():
    # Use __new__ to bypass Playwright instantiation in unit tests
    worker = BrowserWorker.__new__(BrowserWorker)
    worker._lease_owner = None
    worker._lease_lock = threading.Lock()

    # Initial check
    assert worker.current_lease_owner() is None
    assert worker.check_lease() is True
    assert worker.check_lease("main") is True

    # Main acquires lease
    assert worker.acquire_lease("main", timeout_sec=0.5) is True
    assert worker.current_lease_owner() == "main"
    assert worker.check_lease("main") is True
    assert worker.check_lease("sub:1") is False

    # Subagent 1 tries to acquire and fails (timeout)
    assert worker.acquire_lease("sub:1", timeout_sec=0.1) is False

    # Main releases lease
    assert worker.release_lease("main") is True
    assert worker.current_lease_owner() is None

    # Subagent 1 acquires lease
    assert worker.acquire_lease("sub:1", timeout_sec=0.5) is True
    assert worker.current_lease_owner() == "sub:1"

    # Subagent 1 releases lease
    assert worker.release_lease("sub:1") is True
    assert worker.current_lease_owner() is None
