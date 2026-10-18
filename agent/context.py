"""协议感知的上下文组装：保留当前证据与任务目标，按任务限制自动截图。"""
from __future__ import annotations

from pathlib import Path
import json
import re
from dataclasses import replace

from .models import Message

from typing import Callable

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_system_prompt() -> str:
    return (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")


def _protocol_groups(conversation: list[Message]) -> list[tuple[list[Message], bool]]:
    """Keep tool requests, their replies, and visual observations indivisible."""
    groups: list[tuple[list[Message], bool]] = []
    index = 0
    while index < len(conversation):
        message = conversation[index]
        if message.role == "assistant" and message.tool_calls:
            expected = [call.id for call in message.tool_calls]
            replies: dict[str, Message] = {}
            index += 1
            while index < len(conversation) and conversation[index].role == "tool":
                reply = conversation[index]
                if reply.tool_call_id in expected and reply.tool_call_id not in replies:
                    replies[reply.tool_call_id] = reply
                index += 1

            observations = []
            while (index < len(conversation)
                   and conversation[index].role == "user"
                   and conversation[index].name == "tool_observation"):
                observations.append(conversation[index])
                index += 1

            if (all(expected) and len(set(expected)) == len(expected)
                    and set(replies) == set(expected)):
                # Tool replies follow request order, before any image observation.
                groups.append(([message] + [replies[call_id] for call_id in expected]
                               + observations, True))
            # An incomplete historic request must never reach the API or be rerun.
            continue
        if message.role != "tool" and message.name != "tool_observation":
            groups.append(([message], False))
        index += 1
    return groups



def _is_real_user(message: Message) -> bool:
    return (message.role == "user"
            and message.name not in {"tool_observation", "verification_feedback", "plan_step", "context_task_goal"}
            and not message.content.startswith("[自我验收"))


def _visual_context(groups, max_observations):
    """Limit automatic screenshots to the active task, without editing history."""
    real_users = [message for messages, _ in groups for message in messages if _is_real_user(message)]
    latest = real_users[-1] if real_users else None
    scoped = bool(latest and (latest.metadata.get("task_origin_id") or latest.metadata.get("task_goal")))
    active = (latest.metadata.get("task_origin_id") or latest.id) if scoped else None
    entries = []
    inherited = None
    for index, (messages, _) in enumerate(groups):
        for message in messages:
            if _is_real_user(message):
                inherited = message.metadata.get("task_origin_id") or message.id
            if message.name == "tool_observation" and message.images:
                origin = message.metadata.get("task_origin_id") or inherited
                entries.append((index, message.id, origin))
    eligible = [entry for entry in entries if not scoped or entry[2] == active]
    keep = {(index, message_id) for index, message_id, _ in eligible[-max_observations:]}
    output = []
    for index, (messages, is_tool) in enumerate(groups):
        selected = []
        for message in messages:
            if (message.name == "tool_observation" and message.images
                    and (index, message.id) not in keep):
                message = replace(
                    message, images=[],
                    content=message.content + "\n[历史自动截图像素已从本次请求省略，保留文字记录；桌面操作前请重新获取当前截图。]",
                    metadata={**message.metadata, "visual_context_omitted": True},
                )
            selected.append(message)
        output.append((selected, is_tool))
    return output


_DESKTOP_EVIDENCE_TOOLS = {
    "desktop_snapshot", "desktop_screenshot", "desktop_inspect_focus",
    "desktop_click", "desktop_type", "desktop_press", "desktop_hotkey", "desktop_scroll",
}


def _desktop_payload(message):
    if message.role != "tool" or message.name not in _DESKTOP_EVIDENCE_TOOLS:
        return None
    text = message.content
    # Action replies contain verification JSON, a screenshot header, then its
    # evidence JSON. Decode the evidence object while preserving all other text.
    decoder = json.JSONDecoder()
    for match in reversed(list(re.finditer(r"(?m)^[{]", text))):
        try:
            payload, end = decoder.raw_decode(text, match.start())
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        controls = payload.get("controls")
        if isinstance(controls, list):
            count = len(controls)
        elif isinstance(controls, dict) and isinstance(controls.get("rows"), list):
            count = len(controls["rows"])
        else:
            continue
        return text[:match.start()], payload, count, text[end:]
    return None


def _compact_historical_desktop(groups):
    """Keep the latest desktop batch exact; older trees are stale, costly evidence."""
    parsed = {}
    latest = -1
    for index, (messages, _) in enumerate(groups):
        for offset, message in enumerate(messages):
            value = _desktop_payload(message)
            if value is not None:
                parsed[(index, offset)] = value
                latest = index
    output = []
    for index, (messages, is_tool) in enumerate(groups):
        selected = []
        for offset, message in enumerate(messages):
            value = parsed.get((index, offset))
            if index < latest and value is not None and value[2]:
                prefix, payload, count, suffix = value
                payload["controls"] = []
                payload["historical_controls_omitted"] = count
                payload["evidence_note"] = (
                    "历史控件树已省略；保留当时操作结果。"
                    "后续操作应使用最新桌面截图和控件证据，不复用旧控件坐标。"
                )
                message = replace(
                    message, content=prefix + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + suffix,
                    metadata={**message.metadata, "historical_controls_omitted": count},
                )
            selected.append(message)
        output.append((selected, is_tool))
    return output


def _protected_context_index(groups):
    latest_tool = next((i for i in range(len(groups) - 1, -1, -1)
                        if groups[i][1]), len(groups) - 1)
    latest_user = next((message for messages, _ in reversed(groups)
                        for message in reversed(messages) if _is_real_user(message)), None)
    scoped = bool(latest_user and (latest_user.metadata.get("task_origin_id")
                                  or latest_user.metadata.get("task_goal")))
    active = (latest_user.metadata.get("task_origin_id") or latest_user.id) if scoped else None
    inherited = None
    latest_desktop = None
    for index, (messages, _) in enumerate(groups):
        for message in messages:
            if _is_real_user(message):
                inherited = message.metadata.get("task_origin_id") or message.id
        origin = next((message.metadata["task_origin_id"] for message in messages
                       if message.name == "tool_observation" and message.metadata.get("task_origin_id")),
                      inherited)
        if (not scoped or origin == active) and any(_desktop_payload(message) is not None for message in messages):
            latest_desktop = index
    # A file/browser call after the current desktop evidence must not make that
    # evidence eligible for slimming or remove it from the protected tail.
    return min(latest_tool, latest_desktop) if latest_desktop is not None else latest_tool

class ContextBuilder:
    def __init__(self, max_tokens: int = 128000, extra_system: str | Callable[[], str] = "",
                 max_tool_observations: int = 2):
        self.max_tokens = max_tokens
        self.extra_system = extra_system
        if (isinstance(max_tool_observations, bool) or not isinstance(max_tool_observations, int)
                or max_tool_observations < 1):
            raise ValueError("max_tool_observations must be a positive integer")
        self.max_tool_observations = max_tool_observations

    def build(self, conversation: list[Message], tools: list[dict] | None = None) -> list[Message]:
        """组装发给 LLM 的消息列表。动态读取 system.md 与最新记忆，按需注入 Skill。"""
        system_text = load_system_prompt()
        extra = self.extra_system() if callable(self.extra_system) else self.extra_system
        if extra and extra.strip():
            system_text += "\n\n# 用户记忆（已持久化偏好与事实）\n" + extra.strip()

        # Skill 按需注入：若本轮 tools 含 browser_* 工具或历史包含 browser 动作，动态追加 browser.md
        need_browser = False
        if tools:
            for t in tools:
                name = (t.get("function") or {}).get("name", "")
                if name.startswith("browser_"):
                    need_browser = True
                    break
        if not need_browser and conversation:
            for m in conversation[-3:]:
                if "browser" in (m.content or "") or any(tc.name.startswith("browser_") for tc in m.tool_calls):
                    need_browser = True
                    break

        if need_browser:
            browser_skill_file = PROMPTS_DIR / "skills" / "browser.md"
            if browser_skill_file.exists():
                try:
                    system_text += "\n\n# Skill: browser\n" + browser_skill_file.read_text(encoding="utf-8")
                except Exception:
                    pass

        system = Message(role="system", content=system_text)
        budget = self.max_tokens - len(system_text) // 3

        groups = _compact_historical_desktop(
            _visual_context(_protocol_groups(conversation), self.max_tool_observations))
        if not groups:
            return [system]
        total_cost = sum(len(message.content) // 3 + 500
                         for messages, _ in groups for message in messages)
        if total_cost > budget and len(conversation) > 4:
            from .compaction import smart_compact
            # Current evidence is indivisible and must retain snapshot_id/controls.
            # Slim only older complete groups, using copies so checkpoints stay exact.
            protected = _protected_context_index(groups)
            older = [replace(message, metadata=dict(message.metadata))
                     for messages, _ in groups[:protected] for message in messages]
            groups = _protocol_groups(smart_compact(older)) + groups[protected:]

        # Keep the latest complete tool interaction and the newer dialogue even
        # when that indivisible interaction exceeds the remaining token budget.
        first = _protected_context_index(groups)
        used = sum(len(message.content) // 3 + 500
                   for messages, _ in groups[first:] for message in messages)
        for index in range(first - 1, -1, -1):
            cost = sum(len(message.content) // 3 + 500 for message in groups[index][0])
            if used + cost > budget:
                break
            first = index
            used += cost
        tail = [message for messages, _ in groups[first:] for message in messages]
        latest_user = next((message for message in reversed(conversation)
                            if _is_real_user(message)), None)
        goal = latest_user.metadata.get("task_goal") if latest_user else None
        if (isinstance(goal, str) and goal.strip()
                and not any(_is_real_user(message) and goal in message.content for message in tail)):
            # Metadata is local-only. Keep the original goal visible to the API
            # when its user message was trimmed; never elevate it to system text.
            tail.insert(0, Message(
                role="user", name="context_task_goal",
                content="[当前任务的原始用户目标]\n" + goal,
            ))
        return [system] + tail
