"""§21.7 轨迹蒸馏：同一类任务成功 3 次 → 自动蒸馏成 Skill。

流程：对齐成功轨迹找公共工具链 → 参数分常量/变量 →
      结构试跑 → 入库（初始 confidence=medium）。
含 L3 操作的轨迹只出草稿，不自动入库。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .risk import classify_tool_risk


L3_HINTS = ("delete", "pay", "purchase", "publish", "post", "send", "transfer")
_MAX_SKILL_STEPS = 30
_SKILL_LOCK = threading.RLock()


def _lcs_pairs(left: list[str], right: list[str]) -> list[tuple[int, int]]:
    n, m = len(left), len(right)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if left[i] == right[j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    pairs = []
    i = j = 0
    while i < n and j < m:
        if left[i] == right[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _align(traces: list[list[dict]]) -> tuple[list[int], list[dict[int, int]]]:
    """以第一条轨迹为基准做多路 LCS，返回基准下标与每条轨迹的对齐映射。"""
    if not traces:
        return [], []
    base = [step.get("tool", "") for step in traces[0]]
    keep = set(range(len(base)))
    mappings: list[dict[int, int]] = [{i: i for i in range(len(base))}]
    for trace in traces[1:]:
        pairs = _lcs_pairs(base, [step.get("tool", "") for step in trace])
        mapping = dict(pairs)
        keep &= set(mapping)
        mappings.append(mapping)
    indices = sorted(keep)
    if not indices:
        return [], []
    return indices, mappings


def longest_common_tool_sequence(traces: list[list[dict]]) -> list[dict]:
    """返回所有成功轨迹的最长公共工具子序列（取第一条轨迹的完整 step）。"""
    indices, _ = _align(traces)
    return [traces[0][i] for i in indices]


def _arg_sets(traces, indices, mappings):
    aligned = []
    for base_idx in indices:
        per_trace = []
        for trace_idx, trace in enumerate(traces):
            pos = mappings[trace_idx].get(base_idx)
            step = trace[pos] if pos is not None and pos < len(trace) else {}
            per_trace.append(step.get("args") or {})
        aligned.append(per_trace)
    return aligned


def _classify(traces, indices, mappings):
    params, constants, bindings = {}, {}, []
    aligned = _arg_sets(traces, indices, mappings)
    for pos, args_list in enumerate(aligned):
        tool = traces[0][indices[pos]].get("tool", "")
        keys = set()
        for args in args_list:
            keys.update(args.keys())
        for key in sorted(keys):
            values = [args.get(key) for args in args_list]
            if all(str(value) == str(values[0]) for value in values):
                constants[f"{tool}.{key}"] = values[0]
                continue
            param_key = f"{tool}.{key}"
            if param_key in params:
                param_key = f"{tool}.{key}@{pos}"
            params[param_key] = "str"
            bindings.append({"step": pos, "arg": key, "param": param_key})
    return params, constants, bindings


def classify_params(traces: list[list[dict]],
                    chain: list[dict]) -> tuple[dict, dict]:
    """参数分类：多次一致 → 常量；每次变化 → Skill 参数。"""
    indices, mappings = _align(traces)
    params, constants, _ = _classify(traces, indices, mappings)
    return params, constants


def _has_l3(steps: list[dict]) -> bool:
    for step in steps:
        tool = str(step.get("tool", ""))
        args = step.get("args") or {}
        if classify_tool_risk(tool, args) == "L3":
            return True
        if any(hint in tool.casefold() for hint in L3_HINTS):
            return True
    return False


def distill_skill(task_pattern: str, traces: list[list[dict]],
                  describe_fn=None, dry_run_fn=None) -> dict | None:
    """返回 Skill dict 或 None（试跑失败时）。含 L3 操作只出草稿。"""
    all_steps = [step for trace in traces for step in trace]
    l3 = _has_l3(all_steps)
    indices, mappings = _align(traces)
    if len(indices) < 2:
        if l3:
            return {"name": task_pattern, "status": "draft",
                    "reason": "含 L3 操作，只出草稿，需人工审批"}
        return None
    chain = [traces[0][i] for i in indices]
    params, constants, bindings = _classify(traces, indices, mappings)
    description = (describe_fn(chain) if describe_fn
                   else f"自动蒸馏：{' → '.join(s['tool'] for s in chain)}")
    skill = {
        "name": re.sub(r"[^\w\-]", "-", task_pattern.lower())[:40],
        "description": description,
        "steps": [{"tool": step.get("tool", ""), "args": dict(step.get("args") or {})}
                  for step in chain],
        "params": params,
        "constants": constants,
        "param_bindings": bindings,
        "source": "distilled",
        "confidence": "medium",
        "status": "active",
        "created_at": time.time(),
        "use_count": 0,
        "success_count": 0,
        "fail_count": 0,
    }
    if l3:
        skill["status"] = "draft"
        skill["reason"] = "含 L3 操作，只出草稿，需人工审批"
        return skill
    if dry_run_fn and not dry_run_fn(skill):
        return None
    return skill


def _external_param_names(params: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    used: set[str] = set()
    for key in params:
        candidate = re.split(r"[.@]", key)[-1] or "arg"
        if candidate in used:
            candidate = re.sub(r"[^\w]", "_", key)
        while candidate in used:
            candidate += "_2"
        used.add(candidate)
        names[key] = candidate
    return names


def _render(value, args: dict):
    if not isinstance(value, str):
        return value
    return re.sub(r"\{\{(\w+)\}\}",
                  lambda m: str(args.get(m.group(1), m.group(0))), value)


def skill_to_tool(skill: dict, registry, manager=None) -> "ToolDefinition":
    """将已蒸馏的 Skill 转化为可执行的 ToolDefinition（spec §21.7）。"""
    from .models import tool_result_is_error
    from .tools_registry import ToolDefinition

    skill_name = skill.get("name", "skill")
    name = f"skill_{skill_name.replace('-', '_')}"
    description = skill.get("description", "已蒸馏的自动化技能")
    params = skill.get("params") or {}
    external_names = _external_param_names(params)
    params_schema = {
        "type": "object",
        "properties": {external_names[key]: {"type": "string"} for key in params},
        "required": [],
    }
    bindings_by_step: dict[int, list[dict]] = {}
    for binding in skill.get("param_bindings") or []:
        bindings_by_step.setdefault(int(binding.get("step", -1)), []).append(binding)

    def execute_skill(args: dict) -> str:
        from vault.manager import Vault
        call_args = args if isinstance(args, dict) else {}
        results = []
        ok = True
        steps = skill.get("steps") or []
        if len(steps) > _MAX_SKILL_STEPS:
            return f"[Skill 执行失败] 步骤数超过上限 {_MAX_SKILL_STEPS}"
        for step_index, step in enumerate(steps):
            tool_name = step.get("tool", "")
            if tool_name not in registry:
                ok = False
                results.append(f"[{tool_name}] 工具不存在")
                break
            step_args = {key: _render(value, call_args)
                         for key, value in (step.get("args") or {}).items()}
            bindings = bindings_by_step.get(step_index) or []
            if bindings:
                for binding in bindings:
                    external = external_names.get(binding.get("param", ""))
                    if external and external in call_args:
                        step_args[binding.get("arg")] = call_args[external]
            else:
                for key, value in call_args.items():
                    for param_key in params:
                        if param_key.endswith(f".{key}") or param_key == key:
                            step_args[key] = value
            try:
                message = registry.execute(
                    tool_name, step_args,
                    _provenance={"level": 0,
                                 "source_ref": f"skill:{skill_name}:{step_index}"})
            except Exception as exc:  # noqa: BLE001 —— Skill 执行失败须回传模型
                ok = False
                results.append(f"[{tool_name}] {type(exc).__name__}: {exc}")
                break
            content = getattr(message, "content", str(message))
            if tool_result_is_error(message):
                ok = False
                results.append(f"[{tool_name}] {str(content)[:4000]}")
                break
            results.append(f"[{tool_name}] {str(content)[:4000]}")
        if manager is not None:
            manager.record_use(skill_name, ok)
        return Vault.scrub_text("\n".join(results))

    return ToolDefinition(
        name=name,
        description=description,
        parameters=params_schema,
        func=execute_skill,
        parallel_safe=False,
        is_readonly=False,
    )


class SkillManager:
    """Skill 持久化与生命周期管理器（spec §21）。"""

    def __init__(self, storage_path: str | Path):
        self.path = Path(storage_path)
        self.skills: dict[str, dict] = {}
        self.load()

    def load(self):
        if self.path.exists():
            try:
                self.skills = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.skills = {}

    def save(self):
        with _SKILL_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".skills.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(self.skills, ensure_ascii=False, indent=2))
                os.replace(tmp_path, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    def add_skill(self, skill: dict):
        with _SKILL_LOCK:
            skill.setdefault("created_at", time.time())
            skill.setdefault("use_count", 0)
            skill.setdefault("success_count", 0)
            skill.setdefault("fail_count", 0)
            skill.setdefault("status", "active")
            self.skills[skill["name"]] = skill
            self.save()

    def find_by_signature(self, signature: str) -> dict | None:
        if not signature:
            return None
        for skill in self.skills.values():
            if skill.get("source_signature") == signature:
                return skill
        return None

    def record_use(self, name: str, worked: bool):
        with _SKILL_LOCK:
            skill = self.skills.get(name)
            if not skill:
                return
            skill["use_count"] = int(skill.get("use_count", 0)) + 1
            if worked:
                skill["success_count"] = int(skill.get("success_count", 0)) + 1
            else:
                skill["fail_count"] = int(skill.get("fail_count", 0)) + 1
            skill["last_used_at"] = time.time()
            total = skill["use_count"]
            successes = int(skill.get("success_count", 0))
            if total >= 5:
                rate = successes / total
                if rate > 0.8:
                    skill["confidence"] = "high"
                elif rate < 0.5:
                    skill["status"] = "rewrite"
            self.save()

    def active_skills(self) -> list[dict]:
        return [skill for skill in self.skills.values()
                if skill.get("status", "active") == "active"
                and skill.get("enabled", True)]

    def audit_job(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        retired, flagged = [], []
        with _SKILL_LOCK:
            for skill in self.skills.values():
                status = skill.get("status", "active")
                if status in {"retired", "rewrite", "draft"}:
                    continue
                uses = int(skill.get("use_count", 0))
                created = float(skill.get("created_at", now))
                age_days = (now - created) / 86400
                if uses == 0 and age_days > 90:
                    skill["status"] = "retired"
                    skill["retire_reason"] = "90天零调用"
                    retired.append(skill["name"])
                elif uses >= 5 and int(skill.get("success_count", 0)) / uses < 0.5:
                    skill["status"] = "rewrite"
                    skill["retire_reason"] = "成功率低于50%"
                    flagged.append(skill["name"])
            if retired or flagged:
                self.save()
        return {"retired": retired, "flagged": flagged}

    def register_all(self, registry):
        for skill in self.active_skills():
            try:
                tool = skill_to_tool(skill, registry, self)
            except Exception:
                continue
            if tool.name not in registry:
                registry.register(tool)
