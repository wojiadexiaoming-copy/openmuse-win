"""进展水位步数预算（代替笨计数器）：

- 软上限到后按进展决定续杯/收尾（窗口内操作多样化且失败未累积 → 续到硬上限）
- 硬上限封顶
- 同一失败调用连续重复 3 次 → 直接早停（卡死）

对应"我是怎么判断步数的"：动作在变、有新信息 → 继续；原地打转 → 停。
"""
import time
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
    orch = Orchestrator(llm, reg, ContextBuilder(max_tokens=4000), **kw)
    # 这些用例只验证步数水位，不额外消耗一次部分交付的 LLM 调用
    orch._partial_delivery = lambda *a, **k: ""
    return orch


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
    assert "步骤上限" in reply and "停止原因" in reply
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
    assert "停止原因" in reply  # 软上限收尾，而非卡死早停
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


def test_observation_only_churn_never_extends():
    """纯观察（截图/快照/切 tab/滚动）再多也不算实质进展。"""
    llm = MagicMock()
    llm.model = "offline"
    names = ["browser_screenshot", "browser_snapshot", "browser_switch_tab",
             "browser_scroll", "browser_wait_for", "browser_tabs"]
    calls = [_tc(i, names[i % len(names)]) for i in range(10)]
    llm.chat.side_effect = [Message(role="assistant", tool_calls=[c]) for c in calls]
    reg = _registry(*[(n, lambda _: "观察完成") for n in set(names)])
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=10)
    reply = orch.handle("look around forever")
    assert llm.chat.call_count == 3, "观察噪声不得触发续杯"
    assert "停止原因" in reply


def test_blocked_action_results_do_not_count_as_progress():
    """重定向/租约冲突/通道无进展的结果不计入进展。"""
    llm = MagicMock()
    llm.model = "offline"
    calls = [_tc(i, "browser_click") for i in range(10)]
    llm.chat.side_effect = [Message(role="assistant", tool_calls=[c]) for c in calls]
    reg = _registry(("browser_click", lambda _: "[重复重定向熔断·铁律17] 换入口"))
    orch = _orch(llm, reg, max_iters=3, max_iters_hard=10)
    reply = orch.handle("retry blocked click")
    assert llm.chat.call_count == 3
    assert "停止原因" in reply


def test_progress_signature_filters_observation_and_blocked_results():
    calls = [
        ToolCall(id="a", name="browser_screenshot", arguments={}),
        ToolCall(id="b", name="browser_click", arguments={"ref": "ae1"}),
        ToolCall(id="c", name="browser_fill_input", arguments={"ref": "ae2", "text": "x"}),
    ]
    results = [
        Message(role="tool", tool_call_id="a", name="browser_screenshot", content="ok"),
        Message(role="tool", tool_call_id="b", name="browser_click",
                content="[通道=C1(树/ref) 尝试=2/2 无进展]"),
        Message(role="tool", tool_call_id="c", name="browser_fill_input",
                content="[已输入] 读回一致"),
    ]
    sig = Orchestrator._progress_signature(calls, results)
    assert len(sig) == 1 and sig[0][0] == "browser_fill_input"


def test_wall_clock_timeout_emits_single_message():
    """run_turn 已追加超时消息时，_handle_turn 不得再追加第二条。"""
    llm = MagicMock()
    llm.model = "offline"
    reg = _registry(("t", lambda _: "ok"))
    orch = Orchestrator(llm, reg, ContextBuilder(max_tokens=4000),
                        max_iters=5, max_iters_hard=10, max_turn_wall_clock_sec=0)
    orch.handle("timeout now")
    timeout_msgs = [m for m in orch.history
                    if m.role == "assistant" and m.content.startswith("[已超时]")]
    assert len(timeout_msgs) == 1, "超时只能发一条消息，不能重复追加"
    assert orch.last_turn_status == "incomplete"


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


class _FakeSubMgr:
    """墙钟续期只需要 progress_snapshot / cancel_all 两个接口。"""

    def __init__(self, records):
        self._records = records
        self.cancel_all_calls = 0

    def progress_snapshot(self):
        now = time.time()
        return [{
            "task_id": "sub-abcdef1234",
            "brief": "Agnes 邮箱注册流程",
            "kind": "browser",
            "step": 7,
            "records": [{**rec, "ts": rec.get("ts", now)} for rec in self._records],
            "deadline": time.time() + 600,
            "idle_sec": 0.0,
        }]

    def cancel_all(self):
        self.cancel_all_calls += 1


def _slow_tool(text="观察完成", delay=0.08):
    def _run(_):
        time.sleep(delay)
        return text
    return _run


