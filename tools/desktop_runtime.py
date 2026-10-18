"""Per-session desktop evidence. Pure state checks, no desktop side effects."""
from __future__ import annotations

from collections import OrderedDict
import contextvars
import hashlib
import math
import threading
import time
import uuid
from dataclasses import dataclass, field

_scope = contextvars.ContextVar("desktop_scope", default=None)
_states = OrderedDict()
_active_scopes = {}
MAX_IDLE_SCOPES = 256
_lock = threading.RLock()


class EvidenceError(ValueError):
    pass


class DesktopHalted(EvidenceError):
    """A trusted desktop stop, cleared only by the next user turn."""
    def __init__(self, reason="desktop_failsafe"):
        self.reason = reason
        self.tool_control = {"retryable": False, "requires_user": True, "reason": reason}
        super().__init__("屏幕四角停止保护已触发；请用户将鼠标移离屏幕四角后回复“继续”，恢复时需重新截图。")


def valid_window_rect(rect):
    if not isinstance(rect, dict):
        return False
    values = [rect.get(key) for key in ("left", "top", "right", "bottom")]
    return (all(type(value) in (int, float) and math.isfinite(value) for value in values)
            and values[2] > values[0] and values[3] > values[1])


def window_rect_matches(actual, expected, tolerance=0):
    return (valid_window_rect(actual) and valid_window_rect(expected)
            and all(abs(actual[key] - expected[key]) <= tolerance
                    for key in ("left", "top", "right", "bottom")))


def maximized_geometry_verified(info):
    rect, work = info.get("rect"), info.get("work_area")
    # Maximized Win32 frames can extend a few pixels beyond the monitor work area.
    return (info.get("maximized") is True and valid_window_rect(rect) and valid_window_rect(work)
            and window_rect_matches(rect, work, tolerance=16))


def screen_rect_to_image(rect, screen_size, image_size):
    """Project physical UIA bounds with each actual screenshot axis scale."""
    if not valid_window_rect(rect):
        return None
    sw, sh = screen_size
    iw, ih = image_size
    if any(type(value) is not int or value <= 0 for value in (sw, sh, iw, ih)):
        raise EvidenceError("截图或屏幕尺寸无效，请重新观察。")
    return {key: round(rect[key] * (iw / sw if key in ("left", "right") else ih / sh), 6)
            for key in ("left", "top", "right", "bottom")}


