"""Operation-based risks: synthetic tools/approvals only, never real UI or shell."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.approval import ApprovalManager
from agent.risk import classify_tool_risk
from agent.tools_registry import ToolDefinition, ToolRegistry


DAILY = [
    ("desktop_wait", {"seconds": 20}),
    ("desktop_click", {"snapshot_id": "shot", "x": 200, "y": 100}),
    ("desktop_type", {"text": "Please send a poster about data format and payment."}),
    ("desktop_press", {"key": "tab"}),
    ("desktop_press", {"name": "return"}),
    ("desktop_press", {"key": "backspace"}),
    ("desktop_hotkey", {"keys": ["controlleft", "a"]}),
    ("desktop_hotkey", {"keys": "ctrl+f"}),
    ("desktop_scroll", {"amount": -3}),
    ("desktop_hover", {"snapshot_id": "shot", "x": 200, "y": 100}),
    ("desktop_drag", {"snapshot_id": "shot", "start_x": 100, "start_y": 100, "end_x": 200, "end_y": 200}),
    ("desktop_probe_app", {"window_title": "Postman"}),
    ("desktop_screenshot", {"format": "png", "out_dir": "posters"}),
    ("desktop_snapshot", {"window_title": "Postman - payment information"}),
    ("desktop_open_app", {"app_name": "Postman"}),
    ("desktop_list_windows", {}),
    ("desktop_show_desktop", {}),
    ("browser_navigate", {"url": "https://postman.example/docs/format"}),
    ("browser_snapshot", {}),
    ("browser_screenshot", {"out_dir": "publish/reports"}),
    ("browser_wait", {"seconds": 10, "reason": "wait for poster generation"}),
    ("browser_click", {"selector": "#send-button"}),
    ("browser_click_at", {"x": 500, "y": 300}),
    ("browser_type", {"selector": "#format", "text": "publish a fictional poster", "press_enter": True}),
    ("browser_save_image", {"out_path": "poster.png"}),
    ("browser_request_takeover", {"reason": "payment label in unrelated page header"}),
    ("browser_new_tab", {"url": "https://example.com", "role": "mailbox"}),
    ("browser_switch_tab", {"role": "mailbox"}),
    ("browser_close_tab", {"role": "register"}),
    ("state_pin", {"key": "email", "value": "a@b.com"}),
    ("state_check", {"key": "email", "value": "a@b.com"}),
    ("file_read", {"path": "posters/format.md"}),
    ("web_search", {"query": "How to uninstall software?"}),
    ("memory_search", {"query": "publish"}),
]


@pytest.mark.parametrize("name,args", DAILY)
def test_routine_operations_are_l1_even_for_untrusted_provenance(name, args):
    manager = ApprovalManager(":memory:")
    try:
        assert manager.classify(name, args) == classify_tool_risk(name, args) == "L1"
        assert ToolRegistry._risk_of(name, args, 2) == "L1"
        assert manager.check(name, args, prov_level=2, on_ask=lambda _: pytest.fail("routine operation asked"))
    finally:
        manager.db.close()


@pytest.mark.parametrize("name,args", DAILY)
def test_persisted_ask_does_not_prompt_for_routine_operations(name, args):
    manager = ApprovalManager(":memory:", settings=SimpleNamespace(get=lambda _: "ask"))
    registry = ToolRegistry()
    registry.register(ToolDefinition(name=name, description="synthetic", parameters={}, func=lambda _: "executed"))
    registry.set_approval(manager, on_ask=lambda _: pytest.fail("legacy ask must not prompt"))
    try:
        assert registry.execute(name, args).content == "executed"
    finally:
        manager.db.close()


@pytest.mark.parametrize("name,args", DAILY)
def test_explicit_denial_remains_authoritative(name, args):
    manager = ApprovalManager(":memory:", settings=SimpleNamespace(get=lambda _: "deny"))
    try:
        assert manager.check(name, args, prov_level=2, on_ask=lambda _: pytest.fail("denied operation asked")) is False
        assert manager.stats(name) == {"approved": 0, "rejected": 1}
    finally:
        manager.db.close()


@pytest.mark.parametrize("command", [
    "where chrome", "WHERE chrome", "dir /b C:\\Downloads", "ls ./downloads",
    "pwd", "whoami", "hostname", "tasklist", "which python",
    'findstr /n error "poster.txt"', 'type "C:\\Reports\\format.txt"', "cat poster.txt",
    'echo "format publish delete payment post send"',
])
def test_only_single_known_readonly_shell_commands_are_l1(command):
    assert classify_tool_risk("shell_exec", {"command": command}) == "L1"
    assert ToolRegistry._risk_of("shell_exec", {"command": command}, 2) == "L1"


@pytest.mark.parametrize("command", [
    "where chrome & echo hi", "where chrome && del report.txt", "where chrome | python script.py",
    "echo hello > report.txt", "echo hello >> report.txt", "type < report.txt",
    "echo $(whoami)", "echo `whoami`", "echo %COMSPEC%", "echo !SHELL!", "echo ^& stuff",
    "dir (nested)", "where chrome\necho hi", "where chrome; echo hi", "where chrome\x00ignored",
    "python -c \"print(1)\"", "node script.js", "git status", "git commit -am change",
    "start chrome https://example.com", "Get-Command chrome", "curl https://example.com",
    "set X=changed", "env", "", 'where "unterminated',
])
def test_compound_or_unrecognized_shell_retains_approval(command):
    risk = classify_tool_risk("shell_exec", {"command": command})
    assert risk in {"L2", "L3"}
    assert ToolRegistry._risk_of("shell_exec", {"command": command}, 2) == "L3"


@pytest.mark.parametrize("name,args", [
    ("file_delete", {"path": "a.txt"}), ("payment", {"amount": 1}), ("publish", {}),
    ("send_email", {"text": "hello"}), ("shutdown", {}),
    ("browser_click", {"selector": "#button", "action": "delete"}),
    ("desktop_click", {"operation": "PAYMENT"}),
    ("browser_type", {"text": "harmless", "action": "publish-post"}),
    ("desktop_hotkey", {"keys": ["shiftleft", "delete"]}),
    ("desktop_hotkey", {"keys": "ControlLeft+AltRight+Del"}),
    ("desktop_hotkey", {"keys": ["rightalt", "F4"]}),
    ("desktop_press", {"key": "delete"}),
    ("shell_exec", {"command": "del report.txt"}),
    ("shell_exec", {"command": "Remove-Item report.txt"}),
    ("shell_exec", {"command": "git push origin main"}),
    ("shell_exec", {"command": "format C:"}),
])
def test_explicit_dangerous_operation_is_l3_and_tool_allow_cannot_bypass_it(name, args):
    manager = ApprovalManager(":memory:", settings=SimpleNamespace(get=lambda _: "allow"))
    cards = []
    try:
        assert classify_tool_risk(name, args) == "L3"
        assert ToolRegistry._risk_of(name, args, 0) == "L3"
        assert manager.check(name, args, on_ask=lambda card: (cards.append(card) or False, "once")) is False
        assert len(cards) == 1 and cards[0].risk == "L3"
    finally:
        manager.db.close()


def test_write_content_is_data_and_unknown_operations_keep_provenance_escalation():
    assert classify_tool_risk("file_write", {"path": "publish.md", "content": "send payment; format disk"}) == "L2"
    assert ToolRegistry._risk_of("file_write", {"path": "publish.md"}, 0) == "L2"
    assert ToolRegistry._risk_of("file_write", {"path": "publish.md"}, 2) == "L3"
    assert classify_tool_risk("unknown_tool", {"text": "delete"}) == "L2"
    manager = ApprovalManager(":memory:")
    cards = []
    try:
        assert manager.check("file_write", {"path": "a.txt"}, prov_level=2,
                             on_ask=lambda card: (cards.append(card) or True, "once"))
        assert cards[0].risk == "L3"
    finally:
        manager.db.close()


def test_dangerous_operations_never_use_old_permanent_whitelist_or_create_new_one():
    manager = ApprovalManager(":memory:")
    args = {"path": "a.txt"}
    key = manager._wkey("file_delete", args)
    manager.db.execute("INSERT INTO whitelist VALUES (?, ?, ?)", (key, "file_delete", 0))
    manager.db.commit()
    try:
        assert manager.check("file_delete", args) is False
        manager.revoke(key)
        assert manager.check("file_delete", args, on_ask=lambda _: (True, "always")) is True
        assert manager.list_whitelist() == []
        assert manager.check("file_delete", args) is False
    finally:
        manager.db.close()


def test_daily_l1_classification_cannot_skip_desktop_evidence_validation():
    from tools.desktop_runtime import begin_turn, clear_scope, end_turn
    token = begin_turn("risk-evidence-regression")
    calls = []
    def fake_click(args):
        calls.append(args)
        return "executed"
    fake_click.__module__ = "tools.desktop"
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="desktop_click", description="synthetic", parameters={}, func=fake_click))
    approval = MagicMock()
    registry.set_approval(approval)
    try:
        result = registry.execute("desktop_click", {"x": 100, "y": 100})
        assert "验证失败" in result.content
        assert calls == []
        approval.check.assert_not_called()
    finally:
        end_turn(token)
        clear_scope("risk-evidence-regression")


def test_registry_without_approval_manager_uses_same_daily_classification():
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="desktop_wait", description="synthetic", parameters={}, func=lambda _: "waited"))
    registry.register(ToolDefinition(name="shell_exec", description="synthetic", parameters={}, func=lambda _: "queried"))
    assert registry.execute("desktop_wait", {"seconds": 20}).content == "waited"
    assert registry.execute("shell_exec", {"command": "where chrome"}).content == "queried"
    assert "审批未通过" in registry.execute("shell_exec", {"command": "python script.py"}).content