"""Batch 5 自动化验收测试：工具面收敛与一致性。

覆盖范围：
1. B5-01: ToolDefinition 作为工具元数据唯一真相源 (parallel_safe / is_readonly / cacheable / cache_ttl_sec)。
2. B5-02 & B5-05: 幽灵工具与冗余别名收敛：openai_schemas() 隐藏别名，而 execute() 透明重定向。
3. B5-03: 100% 工具注册中文化映射 (ACTION_CN_MAP) 与 tool_summary 零英文泄露。
4. B5-04: 全量工具五元组规范校验与 cache_ttl 自定义生效。
5. P1-01: AgentServer (UI) 与 main.py (CLI) 工具装配对齐。
"""
import asyncio
import inspect
from pathlib import Path
import pytest

from agent.tools_registry import ToolDefinition, ToolRegistry
from agent.parallel import execute_tool_calls, _attrs
from agent.models import ToolCall, Message
from agent.approval import ApprovalManager
from tools.cache import ToolResultCache
from tools import register_all
import tools.desktop as dt
from agent.state_text import ACTION_CN_MAP, tool_summary, result_summary


def test_b5_01_tool_definition_single_source_of_truth():
    """验证 ToolDefinition 为属性唯一真相源，Orchestrator 并行引擎动态读取 registry 中的 tool."""
    reg = ToolRegistry()
    reg.set_approval(ApprovalManager(":memory:"), on_ask=lambda card: (True, "once"))

    # 注册一个自定义工具，明确标记 parallel_safe=False, is_readonly=False
    reg.register(ToolDefinition(
        name="custom_unsafe_tool",
        description="test unsafe",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "done_unsafe",
        parallel_safe=False,
        is_readonly=False,
    ))
    # 注册一个自定义工具，明确标记 parallel_safe=True, is_readonly=True
    reg.register(ToolDefinition(
        name="custom_safe_tool",
        description="test safe",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "done_safe",
        parallel_safe=True,
        is_readonly=True,
    ))

    # 1. 验证属性由 ToolDefinition 决定，_attrs 动态读取
    attrs_unsafe = _attrs(reg, "custom_unsafe_tool")
    assert attrs_unsafe["parallel_safe"] is False
    assert attrs_unsafe["is_readonly"] is False

    attrs_safe = _attrs(reg, "custom_safe_tool")
    assert attrs_safe["parallel_safe"] is True
    assert attrs_safe["is_readonly"] is True

    # 2. 验证 execute_tool_calls 依据 registry 的定义进行并发与串行判定
    calls = [
        ToolCall(id="c1", name="custom_unsafe_tool", arguments={}),
        ToolCall(id="c2", name="custom_safe_tool", arguments={}),
    ]
    results = asyncio.run(execute_tool_calls(calls, reg))
    assert len(results) == 2
    assert results[0].content == "done_unsafe"
    assert results[1].content == "done_safe"


def test_b5_02_and_b5_05_alias_suppression_and_redirection():
    """验证别名从 openai_schemas() 中隐藏，同时调用时透明重定向至正规实现。"""
    reg = ToolRegistry()
    reg.set_approval(ApprovalManager(":memory:"), on_ask=lambda card: (True, "once"))
    target_executed = []

    def _sample_target(args: dict):
        target_executed.append(args.get("action", ""))
        return f"target_ok:{args.get('action')}"

    reg.register(ToolDefinition(
        name="canonical_tool",
        description="正规工具",
        parameters={"type": "object", "properties": {"action": {"type": "string"}}},
        func=_sample_target,
    ))
    reg.register_alias("alias_tool", "canonical_tool")

    # 1. 验证 schemas 中只有正规工具，别名被过滤以节省 token
    schemas = reg.openai_schemas()
    schema_names = [s["function"]["name"] for s in schemas]
    assert "canonical_tool" in schema_names
    assert "alias_tool" not in schema_names

    # 2. 验证调用别名时，透明重定向并执行目标工具
    res = reg.execute("alias_tool", {"action": "ping"})
    assert isinstance(res, Message)
    assert res.content == "target_ok:ping"
    assert target_executed == ["ping"]

    # 3. 验证 get 和 contains
    assert "alias_tool" in reg
    assert reg.get("alias_tool").name == "canonical_tool"


def test_b5_02_desktop_aliases_registered():
    """验证 desktop 中的 wait/sleep/desktop_open/desktop_list_shortcuts 透明重定向生效。"""
    reg = ToolRegistry()
    cfg = {"agent": {"workspace": "."}, "tools": {"browser": {"enabled": False}, "desktop": {"enabled": True}}}
    register_all(reg, cfg)

    # 验证 schema 中不存在别名
    schemas = reg.openai_schemas()
    schema_names = [s["function"]["name"] for s in schemas]
    for alias in dt.DESKTOP_ALIASES:
        assert alias not in schema_names, f"别名 {alias} 不应暴露在 LLM schemas 中"

    # 验证 schema 中存在规范工具
    assert "desktop_open_app" in schema_names
    assert "desktop_list_items" in schema_names
    assert "desktop_wait" in schema_names

    # 验证别名在 registry 中透明可用
    for alias, target in dt.DESKTOP_ALIASES.items():
        assert alias in reg
        assert reg.get(alias).name == target


