"""Classify actual tool operations without treating ordinary data as commands."""
from __future__ import annotations

import re
import shlex


_DAILY_TOOLS = frozenset({
    "file_read", "web_search", "memory_search",
    "desktop_list_items", "desktop_list_shortcuts", "desktop_list_windows",
    "desktop_snapshot", "desktop_inspect_focus", "desktop_screenshot",
    "desktop_show_desktop", "desktop_open_app", "desktop_open", "desktop_wait",
    "desktop_click", "desktop_type", "desktop_press", "desktop_hotkey", "desktop_scroll",
    "desktop_hover", "desktop_drag", "desktop_probe_app",
    "browser_navigate", "browser_snapshot", "browser_screenshot", "browser_wait",
    "browser_evaluate", "browser_console", "browser_network_log",
    "browser_inspect", "browser_resource",
    "browser_wait_for", "browser_storage", "browser_press",
    "browser_handler", "browser_observe",
    "browser_upload", "browser_scroll", "browser_hover", "browser_select",
    "browser_find_text",
    "browser_click", "browser_click_at", "browser_probe_at", "browser_type", "browser_fill_input", "browser_save_image", "browser_request_takeover",
    # browser_click_at 与 browser_click 同级：普通点击动作，不弹审批卡
    "browser_new_tab", "browser_switch_tab", "browser_close_tab",
    # 浏览器录屏控制
    "browser_record_start", "browser_record_stop",
    # 浏览器多进程/上下文与辅助工具，同属常规自动化工具，永不弹审批卡
    "browser_context_new", "browser_context_switch", "browser_context_close",
    "browser_context_list", "browser_context_cookies",
    "browser_verify_code_countdown", "browser_lock_input", "browser_unlock_input",
    # 内部状态记账工具（钉死/核对关键值），无外部副作用，永不弹审批卡
    # v4 实测：state_pin 被定 L2→L3，用户被迫在微信上点两次通过，任务还疑似卡住
    "state_pin", "state_check",
})
_DANGEROUS_TOOLS = frozenset({
    "file_delete", "file_remove", "delete_file", "delete_directory", "delete_account",
    "payment", "pay", "purchase", "transfer_funds", "publish", "publish_post",
    "send", "send_email", "email_send", "send_message", "message_send",
    "shutdown", "reboot", "uninstall", "format_disk", "format_volume", "wipe_data",
})
_DANGEROUS_ACTIONS = frozenset({
    "delete", "remove", "delete_file", "delete_directory", "delete_account",
    "purge", "wipe", "wipe_data", "format_disk", "format_volume",
    "payment", "pay", "purchase", "transfer_funds", "publish", "publish_post",
    "post", "send", "send_email", "send_message", "shutdown", "reboot", "uninstall",
})
# These are already permitted by the shell execution layer and have no write mode.
# Do not include arbitrary interpreters, launchers, network clients, or env/set.
_READONLY_SHELL = frozenset({
    "where", "which", "ls", "dir", "pwd", "whoami", "hostname", "tasklist",
    "findstr", "cat", "type", "echo",
})
_DANGEROUS_SHELL = frozenset({
    "rm", "rmdir", "rd", "del", "erase", "remove-item", "format", "mkfs",
    "shutdown", "regedit", "uninstall",
})
# Reject command chaining, substitutions, expansion and redirection even in quotes.
# A conservative false negative only retains the existing approval path.
_SHELL_SYNTAX = re.compile(r"[\r\n\x00&|;<>`$^%!()]")


def _operation(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s-]+", "_", value.strip().casefold())


def _shell_risk(command):
    if not isinstance(command, str) or not command.strip():
        return "L2"
    try:
        argv = shlex.split(command, posix=False)
    except ValueError:
        return "L2"
    if not argv:
        return "L2"
    name = argv[0].strip('"').casefold()
    if name in _DANGEROUS_SHELL:
        return "L3"
    if name == "git" and len(argv) > 1 and argv[1].casefold() in {"push", "clean"}:
        return "L3"
    if name in _READONLY_SHELL and not _SHELL_SYNTAX.search(command):
        return "L1"
    return "L2"


def _dangerous_keys(tool, args):
    if tool not in {"desktop_hotkey", "desktop_press"}:
        return False
    keys = args.get("keys", []) if tool == "desktop_hotkey" else args.get("key") or args.get("name") or ""
    if isinstance(keys, str):
        keys = keys.split("+")
    if not isinstance(keys, (list, tuple)):
        return False
    # Use the same pure key aliases as the actual desktop runtime.
    from tools.desktop_runtime import normalize_key
    normalized = {normalize_key(key) for key in keys if isinstance(key, str)}
    if "del" in normalized:
        normalized.add("delete")
    return ("delete" in normalized
            or {"alt", "f4"}.issubset(normalized)
            or {"ctrl", "alt", "delete"}.issubset(normalized))


def classify_tool_risk(tool: str, args: dict) -> str:
    """Return L1 for routine actions, L3 for explicit dangerous operations.

    Text, URLs, paths, titles and unrelated metadata are data, not action names.
    Desktop L1 classification does not waive the runtime's evidence checks.
    Unknown operations retain L2 and the caller's provenance escalation.
    """
    name = str(tool or "").casefold()
    values = args if isinstance(args, dict) else {}
    if name in _DANGEROUS_TOOLS:
        return "L3"
    if any(_operation(values.get(field)) in _DANGEROUS_ACTIONS for field in ("action", "operation")):
        return "L3"
    if _dangerous_keys(name, values):
        return "L3"
    if name == "shell_exec":
        return _shell_risk(values.get("command"))
    if name in _DAILY_TOOLS:
        return "L1"
    return "L2"