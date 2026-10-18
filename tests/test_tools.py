"""M1 测试：工具注册表 + 基础工具（不需要 LLM）。"""
from pathlib import Path

import pytest

from agent.tools_registry import ToolRegistry
from tools import register_all
from tools import file_tools


@pytest.fixture
def registry(tmp_path):
    file_tools.WORKSPACE = tmp_path.resolve()
    r = ToolRegistry()
    register_all(r, {"agent": {"workspace": str(file_tools.WORKSPACE)},
                     "tools": {"shell_exec": {"enabled": True}}})
    # 测试用审批器：自动通过（测的是工具逻辑，不是审批流；审批流见 test_approval.py）
    from agent.approval import ApprovalManager
    r.set_approval(ApprovalManager(":memory:"),
                   on_ask=lambda card: (True, "once"))
    return r


def test_register_all(registry):
    names = set(registry._tools)
    assert {"shell_exec", "file_read", "file_write", "file_edit"} <= names


def test_file_write_read_roundtrip(registry):
    w = registry.execute("file_write", {"path": "a.txt", "content": "hello"})
    assert "已写入" in w.content
    r = registry.execute("file_read", {"path": "a.txt"})
    assert "hello" in r.content


def test_file_edit_unique(registry):
    registry.execute("file_write", {"path": "b.txt", "content": "aaa bbb"})
    r = registry.execute("file_edit", {"path": "b.txt", "old": "bbb", "new": "ccc"})
    assert "已替换" in r.content
    r2 = registry.execute("file_read", {"path": "b.txt"})
    assert "ccc" in r2.content


def test_path_escape_blocked(registry):
    r = registry.execute("file_read", {"path": "../../etc/passwd"})
    assert "失败" in r.content or "越界" in r.content


def test_shell_whitelist(registry):
    r = registry.execute("shell_exec", {"command": "echo hi"})
    assert "hi" in r.content


def test_shell_blocked(registry):
    r = registry.execute("shell_exec", {"command": "rm -rf /"})
    assert "拒绝" in r.content


def test_unknown_tool(registry):
    with pytest.raises(ValueError):
        registry.get("nope")
