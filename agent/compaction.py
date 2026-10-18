"""§5.6 智能压缩：三阶段（规则瘦身 → 分层取舍 → LLM 摘要）。

M4 接入 ContextBuilder：超限时先 smart_compact 再硬截断。
"""
from __future__ import annotations

import re

from .models import Message

# 工具结果瘦身：保留含这些关键词的行
_KEEP_KW = ("结果", "完成", "失败", "错误", "找到", "http", "✅", "❌")


def slim_tool_result(m: Message, limit: int = 2000) -> Message:
    """工具结果 >limit 字符 → 只留结论行 + 数字/链接。"""
    if m.role != "tool" or len(m.content) <= limit:
        return m
    lines = m.content.split("\n")
    keep = [l for l in lines if any(k in l for k in _KEEP_KW)][:30]
    nums = sorted(set(re.findall(r"\d+(?:\.\d+)?%?|https?://\S+", m.content)))
    m.content = ("[工具结果已瘦身]\n" + "\n".join(keep)
                 + f"\n关键数字/链接：{'; '.join(nums[:20])}")
    return m


def _atomic_groups(conversation: list[Message]) -> list[list[Message]]:
    """§5.6 原子组切分：[assistant(带 tool_calls) + 其后所有对应 tool 消息] 为不可分割整体。

    OpenAI/Anthropic/Qwen 的 Function Calling 铁律：
    - 任何 role="tool" 的消息，其紧邻前文必须有包含对应 tool_call_id 的 assistant 消息
    - 包含 tool_calls 的 assistant 消息后必须紧跟对应的全部 tool 结果消息

    违反此规则 → 下一轮 llm.chat() 直接 HTTP 400，整个会话报废。
    因此切分/筛选历史时必须以原子组为单位：要么整组保留，要么整组进摘要，严禁拆散。
    """
    groups: list[list[Message]] = []
    i = 0
    n = len(conversation)
    while i < n:
        m = conversation[i]
        # assistant 带 tool_calls：向后收集所有对应的 tool 消息
        if m.role == "assistant" and m.tool_calls:
            group = [m]
            expected_ids = {tc.id for tc in m.tool_calls}
            j = i + 1
            while j < n and conversation[j].role == "tool":
                group.append(conversation[j])
                expected_ids.discard(conversation[j].tool_call_id)
                j += 1
            # 即使 tool 结果不全，也整组绑定（残缺组进摘要时一并处理）
            groups.append(group)
            i = j
        else:
            groups.append([m])
            i += 1
    return groups


def triage(conversation: list[Message]) -> tuple[list[Message], list[Message]]:
    """分层：keep 直接保留，compressible 进摘要。只看最近 40 条。

    §5.6 铁律：以原子组为单位筛选，严禁拆散 assistant(tool_calls)+tool 结果对。
    """
    keep, compressible = [], []
    # 先按原子组切分，再对每组整体决策
    groups = _atomic_groups(conversation[-40:])
    for group in groups:
        # 组内任一消息命中保留规则 → 整组保留
        keep_group = False
        for m in group:
            if m.role == "user":
                keep_group = True
                break
            if "决策" in m.content or "确认" in m.content or "批准" in m.content:
                keep_group = True
                break
            if m.role == "tool" and "失败" in m.content:
                # 失败的工具组：保留精简版（整组替换为一条摘要）
                keep.append(Message(role="tool", name=m.name,
                                    content=f"[{m.name}] 曾失败，已换方法处理"))
                keep_group = True  # 标记已处理，跳过整组
                break
        if keep_group:
            # 已单独处理失败组的情况不重复添加
            if not (len(group) == 1 and group[0].role == "tool"
                    and "曾失败" in group[0].content):
                # 检查是否已添加过精简版
                already_added = any(
                    k.role == "tool" and "曾失败" in k.content for k in keep[-1:])
                if not already_added:
                    keep.extend(group)
        else:
            compressible.extend(group)
    return keep, compressible


def smart_compact(conversation: list[Message], summarize=None) -> list[Message]:
    """summarize(texts) -> str：LLM 摘要函数（由调用方注入，便于测试）。"""
    slimmed = [slim_tool_result(m) for m in conversation]
    if summarize is None:
        return slimmed  # 无摘要函数时不丢消息，只做规则瘦身
    keep, compressible = triage(slimmed)
    if not compressible:
        return keep
    summary = summarize("\n".join(
        f"[{m.role}] {m.content[:500]}" for m in compressible))
    return [Message(role="system",
                    content=f"[历史摘要]\n{summary}")] + keep


def make_worker_summarizer(cfg: dict | None = None, vault=None):
    """构建基于 Worker 角色的 LLM 摘要函数（spec §5.6 / prompts/worker.md）。"""
    from .llm import LLMClient
    from pathlib import Path
    worker_prompt_file = Path("prompts/worker.md")
    worker_sys = (worker_prompt_file.read_text(encoding="utf-8")
                  if worker_prompt_file.exists()
                  else "你是后台 Worker，请对以下对话内容进行精炼条列摘要。")

    def _summarize(text: str) -> str:
        try:
            if not cfg:
                return text[:1000]
            worker_llm = LLMClient.for_role(cfg, "worker", vault)
            res = worker_llm.chat([
                Message(role="system", content=worker_sys),
                Message(role="user", content=f"请对以下材料进行客观摘要（输出中文条列）：\n\n{text[:6000]}"),
            ])
            return res.content.strip()
        except Exception:
            return text[:1000]

    return _summarize

