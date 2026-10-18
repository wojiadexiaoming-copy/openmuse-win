"""§19 桌面自动化：Windows UIA (pywinauto) + 屏幕控制 (pyautogui)。

安全机制（spec §19）：
1. pyautogui.FAILSAFE = True：甩到屏幕左上角立即熔断停止。
2. 权限分级：只读（截图/读窗口）放行；交互（点击/输入）记日志；危险（删除/付款）强制 L3 审批。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
import os
import subprocess
import time
from pathlib import Path

from agent.models import ImagePart, Message
from agent.tools_registry import ToolDefinition
from .desktop_runtime import (current_state, EvidenceError, normalize_key, valid_window_rect,
                              window_rect_matches, maximized_geometry_verified, DesktopHalted,
                              screen_rect_to_image)

logger = logging.getLogger(__name__)

def _init_dpi_awareness():
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

_init_dpi_awareness()

# 开启强制 FailSafe：鼠标移到屏幕任意角落立即抛 FailSafeException 终止
try:
    import pyautogui
    pyautogui.FAILSAFE = True
except Exception:
    pyautogui = None

try:
    import pyperclip
except Exception:
    pyperclip = None


def check_focus_contention(threshold_sec: float = 2.0) -> dict:
    """Check physical mouse/keyboard activity to avoid fighting with the user."""
    import sys
    if "pytest" in sys.modules or os.environ.get("ALTEREGO_SKIP_CONTENTION"):
        return {"contention": False, "idle_sec": 999.0}
    try:
        import ctypes
        from ctypes import wintypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            idle_sec = max(0.0, millis / 1000.0)
            return {"contention": idle_sec < threshold_sec, "idle_sec": idle_sec}
    except Exception:
        pass
    return {"contention": False, "idle_sec": 999.0}


def is_secure_desktop() -> bool:
    """Detect if system switched to Secure Desktop (UAC elevation prompt / lock screen)."""
    try:
        import ctypes
        DESKTOP_SWITCHDESKTOP = 0x0100
        h_desk = ctypes.windll.user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if h_desk:
            ctypes.windll.user32.CloseDesktop(h_desk)
            return False
        err = ctypes.GetLastError()
        return err == 5  # ERROR_ACCESS_DENIED: Secure Desktop active
    except Exception:
        return False


def _check_safe_desktop_environment(tool_name: str) -> Message | None:
    if is_secure_desktop():
        return Message(
            role="tool", name=tool_name,
            content=("[桌面安全拦截] 检测到系统处于安全桌面 (UAC 凭据请求 / 锁屏 / 安全弹窗)，"
                     "AlterEgo 权限不足无法直接穿透；请在屏幕上手动授权后回复“继续”。"),
            metadata={"tool_control": {"retryable": False, "requires_user": True, "reason": "secure_desktop"}},
        )
    for _ in range(2):
        contention = check_focus_contention(2.0)
        if not contention["contention"]:
            break
        time.sleep(1.0)
    else:
        idle = contention.get("idle_sec", 0.0)
        return Message(
            role="tool", name=tool_name,
            content=(f"[桌面已让出] 检测到物理用户正在操作鼠标或键盘（空闲时间仅 {idle:.1f}s < 2.0s），"
                     "自动化已暂停以防冲突；请在停止操作后回复“继续”。"),
            metadata={"tool_control": {"retryable": False, "requires_user": True, "reason": "focus_contention"}},
        )
    return None


_IRREVERSIBLE_ACTION_PATTERN = re.compile(
    r"(?:彻底删除|永久删除|格式化|清空全部|注销账号|抹掉数据|wipe\s*data|format\s*disk|delete\s*account)",
    re.I
)


def _get_process_id_from_hwnd(hwnd: int) -> int:
    try:
        import ctypes
        from ctypes import wintypes
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def classify_app(pid: int, hwnd: int | None = None) -> dict:
    """Classify the UI framework and accessibility architecture of the target application."""
    framework = "unknown"
    proc_name = ""
    win_class = ""
    notes = []
    
    try:
        import psutil
        proc = psutil.Process(pid)
        proc_name = proc.name().lower()
    except Exception:
        try:
            output = subprocess.check_output(f'tasklist /fi "PID eq {pid}" /fo csv /nh', shell=True, text=True)
            if output and '"' in output:
                proc_name = output.split('","')[0].strip('"').lower()
        except Exception:
            pass

    if hwnd:
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
            win_class = buf.value
        except Exception:
            pass

    if proc_name in ("chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe"):
        framework = "chromium"
        notes.append("检测到 Chromium 内核浏览器；如需高精度的页面自动化，强烈建议使用 browser_* 系列工具。")
    elif proc_name == "firefox.exe":
        framework = "gecko"
        notes.append("检测到 Firefox 浏览器；如可连 CDP 建议使用 browser 工具。")
    elif win_class == "ApplicationFrameWindow" or "Windows.UI" in win_class:
        framework = "uwp"
        notes.append("检测到 Windows UWP 容器窗口 (ApplicationFrameWindow)；实际控件位于 CoreWindow 子层级。")
    elif "HwndWrapper" in win_class or "WPF" in win_class:
        framework = "wpf"
        notes.append("检测到 WPF 应用；UIA 支持通常完备，直接优先使用 UIA Pattern。")
    elif "Qt" in win_class or "QWidget" in win_class:
        framework = "qt"
        notes.append("检测到 Qt 应用；若 Qt 未开启 accessibility，部分控件可能无法读取。")
    elif "SunAwt" in win_class:
        framework = "java_swing"
        notes.append("检测到 Java AWT/Swing 应用；需依赖 Java Access Bridge。")
    elif win_class in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
        framework = "terminal"
        notes.append("检测到 Windows 终端/控制台窗口；建议配合 CLI 或终端自动化处理。")
    elif win_class in ("Notepad", "Edit", "Button", "#32770"):
        framework = "win32_standard"
        notes.append("检测到标准 Win32 控件窗口。")
    else:
        framework = "custom_or_win32"

    return {
        "pid": pid,
        "hwnd": hwnd,
        "process_name": proc_name,
        "window_class": win_class,
        "framework": framework,
        "recommendations": notes,
    }


def _is_failsafe_exception(exc):
    exception_type = getattr(pyautogui, "FailSafeException", None)
    return ((isinstance(exception_type, type) and isinstance(exc, exception_type))
            or (type(exc).__name__ == "FailSafeException"
                and (type(exc).__module__ == "pyautogui" or type(exc).__module__.startswith("pyautogui."))))


def _desktop_stop_result(tool_name, exc):
    if not isinstance(exc, DesktopHalted) and not _is_failsafe_exception(exc):
        return None
    state = current_state()
    reason = exc.reason if isinstance(exc, DesktopHalted) else "desktop_failsafe"
    state.halt(reason)
    return Message(
        role="tool", name=tool_name,
        content=("[桌面已暂停] 已触发鼠标屏幕四角停止保护，操作已停止，先前效果未验证。"
                 "请用户将鼠标移离屏幕四角后回复“继续”；恢复后先获取新截图检查实际结果，避免重复输入或提交。"),
        metadata={"tool_control": {"retryable": False, "requires_user": True, "reason": reason}},
    )


def _native_window_info(hwnd):
    """Read only the exact target's native style, state and monitor work area."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    if not user32.IsWindow(hwnd):
        raise EvidenceError("目标窗口已经关闭。")
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsZoomed.argtypes, user32.IsZoomed.restype = [wintypes.HWND], wintypes.BOOL
    user32.IsIconic.argtypes, user32.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    rect, monitor = wintypes.RECT(), MonitorInfo()
    monitor.cbSize = ctypes.sizeof(monitor)
    if (not user32.GetWindowRect(hwnd, ctypes.byref(rect))
            or not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(monitor))):
        raise ctypes.WinError()
    rectangle = lambda value: {key: int(getattr(value, key)) for key in ("left", "top", "right", "bottom")}
    style = user32.GetWindowLongW(hwnd, -16)
    return {"hwnd": hwnd, "rect": rectangle(rect), "work_area": rectangle(monitor.rcWork),
            "maximizable": bool(style & 0x00010000), "maximized": bool(user32.IsZoomed(hwnd)),
            "minimized": bool(user32.IsIconic(hwnd)),
            "foreground": int(user32.GetForegroundWindow() or 0) == hwnd}


