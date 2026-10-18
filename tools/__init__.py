"""M1 工具注册入口。"""
from __future__ import annotations

import os
from pathlib import Path

from agent.tools_registry import ToolDefinition, ToolRegistry

from . import file_tools
from .file_tools import FILE_TOOLS
from .media_tools import MEDIA_TOOLS
from .media_tools import configure as _media_configure
from .shell_exec import SHELL_TOOL
from .web_search import WEB_SEARCH_TOOL
from .web_search import configure as _ws_configure

# §39.1.2 关键状态钉死：state_pin / state_check（agent 层模块，无 tools 层依赖）
from agent.task_state import TASK_STATE_TOOLS


def register_all(registry: ToolRegistry, cfg: dict, api_key: str = ""):
    """注册 M1 全部工具及多模态媒体生成工具。M2+ 的工具在各自模块注册。"""
    # workspace 根目录注入（file_* 工具的安全边界）
    file_tools.WORKSPACE = Path(cfg["agent"].get("workspace", ".")).resolve()
    tools_cfg = cfg.get("tools", {})
    registry.register(_apply_cfg(SHELL_TOOL, tools_cfg.get("shell_exec", {})))
    for t in FILE_TOOLS:
        registry.register(t)
    for t in TASK_STATE_TOOLS:
        registry.register(t)
    ws_cfg = tools_cfg.get("web_search", {})
    if ws_cfg.get("enabled"):
        _ws_configure(ws_cfg.get("api_url", ""),
                      os.environ.get(ws_cfg.get("api_key_env", ""), ""))
        registry.register(_apply_cfg(WEB_SEARCH_TOOL, ws_cfg))

    media_cfg = cfg.get("media", {})
    llm_key = api_key or os.environ.get("ALTEREGO_LLM_API_KEY", "")
    _media_configure(media_cfg.get("base_url", ""), llm_key)
    for t in MEDIA_TOOLS:
        registry.register(t)

    browser_cfg = tools_cfg.get("browser", {})
    if browser_cfg.get("enabled", True):
        from .browser.circuit import CircuitBreaker
        from .browser.profiles import ProfileManager
        from .browser.worker import BrowserWorker, make_browser_tools
        data_dir = Path(os.environ.get("ALTEREGO_DATA", str(file_tools.WORKSPACE)))
        pm = ProfileManager(data_dir / "profiles.yaml", data_dir / "browsers")
        cb = CircuitBreaker("default")
        split_screen = browser_cfg.get("split_screen", True)
        browser_ratio = float(browser_cfg.get("browser_ratio", 0.80))
        bw = BrowserWorker(
            pm, cb, profile="default",
            headless=browser_cfg.get("headless", False),
            split_screen=split_screen,
            browser_ratio=browser_ratio,
            auto_record=browser_cfg.get("auto_record", True),
        )
        registry.browser_worker = bw
        for t in make_browser_tools(bw):
            registry.register(_apply_cfg(t, browser_cfg))

    desktop_cfg = tools_cfg.get("desktop", {})
    if desktop_cfg.get("enabled", True):
        from .desktop import DESKTOP_TOOLS
        for t in DESKTOP_TOOLS:
            registry.register(_apply_cfg(t, desktop_cfg))


def _apply_cfg(tool: ToolDefinition, cfg: dict) -> ToolDefinition:
    if "enabled" in cfg:
        tool.enabled = bool(cfg["enabled"])
    return tool
