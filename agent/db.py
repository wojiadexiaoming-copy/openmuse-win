"""§4.x 数据库连接封装：所有 SQLite 必须走 get_db()。

Windows 上多线程并发写 SQLite 默认配置 100% 触发
`sqlite3.OperationalError: database is locked`。
本模块强制 WAL 模式 + 忙等待，消除该类崩溃。
"""
from __future__ import annotations

import sqlite3
import threading

_lock = threading.Lock()
_connections: dict[str, sqlite3.Connection] = {}


def get_db(db_path: str) -> sqlite3.Connection:
    """获取配置好的 SQLite 连接（线程安全复用）。

    强制配置：
    - WAL 模式：允许多个读 + 一个写并发
    - synchronous=NORMAL：提升 Windows 磁盘写入性能
    - busy_timeout=10s：锁冲突时等待而非直接抛异常
    - timeout=30s：连接级超时
    - check_same_thread=False：允许跨线程使用（调用方需自行加锁）
    """
    key = str(db_path)
    if key == ":memory:":
        conn = sqlite3.connect(
            key, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=10000;")
        return conn

    with _lock:
        conn = _connections.get(key)
        if conn is not None:
            try:
                conn.execute("SELECT 1;")
            except Exception:
                conn = None
        if conn is None:
            conn = sqlite3.connect(
                key, timeout=30.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
            _connections[key] = conn
        return conn
