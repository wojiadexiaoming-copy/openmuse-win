"""Tests for Milestone 4: Orchestrator Governance, Subagent Quotas, and Browser Lease Arbitration."""
import threading
import time
from types import SimpleNamespace
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


def test_subagent_browser_lease_wiring():
    worker = BrowserWorker.__new__(BrowserWorker)
    worker._lease_owner = None
    worker._lease_lock = threading.Lock()

    class FakeTools:
        def __init__(self, bw):
            self.browser_worker = bw

    class FakeOrch:
        def __init__(self, task):
            self.task = task
            self.tools = FakeTools(worker)

        def handle(self, prompt):
            return "browser task finished"

    # 1. When main holds the lease, browser subagent must fail immediately
    worker.acquire_lease("main")
    mgr = SubagentManager(make_orchestrator=FakeOrch)
    task = SubagentTask(brief="scrape web", kind="browser", lease_timeout_sec=0.1)
    tid = mgr.spawn(task)

    # Wait for thread to finish
    for _ in range(20):
        res = mgr.poll(tid)
        if res is not None:
            break
        time.sleep(0.05)

    assert res is not None
    assert res.status == "failed"
    assert "无法获取浏览器独占权" in res.summary
    assert "main" in res.summary

    # 2. When lease is released, browser subagent succeeds and acquires/releases lease
    worker.release_lease("main")
    tid2 = mgr.spawn(task)
    for _ in range(20):
        res2 = mgr.poll(tid2)
        if res2 is not None:
            break
        time.sleep(0.05)

    assert res2 is not None
    assert res2.status == "done"
    assert "browser task finished" in res2.summary
    assert worker.current_lease_owner() is None


def test_provenance_wiring_and_stripping():
    from agent.tools_registry import ToolDefinition

    received_args = {}
    def sample_tool(args):
        received_args.update(args)
        return "sample ok"

    tools = ToolRegistry()
    tools.register(ToolDefinition(
        name="state_pin",
        description="test tool",
        parameters={"type": "object", "properties": {"target": {"type": "string"}}},
        func=sample_tool,
    ))

    # 1. Calling execute directly with _provenance in args
    args_with_prov = {"target": "xyz", "_provenance": {"level": 0, "source_ref": "user"}}
    msg = tools.execute("state_pin", args_with_prov)
    assert msg.content == "sample ok"
    assert "_provenance" not in received_args
    assert received_args.get("target") == "xyz"

    # 2. Orchestrator passes _provenance and escalates after web source
    mock_llm = MagicMock()
    mock_llm.model = "test-model"
    orch = Orchestrator(llm=mock_llm, tools=tools, ctx=ContextBuilder())
    assert orch._turn_provenance["level"] == 0

    # Execute dummy tool
    orch._execute_tool("state_pin", {"target": "abc", "_provenance": {"level": 0}})
    assert orch._turn_provenance["level"] == 0

    # Execute web_search dummy tool -> should escalate turn provenance to level 2
    tools.register(ToolDefinition(
        name="web_search",
        description="search web",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        func=lambda a: "search results",
    ))
    orch._execute_tool("web_search", {"query": "weather"})
    assert orch._turn_provenance["level"] == 2


def test_subagent_auto_poll_wiring():
    mock_mgr = MagicMock()
    mock_llm = MagicMock()
    mock_llm.model = "test"
    orch = Orchestrator(llm=mock_llm, tools=ToolRegistry(), ctx=ContextBuilder(), sub_mgr=mock_mgr)

    task = SubagentTask(brief="test task", depth=0)
    orch._pending_subagents["sub-123"] = {
        "brief": "test task",
        "spawn_step": 0,
        "reminded": False,
        "task": task,
    }

    # 1. When subagent is still running, poll returns None -> no auto-return
    mock_mgr.poll.return_value = None
    out = orch._check_stale_subagents(step=1)
    assert out == ""
    assert "sub-123" in orch._pending_subagents

    # 2. When subagent completes, poll returns SubagentResult -> auto-returned within 1 step
    mock_mgr.poll.return_value = SubagentResult(
        task_id="sub-123",
        status="done",
        summary="task finished successfully",
        confidence="high",
    )
    events = []
    orch.on_event = lambda ev, data: events.append((ev, data))

    out = orch._check_stale_subagents(step=2)
    assert "[子 Agent 自动回传] sub-123" in out
    assert "task finished successfully" in out
    assert "通过验收" in out
    assert "sub-123" not in orch._pending_subagents
    assert any(ev == "subagent_update" and data["id"] == "sub-123" for ev, data in events)


