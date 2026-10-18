"""§32.5 动态复杂度路由：每次调用前按输入复杂度实时选模型。

三档：weak（分类/抽取/格式化）/ standard（默认）/ strong（复杂推理）。
弱模型翻车 → 自动升级强模型重试一次。
"""
from __future__ import annotations

# 任务类型 → 固定档位（零 LLM 调用）
LOW_TASKS = {"memory_extract", "compact_summary", "format", "classify"}
HIGH_TASKS = {"browser_decide", "workflow_plan", "code_write"}


def estimate_complexity(task: str, context: dict) -> str:
    """纯规则打分，<1ms。返回 low | standard | high。"""
    if task in LOW_TASKS:
        return "low"
    if context.get("weak_failed_before"):
        return "high"          # 弱模型刚翻车过 → 直接上强模型
    if context.get("quality_first"):
        return "high"
    score = 0
    if task in HIGH_TASKS:
        score += 2
    tokens = context.get("est_tokens", 0)
    if tokens > 8000:
        score += 2
    elif tokens > 2000:
        score += 1
    if score >= 3:
        return "high"
    if score >= 1:
        return "standard"
    return "low"


def route_model(task: str, context: dict, models: dict) -> str:
    """models: {"weak": "...", "standard": "...", "strong": "..."}。"""
    level = estimate_complexity(task, context)
    return models[level]


def chat_with_fallback(llm_chat, messages, task: str, context: dict,
                       models: dict, strong_model: str):
    """llm_chat(messages, model) -> str。弱模型失败升级重试一次。"""
    model = route_model(task, context, models)
    try:
        return llm_chat(messages, model), model
    except Exception as e:  # noqa: BLE001 —— 升级重试
        context = dict(context, weak_failed_before=True)
        return llm_chat(messages, strong_model), strong_model
