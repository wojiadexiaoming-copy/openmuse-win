"""§23 Proactivity 主动推送 [必选]

后台做好研究攒着，挑用户在场且相关的时机推。
不是"你问我答"，是"我觉得你该知道"。

流程：生产（Job 定时扫）→ 暂存 pending.json → 择机推送（用户发言时带出）。
"""
from __future__ import annotations

import logging

log = logging.getLogger("proactivity")

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict


@dataclass
class ProactiveItem:
    item_id: str
    topic: str                          # "阶跃星辰限免"
    one_liner: str                      # 一句话
    detail: str = ""                    # 展开详情
    sources: list[str] = field(default_factory=list)
    priority: str = "medium"            # high | medium | low
    created_at: float = 0.0
    expires_at: float = 0.0             # 过期自动丢弃
    status: str = "pending"             # pending | delivered | dismissed | expired


class ProactivityStore:
    """候选推送暂存：pending.json 文件存储。"""

    def __init__(self, path: str | None = None):
        if path is None:
            path = os.path.join(os.path.expanduser("~"), ".alterego",
                                 "proactivity", "pending.json")
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save(self, items: list[dict]):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

    def add(self, topic: str, one_liner: str, detail: str = "",
            sources: list[str] | None = None,
            priority: str = "medium", ttl_hours: float = 24) -> str:
        """生产一条候选（由定时 Job 调用）。"""
        now = time.time()
        item = ProactiveItem(
            item_id=f"pi-{uuid.uuid4().hex[:8]}",
            topic=topic, one_liner=one_liner, detail=detail,
            sources=sources or [], priority=priority,
            created_at=now, expires_at=now + ttl_hours * 3600,
        )
        items = self._load()
        items.append(asdict(item))
        self._save(items)
        log.info(f"[proactivity] 暂存候选 {item.item_id} [{item.priority}] {item.topic}")
        return item.item_id

    def pending(self) -> list[ProactiveItem]:
        """读未过期、未投递的候选（自动标记过期）。"""
        now = time.time()
        items = self._load()
        changed = False
        out = []
        for d in items:
            if d["status"] == "pending" and d["expires_at"] < now:
                d["status"] = "expired"
                changed = True
            if d["status"] == "pending":
                out.append(ProactiveItem(**d))
        if changed:
            self._save(items)
        # 按优先级 + 新鲜度排序
        prio = {"high": 0, "medium": 1, "low": 2}
        out.sort(key=lambda x: (prio.get(x.priority, 1), -x.created_at))
        return out

    def mark_delivered(self, item_id: str):
        self._mark(item_id, "delivered")
        log.info(f"[proactivity] 已推送 {item_id}")

    def mark_dismissed(self, item_id: str):
        self._mark(item_id, "dismissed")

    def _mark(self, item_id: str, status: str):
        items = self._load()
        for d in items:
            if d["item_id"] == item_id:
                d["status"] = status
                break
        self._save(items)


def pick_for_user(store: ProactivityStore, user_text: str,
                  max_per_day: int = 2) -> ProactiveItem | None:
    """择机：用户发言时挑一条最相关的候选。

    简单规则：每天最多推 max_per_day 条；优先 high；话题相关度靠关键词。
    （完整版可用 LLM 排序，这里先规则版保证可用）
    """
    # 今日已推计数（用 delivered 状态 + created_at 判断）
    try:
        items_raw = store._load()
        today_start = time.time() - (time.time() % 86400)
        delivered_today = sum(
            1 for d in items_raw
            if d["status"] == "delivered" and d["created_at"] >= today_start)
        if delivered_today >= max_per_day:
            return None
    except Exception:
        pass
    candidates = store.pending()
    if not candidates:
        return None
    # 相关度：topic/one_liner 与用户文本关键词重叠
    ut = user_text.lower()
    def _score(item: ProactiveItem) -> int:
        text = (item.topic + " " + item.one_liner).lower()
        # 简单分词重叠
        words = set(ut.split()) | set(text.split())
        overlap = len(set(ut.split()) & set(text.split()))
        prio_bonus = {"high": 10, "medium": 5, "low": 0}.get(item.priority, 0)
        return overlap * 2 + prio_bonus
    candidates.sort(key=_score, reverse=True)
    # 阈值：完全不相关（0 分）且非 high 优先级 → 不推
    top = candidates[0]
    if _score(top) == 0 and top.priority != "high":
        return None
    return top


def make_proactivity_tools(store: "ProactivityStore"):
    """构建主动推送工具。"""
    from agent.tools_registry import ToolDefinition

    def _add(args: dict) -> str:
        iid = store.add(args.get("topic", ""), args.get("one_liner", ""),
                         detail=args.get("detail", ""),
                         priority=args.get("priority", "medium"))
        return f"[推送候选已暂存] {iid}，用户下次发言时择机带出"

    def _pending(args: dict) -> str:
        items = store.pending()
        if not items:
            return "[推送] 暂无待推送候选"
        lines = [f"- {i.item_id} [{i.priority}] {i.topic}：{i.one_liner}"
                 for i in items[:5]]
        return "[推送候选]\n" + "\n".join(lines)

    return [
        ToolDefinition(
            name="proactivity_add",
            description="暂存一条主动推送候选（后台研究攒着，择机推给用户）。",
            parameters={"type": "object",
                         "properties": {
                             "topic": {"type": "string"},
                             "one_liner": {"type": "string"},
                             "detail": {"type": "string"},
                             "priority": {"type": "string",
                                           "enum": ["high", "medium", "low"]}},
                         "required": ["topic", "one_liner"]},
            func=_add,
        ),
        ToolDefinition(
            name="proactivity_pending",
            description="查看待推送候选列表。",
            parameters={"type": "object", "properties": {}, "required": []},
            func=_pending,
        ),
    ]
