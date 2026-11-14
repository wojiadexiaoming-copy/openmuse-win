"""§7.3 工具结果双级热缓存：L1 内存热缓存（微秒级）+ L2 SQLite 持久化缓存，只读工具按参数 hash 缓存，写操作后即刻失效。"""
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
        "desktop_list_windows": 5,
        "desktop_list_items": 5,
    }

    # L1 内存单条结果最大体积（200KB），超大结果仅持久化至 SQLite，防止占用过多堆内存
    MAX_L1_ITEM_BYTES = 200_000

    def __init__(self, db_path: str, max_l1_entries: int = 1000):
        # check_same_thread=False 配套跨线程锁，保障 UI 线程 / 编排线程 / 并行 worker 并发安全
        self._lock = threading.Lock()
        self._max_l1 = max_l1_entries
        # L1 内存热缓存字典: key -> (result, expires_at, tool_name)
        self._l1: dict[str, tuple[str, float, str]] = {}
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS tool_cache"
                "(ckey TEXT PRIMARY KEY, tool TEXT, result TEXT, expires_at REAL)")

    @staticmethod
    def _key(tool: str, args: dict, prov_level: int = 0) -> str:
        # default=str 防御性处理非标准 JSON 对象（如 Path、枚举等），杜绝 TypeError 崩溃
        raw = f"{tool}:{prov_level}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, tool: str, args: dict, prov_level: int = 0) -> str | None:
        ckey = self._key(tool, args, prov_level=prov_level)
        now = time.time()
        with self._lock:
            # 1. 优先微秒级读取 L1 内存热缓存
            l1_hit = self._l1.get(ckey)
            if l1_hit is not None:
                res, exp, _ = l1_hit
                if exp > now:
                    return res
                del self._l1[ckey]

            # 2. L1 未命中时回落查询 L2 SQLite 缓存，并热回填 L1
            row = self.db.execute(
                "SELECT result, expires_at FROM tool_cache WHERE ckey=? AND expires_at>?",
                (ckey, now)).fetchone()
            if row:
                res, exp = row
                if len(self._l1) < self._max_l1 and len(res) <= self.MAX_L1_ITEM_BYTES:
                    self._l1[ckey] = (res, exp, tool)
                return res
        return None

    def put(self, tool: str, args: dict, result: str, ttl_sec: int | None = None, ttl: int | None = None, prov_level: int = 0):
        effective_ttl = ttl_sec if ttl_sec is not None else ttl
        if effective_ttl is None or effective_ttl <= 0:
            effective_ttl = self.TTL.get(tool, 0)
        if effective_ttl <= 0:
            return
        ckey = self._key(tool, args, prov_level=prov_level)
        exp = time.time() + effective_ttl
        with self._lock:
            # 写入 L1 内存热缓存（有界淘汰，杜绝内存泄漏）
            if len(result) <= self.MAX_L1_ITEM_BYTES:
                if len(self._l1) >= self._max_l1:
                    now = time.time()
                    self._l1 = {k: v for k, v in self._l1.items() if v[1] > now}
                    # 淘汰后仍超出上限，按 FIFO 淘汰最早插入的 20%
                    if len(self._l1) >= self._max_l1:
                        excess = len(self._l1) - int(self._max_l1 * 0.8)
                        for k in list(self._l1.keys())[:excess]:
                            del self._l1[k]
                self._l1[ckey] = (result, exp, tool)

            # 持久化至 L2 SQLite
            self.db.execute(
                "INSERT OR REPLACE INTO tool_cache VALUES (?,?,?,?)",
                (ckey, tool, result, exp))
            self.db.commit()

    def invalidate_tool(self, tool: str):
        """某工具的写操作成功后，清掉该工具的读缓存（如 file_write → file_read，desktop 写操作 → desktop 读缓存）。"""
        with self._lock:
            self._l1 = {k: v for k, v in self._l1.items() if v[2] != tool}
            self.db.execute("DELETE FROM tool_cache WHERE tool=?", (tool,))
            self.db.commit()

    def sweep(self):
        """清过期条目（定时 Job 调）。"""
        now = time.time()
        with self._lock:
            self._l1 = {k: v for k, v in self._l1.items() if v[1] > now}
            self.db.execute("DELETE FROM tool_cache WHERE expires_at<?", (now,))
            self.db.commit()