def _show_target_window(hwnd, command):
    current_state().require_not_halted()
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.ShowWindow(hwnd, command)


def _maximize_target_window(window_title="", hwnd=None):
    """Prepare one explicit window before collecting controls or screenshot pixels."""
    current_state().require_not_halted()
    if not window_title and not hwnd:
        return {"status": "untargeted", "geometry_verified": False, "changed": False}
    changed = False
    native_checked = False
    try:
        import pygetwindow as gw
        matches = [window for window in gw.getAllWindows()
                   if ((hwnd and int(getattr(window, "_hWnd", 0)) == hwnd)
                       or (not hwnd and (window.title or "").casefold() == window_title.casefold()))]
        if len(matches) != 1:
            return {"status": "unresolved", "geometry_verified": False, "changed": False}
        target = matches[0]
        target_hwnd = int(getattr(target, "_hWnd", 0))
        if target_hwnd <= 0:
            return {"status": "unresolved", "geometry_verified": False, "changed": False}
        before = _native_window_info(target_hwnd)
        native_checked = True
        if not before.get("foreground"):
            target.activate()
        if not before.get("maximizable"):
            if before.get("minimized"):
                changed = True
                _show_target_window(target_hwnd, 9)
            expected_status = "fixed_size"
        else:
            expected_status = "maximized"
            if not maximized_geometry_verified(before):
                changed = True
                _show_target_window(target_hwnd, 3)
        previous_rect, stable = None, 0
        for _ in range(12):
            after = _native_window_info(target_hwnd)
            rect = after.get("rect")
            ready = (valid_window_rect(rect) and not after.get("minimized") and after.get("foreground") is True
                     and (expected_status == "fixed_size" or maximized_geometry_verified(after)))
            stable = stable + 1 if ready and rect == previous_rect else 0
            if stable >= 1:
                return {"status": expected_status, "geometry_verified": True, "hwnd": target_hwnd,
                        "rect": dict(rect), "changed": changed or rect != before.get("rect")}
            previous_rect = rect
            time.sleep(.05)
        return {"status": "maximize_unverified", "geometry_verified": False,
                "hwnd": target_hwnd, "changed": changed}
    except Exception as exc:
        if isinstance(exc, DesktopHalted) or _is_failsafe_exception(exc):
            raise
        return {"status": "maximize_unverified" if native_checked else "unavailable",
                "geometry_verified": False, "changed": changed, "error_type": type(exc).__name__}


def _controls_for_model(controls, limit=64, *, screen_size=None, image_size=None):
    """Keep full records in DesktopState; send a bounded actionable projection with ref handles."""
    defaults = {"enabled": True, "focusable": False, "is_password": False, "has_keyboard_focus": False}
    editable = {"Edit", "ComboBox", "TextBox"}
    buttons = {"Button", "CheckBox", "RadioButton", "TabItem", "MenuItem", "Hyperlink"}
    primary_action = re.compile(r"^(?:发送|提交|生成|继续|登录|下载|保存|搜索|确认|完成|"
                                r"send\b|submit\b|generate\b|continue\b|login\b|download\b|save\b|search\b|confirm\b|done\b)", re.I)
    def priority(control):
        if control.get("has_keyboard_focus"):
            return 0
        if not valid_window_rect(control.get("rect")):
            return 7
        if control.get("enabled") and (control.get("control_type") in editable or control.get("editable")):
            return 1
        if control.get("enabled") and control.get("control_type") in buttons:
            name = str(control.get("name") or "").strip()
            return 2 if primary_action.search(name) else (3 if name else 4)
        if control.get("enabled") and control.get("focusable"):
            return 5
        return 6
    selected = sorted((control for control in controls if isinstance(control, dict)), key=priority)[:limit]
    result = []
    required = {"control_ref", "control_type", "name", "rect", "patterns", "editable",
                "value_supported", "is_read_only", "automation_id", "ref"}
    for idx, control in enumerate(selected, 1):
        ref_id = f"c{idx}"
        control["ref"] = ref_id
        record = {key: value for key, value in control.items()
                  if key in required or (key in defaults and value != defaults[key])}
        record["ref"] = ref_id
        # Missing flags retain unknown/unsafe meaning rather than inheriting safe defaults.
        for key in defaults:
            if key not in control:
                record[key] = None
        if screen_size is not None and image_size is not None:
            record["image_rect"] = screen_rect_to_image(control.get("rect"), screen_size, image_size)
        result.append(record)
    return result, defaults


def _layout_note(layout):
    if layout.get("status") == "maximized" and layout.get("geometry_verified"):
        return "；目标窗口已最大化并验证"
    if layout.get("status") == "fixed_size":
        return "；固定尺寸窗口已保留"
    return "；最大化尚未验证，请观察目标窗口"


def _probe(window_title="", hwnd=None):
    from .desktop_accessibility import probe_window
    return probe_window(window_title=window_title, hwnd=hwnd, max_controls=200)


def _read_focus(hwnd=None):
    from .desktop_accessibility import read_focus
    return read_focus(hwnd=hwnd)


def _restore_window(hwnd):
    """Restore the exact approved target after the approval UI took focus."""
    current_state().require_not_halted()
    try:
        import pygetwindow as gw
        matches = [window for window in gw.getAllWindows() if int(getattr(window, "_hWnd", 0)) == hwnd]
        if len(matches) != 1:
            return False
        if getattr(matches[0], "isMinimized", False):
            matches[0].restore()
        matches[0].activate()
        time.sleep(.15)
        return True
    except Exception as exc:
        if isinstance(exc, DesktopHalted) or _is_failsafe_exception(exc):
            raise
        return False


def _live_window():
    current_state().require_not_halted()
    shot = current_state().snapshot
    target = shot.get("window") or {}
    if shot and time.monotonic() - shot["captured_at"] > current_state().max_age_sec:
        current_state().invalidate()
        raise EvidenceError("截图已过期，请重新观察目标窗口。")
    if not target.get("hwnd"):
        raise EvidenceError("没有可验证的目标窗口，请 desktop_snapshot(window_title=精确窗口名)。")
    result = _probe(hwnd=target["hwnd"])
    window = result.get("window") or {}
    if not result.get("ok") or window.get("hwnd") != target["hwnd"]:
        raise EvidenceError("目标窗口不可读取，禁止向其他窗口发送操作。")
    if window.get("rect") != target.get("rect") or window.get("title") != target.get("title"):
        current_state().invalidate()
        raise EvidenceError("窗口位置或尺寸已变化，请重新截图定位。")
    if not window.get("foreground"):
        if not _restore_window(target["hwnd"]):
            raise EvidenceError("无法恢复已授权目标窗口的前台状态，禁止输入。")
        result = _probe(hwnd=target["hwnd"])
        window = result.get("window") or {}
        if (not result.get("ok") or not window.get("foreground")
                or window.get("hwnd") != target["hwnd"]
                or window.get("rect") != target.get("rect") or window.get("title") != target.get("title")):
            raise EvidenceError("目标窗口前台或几何状态验证失败，请重新观察。")
    return window


