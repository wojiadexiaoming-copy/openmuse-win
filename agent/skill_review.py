"""§21.3/§21.6/§21.7 夜间 Skill Review：把重复成功轨迹蒸馏成技能。

输入是已持久化的主会话轨迹；同一工具形状成功 ≥3 次才进入蒸馏。
dry-run 只做结构与风险校验，不重放副作用动作。
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .models import tool_result_is_error
from .risk import classify_tool_risk
from .skill_distiller import distill_skill


OBSERVATION_TOOLS = frozenset({
    "browser_snapshot", "browser_screenshot", "browser_wait", "browser_wait_for",
    "browser_switch_tab", "browser_scroll", "browser_observe", "browser_inspect",
    "browser_console", "browser_network_log", "browser_resource", "browser_find_text",
    "browser_react_probe",
    "browser_storage", "state_pin", "state_check", "state_clear",
})
PARTIAL_MARKERS = ("[已超时]", "[部分交付]", "任务尚未完成", "尚未完成：", "[任务已取消]")
FORBIDDEN_SKILL_TOOLS = frozenset({
    "spawn_subagent", "poll_subagent", "cancel_subagent", "schedule_once",
})
_MIN_REPEAT = 3
_MIN_STEPS = 5
_MAX_TRACE_STEPS = 30
_MAX_TRACES = 5


def _shape(steps: list[dict]) -> list[str]:
    shape: list[str] = []
    for step in steps:
        tool = step.get("tool", "")
        if not tool or (shape and shape[-1] == tool):
            continue
        shape.append(tool)
    return shape


def _trajectory(history: list) -> dict | None:
    calls, results = [], {}
    task = ""
    for message in history:
        role = getattr(message, "role", "")
        content = getattr(message, "content", "") or ""
        if role == "user" and not task and content.strip():
            task = re.sub(r"^\[[^\]]{1,20}\]\s*", "", content.strip())
        if role == "assistant":
            for call in getattr(message, "tool_calls", []) or []:
                calls.append({
                    "tool": call.name,
                    "args": dict(call.arguments or {}),
                    "id": call.id,
                })
        elif role == "tool" and getattr(message, "tool_call_id", None):
            results[message.tool_call_id] = message
    if not calls or not task or not history:
        return None
    final_assistant = history[-1]
    if getattr(final_assistant, "role", "") != "assistant":
        return None
    if not (getattr(final_assistant, "content", "") or "").strip():
        return None

    final_meta = getattr(final_assistant, "metadata", {}) or {}
    if final_meta.get("task_status") in {"incomplete", "waiting_user", "error", "running"}:
        return None
    final_content = final_assistant.content or ""
    if any(marker in final_content for marker in PARTIAL_MARKERS):
        return None

    successful = sum(
        1 for call in calls
        if call["id"] in results and not tool_result_is_error(results[call["id"]])
    )
    if successful / len(calls) < 0.8:
        return None

    actions = [call for call in calls if call["tool"] not in OBSERVATION_TOOLS]
    if len(actions) < _MIN_STEPS:
        return None
    shape = _shape(actions)
    if len(shape) < _MIN_STEPS or len(actions) > _MAX_TRACE_STEPS:
        return None
    l3 = any(
        classify_tool_risk(step["tool"], step["args"]) == "L3"
        or step["tool"] in FORBIDDEN_SKILL_TOOLS
        for step in actions
    )
    return {"task": task[:160], "steps": actions, "shape": shape, "l3": l3}


def _structural_dry_run(skill: dict, registry) -> bool:
    steps = skill.get("steps") or []
    if not steps or len(steps) > _MAX_TRACE_STEPS:
        return False
    for step in steps:
        tool = step.get("tool", "")
        if tool in FORBIDDEN_SKILL_TOOLS or tool.startswith("skill_"):
            return False
        if tool not in registry:
            return False
        args = step.get("args") or {}
        if not isinstance(args, dict):
            return False
        try:
            json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            return False
        if classify_tool_risk(tool, args) == "L3":
            return False
    return True


def collect_patterns(store, limit: int = 60) -> dict[tuple, list[dict]]:
    patterns: dict[tuple, list[dict]] = {}
    for session in store.list_sessions(limit=limit):
        try:
            history = store.load_history(session["session_id"])
        except Exception:
            continue
        trajectory = _trajectory(history)
        if trajectory is None:
            continue
        trajectory["session_id"] = session.get("session_id", "")
        trajectory["started_ts"] = session.get("started_ts") or 0.0
        patterns.setdefault(tuple(trajectory["shape"]), []).append(trajectory)
    return patterns


def run_skill_review(store, skill_manager, registry, tracer=None,
                     limit: int = 60, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    patterns = collect_patterns(store, limit=limit)
    created, skipped = [], []
    for shape, items in patterns.items():
        if len(items) < _MIN_REPEAT:
            continue
        signature = hashlib.sha1("|".join(shape).encode("utf-8")).hexdigest()[:12]
        if skill_manager.find_by_signature(signature):
            skipped.append(f"sig:{signature}")
            continue
        name = f"auto-{signature[:8]}"
        if name in skill_manager.skills:
            skipped.append(name)
            continue
        traces = [item["steps"] for item in items[:_MAX_TRACES]]
        skill = distill_skill(
            items[0]["task"], traces,
            dry_run_fn=lambda candidate: _structural_dry_run(candidate, registry))
        if not skill:
            skipped.append(f"dry-run:{signature}")
            continue
        skill["name"] = name
        skill["description"] = (
            f"{items[0]['task'][:80]}（自动蒸馏，基于 {len(items)} 条成功轨迹）")
        skill["source_signature"] = signature
        skill["task_pattern"] = items[0]["task"][:120]
        skill["source_sessions"] = [
            item.get("session_id", "") for item in items[:_MAX_TRACES]]
        if any(item["l3"] for item in items):
            skill["status"] = "draft"
            skill["reason"] = "轨迹含 L3/受限操作，需人工审批"
        skill["created_at"] = now
        skill_manager.add_skill(skill)
        skill_manager.register_all(registry)
        created.append({"name": name, "status": skill.get("status", "active")})
        if tracer:
            tracer.log_event("skill_created", {
                "name": name,
                "status": skill.get("status", "active"),
                "tool_count": len(skill.get("steps") or []),
                "input_count": len(items),
            })

    audit = skill_manager.audit_job(now=now)
    summary = {
        "status": "ok",
        "scanned": limit,
        "patterns": len(patterns),
        "created": created,
        "skipped": skipped,
        "audit": audit,
    }
    if tracer:
        tracer.log_event("skill_review", {
            "status": "ok",
            "input_count": len(patterns),
            "tool_count": len(created),
        })
    return summary
