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


def test_sec01_prototype_pollution_blocked(registry):
    r = registry.execute("file_write", {"__class__": {"__init__": {}}, "path": "a.txt", "content": "123"})
    assert "[参数校验失败]" in r.content
    assert "__class__" in r.content


def test_sec01_missing_required_param(registry):
    r = registry.execute("file_write", {"path": "a.txt"})
    assert "[参数校验失败]" in r.content
    assert "content" in r.content


def test_perf02_provenance_isolated_cache(registry, tmp_path):
    from tools.cache import ToolResultCache
    cache = ToolResultCache(str(tmp_path / "cache.db"))
    registry.set_cache(cache)

    # 1. 以 L1 溯源等级写入并缓存
    r1 = registry.execute("file_read", {"path": "a.txt"}, _provenance={"level": 1})
    assert "[缓存命中]" not in r1.content

    # 2. 再次以 L1 请求 -> 应该命中缓存
    r2 = registry.execute("file_read", {"path": "a.txt"}, _provenance={"level": 1})
    assert "[缓存命中]" in r2.content

    # 3. 以 L2 (不可信源) 请求相同参数 -> 必须不能命中 L1 缓存！
    r3 = registry.execute("file_read", {"path": "a.txt"}, _provenance={"level": 2})
    assert "[缓存命中]" not in r3.content
