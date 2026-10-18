"""任务关键状态钉死（spec §39.1.2）。

铁律：模型的自述值一律视为未经验证。关键值（邮箱/验证码/订单号等）
必须从截图由验证者读回、state_pin 入库；填表/提交前 state_check
（或 browser_type 的 assert_pinned_key）assert 相等，不一致直接拒绝。

Agnes 翻车现场：模型自述 9e70240a22@inodetree.com，截图里是
caleb...@temp.inodetree.com，一次口误污染全任务。有钉死机制，
口误在第一步就被拦下。
"""
from __future__ import annotations

import time

from agent.tools_registry import ToolDefinition


class TaskState:
    """单任务内的关键值钉死存储。"""

    def __init__(self):
        self._pins: dict[str, dict] = {}

    def pin(self, key: str | None, value: str | None, source: str = "") -> tuple[bool, str]:
        key = (key or "").strip()
        if not key:
            return False, "[钉死失败] key 不能为空"
        if value is None or str(value).strip() == "":
            return False, "[钉死失败] value 不能为空（不许钉死空值）"
        self._pins[key] = {"value": str(value), "source": source or "未注明",
                           "pinned_at": time.time()}
        return True, f"[已钉死] {key}={value}（来源: {self._pins[key]['source']}）"

    def get(self, key: str | None) -> str | None:
        pin = self._pins.get((key or "").strip())
        return pin["value"] if pin else None

    def check(self, key: str | None, value: str | None) -> tuple[bool, str]:
        """填表/提交前 assert：未钉死或不一致都拒绝。"""
        key = (key or "").strip()
        pin = self._pins.get(key)
        if pin is None:
            return False, (f"[钉死检查失败] {key} 从未钉死。"
                           "先从截图读回真实值，用 state_pin 钉死后再用。")
        if str(value) != pin["value"]:
            return False, (f"[钉死检查失败] {key} 要用的值 {value!r} 与钉死值 "
                           f"{pin['value']!r} 不一致。以钉死值为准"
                           f"（来源: {pin['source']}），不许用自述值覆盖。")
        return True, f"[钉死检查通过] {key}={value}"

    def reset(self):
        self._pins.clear()


_state = TaskState()


def current_task_state() -> TaskState:
    return _state


def _tool_pin(args: dict) -> str:
    ok, msg = current_task_state().pin(args.get("key"), args.get("value"),
                                       args.get("source", ""))
    return msg


def _tool_check(args: dict) -> str:
    ok, msg = current_task_state().check(args.get("key"), args.get("value"))
    return msg


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required}


TASK_STATE_TOOLS = [
    ToolDefinition(
        name="state_pin",
        description=("钉死关键值（§39.1.2）。邮箱/验证码/订单号等关键值必须从截图"
                     "读回真实值后钉死，模型的自述值一律视为未经验证，不许直接钉死自述值。"
                     "钉死后填表用 browser_type(..., assert_pinned_key=key) 强制比对。"),
        parameters=_schema({
            "key": {"type": "string", "description": "键名，如 email、verify_code"},
            "value": {"type": "string", "description": "从截图读回的真实值"},
            "source": {"type": "string", "default": "",
                       "description": "来源说明，如'邮箱页截图读回'"}},
            ["key", "value"]),
        func=_tool_pin),
    ToolDefinition(
        name="state_check",
        description=("钉死检查（§39.1.2）。使用关键值前 assert：未钉死或与钉死值不一致"
                     "直接拒绝。"),
        parameters=_schema({
            "key": {"type": "string", "description": "键名"},
            "value": {"type": "string", "description": "要使用的值"}},
            ["key", "value"]),
        func=_tool_check),
]
