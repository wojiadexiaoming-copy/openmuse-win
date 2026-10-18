"""§8.5 经验置信度与遗忘曲线：越老越精，不越老越犟。

适用：error_fix、蒸馏 Skill、挖出的模式 —— 所有"自动积累"的东西。
MEMORY.md 里用户亲口说的 Facts/Preferences 是圣旨，不走这套。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass
class Experience:
    key: str
    claim: str
    confidence: float = 0.5
    created_ts: float = 0.0
    last_used_ts: float = 0.0
    use_count: int = 0
    contradict_count: int = 0


class ExperienceStore:
    def __init__(self, db_path: str):
        from agent.db import get_db
        self.db = get_db(db_path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS experience"
            "(ekey TEXT PRIMARY KEY, claim TEXT, confidence REAL,"
            " created_ts REAL, last_used_ts REAL, use_count INTEGER,"
            " contradict_count INTEGER, archived INTEGER DEFAULT 0)")

    def add(self, key: str, claim: str, confidence: float = 0.5):
        now = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO experience VALUES (?,?,?,?,?,?,?,0)",
            (key, claim, confidence, now, now, 0, 0))
        self.db.commit()

    def get(self, key: str) -> Experience | None:
        row = self.db.execute(
            "SELECT * FROM experience WHERE ekey=? AND archived=0",
            (key,)).fetchone()
        if not row:
            return None
        return Experience(key=row[0], claim=row[1], confidence=row[2],
                          created_ts=row[3], last_used_ts=row[4],
                          use_count=row[5], contradict_count=row[6])

    def should_trust(self, key: str) -> bool:
        exp = self.get(key)
        return exp is not None and exp.confidence >= 0.4

    def feedback(self, key: str, worked: bool):
        """每次经验被调用后调：用对 +0.1，打脸 -0.3。"""
        exp = self.get(key)
        if not exp:
            return
        now = time.time()
        if worked:
            exp.use_count += 1
            exp.confidence = min(1.0, exp.confidence + 0.1)
        else:
            exp.contradict_count += 1
            exp.confidence = max(0.0, exp.confidence - 0.3)
        self.db.execute(
            "UPDATE experience SET confidence=?, last_used_ts=?,"
            " use_count=?, contradict_count=? WHERE ekey=?",
            (exp.confidence, now, exp.use_count, exp.contradict_count, key))
        self.db.commit()

    def decay_job(self):
        """每夜 Job：90 天不用减半，180 天不用归档，被打脸 3 次归档。"""
        now = time.time()
        for row in self.db.execute(
                "SELECT ekey, confidence, last_used_ts, contradict_count"
                " FROM experience WHERE archived=0").fetchall():
            key, conf, last_used, contra = row
            idle_days = (now - last_used) / 86400
            if contra >= 3:
                self._archive(key, "连续被新事实推翻 3 次")
            elif idle_days > 180:
                self._archive(key, "180天未用")
            elif idle_days > 90:
                self.db.execute(
                    "UPDATE experience SET confidence=? WHERE ekey=?",
                    (conf * 0.5, key))
        self.db.commit()

    def _archive(self, key: str, reason: str):
        self.db.execute("UPDATE experience SET archived=1 WHERE ekey=?", (key,))
        self.db.commit()
        try:
            from pathlib import Path
            archive_dir = Path("memory/bank/archive")
            archive_dir.mkdir(parents=True, exist_ok=True)
            safe_key = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in key)
            record = {
                "key": key,
                "reason": reason,
                "archived_at": time.time(),
            }
            (archive_dir / f"{safe_key}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

