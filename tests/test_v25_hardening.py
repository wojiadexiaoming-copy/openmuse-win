"""v2.5 加固批次回归：V1/V2/V3（前端注入面与 token）、U1（审批事件优先投递）、
V4（缓存并发）、P1（session 水位差量正确性）。
"""
import queue
import subprocess
import shutil
import threading
from pathlib import Path

import pytest

from agent.models import Message
from agent.session_store import SessionStore
from tools.cache import ToolResultCache
from ui.server import EventBus

TESTS_DIR = Path(__file__).parent
APP_JS = TESTS_DIR.parent / "ui" / "static" / "app.js"


def test_ui_esc_jsq_md_token_regressions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend injection regressions")
    script = TESTS_DIR / "test_ui_esc_and_token.js"
    result = subprocess.run([node, str(script)], capture_output=True, text=True,
                            encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_eventbus_priority_approval_not_dropped_when_full():
    """U1：队列满时普通事件丢弃（旧行为），approval_request 必须挤入投递。"""
    bus = EventBus()
    q = bus.subscribe()
    for i in range(100):  # 灌满
        bus.broadcast("progress", {"n": i})
    # 普通事件满 → 丢弃
    bus.broadcast("progress", {"n": 999})
    assert q.qsize() == 100
    assert "n\": 999" not in "".join(list(q.queue))
    # 审批事件满 → 挤掉最旧一条普通事件，审批消息必达
    bus.broadcast("approval_request", {"approval_id": "ap-1"})
    assert q.qsize() == 100
    msgs = list(q.queue)
    assert any("approval_request" in m and "ap-1" in m for m in msgs), \
        "审批事件不得被静默丢弃"


def test_tool_cache_concurrent_access_smoke():
    """V4：多线程并发 get/put/invalidate 不得抛 sqlite3 线程错误。"""
    import tempfile, os
    cache = ToolResultCache(os.path.join(tempfile.mkdtemp(), "cache.db"))
    errors: list[Exception] = []

    def worker(n: int):
        try:
            for i in range(60):
                cache.put("web_search", {"q": f"{n}-{i}"}, f"r{i}", ttl_sec=60)
                cache.get("web_search", {"q": f"{n}-{i}"})
                if i % 10 == 0:
                    cache.invalidate_tool("web_search")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"并发缓存访问出现异常: {errors[:3]}"


def _mk_session(tmp_path) -> SessionStore:
    return SessionStore(str(tmp_path / "conversations.db"))


def test_session_watermark_roundtrip_and_partial_update(tmp_path):
    """P1：水位差量在往返 / 局部更新 / 截断 / 重载场景下结果与全量一致。"""
    store = _mk_session(tmp_path)
    sid = store.new_session()
    history = [Message(role="user", content="hello"),
               Message(role="assistant", content="world")]
    store.save_history(sid, history)
    assert [m.content for m in store.load_history(sid)] == ["hello", "world"]

    # 第二次保存（未变更）→ 走水位跳过，内容不变
    store.save_history(sid, history)
    assert [m.content for m in store.load_history(sid)] == ["hello", "world"]

    # 修改中间一条 → 只更新该行，其余保持
    history[1].content = "world!"
    store.save_history(sid, history)
    assert [m.content for m in store.load_history(sid)] == ["hello", "world!"]

    # 历史缩短 → 尾部行删除
    del history[1]
    store.save_history(sid, history)
    assert [m.content for m in store.load_history(sid)] == ["hello"]

    # 追加新行
    history.append(Message(role="user", content="again"))
    store.save_history(sid, history)
    assert [m.content for m in store.load_history(sid)] == ["hello", "again"]


def test_session_watermark_survives_reload_cycle(tmp_path):
    """P1：load → save（未变更）循环下行数与内容稳定，不产生重复/丢失。"""
    store = _mk_session(tmp_path)
    sid = store.new_session()
    history = [Message(role="user", content=f"m{i}") for i in range(30)]
    store.save_history(sid, history)
    for _ in range(3):
        loaded = store.load_history(sid)
        store.save_history(sid, loaded)
        rows = store.db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
        assert rows == 30
    contents = [m.content for m in store.load_history(sid)]
    assert contents == [f"m{i}" for i in range(30)]