def _capture(args, *, preserve_input=False, prepare_window=True):
    state = current_state()
    state.require_not_halted()
    from PIL import Image
    out_dir = Path(args.get("out_dir", "."))
    out_dir.mkdir(parents=True, exist_ok=True)
    if pyautogui is None:
        raise RuntimeError("pyautogui unavailable")
    state = current_state()
    previous = state.snapshot.get("window") or {}
    requested_title = args.get("window_title", "")
    bound_hwnd = previous.get("hwnd") if not requested_title else None
    layout = (_maximize_target_window(window_title=requested_title, hwnd=bound_hwnd)
              if prepare_window else {"status": "preserved_after_action", "geometry_verified": False, "changed": False})
    if layout.get("changed"):
        state.invalidate()
        preserve_input = False
    if layout.get("status") == "maximize_unverified" or layout.get("changed") and not layout.get("geometry_verified"):
        raise EvidenceError("窗口最大化后的几何状态未稳定，请重新观察目标窗口。")
    probe = _probe(window_title=requested_title, hwnd=bound_hwnd)
    window = probe.get("window") or {}
    bound_hwnd = previous.get("hwnd") if not requested_title else None
    if (requested_title or bound_hwnd) and (
            not probe.get("ok") or not window.get("hwnd")
            or (bound_hwnd and window.get("hwnd") != bound_hwnd)):
        state.invalidate()
        raise EvidenceError("目标窗口未唯一定位：" + str(probe.get("error_type", "window_mismatch")))
    if layout.get("geometry_verified") and (
            window.get("hwnd") != layout.get("hwnd")
            or not window_rect_matches(window.get("rect"), layout.get("rect"), tolerance=16)):
        state.invalidate()
        raise EvidenceError("窗口最大化后的 UIA 几何不匹配，请重新观察。")
    shot = pyautogui.screenshot()
    if layout.get("geometry_verified"):
        current = _native_window_info(layout["hwnd"])
        if not window_rect_matches(current.get("rect"), layout.get("rect")):
            state.invalidate()
            raise EvidenceError("截图采集期间目标窗口尺寸变化，请重新观察。")
    w, h = shot.size
    scale = min(1.0, 1568 / max(w, h))
    if scale < 1:
        shot = shot.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out_file = out_dir / f"desktop_shot_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
    shot.convert("RGB").save(out_file, "JPEG", quality=80)
    if not preserve_input and state.focus and window.get("hwnd") == previous.get("hwnd"):
        try:
            state.require_focus(_read_focus(hwnd=window["hwnd"]))
            preserve_input = True
        except EvidenceError:
            pass
    controls = probe.get("controls") or []
    projected, control_defaults = _controls_for_model(controls, screen_size=(w, h), image_size=shot.size)
    ref_map = {c["ref"]: c for c in projected if "ref" in c}
    evidence = state.observe(window, (w, h), shot.size, controls,
                             preserve_input=preserve_input, ref_map=ref_map)
    details = {
        "snapshot_id": evidence["snapshot_id"], "window": window,
        "controls": projected, "control_defaults": control_defaults,
        "coordinate_spaces": {"rect": "screen", "image_rect": "image", "click_x_y": "image"},
        "screen_size": [w, h], "image_size": list(shot.size),
        "image_coordinate_range": {"x": [0, shot.size[0] - 1], "y": [0, shot.size[1] - 1]},
        "window_image_rect": screen_rect_to_image(window.get("rect"), (w, h), shot.size),
        "controls_total": len(controls), "controls_shown": len(projected),
        "uia_supported": probe.get("supported", False), "window_layout": layout,
        "uia_error": probe.get("error_type"), "truncated": probe.get("truncated", False) or len(projected) < len(controls),
    }
    return Message(
        role="tool", name="desktop_screenshot",
        content=(f"[桌面截图] 已保存至: {out_file.resolve()}（截图尺寸: {shot.size[0]}x{shot.size[1]}，"
                 f"原屏尺寸: {w}x{h}，缩放比例 scale: {scale:.4f}；legacy scale={scale:.4f}）\n"
                 "必须使用下面的 snapshot_id；优先使用短句柄 ref（如 c1..c64）或 control_ref，也可使用截图像素 x/y。"
                 "UIA rect 是 screen 空间，image_rect 已按实际宽高分别换算为 image 空间，不能混用。\n"
                 + ("未指定目标窗口，本次未请求最大化；请指定 window_title 重新观察目标后再操作。\n"
                    if layout.get("status") == "untargeted" else "")
                 + json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
        images=[ImagePart(data=out_file.read_bytes(), mime="image/jpeg", mime_type="image/jpeg")],
        metadata={"desktop_observation": details},
    )


def desktop_screenshot(args: dict) -> Message | str:
    try:
        return _capture(args)
    except Exception as exc:
        stopped = _desktop_stop_result("desktop_screenshot", exc)
        if stopped is not None:
            return stopped
        detail = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
        if "screen grab failed" in str(exc).lower():
            detail = "当前运行环境缺少交互式桌面显示"
        return "[桌面截图失败] " + detail


def desktop_snapshot(args: dict) -> Message | str:
    return desktop_screenshot(args)


def _post_action(label, verification, *, preserve_input=False):
    state = current_state()
    tool_name = "desktop_" + verification.get("action", "inspect_focus")
    try:
        # Initial observations prepare/maximize a target. Post-action observations
        # must never move it after an input/focus result has already been verified.
        result = _capture({}, preserve_input=preserve_input, prepare_window=False)
        verification = dict(verification)
        if "focus_verified" in verification:
            verification["focus_verified"] = bool(verification["focus_verified"] and state.focus)
            verification["input_allowed"] = verification["focus_verified"]
            reason = state.focus_verification_reason
            verification["verification_reason"] = (reason if state.focus or reason not in {"unverified", "verified_successful_click"}
                                                       else "final_focus_not_verified")
            if not verification["focus_verified"]:
                label += ("；读到editable/keyboard_focus只是观察值，未关联已成功点击的目标，input_allowed=false；"
                          "需新截图和成功点击后再输入。")
        if verification.get("input_verified") is True:
            valid_input = bool(state.focus and state.typed_hash and state.typed_control
                               and state.typed_control == state.focus.get("control_ref")
                               and state.typed_hash == verification.get("text_sha256"))
            verification["input_verified"] = valid_input
            verification["input_allowed"] = valid_input
            if not valid_input:
                verification["verification_reason"] = "final_input_state_invalidated"
                label = "[桌面输入验证失败] 操作后焦点或窗口状态已变化，输入验证不可复用；需重新截图检查实际输入。"
        result.content = label + "\n" + json.dumps(verification, ensure_ascii=False) + "\n" + result.content
        result.metadata["desktop_verification"] = verification
        return result
    except Exception as exc:
        stopped = _desktop_stop_result(tool_name, exc)
        if stopped is not None:
            return stopped
        state.invalidate()
        return "[桌面验证失败] 动作可能已发送，但无法取得操作后证据；" + type(exc).__name__


def desktop_inspect_focus(args: dict) -> Message | str:
    try:
        state = current_state()
        _live_window()
        focus = _read_focus(hwnd=state.snapshot["window"]["hwnd"])
        verified = state.set_focus(focus)
        return _post_action("[桌面焦点检查]" if verified else "[桌面焦点验证失败]",
                            {"focus_verified": verified, "focus": focus.get("focus") or {},
                             "error_type": focus.get("error_type"),
                             "verification_reason": state.focus_verification_reason,
                             "input_allowed": verified}, preserve_input=verified)
    except Exception as exc:
        stopped = _desktop_stop_result("desktop_inspect_focus", exc)
        if stopped is not None:
            return stopped
        return "[桌面焦点验证失败] " + (str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)


def desktop_list_windows(args: dict) -> str:
    """枚举当前桌面可见窗口标题及进程。"""
    try:
        import pygetwindow as gw
        windows = gw.getAllWindows()
        lines = []
        for w in windows:
            title = (w.title or "").strip()
            if title and w.visible:
                lines.append(f"- \"{title}\" (位置: {w.left},{w.top} 尺寸: {w.width}x{w.height})")
        if not lines:
            return "[桌面窗口] 当前未检测到可见窗口"
        return "[当前桌面窗口列表]\n" + "\n".join(lines[:30])
    except Exception as e:
        return f"[枚举窗口失败] {e}"


def find_desktop_items() -> list[dict]:
    """搜索 Windows 当前用户桌面、公共桌面和开始菜单的全部项目（包含文件夹、快捷方式、常规文件）。"""
    desktop_dirs = [
        Path.home() / "Desktop",
        Path(os.environ.get("PUBLIC", "C:\\Users\\Public")) / "Desktop",
        Path.home() / "OneDrive" / "Desktop",
    ]
    start_menu_dirs = [
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]

    results = []
    seen = set()

    # 1. 扫描桌面主目录下的所有文件夹、文件和快捷方式
    for d in desktop_dirs:
        if not d.exists():
            continue
        try:
            for item in d.iterdir():
                name = item.name
                if name.lower() in ("desktop.ini", "thumbs.db"):
                    continue
                if item.is_dir():
                    key = ("folder", name.lower())
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "name": name,
                            "type": "folder",
                            "path": item,
                            "location": "desktop",
                        })
                elif item.suffix.lower() == ".lnk":
                    stem = item.stem
                    key = ("shortcut", stem.lower())
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "name": stem,
                            "type": "shortcut",
                            "path": item,
                            "location": "desktop",
                        })
                else:
                    key = ("file", name.lower())
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "name": name,
                            "type": "file",
                            "path": item,
                            "location": "desktop",
                        })
        except Exception:
            pass

    # 2. 扫描开始菜单快捷方式作为补充
    for d in start_menu_dirs:
        if not d.exists():
            continue
        try:
            for p in d.rglob("*.lnk"):
                stem = p.stem
                key = ("shortcut", stem.lower())
                if key not in seen:
                    seen.add(key)
                    results.append({
                        "name": stem,
                        "type": "start_menu_shortcut",
                        "path": p,
                        "location": "start_menu",
                    })
        except Exception:
            pass

    return results


