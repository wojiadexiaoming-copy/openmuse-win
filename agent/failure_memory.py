"""§12.1 错误策略记忆：任务内去重 + 跨任务解法。

- task_tried：任务内试过失败的策略不再试（任务结束删）
- error_fix：错误签名 → 成功解法（跨任务持久化，按成功次数排序）
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time


class FailureMemory:
    def __init__(self, db_path: str):
        from agent.db import get_db
        self.db = get_db(db_path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS task_tried"
            "(task_id TEXT, strategy_sig TEXT, PRIMARY KEY (task_id, strategy_sig))")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS error_fix"
            "(error_sig TEXT PRIMARY KEY, fix_desc TEXT, success_count INTEGER,"
            " updated_ts REAL)")

    @staticmethod
    def error_sig(err_type: str, tool: str, args: dict) -> str:
        """错误签名：类型+工具+参数名（脱敏，不含参数值）。"""
        arg_pattern = ",".join(sorted(args.keys()))
        return f"{err_type}:{tool}({arg_pattern})"

    @staticmethod
    def _sig(strategy: str) -> str:
        return hashlib.sha256(strategy.encode()).hexdigest()[:16]

    def already_tried(self, task_id: str, strategy: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM task_tried WHERE task_id=? AND strategy_sig=?",
            (task_id, self._sig(strategy))).fetchone() is not None

    def record_tried(self, task_id: str, strategy: str, ok: bool,
                     error_sig: str = "", fix_desc: str = ""):
        self.db.execute(
            "INSERT OR IGNORE INTO task_tried VALUES (?,?)",
            (task_id, self._sig(strategy)))
        if ok and error_sig and fix_desc:
            self.db.execute(
                """INSERT INTO error_fix VALUES (?,?,1,?)
                   ON CONFLICT(error_sig) DO UPDATE SET
                   success_count = success_count + 1,
                   fix_desc = excluded.fix_desc,
                   updated_ts = excluded.updated_ts""",
                (error_sig, fix_desc, time.time()))
        self.db.commit()

    def suggest(self, error_sig: str) -> str | None:
        """这类错误历史上成功次数最多的解法。"""
        row = self.db.execute(
            "SELECT fix_desc FROM error_fix WHERE error_sig=?"
            " ORDER BY success_count DESC LIMIT 1",
            (error_sig,)).fetchone()
        return row[0] if row else None

    def clear_task(self, task_id: str):
        """任务结束删任务级记录（防污染）。"""
        self.db.execute("DELETE FROM task_tried WHERE task_id=?", (task_id,))
        self.db.commit()