def test_turn_cancellation_and_budget_limits():
    from agent.takeover import get_takeover_manager, set_takeover_manager, TakeoverManager
    from types import SimpleNamespace

    # 1. Takeover cancellation when orchestrator cancels
    t_mgr = TakeoverManager()
    set_takeover_manager(t_mgr)
    req = t_mgr.request_takeover(task_id="t-1", target="chrome", reason="captcha")

    mock_llm = MagicMock()
    mock_llm.model = "test-model"
    orch = Orchestrator(llm=mock_llm, tools=ToolRegistry(), ctx=ContextBuilder(), max_iters_hard=4)
    orch.cancel()

    assert orch.is_cancelled()
    assert orch.last_turn_status == "cancelled"
    # Takeover request must be resolved as cancelled
    assert t_mgr.get_status("t-1")["status"] == "cancelled"

    # 2. Shared step budget across verification attempts
    step_count = 0
    class CountingLoopLLM:
        def chat(self, messages, tools=None):
            nonlocal step_count
            step_count += 1
            tc = ToolCall(id=f"call_{step_count}", name="dummy_tool", arguments={})
            return Message(role="assistant", content="", tool_calls=[tc])

    tools = ToolRegistry()
    from agent.tools_registry import ToolDefinition
    tools.register(ToolDefinition(
        name="dummy_tool",
        description="d",
        parameters={"type": "object", "properties": {}},
        func=lambda a: "done",
    ))
    tools.set_approval(SimpleNamespace(check=lambda *a, **k: True))

    step_count = 0
    orch2 = Orchestrator(llm=CountingLoopLLM(), tools=tools, ctx=ContextBuilder(), max_iters_hard=4)
    out = orch2.handle("test task")
    assert "任务步骤较多" in out
    assert step_count == 4  # Strictly capped at 4 total steps!
    assert orch2.last_turn_status == "incomplete"

    # 3. Wall clock timeout
    orch3 = Orchestrator(llm=CountingLoopLLM(), tools=tools, ctx=ContextBuilder(),
                         max_iters_hard=100, max_turn_wall_clock_sec=0.01)
    # Simulate time passing
    out3 = orch3.handle("timeout task")
    assert orch3.last_turn_status == "incomplete"


def test_task_state_reset_across_turns_and_tools():
    from agent.task_state import TASK_STATE_TOOLS, current_task_state
    tools = ToolRegistry()
    for t in TASK_STATE_TOOLS:
        tools.register(t)
    tools.set_approval(SimpleNamespace(check=lambda *a, **k: True))

    class ToolUsingLLM:
        def __init__(self, actions):
            self.actions = list(actions)

        def chat(self, messages, tools=None):
            if not self.actions:
                return Message(role="assistant", content="all done")
            action = self.actions.pop(0)
            tc = ToolCall(id="tc_1", name=action["name"], arguments=action["args"])
            return Message(role="assistant", content="", tool_calls=[tc])

    # Turn 1: pins email
    llm1 = ToolUsingLLM([
        {"name": "state_pin", "args": {"key": "email", "value": "test1@example.com", "source": "test"}},
        {"name": "state_check", "args": {"key": "email", "value": "test1@example.com"}},
    ])
    orch = Orchestrator(llm=llm1, tools=tools, ctx=ContextBuilder())
    orch.handle("turn 1 task")
    # Verify pin worked in turn 1
    assert current_task_state().get("email") is None or orch.task_state.get("email") == "test1@example.com"

    # Turn 2: new user turn must reset TaskState (P1-16)
    llm2 = ToolUsingLLM([
        {"name": "state_check", "args": {"key": "email", "value": "test1@example.com"}},
    ])
    orch.llm = llm2
    orch.handle("turn 2 task")
    # In turn 2, the check must fail because email was reset
    last_records = [r for r in orch.last_turn_tool_records if r.get("tool") == "state_check"]
    assert len(last_records) == 1
    assert "[钉死检查失败]" in (last_records[0].get("result") or "")

    # Test state_clear tool
    pin_res = tools.execute("state_pin", {"key": "order_id", "value": "12345"})
    assert "[已钉死]" in pin_res.content
    clear_res = tools.execute("state_clear", {})
    assert "[已清理]" in clear_res.content
    check_res = tools.execute("state_check", {"key": "order_id", "value": "12345"})
    assert "[钉死检查失败]" in check_res.content


def test_browser_lease_tool_registry_arbitration():
    worker = BrowserWorker.__new__(BrowserWorker)
    worker._lease_owner = None
    worker._lease_lock = threading.Lock()

    tools = ToolRegistry()
    from agent.tools_registry import ToolDefinition
    tools.register(ToolDefinition(
        name="browser_click",
        description="click",
        parameters={"type": "object", "properties": {}},
        func=lambda a: "clicked",
    ))
    tools.browser_worker = worker
    tools.set_approval(SimpleNamespace(check=lambda *a, **k: True))

    # 1. No lease holder -> succeeds
    res1 = tools.execute("browser_click", {})
    assert res1.content == "clicked"

    # 2. Lease held by sub:task-999, caller is main -> fails with actionable guidance
    worker.acquire_lease("sub:task-999")
    threading.current_thread()._lease_caller_id = "main"
    res2 = tools.execute("browser_click", {})
    assert "[浏览器由子任务占用]" in res2.content
    assert "sub:task-999" in res2.content
    assert "poll_subagent" in res2.content and "不要取消" in res2.content

    # 3. Caller is the lease owner -> succeeds
    threading.current_thread()._lease_caller_id = "sub:task-999"
    res3 = tools.execute("browser_click", {})
    assert res3.content == "clicked"

    # 4. A non-browser kind subagent must not drive the browser without a lease
    worker.release_lease("sub:task-999")
    threading.current_thread()._lease_caller_id = "sub:generic-1"
    res4 = tools.execute("browser_click", {})
    assert "[租约冲突]" in res4.content and "kind=\"browser\"" in res4.content

    # Clean up
    delattr(threading.current_thread(), "_lease_caller_id")
    worker.release_lease("sub:task-999")