def find_desktop_shortcuts() -> list[tuple[str, Path]]:
    """搜索 Windows 当前用户桌面、公共桌面和开始菜单的全部快捷方式（向后兼容）。"""
    items = find_desktop_items()
    return [(it["name"], it["path"]) for it in items if it["type"] in ("shortcut", "start_menu_shortcut")]


def desktop_show_desktop(args: dict = None) -> Message | str:
    """最小化所有前台窗口，切回并显示 Windows 桌面（等同于按下 Win+D 快捷键）。"""
    try:
        if pyautogui is None:
            return "[显示桌面失败] 当前运行环境未安装 pyautogui"
        current_state().require_not_halted()
        current_state().invalidate()
        pyautogui.hotkey("win", "d")
        time.sleep(0.5)
        return "[已显示桌面] 所有前台窗口已最小化，已成功切回 Windows 桌面"
    except Exception as e:
        stopped = _desktop_stop_result("desktop_show_desktop", e)
        if stopped is not None:
            return stopped
        return f"[显示桌面失败] {e}"


def desktop_list_items(args: dict = None) -> str:
    """获取当前 Windows 桌面上真实存在的所有项目列表（包含全部文件夹、文件、应用快捷方式及路径）。可用于在思考和决策前感知桌面真实内容。"""
    try:
        items = find_desktop_items()
        desktop_items = [it for it in items if it.get("location") == "desktop"]
        if not desktop_items:
            return "[桌面内容] 未在桌面目录中检测到任何文件或文件夹"
        type_labels = {
            "folder": "文件夹",
            "shortcut": "快捷方式",
            "file": "文件",
        }
        lines = []
        for it in desktop_items[:50]:
            label = type_labels.get(it["type"], "项目")
            lines.append(f"- [{label}] \"{it['name']}\" (路径: {it['path']})")
        return f"[当前桌面内容列表（共 {len(desktop_items)} 项）]\n" + "\n".join(lines)
    except Exception as e:
        return f"[枚举桌面内容失败] {e}"


def desktop_list_shortcuts(args: dict = None) -> str:
    """列出当前 Windows 桌面上所有已存在的文件夹、文件与快捷方式（兼容并委托 desktop_list_items）。"""
    return desktop_list_items(args)


def desktop_open_app(args: dict) -> Message | str:
    """启动或打开桌面上的应用程序、文件夹或文件（支持文件夹名如 ffmeng、软件名如 Clash Verge、快捷方式或绝对路径）。优先智能搜索桌面项目并原生调起。"""
    target = (args.get("app_name") or args.get("target") or args.get("name") or "").strip()
    if not target:
        return "[打开失败] 未指定 app_name 或 target"
    try:
        current_state().require_not_halted()
        current_state().invalidate()
        # 1. 尝试先查找并激活已打开的窗口（如资源管理器已打开该文件夹，或软件窗口已存在）
        import pygetwindow as gw
        clean_target = target.lower().replace(".exe", "").replace(".lnk", "")
        matches = [w for w in gw.getAllWindows() if clean_target in (w.title or "").lower()]
        exact = [w for w in matches if (w.title or "").lower() == clean_target]
        matched = exact or matches
        if len(matched) > 1:
            return "[打开失败] 匹配多个已打开窗口，请提供目标窗口精确标题。"
        if matched:
            w = matched[0]
            try:
                w.activate()
                layout = _maximize_target_window(hwnd=int(getattr(w, "_hWnd", 0)))
                return f'[已激活已打开窗口] "{w.title}"' + _layout_note(layout)
            except Exception as exc:
                if isinstance(exc, DesktopHalted) or _is_failsafe_exception(exc):
                    raise
                pass

        # 2. 智能搜索桌面与开始菜单全部项目（包括文件夹、快捷方式、常规文件）
        items = find_desktop_items()
        for it in items:
            name = it["name"]
            name_lower = name.lower().replace(".exe", "").replace(".lnk", "")
            if clean_target == name_lower or clean_target in name_lower or name_lower in clean_target:
                p = it["path"]
                try:
                    os.startfile(str(p), show_cmd=3)
                    time.sleep(1.2)
                    kind_map = {
                        "folder": "文件夹",
                        "shortcut": "快捷方式",
                        "file": "本地文件",
                        "start_menu_shortcut": "开始菜单程序",
                    }
                    kind_cn = kind_map.get(it["type"], "项目")
                    return f"[已成功打开] 找到桌面{kind_cn} \"{name}\"，已调起打开: {p}；已请求最大化，截图前将验证"
                except Exception as ex:
                    if isinstance(ex, DesktopHalted) or _is_failsafe_exception(ex):
                        raise
                    logger.warning(f"打开项目 {p} 异常: {ex}")

        # 3. 如果传入的是真实文件或文件夹绝对/相对路径
        if os.path.exists(target):
            try:
                os.startfile(target, show_cmd=3)
                time.sleep(1.2)
                return f"[已成功打开] 已打开本地路径: {target}；已请求最大化，截图前将验证"
            except Exception as exc:
                if isinstance(exc, DesktopHalted) or _is_failsafe_exception(exc):
                    raise
                pass

        # 4. 否则通过 Windows cmd 的 start 启动（加空标题防空格截断）
        cmd = f'start "" /max "{target}"'
        subprocess.Popen(cmd, shell=True)
        time.sleep(1.0)
        return f"[已通过系统命令启动] {target}；已请求最大化，截图前将验证"
    except Exception as e:
        stopped = _desktop_stop_result("desktop_open_app", e)
        if stopped is not None:
            return stopped
        return f"[打开失败] {target}: {e}"


