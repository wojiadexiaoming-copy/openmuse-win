"""M4 测试：压缩/路由/锁/缓存/并行/挖掘/蒸馏/调优。"""
import asyncio
import time

from agent.compaction import slim_tool_result, smart_compact, triage
from agent.miners import (detect_periodicity, mine_approval_patterns,
                          mine_task_patterns)
from agent.models import Message, ToolCall
from agent.parallel import execute_tool_calls
from agent.planner import Plan, PlanStep, drift_detected, should_plan
from agent.resource_lock import ResourceLock, resource_ids_for
from agent.router import estimate_complexity, route_model
from agent.schema_tuner import apply_tune, tune_tool
from agent.skill_distiller import distill_skill
from agent.task_tracker import TaskTracker
from agent.tools_registry import ToolDefinition, ToolRegistry
from agent.tracing import Tracer, new_trace, trace_id_var
from tools.cache import ToolResultCache


def test_slim_tool_result():
    m = Message(role="tool", name="x", content="ok\n" + "z" * 3000)
    slim_tool_result(m)
    assert len(m.content) < 3000 and "已瘦身" in m.content


def test_triage_keeps_user():
    conv = [Message(role="user", content="记住A"),
            Message(role="assistant", content="闲聊" * 100)]
    keep, comp = triage(conv)
    assert any(m.role == "user" for m in keep)


def test_smart_compact_no_llm():
    conv = [Message(role="assistant", content="x" * 100)]
    out = smart_compact(conv, summarize=None)
    assert out == conv  # 无摘要函数时原样返回


def test_router():
    models = {"low": "w", "standard": "s", "high": "h"}
    assert route_model("format", {}, models) == "w"
    assert route_model("browser_decide", {"est_tokens": 9000}, models) == "h"
    assert route_model("chat", {"est_tokens": 100}, models) == "w"
    assert route_model("chat", {"weak_failed_before": True}, models) == "h"


def test_resource_lock():
    rl = ResourceLock()
    assert rl.acquire("file:a", "t1")
    assert not rl.acquire("file:a", "t2", timeout_sec=1)
    rl.release("file:a", "t1")
    assert rl.acquire("file:a", "t2")
    rl.release_all("t2")
    assert rl.acquire("file:a", "t1")


def test_resource_ids():
    assert resource_ids_for("file_read", {}) == []
    assert resource_ids_for("file_write", {"path": "a"}) == ["file:a"]
    assert resource_ids_for("shell_exec", {}) == ["shell"]


def test_cache(tmp_path):
    c = ToolResultCache(str(tmp_path / "c.db"))
    assert c.get("web_search", {"q": "a"}) is None
    c.put("web_search", {"q": "a"}, "result!")
    assert c.get("web_search", {"q": "a"}) == "result!"
    assert c.get("web_search", {"q": "b"}) is None
    assert c.get("shell_exec", {"q": "a"}) is None  # 不在 TTL 表
    c.put("file_read", {"path": "x"}, "r")
    c.invalidate_tool("file_read")
    assert c.get("file_read", {"path": "x"}) is None


def test_parallel_grouping():
    reg = ToolRegistry()
    calls = []
    t_start = {}
    t_end = {}

    def mk(name):
        def f(args):
            t_start[name] = time.time()
            time.sleep(0.05)
            t_end[name] = time.time()
            calls.append(name)
            return name
        return f

    for n in ("file_read", "web_search"):
        reg.register(ToolDefinition(name=n, description="t",
                                    parameters={"type": "object", "properties": {}},
                                    func=mk(n)))
    tcs = [ToolCall(id="1", name="file_read", arguments={}),
           ToolCall(id="2", name="web_search", arguments={})]
    t0 = time.time()
    out = asyncio.run(execute_tool_calls(tcs, reg))
    dt = time.time() - t0
    assert [m.tool_call_id for m in out] == ["1", "2"]
    # 验证时间窗口相交（真并发重叠），并放宽 Windows 线程调度延迟容差
    assert max(t_start.values()) < min(t_end.values())
    assert dt < 0.35
    assert set(calls) == {"file_read", "web_search"}


def test_miners():
    now = time.time()
    approvals = [{"action": "publish", "risk": "L2", "context_sig": "s",
                  "decision": "rejected", "ts": now - i * 86400}
                 for i in range(6)]
    sugs = mine_approval_patterns(approvals)
    assert any(s["type"] == "deny" for s in sugs)

    base = now - 21 * 86400
    tasks = [{"intent": "查评论", "created_ts": base + i * 7 * 86400,
              "brief": "查评论"} for i in range(4)]
    sugs2 = mine_task_patterns(tasks)
    assert sugs2 and sugs2[0]["type"] == "cron"


def test_detect_periodicity_none():
    assert detect_periodicity([1.0, 2.0]) is None


def test_schema_tuner():
    from agent.tools_registry import ToolDefinition as TD
    tool = TD(name="t", description="d",
              parameters={"type": "object", "properties": {}})
    calls = [{"args": {"a": 1}, "ok": True, "error": ""} for _ in range(12)]
    diff = tune_tool(tool, calls)
    assert len(diff["add_examples"]) == 1
    apply_tune(tool, diff, log_fn=lambda x: None)
    assert len(tool.parameters["examples"]) == 1


def test_distiller():
    traces = [
        [{"tool": "file_read", "args": {"path": "a"}},
         {"tool": "file_write", "args": {"path": "b"}}],
        [{"tool": "file_read", "args": {"path": "c"}},
         {"tool": "file_write", "args": {"path": "d"}}],
        [{"tool": "file_read", "args": {"path": "e"}},
         {"tool": "file_write", "args": {"path": "f"}}],
    ]
    skill = distill_skill("读写文件", traces, dry_run_fn=lambda s: True)
    assert skill["confidence"] == "medium"
    assert "file_write.path" in skill["params"]  # 每次都变 → 参数
    # 含 L3 → 草稿
    traces2 = [[{"tool": "publish_x", "args": {}}]] * 3
    d2 = distill_skill("发布", traces2)
    assert d2["status"] == "draft"


