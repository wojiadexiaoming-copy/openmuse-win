"""v3.3：shell 审批前预校验、curl URL & 放行、拒绝熔断与进展口径。"""
from types import SimpleNamespace

from unittest.mock import MagicMock

from agent.context import ContextBuilder
from agent.models import Message, ToolCall
from agent.orchestrator import Orchestrator
from agent.tools_registry import ToolDefinition, ToolRegistry
from tools.shell_exec import SHELL_TOOL, validate_command


def test_validate_command_allows_ampersand_inside_curl_url():
    assert validate_command('curl "https://example.com/api?from=A&to=B"') is None
    assert validate_command("curl https://example.com/api?from=A&to=B") is None


def test_validate_command_still_rejects_shell_chaining():
    for cmd in ("dir && whoami", "dir | findstr x", "dir > out.txt",
                "cd /tmp", "echo a; whoami"):
        err = validate_command(cmd)
        assert err and err.startswith("[拒绝]"), cmd


def test_validate_command_rejects_ampersand_outside_url_tokens():
    err = validate_command("curl https://example.com & whoami")
    assert err and "元字符" in err


def test_shell_tool_precheck_wired():
    assert SHELL_TOOL.precheck is not None
    assert SHELL_TOOL.precheck({"command": "cd /tmp"}).startswith("[拒绝]")
    assert SHELL_TOOL.precheck({"command": 'curl "https://a?b=1&c=2"'}) is None


def _guarded_registry(asked, ran, precheck):
    reg = ToolRegistry()
    reg.set_approval(
        SimpleNamespace(check=lambda *a, **k: asked.append(1) or True),
        on_ask=lambda card: (True, "once"),
    )
    reg.register(ToolDefinition(
        name="guarded", description="t",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        func=lambda args: ran.append(1) or "executed",
        precheck=precheck,
    ))
    return reg


def test_precheck_blocks_before_approval_and_execution():
    asked, ran = [], []
    reg = _guarded_registry(asked, ran, lambda args: "[拒绝] 预校验失败")
    msg = reg.execute("guarded", {"command": "cd /tmp"})
    assert msg.content.startswith("[拒绝]")
    assert asked == [], "预校验已拒绝，不得再向用户弹审批"
    assert ran == [], "预校验已拒绝，不得执行工具"


def test_precheck_passes_through_to_approval_and_execution():
    asked, ran = [], []
    reg = _guarded_registry(asked, ran, lambda args: None)
    msg = reg.execute("guarded", {"command": "echo hi"})
    assert asked == [1] and ran == [1]
    assert msg.content == "executed"


def _bare_orchestrator():
    orch = Orchestrator.__new__(Orchestrator)
    orch._shell_reject_counts = {}
    return orch


def _shell_message(content):
    return Message(role="tool", name="shell_exec", content=content)


def test_shell_rejections_escalate_then_block():
    orch = _bare_orchestrator()
    rejection = "[拒绝] 不在白名单: cd（可用命令: dir, echo）"
    first = orch._track_shell_rejection(_shell_message(rejection))
    assert "[命令策略熔断]" not in first.content
    second = orch._track_shell_rejection(_shell_message(rejection))
    assert "[命令策略熔断]" in second.content
    assert orch._shell_circuit_block("cd /tmp").startswith("[拒绝] [命令策略熔断]")
    assert orch._shell_circuit_block("echo ok") is None


def test_shell_success_resets_reject_counts():
    orch = _bare_orchestrator()
    orch._shell_reject_counts = {"whitelist": 2}
    orch._track_shell_rejection(_shell_message("exit=0\nok"))
    assert orch._shell_reject_counts == {}
    assert orch._shell_circuit_block("cd /tmp") is None


def test_execute_tool_third_same_category_is_blocked_without_approval():
    asked = []
    reg = ToolRegistry()
    reg.register(SHELL_TOOL)
    reg.set_approval(SimpleNamespace(
        check=lambda *a, **k: asked.append(1) or True))
    llm = MagicMock()
    llm.model = "offline"
    orch = Orchestrator(llm, reg, ContextBuilder(max_tokens=4000))
    orch._pending_tool_results = []
    orch._shell_reject_counts = {}

    first = orch._execute_tool("shell_exec", {"command": "cd /tmp"})
    second = orch._execute_tool("shell_exec", {"command": "cd /var"})
    third = orch._execute_tool("shell_exec", {"command": "cd /home"})

    assert first.content.startswith("[拒绝]")
    assert "[命令策略熔断]" in second.content
    assert third.content.startswith("[拒绝] [命令策略熔断]")
    assert asked == [], "预校验拒绝的命令一次都不应进入审批；第 3 次直接熔断"


def test_rejection_does_not_count_as_progress():
    calls = [ToolCall(id="a", name="shell_exec", arguments={"command": "cd /tmp"})]
    results = [Message(role="tool", tool_call_id="a", name="shell_exec",
                       content="[拒绝] 不在白名单: cd")]
    assert Orchestrator._progress_signature(calls, results) == ()
