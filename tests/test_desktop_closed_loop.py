"""Desktop operations need fresh evidence, real editable focus and text readback."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
import time

import pytest
from PIL import Image

from agent.models import Message
from agent.tools_registry import ToolRegistry
from tools import desktop
from tools.desktop_runtime import DesktopState, EvidenceError, current_state, begin_turn, end_turn


WINDOW = {"hwnd": 100, "title": "Test editor", "foreground": True,
          "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}}
FOCUS = {"hwnd": 100, "control_ref": "uia:editor", "control_type": "Edit",
         "editable": True, "is_password": False, "value_supported": True, "value": "",
         "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    token = begin_turn("test-" + str(tmp_path))
    state = current_state()
    state.invalidate()
    box = {"window": deepcopy(WINDOW), "focus": deepcopy(FOCUS), "clipboard": "original", "value": ""}
    def probe(**kwargs):
        return {"ok": True, "supported": True, "window": deepcopy(box["window"]),
                "controls": [{"control_ref": "uia:editor", "control_type": "Edit", "enabled": True,
                              "is_password": False, "rect": {"left": 300, "top": 650, "right": 1100, "bottom": 800}}]}
    def focus(**kwargs):
        data = deepcopy(box["focus"])
        data["value"] = box["value"]
        return {"ok": True, "supported": True, "window": deepcopy(box["window"]), "focus": data}
    def hotkey(*keys):
        if keys == ("ctrl", "v"):
            box["value"] = box["clipboard"]
        if keys == ("ctrl", "c") and not box.get("copy_fails"):
            box["clipboard"] = box["value"]
    gui = MagicMock()
    gui.screenshot.side_effect = lambda: Image.new("RGB", (1920, 1080), "white")
    gui.hotkey.side_effect = hotkey
    clip = SimpleNamespace(paste=lambda: box["clipboard"],
                           copy=lambda value: box.__setitem__("clipboard", value))
    monkeypatch.setattr(desktop, "pyautogui", gui)
    monkeypatch.setattr(desktop, "pyperclip", clip)
    monkeypatch.setattr(desktop, "_probe", probe)
    monkeypatch.setattr(desktop, "_restore_window", lambda hwnd: False)
    monkeypatch.setattr(desktop, "_maximize_target_window", lambda **kwargs: {
        "status": "unsupported", "geometry_verified": False, "changed": False})
    monkeypatch.setattr(desktop, "_read_focus", focus)
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    yield state, box, gui
    end_turn(token)


def snapshot():
    result = desktop.desktop_snapshot({"window_title": "Test editor"})
    assert isinstance(result, Message)
    return result.metadata["desktop_observation"]["snapshot_id"]


def click_editor():
    return desktop.desktop_click({"snapshot_id": snapshot(), "control_ref": "uia:editor"})


def test_fresh_snapshot_auto_coordinates_and_post_action_image(machine):
    state, box, gui = machine
    shot_id = snapshot()
    result = desktop.desktop_click({"snapshot_id": shot_id, "x": 640, "y": 360})
    assert isinstance(result, Message)
    gui.click.assert_called_once_with(784, 441)
    assert result.images and result.metadata["desktop_verification"]["focus_verified"]
    assert state.snapshot["snapshot_id"] != shot_id


@pytest.mark.parametrize("args", [
    {"x": 600, "y": 650}, {"snapshot_id": "old", "x": 600, "y": 650},
    {"x": -1, "y": 10}, {"x": float("inf"), "y": 20},
])
def test_unbound_or_invalid_click_never_reaches_mouse(machine, args):
    state, box, gui = machine
    snapshot()
    assert isinstance(desktop.desktop_click(args), str)
    gui.click.assert_not_called()


def test_old_or_wrong_scale_cannot_be_reused(machine):
    state, box, gui = machine
    old = snapshot()
    new = snapshot()
    assert "失败" in desktop.desktop_click({"snapshot_id": old, "x": 10, "y": 10})
    assert "失败" in desktop.desktop_click({"snapshot_id": new, "x": 10, "y": 10, "scale": .5})
    gui.click.assert_not_called()


@pytest.mark.parametrize("change", ["foreground", "geometry", "expired"])
def test_live_window_and_age_checked_again_before_action(machine, change):
    state, box, gui = machine
    shot_id = snapshot()
    if change == "foreground":
        box["window"]["foreground"] = False
    elif change == "geometry":
        box["window"]["rect"]["left"] = 20
    else:
        state.snapshot["captured_at"] -= 121
    assert "失败" in desktop.desktop_click({"snapshot_id": shot_id, "x": 10, "y": 10})
    gui.click.assert_not_called()


@pytest.mark.parametrize("field,value", [("editable", False), ("is_password", True), ("control_ref", "uia:other")])
def test_focus_must_stay_editable_and_targeted(machine, field, value):
    state, box, gui = machine
    click_editor()
    box["focus"][field] = value
    result = desktop.desktop_type({"text": "new text"})
    assert isinstance(result, str) and "失败" in result
    gui.hotkey.assert_not_called()
    assert "失败" in desktop.desktop_press({"key": "enter"})
    gui.press.assert_not_called()


def test_valid_input_readback_allows_submission_and_restores_clipboard(machine):
    state, box, gui = machine
    click_editor()
    result = desktop.desktop_type({"text": "心理学海报"})
    assert isinstance(result, Message)
    assert result.metadata["desktop_verification"]["input_verified"]
    assert box["clipboard"] == "original"
    submitted = desktop.desktop_press({"key": "enter"})
    assert isinstance(submitted, Message)
    assert submitted.metadata["desktop_verification"]["submitted"]
    assert not submitted.metadata["desktop_verification"]["generation_verified"]
    gui.press.assert_called_once_with("enter")


def test_mismatch_after_paste_cannot_submit(machine):
    state, box, gui = machine
    click_editor()
    gui.hotkey.side_effect = lambda *keys: None
    result = desktop.desktop_type({"text": "expected", "press_enter": True})
    assert "失败" in result
    gui.press.assert_not_called()


def test_clipboard_fallback_uses_sentinel_to_prove_copy_worked(machine):
    state, box, gui = machine
    click_editor()
    box["focus"]["value_supported"] = False
    box["copy_fails"] = True
    result = desktop.desktop_type({"text": "expected", "press_enter": True})
    assert "失败" in result
    gui.press.assert_not_called()
    assert box["clipboard"] == "original"


def test_clipboard_fallback_with_real_copy_can_verify(machine):
    state, box, gui = machine
    click_editor()
    box["focus"]["value_supported"] = False
    assert isinstance(desktop.desktop_type({"text": "expected"}), Message)


@pytest.mark.parametrize("keys", [["ctrl", "v"], ["shift", "insert"], ["enter"]])
def test_hotkey_cannot_bypass_verified_input(machine, keys):
    state, box, gui = machine
    result = desktop.desktop_hotkey({"snapshot_id": snapshot(), "keys": keys})
    assert "失败" in result
    gui.hotkey.assert_not_called()


def test_no_implicit_enter_and_no_unverified_submit(machine):
    state, box, gui = machine
    snapshot()
    assert "失败" in desktop.desktop_press({})
    assert "失败" in desktop.desktop_press({"key": "enter"})
    gui.press.assert_not_called()


def test_scope_isolation_and_restore(machine):
    state, box, gui = machine
    snapshot()
    token = begin_turn("different-session")
    try:
        assert not current_state().snapshot
        assert "失败" in desktop.desktop_type({"text": "cannot leak"})
    finally:
        end_turn(token)
    assert current_state() is state


def test_registry_rejects_missing_evidence_before_approval(machine):
    state, box, gui = machine
    registry = ToolRegistry()
    for definition in desktop.DESKTOP_TOOLS:
        registry.register(definition)
    approval = MagicMock()
    registry.set_approval(approval)
    result = registry.execute("desktop_click", {"x": 100, "y": 100})
    assert "验证失败" in result.content
    approval.check.assert_not_called()
    gui.click.assert_not_called()


def test_scroll_is_bounded_and_zero_stays_zero(machine):
    state, box, gui = machine
    shot_id = snapshot()
    assert "失败" in desktop.desktop_scroll({"snapshot_id": shot_id, "clicks": -300})
    gui.scroll.assert_not_called()
    assert isinstance(desktop.desktop_scroll({"snapshot_id": shot_id, "clicks": 0}), Message)
    gui.scroll.assert_called_once_with(0)


def test_click_does_not_validate_stale_focus_in_another_input(machine):
    state, box, gui = machine
    box["focus"]["rect"] = {"left": 0, "top": 0, "right": 200, "bottom": 200}
    result = click_editor()
    assert isinstance(result, Message)
    assert not result.metadata["desktop_verification"]["focus_verified"]
    assert "失败" in desktop.desktop_type({"text": "must not enter search field"})
    gui.hotkey.assert_not_called()


def test_control_ref_must_match_real_focused_control(machine):
    state, box, gui = machine
    box["focus"]["control_ref"] = "uia:search"
    result = click_editor()
    assert not result.metadata["desktop_verification"]["focus_verified"]


def test_extra_screenshot_preserves_validated_input_for_submit(machine):
    state, box, gui = machine
    click_editor()
    desktop.desktop_type({"text": "expected"})
    snapshot()
    assert isinstance(desktop.desktop_press({"key": "enter"}), Message)


def test_clipboard_only_input_rechecked_immediately_before_submit(machine):
    state, box, gui = machine
    click_editor()
    box["focus"]["value_supported"] = False
    assert isinstance(desktop.desktop_type({"text": "expected"}), Message)
    box["value"] = "changed by user"
    assert "失败" in desktop.desktop_press({"key": "enter"})
    gui.press.assert_not_called()


def test_approval_focus_can_restore_only_the_bound_target(machine, monkeypatch):
    state, box, gui = machine
    shot_id = snapshot()
    box["window"]["foreground"] = False
    restored = []
    def restore(hwnd):
        restored.append(hwnd)
        box["window"]["foreground"] = True
        return True
    monkeypatch.setattr(desktop, "_restore_window", restore)
    result = desktop.desktop_click({"snapshot_id": shot_id, "x": 640, "y": 360})
    assert isinstance(result, Message)
    assert restored == [100]
    gui.click.assert_called_once()


def test_idle_state_lifecycle_is_bounded_without_evicting_active_scope(machine, monkeypatch):
    import tools.desktop_runtime as runtime
    monkeypatch.setattr(runtime, "MAX_IDLE_SCOPES", 3)
    active = current_state()
    for index in range(10):
        token = begin_turn("idle-test-" + str(index))
        current_state()
        end_turn(token)
    assert current_state() is active
    assert len([key for key in runtime._states if not runtime._active_scopes.get(key)]) <= 3


@pytest.mark.parametrize("keys", [
    ["ctrlleft", "v"], ["ctrlright", "v"], ["control", "v"],
    ["control_r", "v"], ["left-control", "v"],
    ["shiftright", "insert"], ["shiftleft", "insert"], ["shift_r", "insert"],
    ["return"], ["kp_enter"], ["keypad-enter"], ["numpad_enter"], ["\r"], ["\n"],
])
def test_hotkey_modifier_and_enter_aliases_cannot_bypass_input(machine, keys):
    state, box, gui = machine

    result = desktop.desktop_hotkey({"snapshot_id": snapshot(), "keys": keys})

    assert isinstance(result, str) and "失败" in result
    gui.hotkey.assert_not_called()


@pytest.mark.parametrize("key", ["return", "kp_enter", "keypad-enter", "numpad_enter", "vk_return", "\r", "\n"])
def test_enter_aliases_still_require_verified_input(machine, key):
    state, box, gui = machine
    snapshot()

    result = desktop.desktop_press({"key": key})

    assert isinstance(result, str) and "失败" in result
    gui.press.assert_not_called()


def test_verified_enter_alias_is_sent_as_canonical_enter(machine):
    state, box, gui = machine
    click_editor()
    assert isinstance(desktop.desktop_type({"text": "expected"}), Message)

    result = desktop.desktop_press({"key": "kp_enter"})

    assert isinstance(result, Message)
    assert result.metadata["desktop_verification"]["submitted"]
    gui.press.assert_called_once_with("enter")


def test_safe_hotkey_modifier_alias_uses_same_canonical_check_and_dispatch(machine):
    state, box, gui = machine

    result = desktop.desktop_hotkey({"snapshot_id": snapshot(), "keys": ["ctrlleft", "a"]})

    assert isinstance(result, Message)
    gui.hotkey.assert_called_once_with("ctrl", "a")


@pytest.mark.parametrize("failure", ["unavailable", "wrong_hwnd"])
def test_post_action_unreadable_target_is_failure_and_clears_evidence(machine, monkeypatch, failure):
    state, box, gui = machine
    click_editor()
    original_probe = desktop._probe
    calls = 0

    def probe(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_probe(**kwargs)
        if failure == "unavailable":
            return {"ok": False, "supported": True, "error_type": "window_not_found"}
        return {"ok": True, "supported": True,
                "window": {**deepcopy(box["window"]), "hwnd": 999}, "controls": []}

    monkeypatch.setattr(desktop, "_probe", probe)

    result = desktop.desktop_type({"text": "expected", "press_enter": True})

    assert isinstance(result, str) and "验证失败" in result
    assert not state.snapshot and not state.focus and not state.typed_hash
    assert box["clipboard"] == "original"
    gui.press.assert_not_called()


def test_failed_target_snapshot_invalidates_previous_target(machine, monkeypatch):
    state, box, gui = machine
    click_editor()
    gui.screenshot.reset_mock()
    monkeypatch.setattr(desktop, "_probe", lambda **kwargs: {
        "ok": False, "supported": True, "error_type": "window_not_found",
    })

    result = desktop.desktop_snapshot({"window_title": "Another window"})

    assert isinstance(result, str) and "失败" in result
    assert not state.snapshot and not state.focus
    gui.screenshot.assert_not_called()


@pytest.mark.parametrize("identity", ["", None, "   "])
def test_empty_control_identity_never_validates_focus_or_input(machine, identity):
    state, box, gui = machine
    box["focus"]["control_ref"] = identity

    clicked = desktop.desktop_click({"snapshot_id": snapshot(), "x": 640, "y": 360})

    assert isinstance(clicked, Message)
    assert not clicked.metadata["desktop_verification"]["focus_verified"]
    assert not state.focus
    assert "失败" in desktop.desktop_type({"text": "wrong field"})
    assert "失败" in desktop.desktop_press({"key": "enter"})
    gui.hotkey.assert_not_called()
    gui.press.assert_not_called()


def test_empty_control_identity_is_rejected_at_readback_and_submit():
    state = DesktopState()
    with pytest.raises(EvidenceError, match="身份"):
        state.verify_text("expected", "expected", {"control_ref": ""})
    assert not state.typed_hash and not state.typed_control
    state.typed_hash = "old-hash"
    state.typed_control = ""
    with pytest.raises(EvidenceError):
        state.require_submit({"control_ref": ""})


def test_empty_identity_cannot_match_a_different_focused_control():
    state = DesktopState()
    state.observe(WINDOW, (1920, 1080), (1920, 1080))
    state.focus = {**FOCUS, "control_ref": ""}

    with pytest.raises(EvidenceError):
        state.require_focus({
            "ok": True, "window": WINDOW,
            "focus": {**FOCUS, "control_ref": "",
                      "rect": {"left": 10, "top": 10, "right": 20, "bottom": 20}},
        })


@pytest.mark.parametrize("args", [
    {"key": "return"}, {"key": "kp_enter"}, {"key": "keypad-enter"},
    {"name": "numpad_enter"}, {"name": "\r"},
])
def test_registry_enter_aliases_use_real_desktop_guard_before_approval(machine, args):
    state, box, gui = machine
    snapshot()
    registry = ToolRegistry()
    for definition in desktop.DESKTOP_TOOLS:
        registry.register(definition)
    assert registry.get("desktop_press").func is desktop.desktop_press
    assert registry.get("desktop_press").func.__module__ == "tools.desktop"
    approval = MagicMock()
    registry.set_approval(approval)

    result = registry.execute("desktop_press", args)

    assert "验证失败" in result.content
    approval.check.assert_not_called()
    gui.press.assert_not_called()
