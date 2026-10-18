"""agent/session_store.py archive_old_sessions 测试。"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time

import pytest

from agent.session_store import SessionStore
from agent.models import Message


def _make_store(path: str) -> SessionStore:
    return SessionStore(path)


def _add_session_with_msgs(store: SessionStore, n_msgs: int = 3) -> str:
    sid = store.new_session(title="test")
    msgs = [Message(role="user", content=f"msg {i}") for i in range(n_msgs)]
    store.save_history(sid, msgs)
    return sid


def test_archive_moves_old_sessions_to_files():
    """旧 session 被导出为独立 db 文件，主库只剩 keep_session_id。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "conv.db")
        arc_dir = os.path.join(tmp, "archive")
        store = _make_store(db_path)

        sid_old1 = _add_session_with_msgs(store, 3)
        sid_old2 = _add_session_with_msgs(store, 2)
        sid_keep = _add_session_with_msgs(store, 4)

        archived = store.archive_old_sessions(arc_dir, keep_session_id=sid_keep)
        store.close()

        assert set(archived) == {sid_old1, sid_old2}

        # 主库只剩 keep（重新打开验证）
        store2 = _make_store(db_path)
        sessions = store2.list_sessions(limit=50)
        store2.close()
        assert [s["session_id"] for s in sessions] == [sid_keep]

        # 归档文件存在且内容完整
        for sid in (sid_old1, sid_old2):
            files = [f for f in os.listdir(arc_dir) if sid in f]
            assert len(files) == 1, f"archive file for {sid} not found"
            arc_path = os.path.join(arc_dir, files[0])
            con = sqlite3.connect(arc_path)
            cnt = con.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
            con.close()
            assert cnt > 0


def test_archive_idempotent():
    """重复调用不报错，也不重复写文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "conv.db")
        arc_dir = os.path.join(tmp, "archive")
        store = _make_store(db_path)

        sid_old = _add_session_with_msgs(store, 2)
        sid_keep = _add_session_with_msgs(store, 1)

        store.archive_old_sessions(arc_dir, keep_session_id=sid_keep)
        store.archive_old_sessions(arc_dir, keep_session_id=sid_keep)
        store.close()

        files = os.listdir(arc_dir)
        assert len(files) == 1  # 不重复创建


def test_archive_keep_none_archives_all():
    """keep_session_id=None 时归档全部 session。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "conv.db")
        arc_dir = os.path.join(tmp, "archive")
        store = _make_store(db_path)

        sid1 = _add_session_with_msgs(store, 1)
        sid2 = _add_session_with_msgs(store, 1)

        archived = store.archive_old_sessions(arc_dir, keep_session_id=None)
        store.close()
        assert set(archived) == {sid1, sid2}

        store2 = _make_store(db_path)
        result = store2.list_sessions(limit=50)
        store2.close()
        assert result == []


def test_archive_preserves_message_content():
    """归档文件里消息内容与原始完全一致。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "conv.db")
        arc_dir = os.path.join(tmp, "archive")
        store = _make_store(db_path)

        sid = store.new_session(title="preserve-test")
        msgs = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="world"),
        ]
        store.save_history(sid, msgs)
        store.archive_old_sessions(arc_dir, keep_session_id=None)
        store.close()

        files = [f for f in os.listdir(arc_dir) if sid in f]
        assert files
        con = sqlite3.connect(os.path.join(arc_dir, files[0]))
        rows = con.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY seq", (sid,)
        ).fetchall()
        con.close()
        assert rows == [("user", "hello"), ("assistant", "world")]
