"""§7.3 工具结果缓存：只读工具按参数 hash 缓存，写操作后清相关读缓存。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time


class ToolResultCache:
    # TTL（秒）。不在表里的工具不缓存。
    TTL = {
        "web_search": 3600,
        "file_read": 300,
        "memory_search": 600,
    }

    def __init__(self, db_path: str):
        # v2.5 V4：check_same_thread=False 必须配套跨线程锁（对齐 agent/db.py 模式），
        # 否则 UI 线程 / 编排线程 / 并行 worker 并发命中缓存时连接被并发使用。
        self._lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS tool_cache"
                "(ckey TEXT PRIMARY KEY, tool TEXT, result TEXT, expires_at REAL)")

    @staticmethod
    def _key(tool: str, args: dict, prov_level: int = 0) -> str:
        raw = f"{tool}:{prov_level}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, tool: str, args: dict, prov_level: int = 0) -> str | None:
        with self._lock:
            row = self.db.execute(
                "SELECT result FROM tool_cache WHERE ckey=? AND expires_at>?",
                (self._key(tool, args, prov_level=prov_level), time.time())).fetchone()
        return row[0] if row else None

    def put(self, tool: str, args: dict, result: str, ttl_sec: int | None = None, ttl: int | None = None, prov_level: int = 0):
        effective_ttl = ttl_sec if ttl_sec is not None else ttl
        if effective_ttl is None or effective_ttl <= 0:
            effective_ttl = self.TTL.get(tool, 0)
        if effective_ttl <= 0:
            return
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO tool_cache VALUES (?,?,?,?)",
                (self._key(tool, args, prov_level=prov_level), tool, result,
                 time.time() + effective_ttl))
            self.db.commit()

    def invalidate_tool(self, tool: str):
        """某工具的写操作成功后，清掉该工具的读缓存（如 file_write → file_read）。"""
        with self._lock:
            self.db.execute("DELETE FROM tool_cache WHERE tool=?", (tool,))
            self.db.commit()

    def sweep(self):
        """清过期条目（定时 Job 调）。"""
        with self._lock:
            self.db.execute("DELETE FROM tool_cache WHERE expires_at<?",
                            (time.time(),))
            self.db.commit()