def desktop_click(args: dict) -> Message | str:
    try:
        if pyautogui is None:
            raise RuntimeError("pyautogui unavailable")
        state = current_state()
        state.require_not_halted()
        
        # 1. Activate window by title only
        ref = args.get("ref") or args.get("control_ref")
        if args.get("window_title") and args.get("x") is None and not ref:
            import pygetwindow as gw
            matches = [window for window in gw.getAllWindows()
                       if (window.title or "").casefold() == args["window_title"].casefold()]
            if len(matches) != 1:
                raise EvidenceError("窗口不存在或名称歧义，不能声称已经激活。")
            matches[0].activate()
            state.invalidate()
            result = desktop_screenshot({"window_title": args["window_title"]})
            if isinstance(result, Message) and result.metadata["desktop_observation"]["window"].get("foreground"):
                result.content = "[桌面已激活并验证]\n" + result.content
                return result
            return "[桌面激活验证失败] 目标窗口没有成为前台。"

        # 2. Safety & Governance check: UAC / Secure Desktop / Physical focus contention
        env_guard = _check_safe_desktop_environment("desktop_click")
        if env_guard is not None:
            return env_guard

        # 3. Resolve target control and coordinates
        target_ctrl = state.resolve_control(ref) if ref else None
        control_name = (target_ctrl.get("name") if target_ctrl else "") or ""

        # Check irreversible action protection
        if control_name and _IRREVERSIBLE_ACTION_PATTERN.search(control_name) and not args.get("confirmed"):
            return Message(
                role="tool", name="desktop_click",
                content=f"[桌面高风险操作拦截] 目标控件 \"{control_name}\" 涉及可能不可逆的高风险操作（删除/支付/格式化）。若确认执行，请传入 confirmed=True 或由用户确认。",
                metadata={"tool_control": {"requires_user": True, "reason": "irreversible_action"}},
            )

        x, y = state.click_point(args)
        _live_window()

        # 4. Resolve click parameters
        click_type = args.get("click_type") or ("double_click" if args.get("double_click") or args.get("double") else "click")
        clicks = int(args.get("clicks", 2 if click_type == "double_click" else 1))
        if clicks not in (1, 2):
            raise EvidenceError("clicks 只能为 1 或 2。")
        button = args.get("button", "right" if click_type == "right_click" else "left")
        if button not in ("left", "right", "middle"):
            raise EvidenceError("不支持此鼠标按钮。")

        # 5. UIA Pattern direct invocation check
        pattern_used = None
        if clicks == 1 and button == "left" and click_type == "click" and target_ctrl:
            target_patterns = target_ctrl.get("patterns") or []
            target_ref = target_ctrl.get("control_ref") or ref
            target_hwnd = state.snapshot.get("window", {}).get("hwnd")

            pat_to_try = None
            if "Invoke" in target_patterns:
                pat_to_try = "Invoke"
            elif "Toggle" in target_patterns:
                pat_to_try = "Toggle"
            elif "SelectionItem" in target_patterns:
                pat_to_try = "SelectionItem"
            elif "ExpandCollapse" in target_patterns:
                pat_to_try = "ExpandCollapse"

            if pat_to_try and target_hwnd and target_ref:
                from .desktop_accessibility import invoke_pattern
                inv_res = invoke_pattern(target_hwnd, target_ref, pat_to_try)
                if inv_res.get("ok"):
                    pattern_used = pat_to_try

        state.clear_interaction()

        if pattern_used:
            label = f"[桌面Pattern直调成功: {pattern_used}] 无需移动物理光标"
        else:
            if click_type == "long_press":
                duration = float(args.get("duration", 1.0))
                pyautogui.moveTo(x, y)
                pyautogui.mouseDown(button=button)
                time.sleep(duration)
                pyautogui.mouseUp(button=button)
                label = f"[桌面长按已发送 {duration}s]"
            elif clicks == 2:
                (pyautogui.doubleClick)(x, y, **({"button": button} if button != "left" else {}))
                label = "[桌面双击已发送]"
            else:
                (pyautogui.click)(x, y, **({"button": button} if button != "left" else {}))
                label = "[桌面点击已发送]" if button == "left" else "[桌面右击已发送]"

        expected_ref = target_ctrl.get("control_ref") if target_ctrl else (ref or "")
        state.mark_click((x, y), expected_ref)
        time.sleep(.15)

        focus_result = _read_focus(hwnd=state.snapshot["window"]["hwnd"])
        focus_verified = state.set_focus(focus_result)
        post_label = label + f" 物理屏幕坐标: ({x}, {y})；点击本身不代表目标已完成。"
        return _post_action(post_label,
                            {"action": "click", "focus_verified": focus_verified,
                             "pattern_used": pattern_used,
                             "focus": focus_result.get("focus") or {}}, preserve_input=focus_verified)
    except Exception as exc:
        stopped = _desktop_stop_result("desktop_click", exc)
        if stopped is not None:
            return stopped
        current_state().clear_interaction()
        return "[桌面点击失败] " + (str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)


def _clipboard_readback():
    state = current_state()
    state.require_not_halted()
    original = pyperclip.paste()
    failed = False
    try:
        pyperclip.copy("desktop-readback-" + uuid.uuid4().hex)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.hotkey("ctrl", "c")
        time.sleep(.15)
        return pyperclip.paste()
    except Exception as exc:
        failed = True
        if isinstance(exc, DesktopHalted) or _is_failsafe_exception(exc):
            state.halt("desktop_failsafe")
            raise DesktopHalted("desktop_failsafe") from None
        raise
    finally:
        try:
            pyperclip.copy(original)
        except Exception as exc:
            if _is_failsafe_exception(exc):
                state.halt("desktop_failsafe")
                raise DesktopHalted("desktop_failsafe") from None
            if failed and state.halted:
                # Preserve the trusted stop when restoring the clipboard also fails.
                logger.warning("clipboard_restore_failed_after_desktop_stop")
            else:
                raise


