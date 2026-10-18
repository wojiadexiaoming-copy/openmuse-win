"""§37 设置系统：常驻权限（allow/ask/deny）+ 覆盖规则。

两层：常驻策略（本文件）→ 运行时审批（agent/approval.py，只有 ask 才进审批）。
"""
from __future__ import annotations

import json
from pathlib import Path


class Settings:
    """policy: 能力 -> allow | ask | deny。
    overrides: 按域名/app 覆盖，如 {"app:Recordly": {"desktop_click": "deny"}}。
    """

    DEFAULTS = {
        "file_read": "allow",
        "web_search": "allow",
        "memory_search": "allow",
        "image_generate": "allow",
        "video_generate": "allow",
        "browser_navigate": "allow",
        "browser_new_tab": "allow",
        "browser_switch_tab": "allow",
        "browser_close_tab": "allow",
        "browser_context_new": "allow",
        "browser_context_switch": "allow",
        "browser_context_close": "allow",
        "browser_context_list": "allow",
        "browser_context_cookies": "allow",
        "browser_snapshot": "allow",
        "browser_screenshot": "allow",
        "browser_click": "allow",
        "browser_click_at": "allow",
        "browser_probe_at": "allow",
        "browser_type": "allow",
        "browser_save_image": "allow",
        "browser_wait": "allow",
        "browser_wait_for": "allow",
        "browser_evaluate": "allow",
        "browser_console": "allow",
        "browser_network_log": "allow",
        "browser_inspect": "allow",
        "browser_resource": "allow",
        "browser_storage": "allow",
        "browser_press": "allow",
        "browser_handler": "allow",
        "browser_observe": "allow",
        "browser_upload": "allow",
        "browser_scroll": "allow",
        "browser_hover": "allow",
        "browser_select": "allow",
        "browser_find_text": "allow",
        "browser_verify_code_countdown": "allow",
        "browser_request_takeover": "allow",
        "browser_lock_input": "allow",
        "browser_unlock_input": "allow",
        "desktop_screenshot": "allow",
        "desktop_snapshot": "allow",
        "desktop_inspect_focus": "allow",
        "desktop_list_windows": "allow",
        "desktop_list_items": "allow",
        "desktop_list_shortcuts": "allow",
        "desktop_show_desktop": "allow",
        "desktop_open_app": "allow",
        "desktop_open": "allow",
        "file_write": "ask",
        "file_edit": "ask",
        "shell_exec": "ask",
        "desktop_click": "ask",
        "desktop_type": "ask",
        "desktop_hotkey": "ask",
        # L3 类默认 deny，由审批逐次放行（spec §36.1：L3 不允许永久批准）
        "file_delete": "deny",
        "publish": "deny",
        "payment": "deny",
    }

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.policy: dict[str, str] = dict(self.DEFAULTS)
        self.overrides: dict[str, dict[str, str]] = {}
        self.load()

    def load(self):
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.policy.update(data.get("policy", {}))
            self.overrides.update(data.get("overrides", {}))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"policy": self.policy, "overrides": self.overrides},
            ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, capability: str, target: str = "") -> str:
        """查某能力在某目标上的策略。target 如 "app:Recordly" / "domain:youtube.com"。"""
        if target and target in self.overrides:
            if capability in self.overrides[target]:
                return self.overrides[target][capability]
        return self.policy.get(capability, "ask")

    def set(self, capability: str, value: str, target: str = ""):
        assert value in ("allow", "ask", "deny"), f"非法策略值: {value}"
        if target:
            self.overrides.setdefault(target, {})[capability] = value
        else:
            self.policy[capability] = value
        self.save()
        # 铁律（spec §37）：删全部数据不默认删 Vault，防误锁死 —— 在 wipe() 里实现。
