"""v2.8 §39.7：主 Agent 浏览器委派硬门禁。

主 Agent 每轮只保留 1 次直接动作额度（点击/输入/按键/导航），之后必须
spawn_subagent(kind="browser")；只读观察（snapshot/evaluate/probe 等）不消耗该额度
（v1.5.21 P1-4：收尾核实不能被锁死）。子 Agent（caller_id=sub:*）不受影响；
未启用门禁时行为不变。
"""
import threading
from types import SimpleNamespace

from agent.models import Message
from agent.orchestrator import Orchestrator
from agent.tools_registry import ToolDefinition, ToolRegistry


def _browser_registry(flag: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="browser_snapshot",
        description="test browser snapshot",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "snapshot ok",
    ))
    registry.register(ToolDefinition(
        name="browser_click",
        description="test browser click",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "click ok",
    ))
    registry.register(ToolDefinition(
        name="spawn_subagent",
        description="test spawn",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "spawned",
    ))
    registry.browser_delegation_required = flag
    return registry


def _run_as(caller: str, registry: ToolRegistry, tool: str = "browser_click",
            args: dict | None = None) -> Message:
    thread = threading.current_thread()
    previous = getattr(thread, "_lease_caller_id", None)
    thread._lease_caller_id = caller
    try:
        return registry.execute(tool, args or {})
    finally:
        if previous is None:
            delattr(thread, "_lease_caller_id")
        else:
            thread._lease_caller_id = previous


def test_main_gets_only_one_direct_browser_call_per_turn():
    registry = _browser_registry()
    registry.begin_turn("scope-1")

    first = _run_as("main", registry)
    assert first.content == "click ok"

    second = _run_as("main", registry)
    assert "[浏览器任务须委派]" in second.content
    assert "spawn_subagent" in second.content
    assert "kind=\"browser\"" in second.content


def _begin_as(caller: str, registry: ToolRegistry, scope: str) -> None:
    thread = threading.current_thread()
    previous = getattr(thread, "_lease_caller_id", None)
    thread._lease_caller_id = caller
    try:
        registry.begin_turn(scope)
    finally:
        if previous is None:
            delattr(thread, "_lease_caller_id")
        else:
            thread._lease_caller_id = previous


def test_begin_turn_resets_main_budget():
    registry = _browser_registry()
    _begin_as("main", registry, "scope-1")
    assert _run_as("main", registry).content == "click ok"
    assert "[浏览器任务须委派]" in _run_as("main", registry).content

    _begin_as("main", registry, "scope-2")
    assert _run_as("main", registry).content == "click ok"


def test_subagent_is_not_gated():
    registry = _browser_registry()
    registry.begin_turn("scope-1")
    for _ in range(3):
        assert _run_as("sub:task-1", registry).content == "click ok"


def test_gate_disabled_by_default():
    registry = _browser_registry(flag=False)
    registry.begin_turn("scope-1")
    for _ in range(3):
        assert _run_as("main", registry).content == "click ok"


def test_gate_blocked_attempt_does_not_consume_budget_twice():
    registry = _browser_registry()
    registry.begin_turn("scope-1")
    assert _run_as("main", registry).content == "click ok"
    for _ in range(2):
        assert "[浏览器任务须委派]" in _run_as("main", registry).content
    with registry._browser_direct_lock:
        assert registry._browser_direct_calls["main"] == 1


def test_completed_subagent_grants_one_verification_call():
    registry = _browser_registry()
    registry.register(ToolDefinition(
        name="poll_subagent", description="test poll",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "[子 Agent 执行完毕] 状态: done | 总结: ok"))
    registry.set_approval(SimpleNamespace(check=lambda *a, **k: True))
    registry.begin_turn("scope-1")

    assert _run_as("main", registry).content == "click ok"
    assert "[浏览器任务须委派]" in _run_as("main", registry).content

    polled = _run_as("main", registry, tool="poll_subagent", args={"task_id": "t1"})
    assert "执行完毕" in polled.content
    assert _run_as("main", registry).content == "click ok"

    # 同一个 task 重复 poll 不得再发放额度
    _run_as("main", registry, tool="poll_subagent", args={"task_id": "t1"})
    assert "[浏览器任务须委派]" in _run_as("main", registry).content


def test_orchestrator_enables_gate_only_with_spawn_tool():
    enabled = _browser_registry(flag=False)
    Orchestrator(llm=object(), tools=enabled, ctx=object(), sub_mgr=object())
    assert enabled.browser_delegation_required is True

    no_spawn = ToolRegistry()
    no_spawn.register(ToolDefinition(
        name="browser_snapshot", description="x",
        parameters={"type": "object", "properties": {}}, func=lambda args: "ok"))
    Orchestrator(llm=object(), tools=no_spawn, ctx=object(), sub_mgr=object())
    assert no_spawn.browser_delegation_required is False


def test_readonly_observation_does_not_consume_action_quota():
    """v1.5.21 P1-4：收尾核实用的只读观察不吃动作额度。"""
    registry = _browser_registry()
    registry.begin_turn("scope-1")

    for _ in range(3):
        assert _run_as("main", registry, tool="browser_snapshot").content == "snapshot ok"

    assert _run_as("main", registry).content == "click ok"
    assert "[浏览器任务须委派]" in _run_as("main", registry).content
    # 动作额度用完后，只读观察仍可用于回读核验
    assert _run_as("main", registry, tool="browser_snapshot").content == "snapshot ok"


def test_readonly_observation_capped():
    registry = _browser_registry()
    registry.begin_turn("scope-1")
    for _ in range(ToolRegistry._BROWSER_READONLY_CAP):
        assert _run_as("main", registry, tool="browser_snapshot").content == "snapshot ok"
    capped = _run_as("main", registry, tool="browser_snapshot")
    assert "[浏览器观察额度用尽]" in capped.content
    # 观察额度耗尽不影响那 1 次动作额度
    assert _run_as("main", registry).content == "click ok"


def test_action_resets_readonly_counter_for_post_action_verification():
    """v1.5.22 P2：动作之后重新观察核验是正常流程，只读计数随动作重置。"""
    registry = _browser_registry()
    registry.begin_turn("scope-1")
    for _ in range(ToolRegistry._BROWSER_READONLY_CAP - 1):
        assert _run_as("main", registry, tool="browser_snapshot").content == "snapshot ok"

    assert _run_as("main", registry).content == "click ok"

    for _ in range(ToolRegistry._BROWSER_READONLY_CAP):
        assert _run_as("main", registry, tool="browser_snapshot").content == "snapshot ok"
    assert "[浏览器观察额度用尽]" in _run_as("main", registry, tool="browser_snapshot").content


def test_takeover_escalation_is_exempt_from_action_quota():
    """v1.5.22 P1-1：求助出口不能被动作额度挡住，否则 Agent 卡在不能做也不能求助。"""
    registry = _browser_registry()
    registry.register(ToolDefinition(
        name="browser_request_takeover",
        description="test takeover",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "takeover ok",
    ))
    registry.set_approval(SimpleNamespace(check=lambda *a, **k: True))
    registry.begin_turn("scope-1")

    assert _run_as("main", registry).content == "click ok"
    assert "[浏览器任务须委派]" in _run_as("main", registry).content
    takeover = _run_as("main", registry, tool="browser_request_takeover")
    assert takeover.content == "takeover ok"