def desktop_type(args: dict) -> Message | str:
    state = current_state()
    old_clipboard = None
    try:
        state.require_not_halted()
        env_guard = _check_safe_desktop_environment("desktop_type")
        if env_guard is not None:
            return env_guard

        press_enter = args.get("press_enter", False)
        if not isinstance(press_enter, bool):
            raise EvidenceError("press_enter 必须为布尔值 true 或 false；未输入或提交。")
        if pyautogui is None or pyperclip is None:
            raise RuntimeError("desktop input dependencies unavailable")
        _live_window()
        target_hwnd = state.snapshot["window"]["hwnd"]
        before = _read_focus(hwnd=target_hwnd)
        focus_before = state.require_focus(before)
        text = str(args.get("text", ""))
        if not text:
            raise EvidenceError("text 不能为空。")

        val_written = False
        control_ref = focus_before.get("control_ref", "")
        if focus_before.get("value_supported") and control_ref:
            from .desktop_accessibility import invoke_pattern
            inv_res = invoke_pattern(target_hwnd, control_ref, "Value", text)
            if inv_res.get("ok"):
                val_written = True

        if not val_written:
            old_clipboard = pyperclip.paste()
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.hotkey("ctrl", "v")
            time.sleep(.2)

        after = _read_focus(hwnd=target_hwnd)
        focus = state.require_focus(after)
        if focus.get("value_supported"):
            actual = focus.get("value", "")
        else:
            actual = _clipboard_readback()
        state.verify_text(text, actual, focus)
        label = "[桌面输入已验证] (ValuePattern直写) 已确认目标输入文字。" if val_written else "[桌面输入已验证] 已读回并确认目标输入文字。"
        result = _post_action(label,
                              {"action": "type", "input_verified": True, "characters": len(text),
                               "value_pattern_used": val_written,
                               "text_sha256": state.typed_hash}, preserve_input=True)
        if press_enter is True:
            if (not isinstance(result, Message)
                    or result.metadata.get("tool_control", {}).get("requires_user")
                    or not result.metadata.get("desktop_verification", {}).get("input_verified")):
                return result
            return desktop_press({"key": "enter"})
        return result
    except Exception as exc:
        stopped = _desktop_stop_result("desktop_type", exc)
        if stopped is not None:
            return stopped
        state.typed_hash = ""
        return "[桌面输入验证失败] " + (str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)
    finally:
        if old_clipboard is not None:
            try:
                pyperclip.copy(old_clipboard)
            except Exception as exc:
                stopped = _desktop_stop_result("desktop_type", exc)
                if stopped is not None:
                    return stopped
                logger.warning("clipboard_restore_failed")


def desktop_hover(args: dict) -> Message | str:
    """Hover over a desktop control or coordinate without clicking."""
    try:
        if pyautogui is None:
            raise RuntimeError("pyautogui unavailable")
        state = current_state()
        state.require_not_halted()
        env_guard = _check_safe_desktop_environment("desktop_hover")
        if env_guard is not None:
            return env_guard
        x, y = state.click_point(args)
        _live_window()
        duration = float(args.get("duration", 0.2))
        pyautogui.moveTo(x, y, duration=duration)
        return _post_action(f"[桌面悬停已完成] 物理屏幕坐标: ({x}, {y})",
                            {"action": "hover", "x": x, "y": y}, preserve_input=bool(state.focus))
    except Exception as exc:
        stopped = _desktop_stop_result("desktop_hover", exc)
        if stopped is not None:
            return stopped
        return "[桌面悬停失败] " + (str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)


def desktop_drag(args: dict) -> Message | str:
    """Drag from start point to end point on the desktop."""
    try:
        if pyautogui is None:
            raise RuntimeError("pyautogui unavailable")
        state = current_state()
        state.require_not_halted()
        env_guard = _check_safe_desktop_environment("desktop_drag")
        if env_guard is not None:
            return env_guard
        _live_window()
        
        # Start point
        start_args = {"snapshot_id": args.get("snapshot_id")}
        if args.get("start_ref"):
            start_args["ref"] = args["start_ref"]
        elif args.get("start_x") is not None and args.get("start_y") is not None:
            start_args["x"] = args["start_x"]
            start_args["y"] = args["start_y"]
        elif args.get("ref"):
            start_args["ref"] = args["ref"]
        elif args.get("x") is not None and args.get("y") is not None:
            start_args["x"] = args["x"]
            start_args["y"] = args["y"]
        else:
            raise EvidenceError("缺少起点坐标或 start_ref。")
        sx, sy = state.click_point(start_args)

        # End point
        end_args = {"snapshot_id": args.get("snapshot_id")}
        if args.get("end_ref"):
            end_args["ref"] = args["end_ref"]
        elif args.get("end_x") is not None and args.get("end_y") is not None:
            end_args["x"] = args["end_x"]
            end_args["y"] = args["end_y"]
        elif args.get("to_x") is not None and args.get("to_y") is not None:
            end_args["x"] = args["to_x"]
            end_args["y"] = args["to_y"]
        else:
            raise EvidenceError("缺少终点坐标或 end_ref。")
        ex, ey = state.click_point(end_args)

        duration = float(args.get("duration", 0.5))
        button = args.get("button", "left")
        
        pyautogui.moveTo(sx, sy)
        time.sleep(0.05)
        pyautogui.mouseDown(button=button)
        pyautogui.moveTo(ex, ey, duration=duration)
        time.sleep(0.05)
        pyautogui.mouseUp(button=button)
        
        state.clear_interaction()
        return _post_action(f"[桌面拖拽已完成] 从 ({sx}, {sy}) 到 ({ex}, {ey})",
                            {"action": "drag", "from": [sx, sy], "to": [ex, ey]})
    except Exception as exc:
        stopped = _desktop_stop_result("desktop_drag", exc)
        if stopped is not None:
            return stopped
        return "[桌面拖拽失败] " + (str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)


def desktop_probe_app(args: dict) -> str:
    """Diagnose the target window framework, UIA support, and recommend interaction mode."""
    try:
        title = args.get("window_title", "")
        hwnd = args.get("hwnd")
        probe = _probe(window_title=title, hwnd=hwnd)
        if not probe.get("ok"):
            return f"[应用诊断失败] 无法探测目标窗口: {probe.get('error_type', 'unknown')}"
        win = probe.get("window", {})
        target_hwnd = win.get("hwnd") or hwnd
        pid = _get_process_id_from_hwnd(target_hwnd) if target_hwnd else 0
        diag = classify_app(pid, target_hwnd)
        
        controls = probe.get("controls") or []
        pattern_counts = {}
        for c in controls:
            for p in c.get("patterns") or []:
                pattern_counts[p] = pattern_counts.get(p, 0) + 1
        
        lines = [
            "[应用框架诊断报告]",
            f"- 目标窗口: \"{win.get('title', title)}\" (HWND: {target_hwnd})",
            f"- 进程名称: {diag['process_name']} (PID: {pid})",
            f"- 窗口类名: {diag['window_class']}",
            f"- 框架识别: {diag['framework']}",
            f"- UIA 状态: {'支持' if probe.get('supported') else '不可用'} (共扫描到 {len(controls)} 个控件)",
        ]
        if pattern_counts:
            patterns_summary = ", ".join(f"{k}: {v}" for k, v in sorted(pattern_counts.items()))
            lines.append(f"- 支持 Pattern 统计: {patterns_summary}")
        else:
            lines.append("- Pattern 支持: 无直接 Pattern（将回退为坐标点击）")
            
        if diag.get("recommendations"):
            lines.append("- 建议策略:")
            for rec in diag["recommendations"]:
                lines.append(f"  * {rec}")
        if len(controls) == 0:
            lines.append("- 提示: 未检测到可操作的 UIA 控件，该窗口可能为 DirectUI / 自绘 / 游戏无障碍未开启界面，请使用视觉兜底 (通过截图像素坐标操作)。")
            
        return "\n".join(lines)
    except Exception as exc:
        return f"[应用诊断异常] {exc}"


def desktop_hotkey(args: dict) -> Message | str:
    keys = args.get("keys") or []
    if isinstance(keys, str):
        keys = keys.split("+")
    keys = [normalize_key(key) for key in keys]
    try:
        if not keys:
            raise EvidenceError("keys 不能为空。")
        if any(key in keys for key in ("enter", "return")) or ("v" in keys and ("ctrl" in keys or "shift" in keys)) or ("shift" in keys and "insert" in keys):
            raise EvidenceError("输入和提交必须用 desktop_type/desktop_press，不能绕过验证。")
        current_state().evidence(args)
        _live_window()
        pyautogui.hotkey(*keys)
        current_state().focus = {}
        current_state().typed_hash = ""
        return _post_action("[桌面快捷键已发送]", {"action": "hotkey", "verified_effect": False})
    except Exception as exc:
        stopped = _desktop_stop_result("desktop_hotkey", exc)
        if stopped is not None:
            return stopped
        return "[快捷键失败] " + (str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)