def test_planner_rules():
    assert should_plan("先查评论然后整理成表格再发给我") is True
    assert should_plan("你好") is False
    plan = Plan(goal="x", steps=[PlanStep(desc="a", status="failed"),
                                 PlanStep(desc="b", status="failed")])
    assert drift_detected(plan) is True


def test_task_tracker():
    events = []
    tr = TaskTracker(on_update=events.append)
    tid = tr.new_task("t")
    sid = tr.step_start(tid, "s1")
    tr.step_done(tid, sid)
    tr.finish(tid, "done")
    assert len(events) == 3
    assert tr.get(tid).progress == 100.0


def test_tracing(tmp_path):
    new_trace()
    tid = trace_id_var.get()
    assert tid.startswith("t-")
    tr = Tracer(str(tmp_path / "s.db"))
    span = tr.start("tool", "file_read")
    tr.finish(span, status="ok", tokens_in=10, tokens_out=5)
    out = tr.export_trace(tid, str(tmp_path / "trace.zip"))
    import zipfile
    assert zipfile.is_zipfile(out)


def test_skill_manager_and_execution(tmp_path):
    from agent.skill_distiller import SkillManager, distill_skill
    from agent.tools_registry import ToolRegistry, ToolDefinition

    traces = [
        [{"tool": "file_read", "args": {"path": "a"}}, {"tool": "file_write", "args": {"path": "b"}}],
        [{"tool": "file_read", "args": {"path": "c"}}, {"tool": "file_write", "args": {"path": "d"}}],
        [{"tool": "file_read", "args": {"path": "e"}}, {"tool": "file_write", "args": {"path": "f"}}],
    ]
    skill = distill_skill("读写文件", traces, dry_run_fn=lambda s: True)
    assert skill is not None

    mgr = SkillManager(tmp_path / "skills.json")
    mgr.add_skill(skill)
    assert skill["name"] in mgr.skills

    reg = ToolRegistry()
    from agent.approval import ApprovalManager
    reg.set_approval(ApprovalManager(":memory:"), on_ask=lambda card: (True, "once"))
    called = []
    reg.register(ToolDefinition(name="file_read", description="r", parameters={}, func=lambda a: called.append("read")))
    reg.register(ToolDefinition(name="file_write", description="w", parameters={}, func=lambda a: called.append("write")))

    mgr.register_all(reg)
    tool_name = f"skill_{skill['name'].replace('-', '_')}"
    assert tool_name in reg._tools
    reg.execute(tool_name, {"path": "custom"})
    assert called == ["read", "write"]


def test_memory_overflow_summarize(tmp_path):
    from agent.memory_manager import MemoryManager
    mem = MemoryManager(tmp_path)
    (tmp_path / "MEMORY.md").write_text("# 记忆库\n" + "偏好：Python开发\n" * 500, encoding="utf-8")

    # 1. 默认结构化截断
    read_text = mem.read_main(max_chars=1000)
    assert len(read_text) <= 1000
    assert "截断" in read_text

    # 2. 传入摘要函数
    custom_summary = mem.read_main(max_chars=1000, summarize_fn=lambda t: "# 摘要\n用户偏好Python")
    assert custom_summary == "# 摘要\n用户偏好Python"


def test_orchestrator_run_with_plan():
    from agent.context import ContextBuilder
    from agent.orchestrator import Orchestrator
    from agent.planner import Plan, PlanStep, Planner
    from agent.tools_registry import ToolRegistry

    class StepLLM:
        def __init__(self):
            self.turn = 0

        def chat(self, messages, tools=None):
            self.turn += 1
            return Message(role="assistant", content=f"完成阶段 {self.turn}")

    class MockPlanner(Planner):
        def __init__(self):
            pass

        def make_plan(self, user_text: str) -> Plan:
            return Plan(
                goal=user_text,
                steps=[
                    PlanStep(desc="步骤一：分析结构", done_when="分析完成"),
                    PlanStep(desc="步骤二：执行代码", done_when="执行完毕"),
                ],
            )

    orch = Orchestrator(
        llm=StepLLM(),
        tools=ToolRegistry(),
        ctx=ContextBuilder(max_tokens=4000),
        planner=MockPlanner(),
    )

    reply_text = orch.handle("请先分析结构，然后执行代码")
    assert "完成阶段 2" in reply_text
    # 历史记录中包含了各阶段执行
    assert len(orch.history) >= 4


def test_planner_json_parsing():
    from agent.planner import Planner
    from unittest.mock import MagicMock

    mock_llm = MagicMock()
    mock_llm.chat.return_value = Message(
        role="assistant",
        content="""```json
{
  "goal": "分析数据并出图",
  "steps": [
    {"desc": "加载数据", "tools": ["file_read"], "done_when": "数据已读入"},
    {"desc": "生成图表", "tools": ["python_exec"], "done_when": "生成 chart.png"}
  ]
}
```"""
    )
    planner = Planner(mock_llm)
    p = planner.make_plan("请帮我分析数据并出图")
    assert p.goal == "分析数据并出图"
    assert len(p.steps) == 2
    assert p.steps[0].desc == "加载数据"
    assert p.steps[0].tools == ["file_read"]
    assert p.steps[0].done_when == "数据已读入"
    assert p.steps[1].tools == ["python_exec"]


