"""§21.7 轨迹蒸馏：同一类任务成功 3 次 → 自动蒸馏成 Skill。

流程：对齐成功轨迹找公共工具链 → 参数分常量/变量 →
      弱模型生成描述 → 沙箱试跑 → 入库（初始 confidence=medium）。
含 L3 操作的轨迹只出草稿，不自动入库。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


def longest_common_tool_sequence(traces: list[list[dict]]) -> list[dict]:
    """traces：每条是 [{"tool":..., "args":...}]。返回最长公共子序列（工具名）。"""
    if not traces:
        return []
    seqs = [[s["tool"] for s in t] for t in traces]
    # 简化：以第一条为基准，找在其他条里按序出现的工具
    base = seqs[0]
    common = []
    for tool in base:
        if all(_contains_in_order(s, tool, common) for s in seqs[1:]):
            common.append(tool)
    # 取第一条轨迹里对应工具的完整 step（含 args 模板）
    return [s for s in traces[0] if s["tool"] in common]


def _contains_in_order(seq: list[str], tool: str, after: list[str]) -> bool:
    try:
        idx = seq.index(tool)
    except ValueError:
        return False
    # 保证顺序：tool 出现在 after 最后一个之后
    if not after:
        return True
    try:
        return idx > seq.index(after[-1])
    except ValueError:
        return True


def classify_params(traces: list[list[dict]],
                    chain: list[dict]) -> tuple[dict, dict]:
    """参数分类：3 次都一样的 → 常量；每次都变的 → Skill 参数。"""
    params, constants = {}, {}
    for step in chain:
        tool = step["tool"]
        # 收集该工具在各轨迹中的 args
        arg_sets = []
        for t in traces:
            for s in t:
                if s["tool"] == tool:
                    arg_sets.append(s.get("args", {}))
                    break
        keys = set().union(*[set(a) for a in arg_sets]) if arg_sets else set()
        for k in keys:
            vals = [a.get(k) for a in arg_sets]
            if len(set(map(str, vals))) == 1:
                constants[f"{tool}.{k}"] = vals[0]
            else:
                params[f"{tool}.{k}"] = "str"  # 类型推断简化为 str
    return params, constants


L3_HINTS = ("delete", "pay", "publish", "post", "send")


def distill_skill(task_pattern: str, traces: list[list[dict]],
                  describe_fn=None, dry_run_fn=None) -> dict | None:
    """返回 Skill dict 或 None（试跑失败时）。含 L3 操作只出草稿。"""
    # 先查 L3（在长度检查之前：含 L3 的单步轨迹也要出草稿）
    all_tools = [s["tool"] for t in traces for s in t]
    if any(any(h in tool for h in L3_HINTS) for tool in all_tools):
        return {"name": task_pattern, "status": "draft",
                "reason": "含 L3 操作，只出草稿，需人工审批"}
    chain = longest_common_tool_sequence(traces)
    if len(chain) < 2:
        return None  # 太短不值得蒸馏
    params, constants = classify_params(traces, chain)
    description = (describe_fn(chain) if describe_fn
                   else f"自动蒸馏：{' → '.join(s['tool'] for s in chain)}")
    skill = {
        "name": re.sub(r"[^\w\-]", "-", task_pattern.lower())[:40],
        "description": description,
        "steps": [{"tool": s["tool"], "args": s.get("args", {})}
                  for s in chain],
        "params": params,
        "constants": constants,
        "source": "distilled",
        "confidence": "medium",
    }
    if dry_run_fn and not dry_run_fn(skill):
        return None
    return skill


def skill_to_tool(skill: dict, registry) -> "ToolDefinition":
    """将已蒸馏的 Skill 转化为可执行的 ToolDefinition（spec §21.7）。"""
    from .tools_registry import ToolDefinition
    name = f"skill_{skill['name'].replace('-', '_')}"
    description = skill.get("description", "已蒸馏的自动化技能")
    params_schema = {
        "type": "object",
        "properties": {k.split(".")[-1]: {"type": "string"} for k in skill.get("params", {})},
        "required": [],
    }

    def execute_skill(args: dict) -> str:
        results = []
        for step in skill.get("steps", []):
            tool_name = step["tool"]
            step_args = dict(step.get("args", {}))
            # 变量参数回填
            for k, val in args.items():
                for param_key in skill.get("params", {}):
                    if param_key.endswith(f".{k}") or param_key == k:
                        step_args[k] = val
            msg = registry.execute(tool_name, step_args)
            results.append(f"[{tool_name}] {msg.content}")
        return "\n".join(results)

    return ToolDefinition(
        name=name,
        description=description,
        parameters=params_schema,
        func=execute_skill,
    )


class SkillManager:
    """Skill 持久化与生命周期管理器（spec §21）。"""

    def __init__(self, storage_path: str | Path):
        self.path = Path(storage_path)
        self.skills: dict[str, dict] = {}
        self.load()

    def load(self):
        if self.path.exists():
            import json
            try:
                self.skills = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.skills = {}

    def save(self):
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.skills, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_skill(self, skill: dict):
        self.skills[skill["name"]] = skill
        self.save()

    def register_all(self, registry):
        for s in self.skills.values():
            if s.get("status") != "draft":
                t = skill_to_tool(s, registry)
                if t.name not in registry._tools:
                    registry.register(t)

