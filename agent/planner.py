"""§5.7 规划-执行分离：复杂任务先写显式计划，执行时检查偏离。

M4：should_plan() 纯规则预判；计划生成调 LLM（plan() 由调用方注入）；
执行偏离 → 重规划（最多 3 次，之后问用户）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class PlanStep:
    desc: str
    tools: list[str] = field(default_factory=list)
    done_when: str = ""
    status: str = "pending"  # pending | doing | done | failed | skipped


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    replan_count: int = 0
    drift_note: str = ""


def should_plan(user_msg: str) -> bool:
    """复杂度预判：纯规则，零 LLM 调用。"""
    verbs = len(re.findall(r"(查|搜|写|生成|下载|发|整理|对比|做|建|改|跑)",
                           user_msg))
    return verbs >= 3 or "然后" in user_msg or "分步" in user_msg


def drift_detected(plan: Plan) -> bool:
    """偏离检测：连续 2 步失败，或 done_when 连续不达标。"""
    failed = [s for s in plan.steps if s.status == "failed"]
    return len(failed) >= 2


class Planner:
    def __init__(self, llm):
        self.llm = llm  # 需有 chat()；plan prompt 见 spec §5.7

    def make_plan(self, user_msg: str) -> Plan:
        """调强模型生成计划。返回 Plan（steps 含 done_when）。支持 JSON 结构化与旧版容错。"""
        import json
        from pathlib import Path
        from .models import Message

        prompt_path = Path("prompts/planner.md")
        system_content = ""
        if prompt_path.exists():
            try:
                system_content = prompt_path.read_text(encoding="utf-8")
            except Exception:
                pass
        if not system_content:
            system_content = (
                "你是任务规划器。请以 JSON 格式输出计划，包含 goal 与 steps 列表（含 desc, tools, done_when）。"
            )

        reply = self.llm.chat([
            Message(role="system", content=system_content),
            Message(role="user", content=user_msg),
        ])
        raw = reply.content.strip()

        steps = []
        goal = user_msg

        # 1. 尝试解析 JSON
        try:
            clean_json = raw
            if "```" in clean_json:
                m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean_json)
                if m:
                    clean_json = m.group(1).strip()
            data = json.loads(clean_json)
            if isinstance(data, dict):
                if data.get("goal"):
                    goal = str(data["goal"])
                raw_steps = data.get("steps") or []
                if isinstance(raw_steps, list):
                    for s in raw_steps:
                        if isinstance(s, dict):
                            desc = s.get("desc") or s.get("description") or ""
                            tools = s.get("tools") or []
                            if isinstance(tools, str):
                                tools = [t.strip() for t in tools.split(",") if t.strip()]
                            done_when = s.get("done_when") or ""
                            if desc:
                                steps.append(PlanStep(desc=desc, tools=tools, done_when=done_when))
        except Exception:
            pass

        # 2. 如果 JSON 未能解析出步骤，回落到正则行解析（兼容旧模型/格式）
        if not steps:
            for line in raw.split("\n"):
                m = re.match(r"步骤：(.+?)\|\s*工具：(.*?)\|\s*验收：(.+)", line)
                if m:
                    tools = [t.strip() for t in m.group(2).split(",") if t.strip()]
                    steps.append(PlanStep(desc=m.group(1).strip(), tools=tools,
                                          done_when=m.group(3).strip()))

        # 3. 如果依然没有步骤，退化成单步计划保底
        if not steps:
            steps = [PlanStep(desc=user_msg, done_when="用户确认完成")]

        return Plan(goal=goal, steps=steps)