def test_wall_clock_extends_while_subagent_progresses():
    """主任务到点，但子任务仍在产出新实质动作 → 自动续期并告知用户。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[
            ToolCall(id="s1", name="browser_snapshot", arguments={})]),
        Message(role="assistant", content="done"),
    ]
    reg = _registry(("browser_snapshot", _slow_tool()))
    events = []
    sub = _FakeSubMgr([{"tool": "browser_click", "status": "ok",
                        "result": "[已点击] 进入下一步"}])
    orch = _orch(llm, reg, turn_extension_sec=5, turn_max_extensions=2,
                 sub_mgr=sub, on_event=lambda e, d: events.append((e, d)))
    reply = orch.run_turn(Message(role="user", content="register"),
                          deadline=time.time() + 0.05, step_budget=10)

    assert reply.content == "done"
    assert orch._turn_extensions_used == 1
    notices = [d for e, d in events if e == "turn_notice"]
    assert notices, "续期必须给用户可见提示，不能默默续"
    assert "sub-abcdef1234" in notices[0]["text"]
    assert "停止" in notices[0]["text"]


def test_wall_clock_extension_stops_after_max_and_without_fresh_progress():
    """续期次数封顶；同一批旧进展不能反复续期，最终按超时收尾。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[
            ToolCall(id=f"s{i}", name="browser_snapshot", arguments={})])
        for i in range(8)
    ]
    reg = _registry(("browser_snapshot", _slow_tool(delay=0.06)))
    sub = _FakeSubMgr([{"tool": "browser_click", "status": "ok",
                        "result": "[已点击] 进入下一步"}])
    orch = _orch(llm, reg, turn_extension_sec=0.05, turn_max_extensions=1,
                 sub_mgr=sub)
    reply = orch.run_turn(Message(role="user", content="register"),
                          deadline=time.time() + 0.02, step_budget=10)

    assert orch._turn_extensions_used == 1
    assert "[已超时]" in reply.content


def test_wall_clock_timeout_without_progress_does_not_extend():
    """没有子任务进展、自身也只做观察 → 不续期，直接超时收尾。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[
            ToolCall(id="s1", name="browser_snapshot", arguments={})]),
    ]
    reg = _registry(("browser_snapshot", _slow_tool(delay=0.06)))
    orch = _orch(llm, reg, turn_extension_sec=5, turn_max_extensions=3)
    reply = orch.run_turn(Message(role="user", content="look"),
                          deadline=time.time() + 0.02, step_budget=10)

    assert orch._turn_extensions_used == 0
    assert "[已超时]" in reply.content


def test_request_stop_cancels_everything():
    """停止按钮必须真的取消编排与全部子任务，而不是只弹 toast。"""
    llm = MagicMock()
    llm.model = "offline"
    reg = _registry(("t", lambda _: "ok"))
    sub = _FakeSubMgr([])
    orch = _orch(llm, reg, sub_mgr=sub)
    orch.request_stop()

    assert orch.is_cancelled()
    assert orch.last_turn_status == "cancelled"
    assert sub.cancel_all_calls == 1


def test_stop_mid_turn_returns_string():
    """取消路径必须回传字符串：历史 Message 回传会炸掉子任务结果验收。"""
    llm = MagicMock()
    llm.model = "offline"
    llm.chat.side_effect = [
        Message(role="assistant", tool_calls=[
            ToolCall(id="x", name="stopme", arguments={})]),
        Message(role="assistant", content="不该走到这里"),
    ]
    holder = {}

    def _stop(_):
        holder["orch"].request_stop()
        return "stop requested"

    reg = _registry(("stopme", _stop))
    orch = _orch(llm, reg, sub_mgr=_FakeSubMgr([]))
    holder["orch"] = orch
    out = orch.handle("do something")

    assert isinstance(out, str)
    assert "[任务已取消]" in out


def test_subagent_progress_snapshot_reports_running_task():
    """续期依赖的进度快照必须带 task_id/任务书/步数/工具记录。"""
    from agent.subagent import SubagentManager, SubagentTask

    class _SlowOrch:
        def __init__(self, task):
            self._turn_step = 4
            self.last_turn_tool_records = [{
                "tool": "browser_click", "status": "ok", "result": "已点击"}]

        def handle(self, prompt):
            time.sleep(0.5)
            return "done"

    mgr = SubagentManager(make_orchestrator=_SlowOrch, enforce_min_memory=False)
    try:
        tid = mgr.spawn(SubagentTask(brief="测试任务", use_process=False))
        deadline = time.time() + 3
        snap = []
        while time.time() < deadline:
            snap = mgr.progress_snapshot()
            if snap and snap[0].get("step") == 4:
                break
            time.sleep(0.02)
        assert snap and snap[0]["task_id"] == tid
        assert snap[0]["brief"] == "测试任务"
        assert snap[0]["step"] == 4
        assert snap[0]["records"][0]["tool"] == "browser_click"
    finally:
        mgr.shutdown()


def test_build_subagent_result_accepts_message_summary():
    """子 Agent 被取消时会把 Message 当 summary 传回来，不能 TypeError。"""
    from agent.subagent import build_subagent_result
    fake_orch = SimpleNamespace(last_turn_tool_records=[],
                                _turn_exhausted=False,
                                last_turn_status="cancelled")
    result = build_subagent_result(
        "sub-abc", fake_orch,
        Message(role="assistant", content="[任务已取消] 编排循环已终止。"))
    assert result.status == "partial"
    assert "[任务已取消]" in result.summary
