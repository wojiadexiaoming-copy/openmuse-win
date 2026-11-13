"""v2.9 §39.9：子 Agent 动态步数预算 + 结果指纹 + 部分交付 + 证据回传契约。

对应实跑事故：子 Agent 内部已查到 K1622，但 15 步写死上限触发后，
收尾总结覆盖了发现，主 Agent 只看到"任务尚未完成"，对用户说没查到。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.context import ContextBuilder
from agent.models import Message, ToolCall
from agent.orchestrator import Orchestrator
from agent.subagent import (
    SubagentResult,
    SubagentTask,
    accept_result,
    build_subagent_result,
    format_subagent_result,
)
from agent.tools_registry import ToolDefinition, ToolRegistry


def _registry(*defs):
    reg = ToolRegistry()
    for name, func in defs:
        reg.register(ToolDefinition(name=name, description="t",
                                    parameters={"type": "object", "properties": {}},
                                    func=func))
    reg.set_approval(SimpleNamespace(check=lambda *a, **k: True))
    return reg


def _orch(llm, reg, **kw):
    kw.setdefault("max_iters", 3)
    kw.setdefault("max_iters_hard", 10)
    return Orchestrator(llm, reg, ContextBuilder(max_tokens=4000), **kw)


def _tc(i, name, **args):
    return ToolCall(id=f"c{i}", name=name, arguments=args or {"n": i})


def test_step_grant_extends_incrementally_not_to_hard_cap(monkeypatch=None):
    """续杯按 grant 递增（3→5→7→9→10），而不是一次跳到硬上限。"""
    llm = MagicMock()
    llm.model = "offline"
    calls = [_tc(i, f"tool_{i % 3}") for i in range(20)]
    llm.chat.side_effect = [Message(role="assistant", tool_calls=[c]) for c in calls]
    reg = _registry(*[(f"tool_{i}", lambda a: f"data-{a.get('n')}") for i in range(3)])
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=10, step_grant=2)
    orch._partial_delivery = lambda *a, **k: ""

    out = orch.handle("长任务")
    assert llm.chat.call_count == 10, "硬上限是绝对天花板"
    notes = [m.content for m in orch.history
             if m.role == "assistant" and m.content.startswith("[步数预算]")]
    assert "续杯 +2 至 5" in notes[0]
    assert "续杯 +2 至 7" in notes[1]
    assert "续杯 +2 至 9" in notes[2]
    assert notes[-1].endswith("（硬上限 10 步）")
    assert "硬上限（10 步）" in out


def test_step_grant_requires_real_progress():
    """同签名 + 同结果 → 不续杯，软上限即停。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [Message(role="assistant",
                                    tool_calls=[ToolCall(id="x", name="poll",
                                                         arguments={})])] * 10
    reg = _registry(("poll", lambda _: "same result"))
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=10, step_grant=2)
    orch._partial_delivery = lambda *a, **k: "部分交付"
    out = orch.handle("poll forever")
    assert llm.chat.call_count == 3, "无新信息不得续杯"
    assert "停止原因" in out and "[部分交付]" in out


def test_changing_results_allow_extension_even_with_same_call():
    """evaluate 反复探测但每次都拿到新数据 → 视为新信息，允许续杯。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [Message(role="assistant",
                                    tool_calls=[ToolCall(id=f"e{i}", name="browser_evaluate",
                                                         arguments={"script": "read()"})])
                            for i in range(10)]
    counter = iter([f"第{i}页车次数据" for i in range(10)])
    reg = _registry(("browser_evaluate", lambda _: next(counter)))
    orch = _orch(llm, reg, max_iters=2, max_iters_hard=6, step_grant=2)
    orch._partial_delivery = lambda *a, **k: ""

    out = orch.handle("paged reading")
    assert llm.chat.call_count == 6, "新信息应续到硬上限"
    notes = [m.content for m in orch.history
             if m.role == "assistant" and m.content.startswith("[步数预算]")]
    assert any("新信息 2 步" in n for n in notes)
    assert "硬上限（6 步）" in out


def test_hard_cap_forces_partial_delivery():
    """步数耗尽必须先做部分交付，不能再让收尾总结覆盖已查到的数据。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [Message(role="assistant",
                                    tool_calls=[ToolCall(id="e", name="browser_evaluate",
                                                         arguments={"script": "tickets()"})])]
    reg = _registry(("browser_evaluate",
                     lambda _: "12306 接口返回：天津→深圳当天只有 1 趟车（K1622）"))
    orch = _orch(llm, reg, max_iters=1, max_iters_hard=1)
    reasons = []
    orch._partial_delivery = (
        lambda conv, reason="timeout": reasons.append(reason)
        or "已确认：K1622 天津→深圳东 22:16→06:00，为当日唯一车次")

    out = orch.handle("查天津到深圳的车次")
    assert reasons == ["steps"]
    assert "[部分交付]" in out and "K1622" in out


def test_task_anchor_and_fingerprint_helpers():
    assert Orchestrator._result_fingerprint("a  b") == Orchestrator._result_fingerprint("a b")
    assert (Orchestrator._result_fingerprint("x" * 2500)
            == Orchestrator._result_fingerprint("x" * 2000)), "指纹只看前 2000 字"
    conv = [
        Message(role="system", content="sys"),
        Message(role="user", content="[子任务]\n查天津→深圳最快高铁\n完成标准：车次+时间"),
    ]
    anchor = Orchestrator._task_anchor_text(conv)
    assert anchor.startswith("[子任务]") and "完成标准" in anchor


