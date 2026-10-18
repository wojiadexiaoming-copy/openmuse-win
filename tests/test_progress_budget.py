"""进展水位步数预算（代替笨计数器）：

- 软上限到后按进展决定续杯/收尾（窗口内操作多样化且失败未累积 → 续到硬上限）
- 硬上限封顶
- 同一失败调用连续重复 3 次 → 直接早停（卡死）

对应"我是怎么判断步数的"：动作在变、有新信息 → 继续；原地打转 → 停。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.context import ContextBuilder
from agent.models import Message, ToolCall
from agent.orchestrator import Orchestrator
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


def _tc(i, name):
    return ToolCall(id=f"c{i}", name=name, arguments={"n": i})


def test_soft_limit_extends_when_progressing():
    """25 步长任务（如 Agnes 注册）：操作多样化、失败未累积 → 续杯。"""
    llm = MagicMock()
    llm.model = "offline"
    calls = [_tc(i, f"tool_{i % 3}") for i in range(6)]
    llm.chat.side_effect = (
        [Message(role="assistant", tool_calls=[c]) for c in calls]
        + [Message(role="assistant", content="done")]
    )
    reg = _registry(*[(f"tool_{i}", lambda _: "ok") for i in range(3)])
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=10)
    out = orch.handle("do a long task")
    assert llm.chat.call_count == 7, "有进展时软上限应续杯，而不是 3 步砍断"
    assert out == "done"


def test_soft_limit_stops_without_progress():
    """同一调用翻来覆去 → 软上限到后收尾，不续杯。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [Message(role="assistant",
                                    tool_calls=[ToolCall(id="x", name="poll",
                                                         arguments={})])] * 10
    reg = _registry(("poll", lambda _: "same result"))
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=10)
    reply = orch.handle("poll forever")
    assert "步骤上限" in reply and "无实质进展" in reply
    assert llm.chat.call_count == 3, "无进展时 3 步即停"
    assert orch._turn_exhausted


def test_repeat_failing_call_stops_early():
    """同一失败调用连 3 次 → 卡死早停，不等软上限。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [Message(role="assistant",
                                    tool_calls=[ToolCall(id="x", name="boom",
                                                         arguments={})])] * 10
    reg = _registry(("boom", lambda _: "[执行失败] 炸了"))
    orch = _orch(llm, reg, max_iters=10, max_iters_hard=20)
    reply = orch.handle("keep failing")
    assert "卡死" in reply
    assert llm.chat.call_count == 3, "第 3 次重复失败即停"
    assert orch._turn_exhausted


def test_repeat_successful_call_does_not_trigger_stuck():
    """重复成功调用不算卡死（轮询等待是合法模式），走软上限逻辑。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [Message(role="assistant",
                                    tool_calls=[ToolCall(id="x", name="wait",
                                                         arguments={"s": 5})])] * 10
    reg = _registry(("wait", lambda _: "[等待完成] 5 秒"))
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=10)
    reply = orch.handle("wait a lot")
    assert "卡死" not in reply
    assert "无实质进展" in reply  # 软上限收尾，而非卡死早停
    assert llm.chat.call_count == 3


def test_hard_cap_is_absolute():
    """硬上限是绝对天花板：有进展也只能到 hard。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[_tc(i, f"t{i % 4}")]) for i in range(20)
    ]
    reg = _registry(*[(f"t{i}", lambda _: "ok") for i in range(4)])
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=5)
    reply = orch.handle("long task")
    assert "硬上限" in reply and "5 步" in reply
    assert llm.chat.call_count == 5


def test_progress_signature_ignores_arg_order():
    sig1 = Orchestrator._call_signature([ToolCall(id="a", name="t",
                                                  arguments={"x": 1, "y": 2})])
    sig2 = Orchestrator._call_signature([ToolCall(id="b", name="t",
                                                  arguments={"y": 2, "x": 1})])
    assert sig1 == sig2, "参数顺序不同应视为同一签名"


def test_backwards_compat_default_hard_cap():
    """不传 max_iters_hard 的老构造 → 默认 100，且钳制不小于 max_iters。"""
    llm = MagicMock()
    reg = _registry(("t", lambda _: "ok"))
    orch = Orchestrator(llm, reg, ContextBuilder(max_tokens=4000), max_iters=150)
    assert orch.max_iters_hard == 150
    orch2 = Orchestrator(llm, reg, ContextBuilder(max_tokens=4000))
    assert (orch2.max_iters, orch2.max_iters_hard) == (25, 100)


def test_budget_refill_leaves_history_trace():
    """续杯决策写进对话历史：复盘时可见依据（去重签名数/未恢复失败数），不再是黑盒。"""
    llm = MagicMock()
    llm.model = "offline"
    calls = [_tc(i, f"tool_{i % 3}") for i in range(6)]
    llm.chat.side_effect = (
        [Message(role="assistant", tool_calls=[c]) for c in calls]
        + [Message(role="assistant", content="done")]
    )
    reg = _registry(*[(f"tool_{i}", lambda _: "ok") for i in range(3)])
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=10)
    orch.handle("do a long task")
    budget_notes = [m for m in orch.history
                    if m.role == "assistant" and m.content.startswith("[步数预算]")]
    assert len(budget_notes) == 1, "续杯时应留一条历史记录"
    assert "去重签名" in budget_notes[0].content
    assert "续杯" in budget_notes[0].content


def test_budget_wrap_up_reports_evidence_counts():
    """软上限收尾时：exhausted_reason 带上水位依据数字。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [Message(role="assistant",
                                    tool_calls=[ToolCall(id="x", name="poll",
                                                         arguments={})])] * 10
    reg = _registry(("poll", lambda _: "ok"))
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=10)
    out = orch.handle("stuck task")
    assert "去重签名" in out and "未恢复失败" in out


def test_safe_args_for_log_redacts_passwords():
    """参数进 metadata 时密码类值脱敏，邮箱等关键值保留（复盘要看）。"""
    from agent.orchestrator import _safe_args_for_log
    args = _safe_args_for_log("browser_type", {
        "selector": 'input[type="password"]',
        "text": "SuperSecret123",
    })
    assert args["text"] == "***已脱敏***"
    args2 = _safe_args_for_log("browser_type", {
        "selector": 'input[placeholder*="邮箱"]',
        "text": "a@b.com",
    })
    assert args2["text"] == "a@b.com"
    args3 = _safe_args_for_log("browser_navigate", {"url": "https://x.com/" + "y" * 600})
    assert len(args3["url"]) == 500