@dataclass
class DesktopState:
    snapshot: dict = field(default_factory=dict)
    focus: dict = field(default_factory=dict)
    ref_map: dict = field(default_factory=dict)
    typed_hash: str = ""
    typed_control: str = ""
    submitted: bool = False
    clicked_point: tuple | None = None
    expected_control: str = ""
    max_age_sec: float = 120.0
    halted: bool = False
    halt_reason: str = ""
    focus_verification_reason: str = "unverified"

    def halt(self, reason="desktop_failsafe"):
        self.invalidate()
        self.halted = True
        self.halt_reason = reason

    def require_not_halted(self):
        if self.halted:
            raise DesktopHalted(self.halt_reason or "desktop_failsafe")

    def resume_turn(self):
        # Evidence was invalidated on stop; clearing the latch cannot restore it.
        self.halted = False
        self.halt_reason = ""

    def clear_interaction(self):
        self.focus = {}
        self.typed_hash = ""
        self.typed_control = ""
        self.submitted = False
        self.clicked_point = None
        self.expected_control = ""
        self.focus_verification_reason = "unverified"

    def invalidate(self):
        self.snapshot = {}
        self.ref_map = {}
        self.clear_interaction()

    def resolve_control(self, ref: str) -> dict | None:
        """Resolve a control by ref ('c1'..'c64') or full control_ref."""
        if not ref:
            return None
        if ref in self.ref_map:
            return self.ref_map[ref]
        for c in self.snapshot.get("controls") or []:
            if c.get("control_ref") == ref or c.get("ref") == ref:
                return c
        return None

    def observe(self, window, screen_size, image_size, controls=(), *, preserve_input=False, ref_map=None):
        self.require_not_halted()
        old_window = self.snapshot.get("window", {})
        old_hwnd = old_window.get("hwnd")
        geometry_changed = bool(old_hwnd and (old_hwnd != window.get("hwnd")
                                or old_window.get("rect") != window.get("rect")))
        if not preserve_input or old_hwnd != window.get("hwnd") or geometry_changed:
            self.focus = {}
            self.typed_hash = ""
            self.typed_control = ""
        if geometry_changed:
            self.clicked_point = None
            self.expected_control = ""
            self.focus_verification_reason = "window_geometry_changed"
        self.ref_map = dict(ref_map) if ref_map else {}
        self.snapshot = {
            "snapshot_id": "desk-" + uuid.uuid4().hex,
            "captured_at": time.monotonic(),
            "window": dict(window),
            "screen_size": list(screen_size),
            "image_size": list(image_size),
            "controls": list(controls),
            "ref_map": dict(self.ref_map),
        }
        return self.snapshot

    def evidence(self, args=None):
        self.require_not_halted()
        args = args or {}
        snapshot = self.snapshot
        if not snapshot:
            raise EvidenceError("先调用 desktop_snapshot(window_title=目标窗口)，不能盲操作。")
        if time.monotonic() - snapshot["captured_at"] > self.max_age_sec:
            raise EvidenceError("截图已过期，请重新获取 desktop_snapshot。")
        if args.get("snapshot_id") != snapshot["snapshot_id"]:
            raise EvidenceError("必须传入当前截图的 snapshot_id，旧坐标不得复用。")
        return snapshot

    def click_point(self, args):
        shot = self.evidence(args)
        rect = shot["window"].get("rect") or {}
        controls = shot.get("controls") or []
        iw, ih = shot["image_size"]
        sw, sh = shot["screen_size"]
        if any(type(value) is not int or value <= 0 for value in (sw, sh, iw, ih)):
            raise EvidenceError("截图或屏幕尺寸无效，请重新观察。")
        help_text = (f"当前截图大小 {iw}×{ih}，有效图像像素 x=0..{iw - 1}、y=0..{ih - 1}；"
                     "请优先使用 ref（如 c1..c64）或 control_ref，不能传入物理屏幕坐标或旧截图坐标。")
        if args.get("scale") is not None:
            try:
                supplied = float(args["scale"])
            except (TypeError, ValueError):
                raise EvidenceError("scale 无效；只传当前截图坐标，代码按实际尺寸换算。") from None
            actual = iw / sw
            if (not math.isfinite(supplied)
                    or not (math.isclose(supplied, actual, rel_tol=0, abs_tol=1e-9)
                            or supplied == round(actual, 4))):
                raise EvidenceError("scale 与当前截图不符；不接受旧scale。" + help_text)
        ref = args.get("ref") or args.get("control_ref")
        if ref:
            if ref in self.ref_map:
                targets = [self.ref_map[ref]]
            else:
                targets = [c for c in controls if c.get("control_ref") == ref or c.get("ref") == ref]
            if len(targets) != 1 or targets[0].get("enabled") is False or not targets[0].get("enabled", True):
                raise EvidenceError("控件不存在、歧义或已禁用，请重新观察。")
            if targets[0].get("is_password"):
                raise EvidenceError("密码控件不支持此桌面输入流程。")
            target = targets[0].get("rect")
            if not valid_window_rect(target):
                raise EvidenceError("控件屏幕范围无效，请重新观察。")
            x = (target["left"] + target["right"]) / 2
            y = (target["top"] + target["bottom"]) / 2
        else:
            try:
                x, y = float(args["x"]), float(args["y"])
            except (KeyError, ValueError, TypeError):
                raise EvidenceError("需要截图中的 x/y 或 ref/control_ref。" + help_text) from None
            if not math.isfinite(x) or not math.isfinite(y) or not (0 <= x < iw and 0 <= y < ih):
                raise EvidenceError("坐标超出当前截图范围。" + help_text)
            x, y = x * sw / iw, y * sh / ih
        if not valid_window_rect(rect) or not (rect["left"] <= x < rect["right"] and rect["top"] <= y < rect["bottom"]):
            raise EvidenceError("点击不在目标窗口内。" + help_text)
        point = round(x), round(y)
        if not (0 <= point[0] < sw and 0 <= point[1] < sh):
            raise EvidenceError("换算取整后坐标超出屏幕，禁止自动修正。" + help_text)
        if not (rect["left"] <= point[0] < rect["right"] and rect["top"] <= point[1] < rect["bottom"]):
            raise EvidenceError("取整后坐标超出目标窗口，请重新定位。" + help_text)
        if point in {(0, 0), (0, sh - 1), (sw - 1, 0), (sw - 1, sh - 1)}:
            raise EvidenceError("禁止将屏幕四角停止保护点作为点击目标。" + help_text)
        return point

    def mark_click(self, point, control_ref=""):
        self.require_not_halted()
        self.clicked_point = tuple(point)
        self.expected_control = control_ref or ""
        self.focus = {}
        self.typed_hash = ""
        self.typed_control = ""

    def set_focus(self, result):
        self.require_not_halted()
        focus = result.get("focus") or {}
        window = result.get("window") or {}
        target = self.snapshot.get("window") or {}
        self.focus = {}
        self.typed_hash = ""
        self.typed_control = ""
        rect = focus.get("rect") or {}
        ref = focus.get("control_ref")
        valid_ref = isinstance(ref, str) and bool(ref.strip())
        point_matches = bool(self.clicked_point and rect
                             and rect.get("left", 0) <= self.clicked_point[0] < rect.get("right", 0)
                             and rect.get("top", 0) <= self.clicked_point[1] < rect.get("bottom", 0))
        expected_raw = self.expected_control
        if expected_raw and expected_raw in self.ref_map:
            expected_ref = self.ref_map[expected_raw].get("control_ref", expected_raw)
        else:
            expected_ref = expected_raw
        ref_matches = not expected_ref or ref == expected_ref or ref == expected_raw
        checks = ((result.get("ok"), "uia_focus_unavailable"),
                  (window.get("hwnd") == target.get("hwnd"), "target_window_mismatch"),
                  (window.get("foreground"), "target_not_foreground"),
                  (focus.get("editable"), "focus_not_editable"),
                  (not focus.get("is_password"), "password_control"),
                  (valid_ref, "missing_focus_identity"),
                  (self.clicked_point is not None, "no_successful_click"),
                  (point_matches, "click_outside_focused_control"),
                  (ref_matches, "focused_control_mismatch"))
        self.focus_verification_reason = next((reason for ok, reason in checks if not ok), "verified_successful_click")
        if self.focus_verification_reason == "verified_successful_click":
            self.focus = dict(focus)
            return True
        return False

    def require_focus(self, result):
        self.require_not_halted()
        target = self.snapshot.get("window") or {}
        window = result.get("window") or {}
        focus = result.get("focus") or {}
        ref = focus.get("control_ref")
        if (not self.focus or not result.get("ok")
                or not isinstance(ref, str) or not ref.strip()
                or window.get("hwnd") != target.get("hwnd") or not window.get("foreground")
                or not focus.get("editable") or focus.get("is_password")
                or ref != self.focus.get("control_ref")):
            self.typed_hash = ""
            self.typed_control = ""
            raise EvidenceError("未验证目标窗口的可编辑焦点，禁止粘贴或提交。请重新点击并检查焦点。")
        return focus

    def verify_text(self, expected, actual, focus):
        self.require_not_halted()
        self.typed_hash = ""
        self.typed_control = ""
        ref = focus.get("control_ref")
        if not isinstance(ref, str) or not ref.strip():
            raise EvidenceError("焦点控件缺少稳定身份，禁止验证输入或提交。")
        if normalize_text(expected) != normalize_text(actual):
            raise EvidenceError("输入读回与目标文本不一致，禁止提交；请检查焦点和输入框。")
        self.typed_hash = text_hash(expected)
        self.typed_control = ref
        self.submitted = False

    def require_submit(self, focus):
        self.require_not_halted()
        ref = focus.get("control_ref")
        if (not isinstance(ref, str) or not ref.strip() or not self.typed_control
                or not self.typed_hash or self.typed_control != ref):
            raise EvidenceError("尚未验证输入文字，禁止按 Enter 提交。")
        if focus.get("value_supported") and text_hash(focus.get("value", "")) != self.typed_hash:
            self.typed_hash = ""
            raise EvidenceError("输入值已改变，禁止使用旧输入验证提交。")


