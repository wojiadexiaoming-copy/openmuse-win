"""§7.4 工具 Schema 自优化（每夜 Job）：

- 成功样例 → 追加 few-shot examples（标 [auto]，最多 3 个，按多样性选）
- 高频失败模式 → 加 warnings
- 成功率 <50% 且调用 ≥20 → 标"难用"
只追加，不改参数定义；L3 工具不动；记 diff 可回滚。
"""
from __future__ import annotations

import time
from collections import Counter


def pick_diverse(args_list: list[dict], k: int = 3) -> list[dict]:
    """按参数键集合多样性选 k 个。"""
    seen, out = set(), []
    for a in args_list:
        sig = tuple(sorted(a.keys()))
        if sig not in seen:
            seen.add(sig)
            out.append(a)
        if len(out) >= k:
            break
    return out


def frequent_error_patterns(failures: list[dict]) -> list[dict]:
    """failures 项：{"args","error"}。返回 [{"desc","count"}]。"""
    counter = Counter()
    for f in failures:
        err = (f.get("error") or "")[:60]
        counter[err] += 1
    return [{"desc": e, "count": c} for e, c in counter.most_common()
            if c >= 3]


def tune_tool(tool, calls: list[dict]) -> dict:
    """分析一个工具 30 天的调用记录，返回调优 diff（不直接改，由调用方确认）。

    tool: ToolDefinition；calls 项：{"args","ok","error"}。
    diff: {"add_examples": [...], "add_warnings": [...], "flag": str|None}
    """
    if len(calls) < 10:
        return {"add_examples": [], "add_warnings": [], "flag": None}
    ok_rate = sum(1 for c in calls if c["ok"]) / len(calls)
    good = pick_diverse([c["args"] for c in calls if c["ok"]])
    warnings = [
        f"[auto] 注意：{p['desc']}（过去 30 天因此失败 {p['count']} 次）"
        for p in frequent_error_patterns([c for c in calls if not c["ok"]])
    ]
    flag = None
    if ok_rate < 0.5 and len(calls) >= 20:
        flag = f"成功率仅 {ok_rate:.0%}，建议人工重写 Schema"
    return {"add_examples": good, "add_warnings": warnings, "flag": flag}


def apply_tune(tool, diff: dict, log_fn=print):
    """应用调优 diff（记日志可回滚）。L3 工具直接跳过。"""
    if getattr(tool, "risk", "") == "L3":
        log_fn(f"[tune] {tool.name} 是 L3 工具，跳过自动调优")
        return
    params = tool.parameters
    examples = params.setdefault("examples", [])
    for ex in diff["add_examples"]:
        if ex not in examples and len(examples) < 3:
            examples.append(ex)
    warnings = params.setdefault("x-warnings", [])
    for w in diff["add_warnings"]:
        if w not in warnings:
            warnings.append(w)
    log_fn(f"[tune] {tool.name}: +{len(diff['add_examples'])} examples, "
           f"+{len(diff['add_warnings'])} warnings, flag={diff['flag']}")