def desktop_wait(args: dict) -> str:
    """在桌面自动化执行期间等待指定秒数（默认 3 秒），等待窗口加载、渲染、模型生成完成或网络响应。"""
    sec = float(args.get("seconds") or args.get("duration") or args.get("time") or args.get("sec") or 3.0)
    sec = max(0.5, min(sec, 60.0))
    time.sleep(sec)
    return f"[等待完成] 已在桌面等待 {sec:.1f} 秒，界面加载或操作响应完成"


def desktop_press(args: dict) -> Message | str:
    state = current_state()
    try:
        key = normalize_key(args.get("key") or args.get("name") or "")
        if not key:
            raise EvidenceError("key 必须明确指定，不能默认为 Enter。")
        _live_window()
        if key in ("enter", "return"):
            focus = state.require_focus(_read_focus(hwnd=state.snapshot["window"]["hwnd"]))
            state.require_submit(focus)
            if not focus.get("value_supported"):
                from .desktop_runtime import text_hash
                if text_hash(_clipboard_readback()) != state.typed_hash:
                    state.typed_hash = ""
                    raise EvidenceError("提交前读回文本已变化或无法确认，禁止提交。")
                state.require_focus(_read_focus(hwnd=state.snapshot["window"]["hwnd"]))
        else:
            state.evidence(args)
        pyautogui.press(key)
        if key in ("enter", "return"):
            state.submitted = True
        state.typed_hash = ""
        return _post_action("[桌面按键已发送] " + key + "；提交不等于生成完成。",
                            {"action": "press", "submitted": key in ("enter", "return"),
                             "generation_verified": False})
    except Exception as exc:
        stopped = _desktop_stop_result("desktop_press", exc)
        if stopped is not None:
            return stopped
        return "[按键验证失败] " + (str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)


