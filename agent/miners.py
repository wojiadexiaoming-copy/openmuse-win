"""M4 挖掘器（每夜 Job 调其中一个）：

- mine_approval_patterns: §36.10 审批模式挖掘
- mine_task_patterns: §10.7 任务模式挖掘 → 定时任务建议

两个都是纯函数：输入历史记录，输出建议列表。投递走 Proactivity（§23）。
"""
from __future__ import annotations

import re
import time
from collections import Counter, defaultdict


# ---------------- §36.10 审批模式挖掘 ----------------

def mine_approval_patterns(approvals: list[dict]) -> list[dict]:
    """approvals 项：{"action","risk","context_sig","decision","ts"}。
    30 天窗口。返回建议列表。"""
    cutoff = time.time() - 30 * 86400
    recent = [a for a in approvals if a.get("ts", 0) > cutoff]
    groups = defaultdict(list)
    for a in recent:
        groups[(a["action"], a["risk"], a.get("context_sig", ""))].append(a)
    suggestions = []
    for (action, risk, ctx), items in groups.items():
        if len(items) < 3:
            continue
        decisions = Counter(i["decision"] for i in items)
        n = len(items)
        # 模式1：100% 拒绝 ≥5 次 → 建议直接 deny（L3 除外）
        if decisions.get("rejected", 0) == n and n >= 5 and risk != "L3":
            suggestions.append({
                "type": "deny",
                "text": f"过去 30 天 {action}（{ctx}）你拒绝了 {n} 次，"
                        f"建议直接设为拒绝，不再弹卡。",
            })
        # 模式2：100% 批准 ≥20 次 → 建议白名单
        if decisions.get("approved", 0) == n and n >= 20:
            suggestions.append({
                "type": "whitelist",
                "text": f"{action}（{ctx}）你批准了 {n} 次且零拒绝，"
                        f"是否加入白名单？",
            })
    return suggestions


def context_sig(action: str, risk: str, hour: int, target: str = "") -> str:
    """上下文签名：动作+风险+时段+目标（不含具体参数值，防过拟合）。"""
    if 0 <= hour < 6:
        slot = "深夜"
    elif hour < 12:
        slot = "上午"
    elif hour < 18:
        slot = "下午"
    else:
        slot = "晚上"
    return f"{action}/{risk}/{slot}/{target}"


# ---------------- §10.7 任务模式挖掘 ----------------

def detect_periodicity(timestamps: list[float]) -> str | None:
    """纯规则：≥3 次落在同一 weekday+小时 → cron。无规律返回 None。"""
    if len(timestamps) < 3:
        return None
    slots = Counter()
    for ts in timestamps:
        t = time.localtime(ts)
        slots[(t.tm_wday, t.tm_hour)] += 1
    (wday, hour), count = slots.most_common(1)[0]
    if count < 3:
        return None
    # cron: 分 时 * * 周几（周一=1 ... 周日=0/7，按标准 cron 周日=0）
    cron_wday = (wday + 1) % 7
    # 若 7 天内每天都有 → 每天
    days = {time.localtime(ts).tm_wday for ts in timestamps}
    if len(days) >= 5:
        return f"0 {hour} * * *"
    return f"0 {hour} * * {cron_wday}"


def mine_task_patterns(tasks: list[dict]) -> list[dict]:
    """tasks 项：{"intent","created_ts","brief"}。返回定时任务建议。"""
    by_intent = defaultdict(list)
    for t in tasks:
        by_intent[t["intent"]].append(t)
    suggestions = []
    for intent, items in by_intent.items():
        if len(items) < 3:
            continue
        cron = detect_periodicity([t["created_ts"] for t in items])
        if cron:
            suggestions.append({
                "type": "cron",
                "text": f"你过去 30 天手动触发了 {len(items)} 次「{intent}」，"
                        f"建议设成定时（{cron}）自动跑吗？",
                "cron": cron,
                "task_brief": items[0]["brief"],
            })
    return suggestions


def normalize_intent(text: str) -> str:
    """意图归一化（规则版）：去标点、取前 12 字。M4 可用弱模型替代。"""
    t = re.sub(r"[？?！!。，,、\s]", "", text)
    return t[:12]
