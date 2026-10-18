"""异常自动恢复策略。

工具失败后，按结构化策略给出恢复动作建议（按优先级排序）。
对接 agent/failure_memory.py：同一任务内试过失败的策略不再建议。

策略表（通用，与站点无关）：

| 失败类型 | 恢复动作（按序） |
|---|---|
| 定位失败（选择器找不到） | snapshot 重看 → find_text 定位 → probe_at 探 → click_at 坐标点 |
| 动作失败（找到了点不中） | JS 降级已自动做 → 建议 inspect 看状态 → 坐标点 |
| 状态不对（按钮禁用） | 检查前置条件（表单填完没）→ 截图确认 |
| 瞬时错误（超时/网络抖动） | 重试一次（仅一次） |
| 页面异常（空白/崩溃） | 刷新重试 → 重新导航 |
| 验证码/风控 | 停止 + 报告（不硬闯） |
| 未知 | 收集诊断（console+network+inspect）→ 升级 |

这是"建议"不是"自动执行"：返回 RecoveryAction 列表，
由 agent 循环或 LLM 决定采纳哪个。自动执行只发生在明确安全
的动作（如重试一次），且同样受 failure_memory 去重约束。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RecoveryAction:
    """单个恢复动作建议。"""
    action: str          # 动作名：retry / relocate / inspect_state / refresh / renavigate / collect_diagnostics / escalate
    detail: str          # 给 LLM 的具体指引
    tool_hint: str = ""  # 建议用的工具（如 browser_find_text）
    auto: bool = False   # 是否可自动执行（无需 LLM 决策）
    priority: int = 99   # 越小越优先


# 错误分类关键词（按优先级匹配）
_ERROR_PATTERNS: list[tuple[str, list[str]]] = [
    ("captcha", ["captcha", "人机验证", "verify you are human", "turnstile"]),
    ("disabled", ["禁用", "disabled", "is disabled", "not enabled"]),
    ("not_found", ["未找到", "not found", "no element", "找不到", "Timeout", "timeout", "超时"]),
    ("action_failed", ["点不中", "not clickable", "not visible", "被遮挡", "intercept"]),
    ("page_broken", ["空白", "blank", "崩溃", "crashed", "net::", "ERR_"]),
    ("transient", ["socket", "econnreset", "502", "503", "504"]),
]


def classify_error(error_text: str) -> str:
    """错误文本 → 失败类型。"""
    low = (error_text or "").lower()
    for kind, keywords in _ERROR_PATTERNS:
        for kw in keywords:
            if kw.lower() in low:
                return kind
    return "unknown"


class RecoveryPolicy:
    """恢复策略：失败 → 动作建议列表。"""

    def suggest(self, tool_name: str, args: dict, error_text: str,
                failure_memory=None, task_id: str = "") -> list[RecoveryAction]:
        """主入口。failure_memory 可选（传了就去重）。"""
        kind = classify_error(error_text)
        actions = self._actions_for(kind, tool_name, args, error_text)
        if failure_memory is not None and task_id:
            actions = [a for a in actions
                       if not failure_memory.already_tried(task_id, f"{a.action}:{a.tool_hint}")]
        return sorted(actions, key=lambda a: a.priority)

    def _actions_for(self, kind: str, tool_name: str, args: dict,
                     error_text: str) -> list[RecoveryAction]:
        if kind == "captcha":
            return [RecoveryAction(
                "escalate", "遇到验证码/人机验证：不要硬闯。按验证码偏好处理（solve 则尝试一次），"
                "过不了就请求用户接管。", priority=1)]
        if kind == "disabled":
            return [
                RecoveryAction("inspect_state",
                    "按钮被禁用：先 browser_screenshot 看表单哪项没填完/有校验报错，补完再点。",
                    tool_hint="browser_screenshot", priority=1),
                RecoveryAction("collect_diagnostics",
                    "用 browser_inspect 看按钮的 disabled/aria-disabled 状态确认。",
                    tool_hint="browser_inspect", priority=2),
            ]
        if kind == "not_found":
            return [
                RecoveryAction("relocate",
                    "选择器找不到：① browser_snapshot 重看页面结构；② browser_find_text 用文本定位拿坐标；"
                    "③ browser_probe_at 先探后点；④ browser_click_at 按坐标点。不要只微调选择器重试。",
                    tool_hint="browser_find_text", priority=1),
                RecoveryAction("refresh",
                    "如果页面结构明显不对（空白/跳错页）：刷新后重试一次。",
                    tool_hint="browser_navigate", priority=2, auto=False),
            ]
        if kind == "action_failed":
            return [
                RecoveryAction("inspect_state",
                    "找到了但点不中：browser_inspect 看 pointer-events/遮挡/可见性。",
                    tool_hint="browser_inspect", priority=1),
                RecoveryAction("relocate",
                    "换坐标点：browser_find_text 拿精确坐标 → browser_click_at。",
                    tool_hint="browser_click_at", priority=2),
            ]
        if kind == "page_broken":
            return [
                RecoveryAction("refresh", "页面异常：刷新重试一次。",
                    tool_hint="browser_navigate", priority=1, auto=True),
                RecoveryAction("renavigate", "刷新不行：重新导航到任务 URL。",
                    tool_hint="browser_navigate", priority=2),
            ]
        if kind == "transient":
            return [RecoveryAction(
                "retry", "瞬时错误（超时/网络抖动）：原样重试一次。",
                priority=1, auto=True)]
        # unknown
        return [RecoveryAction(
            "collect_diagnostics",
            "未知失败：按沉默失败三查收集信息——browser_console({type:'error'}) → "
            "browser_network_log({errors_only:true, show_body:true}) → "
            "browser_inspect 看目标元素状态。先诊断再动手。",
            tool_hint="browser_console", priority=1)]

    def record(self, failure_memory, task_id: str, action: RecoveryAction, ok: bool):
        """记录恢复动作的结果（成功/失败），供去重。"""
        if failure_memory is None or not task_id:
            return
        try:
            failure_memory.record_tried(task_id, f"{action.action}:{action.tool_hint}", ok)
        except Exception:
            pass
