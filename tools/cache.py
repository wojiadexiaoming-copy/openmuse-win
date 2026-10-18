"""§7.3 工具结果缓存：只读工具按参数 hash 缓存，写操作后清相关读缓存。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time


class ToolResultCache:
    # 工具 -> TTL（秒）。不在表里的工具不缓存。
    TTL = {
        "web_search": 3600,
        "file_read": 300,
        "memory_search": 600,
    }

    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tool_cache"
            "(ckey TEXT PRIMARY KEY, tool TEXT, result TEXT, expires_at REAL)")

    @staticmethod
    def _key(tool: str, args: dict) -> str:
        raw = f"{tool}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, tool: str, args: dict) -> str | None:
        if tool not in self.TTL:
            return None
        row = self.db.execute(
            "SELECT result FROM tool_cache WHERE ckey=? AND expires_at>?",
            (self._key(tool, args), time.time())).fetchone()
        return row[0] if row else None

    def put(self, tool: str, args: dict, result: str):
        if tool not in self.TTL:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO tool_cache VALUES (?,?,?,?)",
            (self._key(tool, args), tool, result,
             time.time() + self.TTL[tool]))
        self.db.commit()

    def invalidate_tool(self, tool: str):
        """某工具的写操作成功后，清掉该工具的读缓存（如 file_write → file_read）。"""
        self.db.execute("DELETE FROM tool_cache WHERE tool=?", (tool,))
        self.db.commit()

    def sweep(self):
        """清过期条目（定时 Job 调）。"""
        self.db.execute("DELETE FROM tool_cache WHERE expires_at<?",
                        (time.time(),))
        self.db.commit()