def test_count_new_info_steps_counts_fresh_results_only():
    assert Orchestrator._count_new_info_steps([{"a"}, {"a"}, {"b"}]) == 2
    assert Orchestrator._count_new_info_steps([set(), {"a"}, {"a"}]) == 1
    assert Orchestrator._count_new_info_steps([]) == 0


def test_extension_blocked_by_accumulating_failures_or_probe_streak():
    llm = MagicMock()
    reg = _registry(("t", lambda _: "ok"))
    orch = _orch(llm, reg)
    sigs = [[("a", "1")], [("b", "2")], [("c", "3")]]
    assert orch._progress_allows_extension(sigs, [0, 1, 2], 0, 0) is False
    assert orch._progress_allows_extension(sigs, [2, 1, 0], 0, 3) is True
    # v1.5.17：probe_streak 只在新增信息不足时否决；new_info=5 足以续杯
    assert orch._progress_allows_extension(sigs, [0, 0, 0], 3, 5) is True
    assert orch._progress_allows_extension(sigs, [0, 0, 0], 3, 1) is False


def test_partial_delivery_runs_once_per_turn():
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.return_value = Message(role="assistant", content="交付正文")
    reg = _registry(("t", lambda _: "ok"))
    orch = _orch(llm, reg)
    orch.last_turn_tool_records = [
        {"tool": "t", "status": "ok", "args": {}, "result": "K1622"}]
    conv = [Message(role="user", content="[子任务] 查车次")]
    assert orch._partial_delivery(conv, "steps") == "交付正文"
    assert orch._partial_delivery(conv, "steps") == ""
    assert llm.chat.call_count == 1


def test_build_subagent_result_marks_partial_and_keeps_evidence():
    records = [{
        "tool": "browser_evaluate",
        "status": "ok",
        "args": {"script": "tickets()"},
        "result": "12306 接口返回：天津→深圳当天只有 1 趟车（K1622）",
    }]
    done_orch = SimpleNamespace(_turn_exhausted=False, last_turn_status="complete",
                                last_turn_tool_records=records)
    done = build_subagent_result("t-done", done_orch, "查到 K1622，已交付")
    assert done.status == "done" and done.confidence == "high"
    assert "K1622" in done.evidence[0]["result"]
    assert done.evidence[0]["tool"] == "browser_evaluate"
    assert "script" in done.evidence[0]["args"]

    exhausted = build_subagent_result(
        "t-partial",
        SimpleNamespace(_turn_exhausted=True, last_turn_status="incomplete",
                        last_turn_tool_records=records),
        "任务尚未完成：步骤上限")
    assert exhausted.status == "partial"

    status_only = build_subagent_result(
        "t-incomplete",
        SimpleNamespace(_turn_exhausted=False, last_turn_status="incomplete",
                        last_turn_tool_records=[]),
        "看起来完成了")
    assert status_only.status == "partial", "状态 incomplete 不得伪装 done"


def test_format_subagent_result_exposes_evidence_excerpt():
    res = SubagentResult(
        task_id="t1", status="partial", summary="任务尚未完成：步骤上限",
        evidence=[{"type": "tool_record", "tool": "browser_evaluate",
                   "args": '{"script": "tickets()"}',
                   "result": "天津→深圳当天只有 1 趟车（K1622），非高铁"}],
        confidence="high")
    text = format_subagent_result(res, header="[子 Agent 自动回传] t1",
                                  acceptance="未通过验收（子任务未完成）")
    assert "状态: partial" in text
    assert "browser_evaluate(" in text
    assert "K1622" in text, "证据必须带结果原文，不能只有工具名"
    assert "未通过验收" in text


def test_accept_result_rejects_partial_and_incomplete_summary():
    task = SubagentTask(brief="查天津→深圳最快车次")
    evidence = [{"type": "tool_record", "tool": "browser_evaluate",
                 "ref": "browser_evaluate", "result": "K1622"}]
    partial = SubagentResult(task_id="t", status="partial",
                             summary="任务尚未完成", evidence=evidence)
    ok, reason = accept_result(partial, task)
    assert not ok and "未完成" in reason

    sneaky = SubagentResult(task_id="t", status="done",
                            summary="任务尚未完成：步骤到上限", evidence=evidence)
    ok, reason = accept_result(sneaky, task)
    assert not ok and "未完成" in reason

    clean = SubagentResult(task_id="t", status="done", summary="查到 K1622", evidence=evidence)
    assert accept_result(clean, task)[0] is True

    no_evidence = SubagentResult(task_id="t", status="done", summary="查到 K1622")
    assert accept_result(no_evidence, task)[0] is False


def test_partial_delivery_artifact_is_traceable():
    """部分交付正文进入历史，主 Agent 后续可引用（而非只留一句"步骤上限"）。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[ToolCall(id="e", name="t", arguments={})]),
        Message(role="assistant", content="已确认：K1622 天津→深圳东 22:16→06:00"),
    ]
    reg = _registry(("t", lambda _: "K1622 天津→深圳东 22:16→06:00"))
    orch = _orch(llm, reg, max_iters=1, max_iters_hard=1)
    orch._partial_delivery = Orchestrator._partial_delivery.__get__(orch, Orchestrator)

    out = orch.handle("查车次")
    assert "K1622" in out
    assert any(m.role == "assistant" and "[部分交付]" in m.content
               for m in orch.history)
