"""§5.5 工具并行执行：分组（只读并行 / 写串行 / 不可并行串行）。

M4 接入 orchestrator.run_turn。依赖 asyncio；工具函数本身是同步的，
用线程池跑（run_in_executor），不改现有工具实现。
"""
from __future__ import annotations

import asyncio

# 工具默认兜底属性表（仅在无 registry 或未注册工具时兜底使用）
TOOL_ATTRS = {
    "file_read":    {"parallel_safe": True, "is_readonly": True},
    "web_search":   {"parallel_safe": True, "is_readonly": True},
    "memory_search": {"parallel_safe": True, "is_readonly": True},
    "file_write":   {"parallel_safe": True, "is_readonly": False},
    "file_edit":    {"parallel_safe": True, "is_readonly": False},
    "shell_exec":   {"parallel_safe": False, "is_readonly": False},
}


def _attrs(registry, name: str) -> dict:
    """以 ToolDefinition 作为属性优先来源 (B5-01)，已知标准工具提供兜底。"""
    fallback = TOOL_ATTRS.get(name, {"parallel_safe": True, "is_readonly": False})
    if registry is not None and hasattr(registry, "get"):
        try:
            tool = registry.get(name)
            p_safe = getattr(tool, "parallel_safe", None)
            if p_safe is None or (p_safe is True and fallback.get("parallel_safe") is False):
                p_safe = fallback.get("parallel_safe", True)

            ro = getattr(tool, "is_readonly", None)
            if ro is None or (ro is False and fallback.get("is_readonly") is True):
                ro = fallback.get("is_readonly", False)

            return {
                "parallel_safe": bool(p_safe),
                "is_readonly": bool(ro),
            }
        except Exception:
            pass
    return fallback


async def execute_tool_calls(tool_calls, registry, task_id: str = "?"):
    """分组执行，返回与 tool_calls 同顺序的 Message 列表。"""
    from .models import Message
    results: dict[str, Message] = {}

    readonly = [tc for tc in tool_calls
                if _attrs(registry, tc.name)["parallel_safe"] and _attrs(registry, tc.name)["is_readonly"]]
    writers = [tc for tc in tool_calls
               if _attrs(registry, tc.name)["parallel_safe"] and not _attrs(registry, tc.name)["is_readonly"]]
    serial = [tc for tc in tool_calls if not _attrs(registry, tc.name)["parallel_safe"]]

    # 1. 只读组：线程池并发
    if readonly:
        loop = asyncio.get_running_loop()
        done = await asyncio.gather(*[
            loop.run_in_executor(None, registry.execute, tc.name, tc.arguments)
            for tc in readonly
        ], return_exceptions=True)
        for tc, msg in zip(readonly, done):
            if isinstance(msg, Exception):
                msg = Message(role="tool", name=tc.name,
                              content=f"[工具执行失败] {tc.name}: {msg}")
            msg.tool_call_id = tc.id
            results[tc.id] = msg

    # 2. 写操作：串行（防竞态）
    for tc in writers:
        msg = registry.execute(tc.name, tc.arguments)
        msg.tool_call_id = tc.id
        results[tc.id] = msg

    # 3. 不可并行：逐个串行
    for tc in serial:
        if tc.name == "spawn_subagent":
            raise NotImplementedError("spawn_subagent 走 SubagentManager 异步派发")
        msg = registry.execute(tc.name, tc.arguments)
        msg.tool_call_id = tc.id
        results[tc.id] = msg

    return [results[tc.id] for tc in tool_calls]