def normalize_key(value):
    """Canonicalize modifier/Enter aliases before checking or sending keys."""
    raw = str(value or "")
    if raw in {"\n", "\r", "\r\n"}:
        return "enter"
    key = raw.casefold().strip()
    compact = "".join(key.split()).replace("_", "").replace("-", "")
    aliases = {
        "control": "ctrl", "ctrlleft": "ctrl", "ctrlright": "ctrl",
        "controlleft": "ctrl", "controlright": "ctrl",
        "leftctrl": "ctrl", "rightctrl": "ctrl", "leftcontrol": "ctrl", "rightcontrol": "ctrl",
        "lctrl": "ctrl", "rctrl": "ctrl", "ctrll": "ctrl", "ctrlr": "ctrl",
        "controll": "ctrl", "controlr": "ctrl",
        "shiftleft": "shift", "shiftright": "shift", "leftshift": "shift", "rightshift": "shift",
        "lshift": "shift", "rshift": "shift", "shiftl": "shift", "shiftr": "shift",
        "altleft": "alt", "altright": "alt", "leftalt": "alt", "rightalt": "alt",
        "lalt": "alt", "ralt": "alt", "altl": "alt", "altr": "alt",
        "winleft": "win", "winright": "win", "windows": "win", "super": "win",
        "return": "enter", "numenter": "enter", "numpadenter": "enter",
        "kpenter": "enter", "keypadenter": "enter", "vkreturn": "enter",
        "carriagereturn": "enter", "newline": "enter",
    }
    return aliases.get(compact, key)


def normalize_text(value):
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def text_hash(value):
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def _prune_idle():
    idle = [key for key in _states if not _active_scopes.get(key)]
    for key in idle[:-MAX_IDLE_SCOPES]:
        _states.pop(key, None)


def begin_turn(scope_id):
    key = str(scope_id)
    with _lock:
        if not _active_scopes.get(key):
            state = _states.get(key)
            if state is not None:
                state.resume_turn()
        _active_scopes[key] = _active_scopes.get(key, 0) + 1
    return _scope.set(key)


def end_turn(token):
    key = _scope.get()
    _scope.reset(token)
    with _lock:
        if key in _active_scopes:
            _active_scopes[key] -= 1
            if not _active_scopes[key]:
                _active_scopes.pop(key, None)
        _prune_idle()


def current_state():
    key = _scope.get() or "direct:" + str(threading.get_ident())
    with _lock:
        state = _states.setdefault(key, DesktopState())
        _states.move_to_end(key)
        _prune_idle()
        return state


def clear_scope(scope_id):
    with _lock:
        _states.pop(str(scope_id), None)