def test_b5_03_action_cn_map_full_coverage_and_zero_leak():
    """验证系统中所有已注册工具与别名 100% 具备中文翻译，tool_summary 无英文泄露。"""
    reg = ToolRegistry()
    cfg = {"agent": {"workspace": "."}, "tools": {"browser": {"enabled": True}, "desktop": {"enabled": True}}}
    register_all(reg, cfg)

    from agent.connectors import ConnectorManager, make_connector_tools
    from agent.proactivity import ProactivityStore, make_proactivity_tools
    from agent.workflow import WorkflowEngine, make_workflow_tools
    from agent.artifacts import ArtifactManager, make_artifact_tools
    from agent.tts import TTSManager, make_tts_tools
    from agent.backup import make_backup_tools
    from agent.subagent import SubagentManager, make_subagent_tools

    for t in make_connector_tools(ConnectorManager(vault=None)): reg.register(t)
    for t in make_proactivity_tools(ProactivityStore()): reg.register(t)
    for t in make_workflow_tools(WorkflowEngine()): reg.register(t)
    for t in make_artifact_tools(ArtifactManager()): reg.register(t)
    for t in make_tts_tools(TTSManager()): reg.register(t)
    for t in make_backup_tools(): reg.register(t)
    for t in make_subagent_tools(SubagentManager(make_orchestrator=lambda x: None)): reg.register(t)

    all_names = set(reg._tools.keys()) | set(reg._aliases.keys())
    all_names.add("schedule_once")

    # 1. 验证 100% 覆盖
    missing = [n for n in sorted(all_names) if n not in ACTION_CN_MAP]
    assert not missing, f"工具缺少 ACTION_CN_MAP 中文映射: {missing}"

    # 2. 验证 tool_summary 人话无英文代码泄露
    leaks = []
    for n in sorted(all_names):
        summary = tool_summary(n, {})
        if n in summary:
            leaks.append((n, summary))
    assert not leaks, f"tool_summary 出现英文工具名裸露: {leaks}"


def test_b5_04_cache_ttl_and_cacheable_adherence(tmp_path):
    """验证 ToolDefinition 的 cacheable 与 cache_ttl_sec 在执行时被正确应用。"""
    cache_db = str(tmp_path / "cache.db")
    cache = ToolResultCache(cache_db)

    calls = 0
    def _dynamic_calc(args: dict) -> str:
        nonlocal calls
        calls += 1
        return f"result_{calls}"

    reg = ToolRegistry()
    reg.set_approval(ApprovalManager(":memory:"), on_ask=lambda card: (True, "once"))
    reg.set_cache(cache)

    reg.register(ToolDefinition(
        name="cached_tool",
        description="带缓存工具",
        parameters={"type": "object", "properties": {"v": {"type": "string"}}},
        func=_dynamic_calc,
        cacheable=True,
        cache_ttl_sec=60,
    ))

    reg.register(ToolDefinition(
        name="uncached_tool",
        description="不带缓存工具",
        parameters={"type": "object", "properties": {"v": {"type": "string"}}},
        func=_dynamic_calc,
        cacheable=False,
    ))

    # 1. 验证带缓存工具命中缓存
    res1 = reg.execute("cached_tool", {"v": "1"}).content
    assert res1 == "result_1"
    assert calls == 1

    res2 = reg.execute("cached_tool", {"v": "1"}).content
    assert "result_1" in res2
    assert "[缓存命中]" in res2
    assert calls == 1  # 命中缓存，不增加调用

    # 2. 验证不带缓存工具每次重新计算
    res3 = reg.execute("uncached_tool", {"v": "1"}).content
    assert res3 == "result_2"
    assert calls == 2

    res4 = reg.execute("uncached_tool", {"v": "1"}).content
    assert res4 == "result_3"
    assert calls == 3


def test_p1_01_ui_server_and_cli_tool_parity(tmp_path, monkeypatch):
    """验证 AgentServer (UI) 注册了与 CLI main.py 一致的全套外围工具（P1-01 闭环）。"""
    monkeypatch.setenv("ALTEREGO_LLM_API_KEY", "sk-mock-batch5-key")
    from ui.server import AgentServer

    # 实例化 AgentServer
    server = AgentServer(data_dir=tmp_path)
    tools = server.registry._tools

    # 验证关键外围工具皆已装配
    expected_peripheral_tools = [
        "connector_auth_link",
        "gmail_list",
        "gmail_send",
        "proactivity_add",
        "proactivity_pending",
        "workflow_start",
        "workflow_status",
        "workflow_complete_step",
        "artifact_create",
        "artifact_list",
        "tts_synthesize",
        "backup_run",
        "backup_verify",
        "schedule_once",
        "spawn_subagent",
        "poll_subagent",
        "cancel_subagent",
    ]
    for tool_name in expected_peripheral_tools:
        assert tool_name in tools, f"AgentServer 缺少工具装配: {tool_name}"