def desktop_scroll(args: dict) -> Message | str:
    try:
        state = current_state()
        state.evidence(args)
        window = _live_window()
        clicks = int(args.get("clicks", args.get("amount", -3)))
        if abs(clicks) > 15:
            raise EvidenceError("滚动步长限制为 -15 到 15，避免页面跳过目标。")
        rect = window["rect"]
        pyautogui.moveTo((rect["left"] + rect["right"]) // 2, (rect["top"] + rect["bottom"]) // 2)
        pyautogui.scroll(clicks)
        return _post_action("[桌面滚动已发送]", {"action": "scroll", "clicks": clicks})
    except Exception as exc:
        stopped = _desktop_stop_result("desktop_scroll", exc)
        if stopped is not None:
            return stopped
        return "[滚轮失败] " + (str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__)


DESKTOP_TOOLS = [
    ToolDefinition(
        name="desktop_screenshot",
        description="截取当前 Windows 操作系统桌面屏幕并保存至本地，返回截图路径。",
        parameters={"type": "object", "properties": {"out_dir": {"type": "string", "default": "."}}, "required": []},
        func=desktop_screenshot,
    ),
    ToolDefinition(
        name="desktop_show_desktop",
        description="最小化当前屏幕上的所有窗口并切回 Windows 桌面（相当于按下 Win+D 快捷键）。当用户要求去桌面、看桌面或操作桌面图标前必须调用。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=desktop_show_desktop,
    ),
    ToolDefinition(
        name="desktop_list_items",
        description="获取当前 Windows 桌面上真实存在的所有项目列表（包含全部文件夹、文件、应用快捷方式及路径）。在操作桌面目标或判断桌面是否存在某项目前，必须调用本工具自主感知桌面真实内容。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=desktop_list_items,
    ),
    ToolDefinition(
        name="desktop_list_shortcuts",
        description="扫描并列出当前 Windows 桌面上所有已存在的文件夹、常规文件与快捷方式图标（兼容别名，功能同 desktop_list_items）。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=desktop_list_shortcuts,
    ),
    ToolDefinition(
        name="desktop_list_windows",
        description="列出当前 Windows 桌面上所有已打开的可见应用程序窗口名称及位置。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=desktop_list_windows,
    ),
    ToolDefinition(
        name="desktop_open_app",
        description="启动或打开 Windows 桌面上的文件夹、应用程序或常规文件（支持文件夹名如 ffmeng、软件名如 Clash Verge、快捷方式名或绝对路径）。会自动智能匹配桌面文件夹或快捷方式并原生调起打开。",
        parameters={"type": "object", "properties": {
            "app_name": {"type": "string", "description": "要打开的目标名称（支持文件夹名如 ffmeng、软件名如 Clash Verge、快捷方式名或绝对路径）"},
            "target": {"type": "string", "description": "同 app_name，目标名称或路径"}
        }, "required": []},
        func=desktop_open_app,
    ),
    ToolDefinition(
        name="desktop_open",
        description="打开 Windows 桌面上的文件夹、应用程序或常规文件（功能同 desktop_open_app）。",
        parameters={"type": "object", "properties": {
            "target": {"type": "string", "description": "要打开的目标文件夹名、软件名、快捷方式或路径"},
            "app_name": {"type": "string", "description": "同 target"}
        }, "required": []},
        func=desktop_open_app,
    ),
    ToolDefinition(
        name="desktop_click",
        description="点击桌面屏幕指定坐标 (x, y)，或前置激活指定名称的桌面窗口。支持 clicks=1 单击或 clicks=2 双击（打开桌面图标需双击）。",
        parameters={"type": "object", "properties": {
            "x": {"type": "number", "description": "当前截图中的 X 坐标，必须引用该截图 snapshot_id"},
            "y": {"type": "number", "description": "当前截图中的 Y 坐标，必须引用该截图 snapshot_id"},
            "clicks": {"type": "integer", "default": 1, "description": "点击次数：1 为单击，2 为双击（桌面图标需双击打开）"},
            "double_click": {"type": "boolean", "default": False, "description": "是否双击"},
            "scale": {"type": "number", "description": "兼容旧调用，可省略；代码按当前截图尺寸自动换算物理屏幕坐标"},
            "window_title": {"type": "string", "description": "目标窗口精确标题；只激活窗口时使用"}
        }, "required": []},
        func=desktop_click,
    ),
    ToolDefinition(
        name="desktop_type",
        description="向已验证的可编辑焦点粘贴并读回文字；明确需要提交且确认 Enter=提交时，可在同次调用验证后提交。",
        parameters={"type": "object", "properties": {
            "text": {"type": "string", "description": "要输入的文本内容"},
            "press_enter": {"type": "boolean", "default": False, "description": "仅当用户原目标明确要求向聊天或生成界面提交，且确认 Enter=提交时设为 true；读回文字并复核焦点、内容后才发送 Enter。草稿、表单、多行编辑或 Enter 语义不明时保持 false（默认）。submitted 只表示已提交，generation_verified 才表示生成结果验证。"}
        }, "required": ["text"]},
        func=desktop_type,
    ),
    ToolDefinition(
        name="desktop_hotkey",
        description="向操作系统发送全局快捷键组合（如 ['ctrl', 'c'] 或 ['win', 'd'] 或 'alt+tab'）。",
        parameters={"type": "object", "properties": {
            "keys": {"type": "array", "items": {"type": "string"}, "description": "按键列表，如 ['ctrl', 's']"}
        }, "required": ["keys"]},
        func=desktop_hotkey,
    ),
    ToolDefinition(
        name="desktop_wait",
        description="在桌面自动化操作期间等待指定秒数（支持 seconds 参数，如 3 或 5 秒），等待软件启动、窗口加载、AI模型生成或网络响应。",
        parameters={"type": "object", "properties": {
            "seconds": {"type": "number", "default": 3.0, "description": "等待秒数（0.5 - 60 秒）"},
            "duration": {"type": "number", "description": "同 seconds"}
        }, "required": []},
        func=desktop_wait,
    ),
    ToolDefinition(
        name="desktop_sleep",
        description="等待指定秒数（功能同 desktop_wait）。",
        parameters={"type": "object", "properties": {
            "seconds": {"type": "number", "default": 3.0, "description": "等待秒数"}
        }, "required": []},
        func=desktop_wait,
    ),
    ToolDefinition(
        name="wait",
        description="等待指定秒数（功能同 desktop_wait）。",
        parameters={"type": "object", "properties": {
            "seconds": {"type": "number", "default": 3.0, "description": "等待秒数"}
        }, "required": []},
        func=desktop_wait,
    ),
    ToolDefinition(
        name="sleep",
        description="等待指定秒数（功能同 desktop_wait）。",
        parameters={"type": "object", "properties": {
            "seconds": {"type": "number", "default": 3.0, "description": "等待秒数"}
        }, "required": []},
        func=desktop_wait,
    ),
    ToolDefinition(
        name="desktop_press",
        description="向已绑定目标窗口发送明确指定的单个按键；Enter须先验证输入，其他按键须携带当前snapshot_id。",
        parameters={"type": "object", "properties": {
            "key": {"type": "string", "description": "必须明确指定按键，如 'enter', 'esc', 'tab'"}
        }, "required": ["key"]},
        func=desktop_press,
    ),
    ToolDefinition(
        name="desktop_scroll",
        description="在目标窗口中心滚动滚轮；负数向下、正数向上，默认-3，步长限制为-15到15。",
        parameters={"type": "object", "properties": {
            "clicks": {"type": "integer", "default": -3, "minimum": -15, "maximum": 15, "description": "目标窗口中心的滚动步长，负数向下、正数向上，限制-15到15"}
        }, "required": []},
        func=desktop_scroll,
    ),
]



# Evidence-aware schemas replace optional/manual coordinate conversion.
for _tool in DESKTOP_TOOLS:
    if _tool.name == "desktop_screenshot":
        _tool.description = "获取目标窗口的当前截图与 UIA 控件树，返回 snapshot_id；所有坐标点击必须引用此 ID。"
        _tool.parameters["properties"]["window_title"] = {"type": "string", "description": "目标窗口精确标题"}
    if _tool.name == "desktop_click":
        _tool.description = "用新截图的 snapshot_id 和 ref(如 c1..c64) 或 control_ref 或坐标点击；优先尝试 UIA Pattern 直调（无需瞬移鼠标），否则自动回退为物理点击。"
        _tool.parameters["properties"].update({
            "snapshot_id": {"type": "string"},
            "ref": {"type": "string", "description": "当前 UIA snapshot 中的短控件引用 (如 c1..c64，强烈推荐)"},
            "control_ref": {"type": "string", "description": "当前 UIA snapshot 中的完整控件引用"},
            "click_type": {"type": "string", "enum": ["click", "double_click", "right_click", "long_press"], "default": "click", "description": "点击类型：click 单击，double_click 双击，right_click 右击，long_press 长按"},
            "confirmed": {"type": "boolean", "default": False, "description": "若控件涉及删除/支付等不可逆操作，设为 true 确认执行"},
        })
    if _tool.name == "desktop_type":
        _tool.description = "仅向已经验证的可编辑焦点粘贴并读回检查。支持 ValuePattern 直接输入或无损剪贴板备份恢复粘贴。严禁使用键盘逐字打中文。用户原目标明确要求向聊天或生成界面提交且确认 Enter=提交时，使用 press_enter=True 在同次调用通过验证后提交，减少模型往返。草稿、表单、多行编辑或 Enter 语义不明时保持默认 False；验证失败禁止按 Enter。submitted 只表示已提交，generation_verified 才表示生成结果验证。"
        _tool.parameters["properties"].update({
            "ref": {"type": "string", "description": "目标输入控件短引用 (如 c1..c64)"},
            "control_ref": {"type": "string", "description": "目标输入控件完整引用"},
        })
    if _tool.name in {"desktop_hotkey", "desktop_press", "desktop_scroll", "desktop_hover", "desktop_drag"}:
        _tool.parameters["properties"]["snapshot_id"] = {"type": "string"}
    if _tool.name == "desktop_scroll":
        _tool.parameters["properties"]["clicks"] = {"type": "integer", "minimum": -15, "maximum": 15, "default": -3, "description": "目标窗口中心的滚动步长，负数向下、正数向上，限制-15到15"}
    _tool.parallel_safe = False

DESKTOP_TOOLS.extend([
    ToolDefinition(name="desktop_snapshot", description="观察目标窗口：截图、UIA控件树与绑定坐标的snapshot_id，输出可直接调用的短句柄 c1..c64。",
                   parameters={"type": "object", "properties": {"window_title": {"type": "string"}}, "required": ["window_title"]},
                   func=desktop_snapshot, parallel_safe=False, is_readonly=True),
    ToolDefinition(name="desktop_inspect_focus", description="检查目标前台窗口的真实可编辑焦点并返回截图；未验证焦点不可输入。",
                   parameters={"type": "object", "properties": {}}, func=desktop_inspect_focus,
                   parallel_safe=False, is_readonly=True),
    ToolDefinition(name="desktop_hover", description="将鼠标悬停在桌面控件（通过 ref 如 c1 或 control_ref）或指定坐标 (x, y) 上，触发悬停浮层或提示，不触发点击。",
                   parameters={"type": "object", "properties": {
                       "ref": {"type": "string", "description": "目标控件短引用，如 c1..c64"},
                       "control_ref": {"type": "string", "description": "完整 UIA 控件引用"},
                       "x": {"type": "number", "description": "截图中的 X 坐标"},
                       "y": {"type": "number", "description": "截图中的 Y 坐标"},
                       "snapshot_id": {"type": "string", "description": "当前截图的 snapshot_id"},
                       "duration": {"type": "number", "default": 0.2, "description": "鼠标移动时长（秒）"}
                   }, "required": []}, func=desktop_hover, parallel_safe=False),
    ToolDefinition(name="desktop_drag", description="在桌面上按住鼠标从起点拖拽到终点（如拖拽滑块、选区、移动窗口或拖拽文件）。",
                   parameters={"type": "object", "properties": {
                       "start_ref": {"type": "string", "description": "起点控件短引用，如 c1"},
                       "end_ref": {"type": "string", "description": "终点控件短引用，如 c2"},
                       "start_x": {"type": "number", "description": "起点 X 坐标"},
                       "start_y": {"type": "number", "description": "起点 Y 坐标"},
                       "end_x": {"type": "number", "description": "终点 X 坐标"},
                       "end_y": {"type": "number", "description": "终点 Y 坐标"},
                       "snapshot_id": {"type": "string", "description": "当前截图 snapshot_id"},
                       "button": {"type": "string", "default": "left", "description": "拖拽使用的鼠标按键"},
                       "duration": {"type": "number", "default": 0.5, "description": "拖拽移动耗时（秒）"}
                   }, "required": []}, func=desktop_drag, parallel_safe=False),
    ToolDefinition(name="desktop_probe_app", description="诊断目标应用窗口的 UI 框架（Win32 / UWP / Chromium / Qt / WPF 等）和 UIA 支持能力，并输出交互与自动化建议。",
                   parameters={"type": "object", "properties": {
                       "window_title": {"type": "string", "description": "目标窗口标题"},
                       "hwnd": {"type": "integer", "description": "目标窗口 HWND 句柄（可选）"}
                   }, "required": []}, func=desktop_probe_app, parallel_safe=False, is_readonly=True),
])
