"""Bounded Windows UI Automation snapshots and verified keyboard focus.

All COM work runs in a disposable worker process. Importing this module never
loads UIA or touches the desktop. Password names and values are never read.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import deque
import multiprocessing
import sys
import time

UIA_TIMEOUT_SECONDS = 8.0
MAX_CONTROLS = 200
MAX_SEARCH_CONTROLS = 500
MAX_SCAN_SECONDS = 5.0
MAX_DEPTH = 24
MAX_WINDOWS = 256
MAX_VALUE_CHARS = 16384
UIA_IS_READ_ONLY_ATTRIBUTE = 40015


class BackendUnavailable(Exception):
    pass


def ensure_com():
    """Ensure COM STA is initialized in current thread (prevents Subagent thread crashes)."""
    try:
        import ctypes
        # COINIT_APARTMENTTHREADED = 0x2
        ctypes.windll.ole32.CoInitializeEx(None, 2)
    except Exception:
        pass


def _failure(error_type: str, *, supported: bool = True, **details) -> dict:
    return {"supported": supported, "ok": False, "error_type": error_type, **details}


def _rectangle(rect) -> dict:
    return {key: int(getattr(rect, key)) for key in ("left", "top", "right", "bottom")}


def _reference(info) -> str:
    try:
        runtime = info.runtime_id
    except Exception:
        runtime = None
    if runtime:
        try:
            return "uia:" + ".".join(str(int(value)) for value in runtime)
        except (TypeError, ValueError):
            pass
    handle = int(info.handle or 0)
    return f"hwnd:{handle}" if handle else ""


class _NativeUIA:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        from pywinauto.uia_defines import IUIA, get_elem_interface
        from pywinauto.uia_element_info import UIAElementInfo

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.automation = IUIA()
        self.element_info = UIAElementInfo
        self.pattern_interface = get_elem_interface
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user32.ClientToScreen.restype = wintypes.BOOL

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def is_window(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindow(hwnd))

    def windows(self):
        callback_type = self.ctypes.WINFUNCTYPE(self.wintypes.BOOL,
                                                self.wintypes.HWND, self.wintypes.LPARAM)
        rows = []
        visited = 0
        def collect(hwnd, unused):
            nonlocal visited
            visited += 1
            if self.user32.IsWindowVisible(hwnd):
                length = min(int(self.user32.GetWindowTextLengthW(hwnd)), 512)
                text = self.ctypes.create_unicode_buffer(length + 1)
                self.user32.GetWindowTextW(hwnd, text, length + 1)
                rows.append((int(hwnd), text.value))
            return visited < MAX_WINDOWS
        callback = callback_type(collect)
        self.user32.EnumWindows.argtypes = [callback_type, self.wintypes.LPARAM]
        self.user32.EnumWindows.restype = self.wintypes.BOOL
        self.user32.EnumWindows(callback, 0)
        return rows

    def window(self, hwnd: int):
        return self.element_info(hwnd)

    def focused(self):
        element = self.automation.get_focused_element()
        return self.element_info(element) if element else None

    def parent(self, info):
        element = self.automation.iuia.ControlViewWalker.GetParentElement(info.element)
        return self.element_info(element) if element else None

    def children(self, info):
        return info.iter_children()

    def pattern(self, info, name):
        try:
            return self.pattern_interface(info.element, name)
        except Exception:
            return None

    def set_focus(self, info):
        # UIAWrapper.set_focus swallows COM failures; use the interface directly.
        info.element.SetFocus()

    def native_caret(self, hwnd: int) -> dict | None:
        from pywinauto import win32functions, win32structures
        gui = win32structures.GUITHREADINFO()
        gui.cbSize = self.ctypes.sizeof(gui)
        thread_id = self.user32.GetWindowThreadProcessId(hwnd, None)
        if not thread_id or not win32functions.GetGUIThreadInfo(thread_id, self.ctypes.byref(gui)):
            return None
        caret_hwnd = int(gui.hwndCaret or 0)
        focus_hwnd = int(gui.hwndFocus or 0)
        if not caret_hwnd or not focus_hwnd:
            return None
        if int(self.user32.GetAncestor(caret_hwnd, 2) or 0) != hwnd:
            return None
        if int(self.user32.GetAncestor(focus_hwnd, 2) or 0) != hwnd:
            return None
        rect = gui.rcCaret
        if rect.bottom <= rect.top or rect.right < rect.left:
            return None
        start = self.wintypes.POINT(rect.left, rect.top)
        end = self.wintypes.POINT(rect.right, rect.bottom)
        if not self.user32.ClientToScreen(caret_hwnd, self.ctypes.byref(start)):
            return None
        if not self.user32.ClientToScreen(caret_hwnd, self.ctypes.byref(end)):
            return None
        return {"hwnd": hwnd, "caret_hwnd": caret_hwnd,
                "rect": {"left": start.x, "top": start.y, "right": end.x, "bottom": end.y}}


@contextmanager
def _backend():
    if sys.platform != "win32":
        raise BackendUnavailable("platform_unsupported")
    # comtypes initializes the importing thread itself; choose its apartment
    # before import, then balance our explicit initialization independently.
    sys.coinit_flags = 0
    try:
        import comtypes
    except ImportError as exc:
        raise BackendUnavailable("backend_missing") from exc
    try:
        comtypes.CoInitializeEx(0)
    except Exception as exc:
        if getattr(exc, "hresult", None) not in (-2147417850, 0x80010106):
            raise BackendUnavailable("com_initialization_failed") from exc
        # A host imported comtypes before this worker entry point. Reuse its
        # existing STA rather than assuming COM is unavailable.
        try:
            comtypes.CoInitializeEx(2)
        except Exception as retry_exc:
            raise BackendUnavailable("com_initialization_failed") from retry_exc
    try:
        try:
            yield _NativeUIA()
        except ImportError as exc:
            raise BackendUnavailable("backend_missing") from exc
    finally:
        comtypes.CoUninitialize()


def _select_window(backend, window_title: str = "", hwnd: int | None = None):
    if hwnd is not None:
        if not backend.is_window(hwnd):
            return None, _failure("window_not_found")
        window = backend.window(hwnd)
        if window_title:
            if bool(window.element.CurrentIsPassword):
                return None, _failure("window_title_mismatch")
            if str(window.name).strip().casefold() != window_title.strip().casefold():
                return None, _failure("window_title_mismatch")
        return window, None
    if not window_title:
        foreground = backend.foreground()
        if not foreground or not backend.is_window(foreground):
            return None, _failure("foreground_unavailable")
        return backend.window(foreground), None

    title = window_title.strip().casefold()
    rows = backend.windows()
    exact = [(handle, name) for handle, name in rows if name.strip().casefold() == title]
    matches = exact or [(handle, name) for handle, name in rows if title in name.casefold()]
    if not matches:
        return None, _failure("window_not_found")
    if len(matches) != 1:
        return None, _failure("window_ambiguous")
    return backend.window(matches[0][0]), None


def _window_record(backend, window) -> dict:
    hwnd = int(window.handle or 0)
    password = bool(window.element.CurrentIsPassword)
    return {"hwnd": hwnd, "title": "" if password else str(window.name or "")[:512],
            "rect": _rectangle(window.rectangle), "foreground": backend.foreground() == hwnd}


_CACHED_PATTERN_IDS = None

def _get_pattern_ids():
    global _CACHED_PATTERN_IDS
    if _CACHED_PATTERN_IDS is None:
        try:
            from pywinauto.uia_defines import pattern_ids
            _CACHED_PATTERN_IDS = {
                name: pattern_ids[name][0]
                for name in ("Invoke", "Toggle", "SelectionItem", "Value", "ExpandCollapse", "ScrollItem", "Text")
                if name in pattern_ids
            }
        except Exception:
            _CACHED_PATTERN_IDS = {}
    return _CACHED_PATTERN_IDS


def _detect_patterns(info, element) -> list[str]:
    mock_patterns = getattr(info, "patterns", None)
    if isinstance(mock_patterns, dict):
        return [k for k, v in mock_patterns.items() if v]
    elif isinstance(mock_patterns, (list, tuple, set)):
        return list(mock_patterns)

    get_pattern = getattr(element, "GetCurrentPattern", None)
    if not callable(get_pattern):
        return []

    patterns = []
    pids = _get_pattern_ids()
    for name, pid in pids.items():
        try:
            if get_pattern(pid):
                patterns.append(name)
        except Exception:
            pass
    return patterns


def _control_record(info) -> dict:
    element = info.element
    # Read the password flag first. Failure here forbids reading the name.
    password = bool(getattr(element, "CurrentIsPassword", False))
    patterns = _detect_patterns(info, element)
    auto_id = ""
    try:
        raw_id = getattr(element, "CurrentAutomationId", "")
        if raw_id:
            auto_id = str(raw_id)[:128]
    except Exception:
        pass
    val_supported = "Value" in patterns
    editable = bool(val_supported or str(info.control_type or "") in ("Edit", "ComboBox", "Document"))
    return {
        "control_ref": _reference(info),
        "name": "" if password else str(info.name or "")[:256],
        "control_type": str(info.control_type or ""),
        "rect": _rectangle(info.rectangle),
        "enabled": bool(element.CurrentIsEnabled),
        "focusable": bool(element.CurrentIsKeyboardFocusable),
        "is_password": password,
        "has_keyboard_focus": bool(element.CurrentHasKeyboardFocus),
        "patterns": patterns,
        "automation_id": auto_id,
        "editable": editable,
        "value_supported": val_supported,
    }


def _walk(backend, root, limit: int):
    """Fair breadth traversal without materializing or exhausting a wide subtree.

    Each parent's lazy sibling iterator yields one node per queue turn. A long
    sidebar therefore cannot consume the entire budget before its neighboring
    content pane gets traversed. Both node count and wall time remain bounded.
    """
    pending = deque([(iter(backend.children(root)), 1)])
    visited = 0
    depth_limited = False
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    while pending and visited < limit and time.monotonic() < deadline:
        children, depth = pending.popleft()
        try:
            info = next(children)
        except StopIteration:
            continue
        visited += 1
        pending.append((children, depth))
        yield info
        if depth < MAX_DEPTH:
            pending.append((iter(backend.children(info)), depth + 1))
        else:
            depth_limited = True
    return bool(pending) or depth_limited


def _snapshot_priority(control: dict) -> int:
    if control["has_keyboard_focus"]:
        return 0
    if control["control_type"] in {"Edit", "ComboBox"}:
        return 1
    if control["control_type"] == "Document":
        return 2
    if control["enabled"] and control["focusable"]:
        return 3
    if control["control_type"] in {"Button", "CheckBox", "RadioButton", "TabItem"}:
        return 4
    return 5


def _inside_window(backend, focused, window) -> bool:
    target = _reference(window)
    current = focused
    for _ in range(MAX_DEPTH + 8):
        if current is None:
            return False
        if _reference(current) == target and target:
            return True
        if int(current.handle or 0) == int(window.handle or 0) and window.handle:
            return True
        current = backend.parent(current)
    return False


def _caret_pattern(backend, info):
    pattern = backend.pattern(info, "TextV2")
    if pattern is None:
        return None, None
    try:
        active, text_range = pattern.GetCaretRange()
        if not active:
            return None, None
        bounds = list(text_range.GetBoundingRectangles()) if text_range else []
        rect = None
        if len(bounds) >= 4:
            left, top, width, height = bounds[:4]
            if height > 0 and width >= 0:
                rect = {"left": int(left), "top": int(top),
                        "right": int(left + width), "bottom": int(top + height)}
        return pattern, rect
    except Exception:
        return None, None


def _writable_text_range(backend, info):
    """Accept only explicit writable full text and one current selection.

    GetAttributeValue returns VT_BOOL for IsReadOnly; unsupported and mixed
    attributes return reserved objects, which must never be coerced to bool.
    GetSelection returns one degenerate range for a current insertion point.
    See Microsoft UIA Text Attribute Identifiers and GetSelection contracts.
    """
    pattern = backend.pattern(info, "Text")
    if pattern is None:
        return None
    try:
        readonly = pattern.DocumentRange.GetAttributeValue(UIA_IS_READ_ONLY_ATTRIBUTE)
        if type(readonly) is not bool or readonly is not False:
            return None
        selection = pattern.GetSelection()
        if selection is None or type(selection.Length) is not int or selection.Length != 1:
            return None
        selected = selection.GetElement(0)
        if selected is None:
            return None
        selected_readonly = selected.GetAttributeValue(UIA_IS_READ_ONLY_ATTRIBUTE)
        if type(selected_readonly) is not bool or selected_readonly is not False:
            return None
        return pattern
    except Exception:
        return None


def _read_focus(backend, hwnd: int | None = None) -> dict:
    window, error = _select_window(backend, hwnd=hwnd)
    if error:
        return error
    window_data = _window_record(backend, window)
    target_hwnd = window_data["hwnd"]
    if not window_data["foreground"]:
        return _failure("window_not_foreground", window=window_data)
    focused = backend.focused()
    if focused is None or not _inside_window(backend, focused, window):
        return _failure("focus_outside_window", window=window_data)
    control = _control_record(focused)
    if not control["has_keyboard_focus"]:
        return _failure("focus_not_confirmed", window=window_data)
    evidence = ["foreground_window_match", "uia_focus_within_target", "uia_keyboard_focus"]
    focus = {**control, "hwnd": target_hwnd, "editable": False,
             "value_supported": False, "caret_rect": None, "evidence": evidence}

    value_pattern = backend.pattern(focused, "Value")
    writable_value = False
    if value_pattern is not None and control["control_type"] in {"Edit", "ComboBox"}:
        try:
            writable_value = not bool(value_pattern.CurrentIsReadOnly)
        except Exception:
            pass
    if writable_value:
        evidence.append("writable_value_pattern")
    text_pattern, caret_rect = _caret_pattern(backend, focused)
    if text_pattern is not None:
        evidence.append("active_text_pattern2_caret")
        focus["caret_rect"] = caret_rect
    writable_text = None
    if not control["is_password"]:
        writable_text = _writable_text_range(backend, focused)
        if writable_text is not None:
            evidence.append("writable_text_range_and_single_selection")
    native_caret = backend.native_caret(target_hwnd)
    if native_caret and native_caret["hwnd"] == target_hwnd:
        caret = native_caret["rect"]
        rect = control["rect"]
        if (rect["left"] <= caret["left"] <= rect["right"]
                and rect["top"] <= caret["top"] <= rect["bottom"]):
            evidence.append("native_caret_within_focused_control")
            if focus["caret_rect"] is None:
                focus["caret_rect"] = caret

    focus["editable"] = bool(control["enabled"] and (
        writable_value or text_pattern is not None or writable_text is not None
        or "native_caret_within_focused_control" in evidence))
    if not control["is_password"]:
        if value_pattern is not None:
            try:
                focus["value"] = str(value_pattern.CurrentValue or "")[:MAX_VALUE_CHARS]
                focus["value_supported"] = True
                focus["value_source"] = "value_pattern"
            except Exception:
                pass
        elif focus["editable"]:
            text_pattern = text_pattern or writable_text or backend.pattern(focused, "Text")
            if text_pattern is not None:
                try:
                    focus["value"] = str(text_pattern.DocumentRange.GetText(MAX_VALUE_CHARS))
                    focus["value_supported"] = True
                    focus["value_source"] = "text_pattern"
                except Exception:
                    pass
    return {"supported": True, "ok": True, "error_type": "", "window": window_data, "focus": focus}


def _execute(operation: str, arguments: dict) -> dict:
    try:
        with _backend() as backend:
            if operation == "read_focus":
                return _read_focus(backend, arguments.get("hwnd"))
            window, error = _select_window(backend, arguments.get("window_title", ""),
                                           arguments.get("hwnd"))
            if error:
                return error
            window_data = _window_record(backend, window)
            if operation == "probe_window":
                controls = []
                unreadable = 0
                # max_controls limits output, not which branch gets scanned.
                walker = _walk(backend, window, MAX_SEARCH_CONTROLS)
                truncated = False
                while True:
                    try:
                        info = next(walker)
                    except StopIteration as finished:
                        truncated = bool(finished.value)
                        break
                    try:
                        controls.append(_control_record(info))
                    except Exception:
                        unreadable += 1
                if not controls:
                    return _failure("controls_unreadable" if unreadable else "no_controls",
                                    window=window_data, controls=[], truncated=truncated,
                                    unreadable_controls=unreadable)
                scanned_controls = len(controls)
                controls.sort(key=_snapshot_priority)
                output_limit = arguments["max_controls"]
                truncated = truncated or scanned_controls > output_limit
                return {"supported": True, "ok": True, "error_type": "",
                        "window": window_data, "controls": controls[:output_limit],
                        "truncated": truncated, "scanned_controls": scanned_controls,
                        "unreadable_controls": unreadable}

            if operation == "invoke_pattern":
                target = arguments["control_ref"]
                pattern_name = arguments["pattern_name"]
                value = arguments.get("value", "")
                matches = [info for info in _walk(backend, window, MAX_SEARCH_CONTROLS)
                           if _reference(info) == target]
                if len(matches) != 1:
                    return _failure("control_ambiguous" if matches else "control_not_found",
                                    window=window_data)
                matched = matches[0]
                try:
                    pattern = backend.pattern(matched, pattern_name)
                    if pattern is None:
                        return _failure("pattern_unsupported", pattern=pattern_name, window=window_data)
                    if pattern_name == "Invoke":
                        pattern.Invoke()
                        return {"supported": True, "ok": True, "action": "invoked", "window": window_data}
                    elif pattern_name == "Toggle":
                        pattern.Toggle()
                        return {"supported": True, "ok": True, "action": "toggled", "window": window_data}
                    elif pattern_name == "SelectionItem":
                        pattern.Select()
                        return {"supported": True, "ok": True, "action": "selected", "window": window_data}
                    elif pattern_name == "Value":
                        pattern.SetValue(str(value))
                        return {"supported": True, "ok": True, "action": "value_set", "value": value, "window": window_data}
                    elif pattern_name == "ExpandCollapse":
                        try:
                            pattern.Expand()
                        except Exception:
                            pattern.Collapse()
                        return {"supported": True, "ok": True, "action": "expand_collapse", "window": window_data}
                    return _failure("unsupported_pattern_action", pattern=pattern_name, window=window_data)
                except Exception as exc:
                    return _failure("pattern_invocation_failed", exception_type=type(exc).__name__, window=window_data)

            if operation != "focus_control":
                return _failure("unknown_operation")
            target = arguments["control_ref"]
            matches = [info for info in _walk(backend, window, MAX_SEARCH_CONTROLS)
                       if _reference(info) == target]
            if len(matches) != 1:
                return _failure("control_ambiguous" if matches else "control_not_found",
                                window=window_data)
            control = _control_record(matches[0])
            if not control["enabled"] or not control["focusable"]:
                return _failure("control_not_focusable", window=window_data)
            backend.set_focus(matches[0])
            for attempt in range(3):
                result = _read_focus(backend, window_data["hwnd"])
                if result["ok"] and result["focus"]["control_ref"] == target:
                    result["focus"]["evidence"].append("requested_control_ref_match")
                    return result
                if attempt < 2:
                    time.sleep(0.05)
            if not result["ok"]:
                return result
            return _failure("focus_mismatch", window=window_data, focus=result["focus"])
    except BackendUnavailable as exc:
        return _failure(str(exc), supported=False)
    except Exception as exc:
        # Exception messages may include user content. Return only the class.
        return _failure("uia_unreadable", exception_type=type(exc).__name__)


def _worker(connection, operation: str, arguments: dict):
    try:
        connection.send(_execute(operation, arguments))
    finally:
        connection.close()


def _run_isolated(operation: str, arguments: dict) -> dict:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, operation, arguments), daemon=True)
    # GUI hosts should not flash a child console while reading UIA.
    try:
        process.start()
        sender.close()
        if not receiver.poll(UIA_TIMEOUT_SECONDS):
            return _failure("uia_timeout", supported=False)
        try:
            result = receiver.recv()
        except (EOFError, OSError):
            return _failure("uia_worker_failed", supported=False)
        if not isinstance(result, dict):
            return _failure("uia_worker_invalid_result", supported=False)
        return result
    except Exception as exc:
        return _failure("uia_worker_failed", supported=False, exception_type=type(exc).__name__)
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            try:
                process.join(0.2)
                if process.is_alive():
                    process.terminate()
                    process.join(1.0)
                if process.is_alive():
                    process.kill()
                    process.join(1.0)
                if not process.is_alive():
                    process.close()
            except (OSError, ValueError):
                pass


def probe_window(window_title: str = "", hwnd: int | None = None,
                 max_controls: int = 80) -> dict:
    """Read one uniquely identified window; never activate or click it."""
    if not isinstance(window_title, str) or (hwnd is not None and (
            not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0)):
        return _failure("invalid_arguments", supported=False)
    if not isinstance(max_controls, int) or isinstance(max_controls, bool) or max_controls < 1:
        return _failure("invalid_arguments", supported=False)
    return _run_isolated("probe_window", {"window_title": window_title.strip(), "hwnd": hwnd,
                                         "max_controls": min(max_controls, MAX_CONTROLS)})


def focus_control(hwnd: int, control_ref: str) -> dict:
    """Focus a specific current UIA element and confirm the resulting focus."""
    if (not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0
            or not isinstance(control_ref, str)
            or not control_ref.startswith(("uia:", "hwnd:"))
            or not control_ref.partition(":")[2]):
        return _failure("invalid_arguments", supported=False)
    return _run_isolated("focus_control", {"hwnd": hwnd, "control_ref": control_ref})


def read_focus(hwnd: int | None = None) -> dict:
    """Read only the requested foreground window's actual focused element."""
    if hwnd is not None and (not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0):
        return _failure("invalid_arguments", supported=False)
    return _run_isolated("read_focus", {"hwnd": hwnd})


def invoke_pattern(hwnd: int, control_ref: str, pattern_name: str, value: str = "") -> dict:
    """Directly invoke a UIA Pattern on a control without moving the physical cursor."""
    if (not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0
            or not isinstance(control_ref, str)
            or not control_ref.startswith(("uia:", "hwnd:"))
            or not control_ref.partition(":")[2]
            or not isinstance(pattern_name, str)):
        return _failure("invalid_arguments", supported=False)
    return _run_isolated("invoke_pattern", {
        "hwnd": hwnd,
        "control_ref": control_ref,
        "pattern_name": pattern_name.strip(),
        "value": str(value) if value is not None else "",
    })
