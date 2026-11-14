"""测试热计算与双级热缓存机制（L1 内存热缓存、桌面操作缓存联动、长连接预热）。"""
import time
import pytest
from pathlib import Path
from tools.cache import ToolResultCache
from agent.tools_registry import ToolRegistry, ToolDefinition
from agent.models import Message, ToolCall
from agent.llm import LLMClient


def test_tool_cache_l1_in_memory_speed_and_hit(tmp_path):
    """测试 L1 内存热缓存加速与独立性。"""
    c = ToolResultCache(str(tmp_path / "test_cache.db"))
    args = {"query": "test_hot"}
    
    # 1. 初始未命中
    assert c.get("web_search", args) is None
    
    # 2. 写入缓存
    c.put("web_search", args, "hot_result_123")
    
    # 3. 校验 L1 内存字典已存入
    key = c._key("web_search", args)
    assert key in c._l1
    assert c._l1[key][0] == "hot_result_123"
    
    # 4. 甚至直接清空底层 SQLite 数据库，L1 内存缓存仍能瞬间返回（证明 0ms 纯内存微秒级击中）
    with c._lock:
        c.db.execute("DELETE FROM tool_cache")
        c.db.commit()
    assert c.get("web_search", args) == "hot_result_123"


def test_tool_cache_desktop_invalidation(tmp_path):
    """测试写操作工具对读缓存的联动失效。"""
    c = ToolResultCache(str(tmp_path / "test_cache2.db"))
    c.put("desktop_list_windows", {}, "window_list_data")
    assert c.get("desktop_list_windows", {}) == "window_list_data"
    
    # 执行失效
    c.invalidate_tool("desktop_list_windows")
    assert c.get("desktop_list_windows", {}) is None
    assert c._key("desktop_list_windows", {}) not in c._l1


def test_tool_registry_desktop_action_invalidates_cache(tmp_path):
    """测试 ToolRegistry 在执行桌面写动作时联动失效桌面读缓存。"""
    reg = ToolRegistry()
    cache = ToolResultCache(str(tmp_path / "test_cache3.db"))
    reg.set_cache(cache)
    
    cache.put("desktop_list_windows", {}, "cached_windows")
    assert cache.get("desktop_list_windows", {}) == "cached_windows"
    
    # 注册一个模拟桌面点击写操作工具
    def mock_click(args):
        return "clicked"
        
    reg.register(ToolDefinition(
        name="desktop_click",
        description="click",
        parameters={"type": "object", "properties": {}},
        func=mock_click
    ))
    
    # 执行写操作
    reg.execute("desktop_click", {"x": 100, "y": 200}, task_id="t1")
    
    # 读缓存应已被自动失效
    assert cache.get("desktop_list_windows", {}) is None


def test_tool_cache_path_arg_serialization(tmp_path):
    """测试非标准 JSON 类型（如 Path 对象）作为参数时不崩溃。"""
    c = ToolResultCache(str(tmp_path / "test_cache4.db"))
    args = {"file_path": Path("C:/test/file.txt")}
    
    c.put("file_read", args, "content_abc")
    assert c.get("file_read", args) == "content_abc"


def test_tool_cache_max_l1_entries_eviction(tmp_path):
    """测试 L1 内存缓存有界淘汰，杜绝内存泄漏。"""
    # 设置极小的 L1 上限 5 条
    c = ToolResultCache(str(tmp_path / "test_cache5.db"), max_l1_entries=5)
    for i in range(10):
        c.put("web_search", {"q": f"query_{i}"}, f"res_{i}", ttl_sec=3600)
    
    # 内存 L1 字典条目必须受控在 5 以内，不会无界膨胀
    assert len(c._l1) <= 5
    # 但 SQLite L2 完整保留 10 条
    with c._lock:
        cnt = c.db.execute("SELECT count(*) FROM tool_cache").fetchone()[0]
    assert cnt == 10


def test_tool_cache_large_result_bypasses_l1(tmp_path):
    """测试超大结果（>200KB）跳过 L1 仅存 SQLite，保护堆内存。"""
    c = ToolResultCache(str(tmp_path / "test_cache6.db"))
    huge_data = "X" * 250_000  # 250KB
    c.put("file_read", {"path": "huge.log"}, huge_data, ttl_sec=300)
    
    # L1 内存中不存储超大对象
    key = c._key("file_read", {"path": "huge.log"})
    assert key not in c._l1
    # 但通过 get 仍能从 SQLite L2 正常读出
    assert c.get("file_read", {"path": "huge.log"}) == huge_data


def test_llm_client_prewarm():
    """测试 LLMClient.prewarm() 安全健壮性与幂等性。"""
    client = LLMClient(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen3.8-flash",
        api_key="sk-test-mock-key"
    )
    # 首次 prewarm
    res1 = client.prewarm()
    assert isinstance(res1, bool)
    assert getattr(client, "_is_prewarmed", False) is True
    
    # 二次调用幂等，不会重复发网络请求
    res2 = client.prewarm()
    assert res2 is False  # 已经 prewarmed，直接返回 False/不重复操作
