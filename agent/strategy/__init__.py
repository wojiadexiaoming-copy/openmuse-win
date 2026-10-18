"""策略层：站点适配器 + 恢复策略 + 跨站流程模板。

工具层（tools/browser/）解决"怎么操作页面"；
策略层解决"出问题怎么办"和"多站点任务怎么编排"。

三件套：
- site_adapter：站点适配器框架（§38.6 桌面适配器模式的浏览器版）
- recovery：异常自动恢复策略（对接 agent/failure_memory.py 的去重记忆）
- flows：跨站任务编排模板（数据驱动的流程定义）

设计原则：
- 通用框架 + 示例，不写单站点专用逻辑进核心
- 给 LLM 提供结构，不替代 LLM 决策
- 恢复策略是"建议"，最终执行仍由 agent 循环决定
"""
from .site_adapter import SiteAdapter, SiteAdapterRegistry
from .recovery import RecoveryPolicy, RecoveryAction
from .flows import FlowTemplate, FLOW_TEMPLATES, get_template, template_prompt, match_template

__all__ = [
    "SiteAdapter", "SiteAdapterRegistry",
    "RecoveryPolicy", "RecoveryAction",
    "FlowTemplate", "FLOW_TEMPLATES", "get_template", "template_prompt", "match_template",
]
