"""v2.8 §39.7：进展水位补标记、evaluate 内嵌错误、连续非变更探测、截止部分交付。"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.context import ContextBuilder
from agent.models import Message, ToolCall
from agent.orchestrator import Orchestrator
from agent.tools_registry import ToolDefinition, ToolRegistry


def _registry(*names):
    reg = ToolRegistry()
    for name in names:
        reg.register(ToolDefinition(name=name, description="t",
                                    parameters={"type": "object", "properties": {}},
                                    func=lambda _: "ok"))
    reg.set_approval(SimpleNamespace(check=lambda *a, **k: True))
    return reg


def test_new_blocked_markers_do_not_count_as_progress():
    calls = [
        ToolCall(id="a", name="browser_fill_combo", arguments={"text": "天津"}),
        ToolCall(id="b", name="browser_fill_combo", arguments={"text": "天津"}),
        ToolCall(id="c", name="browser_fill_combo", arguments={"text": "天津"}),
    ]
    results = [
        Message(role="tool", tool_call_id="a", name="browser_fill_combo",
                content="[通道锁定 → C2(视觉/坐标)] 禁止再用 C1"),
        Message(role="tool", tool_call_id="b", name="browser_fill_combo",
                content="[输入未落地] 回读值为空"),
        Message(role="tool", tool_call_id="c", name="browser_fill_combo",
                content="[无匹配候选] 已拒绝盲选"),
    ]
    assert Orchestrator._progress_signature(calls, results) == ()


def test_embedded_evaluate_error_blocks_progress():
    calls = [ToolCall(id="a", name="browser_evaluate", arguments={"script": "fetch(x)"})]
    results = [Message(role="tool", tool_call_id="a", name="browser_evaluate",
                       content="TypeError: Failed to fetch")]
    assert Orchestrator._progress_signature(calls, results) == ()

    calls2 = [ToolCall(id="b", name="browser_evaluate", arguments={"script": "fetch(y)"})]
    results2 = [Message(role="tool", tool_call_id="b", name="browser_evaluate",
                        content='{"err": "timeout"}')]
    assert Orchestrator._progress_signature(calls2, results2) == ()

    calls3 = [ToolCall(id="c", name="browser_evaluate", arguments={"script": "fetch(z)"})]
    results3 = [Message(role="tool", tool_call_id="c", name="browser_evaluate",
                        content='{"err": null, "rows": [1, 2]}')]
    assert len(Orchestrator._progress_signature(calls3, results3)) == 1


def test_unresolved_failures_counts_blocked_results():
    orch = Orchestrator.__new__(Orchestrator)
    orch.last_turn_tool_records = [
        {"tool": "browser_fill_combo", "status": "ok", "result": "[输入未落地] 空值"},
        {"tool": "browser_click", "status": "ok", "result": "clicked"},
        {"tool": "browser_evaluate", "status": "ok", "result": "TypeError: Failed to fetch"},
    ]
    assert sorted(orch._unresolved_tool_failures()) == [
        "browser_evaluate", "browser_fill_combo"]


def test_probe_streak_blocks_extension_despite_diverse_signatures():
    orch = Orchestrator.__new__(Orchestrator)
    sigs = [(("browser_evaluate", str(i)),) for i in range(6)]
    unresolved = [0] * 6
    # v1.5.17：probe_streak 只在新增信息不足时否决；有 2 步新信息就允许续杯
    assert orch._progress_allows_extension(
        sigs, unresolved, probe_streak=0, new_info_steps=2) is True
    assert orch._progress_allows_extension(
        sigs, unresolved, probe_streak=3, new_info_steps=2) is True
    assert orch._progress_allows_extension(
        sigs, unresolved, probe_streak=3, new_info_steps=1) is False
    assert orch._progress_allows_extension(
        sigs, unresolved, probe_streak=0, new_info_steps=0) is False


def test_consecutive_evaluate_probes_stop_at_limit():
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant",
                tool_calls=[ToolCall(id=f"e{i}", name="browser_evaluate",
                                     arguments={"script": f"probe({i})"})])
        for i in range(30)
    ]
    orch = Orchestrator(llm, _registry("browser_evaluate"),
                        ContextBuilder(max_tokens=4000),
                        max_iters=25, max_iters_hard=100)
    orch._partial_delivery = lambda *a, **k: ""
    reply = orch.handle("probe forever")
    assert "原地打转" in reply
    # 第 1 步拿到“新结果”作为基线，之后连续 12 步同结果才停
    assert llm.chat.call_count == Orchestrator._PROBE_STREAK_LIMIT + 1
    assert any("[进展提醒]" in m.content for m in orch.history if m.role == "system")
    assert orch._turn_exhausted


def test_budget_milestones_are_emitted_once():
    sent = set()
    key, text = Orchestrator._budget_milestone(40, 100, sent)
    assert key == "50" and "50%" in text
    sent.add(key)
    assert Orchestrator._budget_milestone(40, 100, sent) is None

    key, text = Orchestrator._budget_milestone(10, 100, sent)
    assert key == "80" and "部分交付" in text
    sent.add(key)
    assert Orchestrator._budget_milestone(1, 100, sent) is None


def test_timeout_returns_partial_delivery_from_evidence():
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", content="",
                tool_calls=[ToolCall(id="c1", name="slow_tool", arguments={})]),
        Message(role="assistant",
                content="已确认：G903 11:05→21:38（未核验）"),
    ]
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="slow_tool", description="slow",
        parameters={"type": "object", "properties": {}},
        func=lambda _: time.sleep(0.4) or "ok"))
    reg.set_approval(SimpleNamespace(check=lambda *a, **k: True))
    orch = Orchestrator(llm, reg, ContextBuilder(max_tokens=4000),
                        max_iters=5, max_iters_hard=10,
                        max_turn_wall_clock_sec=0.3)

    out = orch.handle("查天津到深圳的高铁")

    assert out.startswith("[已超时]")
    assert "[部分交付]" in out
    assert "G903" in out
    assert llm.chat.call_count == 2
    assert orch.last_turn_status == "incomplete"
