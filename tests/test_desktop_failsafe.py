"""Trusted stop, truthful focus and image-coordinate regressions; all UI is fake."""
from copy import deepcopy
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image
import pytest

from agent.models import Message
from tools import desktop
from tools.desktop_runtime import (DesktopHalted, DesktopState, EvidenceError, begin_turn,
                                   current_state, end_turn, screen_rect_to_image)

FAILSAFE = desktop.pyautogui.FailSafeException
SCREEN = (1920, 958)
IMAGE = (1568, 782)
WINDOW = {"hwnd": 100, "title": "Offline editor", "foreground": True,
          "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 958}}
CONTROL = {"control_ref": "uia:editor", "name": "Editor", "control_type": "Edit",
           "enabled": True, "focusable": True, "has_keyboard_focus": True, "is_password": False,
           "rect": {"left": 620, "top": 818, "right": 1580, "bottom": 842}}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    token = begin_turn("failsafe-" + str(tmp_path))
    state = current_state()
    state.invalidate()
    box = {"window": deepcopy(WINDOW), "clipboard": "original", "value": "before", "value_supported": True}
    def probe(**kwargs):
        return {"ok": True, "supported": True, "window": deepcopy(box["window"]), "controls": [deepcopy(CONTROL)]}
    def read_focus(**kwargs):
        return {"ok": True, "window": deepcopy(box["window"]), "focus": {
            **deepcopy(CONTROL), "editable": True, "value_supported": box["value_supported"], "value": box["value"]}}
    def hotkey(*keys):
        if keys == ("ctrl", "v"):
            box["value"] = box["clipboard"]
        elif keys == ("ctrl", "c"):
            box["clipboard"] = box["value"]
    gui = MagicMock()
    gui.FAILSAFE, gui.FailSafeException = True, FAILSAFE
    gui.screenshot.side_effect = lambda: Image.new("RGB", SCREEN, "white")
    gui.hotkey.side_effect = hotkey
    clip = SimpleNamespace(paste=lambda: box["clipboard"], copy=lambda value: box.__setitem__("clipboard", value))
    maximize = MagicMock(return_value={"status": "unsupported", "geometry_verified": False, "changed": False})
    monkeypatch.setattr(desktop, "pyautogui", gui)
    monkeypatch.setattr(desktop, "pyperclip", clip)
    monkeypatch.setattr(desktop, "_probe", probe)
    monkeypatch.setattr(desktop, "_read_focus", read_focus)
    monkeypatch.setattr(desktop, "_restore_window", lambda hwnd: False)
    monkeypatch.setattr(desktop, "_maximize_target_window", maximize)
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    state.observe(deepcopy(WINDOW), SCREEN, IMAGE, [deepcopy(CONTROL)])
    yield SimpleNamespace(state=state, box=box, gui=gui, clip=clip, maximize=maximize,
                          focus=read_focus, probe=probe, monkeypatch=monkeypatch)
    end_turn(token)


def seed_verified(machine):
    state = machine.state
    state.mark_click((1100, 830), "uia:editor")
    assert state.set_focus(machine.focus())
    state.verify_text(machine.box["value"], machine.box["value"], machine.focus()["focus"])


def assert_stopped(result, machine):
    assert isinstance(result, Message)
    assert result.metadata["tool_control"] == {"retryable": False, "requires_user": True, "reason": "desktop_failsafe"}
    assert "屏幕四角停止保护" in result.content and "先前效果未验证" in result.content
    assert "移离屏幕四角" in result.content and "继续" in result.content
    assert "所有动作均未发送" not in result.content
    state = machine.state
    assert state.halted and not state.snapshot and not state.focus
    assert not state.typed_hash and not state.typed_control and not state.submitted
    assert state.clicked_point is None and not state.expected_control
    assert machine.gui.FAILSAFE is True


@pytest.mark.parametrize("operation,fail_method", [
    ("click", "click"), ("double", "doubleClick"), ("type", "hotkey"),
    ("hotkey", "hotkey"), ("press", "press"), ("scroll_move", "moveTo"),
    ("scroll_wheel", "scroll"), ("showdesktop", "hotkey"), ("snapshot", "screenshot"),
])
def test_real_failsafe_is_structured_stop_and_clears_all_evidence(machine, operation, fail_method):
    seed_verified(machine)
    shot_id = machine.state.snapshot["snapshot_id"]
    getattr(machine.gui, fail_method).side_effect = FAILSAFE("offline only")
    calls = {
        "click": lambda: desktop.desktop_click({"snapshot_id": shot_id, "control_ref": "uia:editor"}),
        "double": lambda: desktop.desktop_click({"snapshot_id": shot_id, "control_ref": "uia:editor", "clicks": 2}),
        "type": lambda: desktop.desktop_type({"text": "offline"}),
        "hotkey": lambda: desktop.desktop_hotkey({"snapshot_id": shot_id, "keys": ["ctrl", "s"]}),
        "press": lambda: desktop.desktop_press({"snapshot_id": shot_id, "key": "tab"}),
        "scroll_move": lambda: desktop.desktop_scroll({"snapshot_id": shot_id, "clicks": 1}),
        "scroll_wheel": lambda: desktop.desktop_scroll({"snapshot_id": shot_id, "clicks": 1}),
        "showdesktop": lambda: desktop.desktop_show_desktop({}),
        "snapshot": lambda: desktop.desktop_snapshot({"window_title": "Offline editor"}),
    }
    result = calls[operation]()
    assert_stopped(result, machine)
    if operation != "scroll_move" and operation != "scroll_wheel":
        machine.gui.moveTo.assert_not_called()


def test_gui_call_sees_old_interaction_cleared_and_failed_click_has_no_phantom_point(machine):
    seed_verified(machine)
    def fail(*args, **kwargs):
        assert not machine.state.focus and not machine.state.typed_hash
        assert machine.state.clicked_point is None and not machine.state.expected_control
        raise FAILSAFE("offline only")
    machine.gui.click.side_effect = fail
    result = desktop.desktop_click({"snapshot_id": machine.state.snapshot["snapshot_id"], "control_ref": "uia:editor"})
    assert_stopped(result, machine)


@pytest.mark.parametrize("failure", [RuntimeError("ordinary"), RuntimeError("FailSafeException in untrusted body")])
def test_ordinary_click_failure_does_not_stop_but_cannot_reuse_old_valid_focus(machine, failure):
    seed_verified(machine)
    machine.gui.click.side_effect = failure
    result = desktop.desktop_click({"snapshot_id": machine.state.snapshot["snapshot_id"], "control_ref": "uia:editor"})
    assert isinstance(result, str) and "桌面点击失败" in result
    assert not machine.state.halted and machine.state.snapshot
    assert not machine.state.focus and not machine.state.typed_hash and machine.state.clicked_point is None
    assert isinstance(desktop.desktop_type({"text": "must not enter"}), str)
    machine.gui.hotkey.assert_not_called()


def test_invalid_click_request_also_clears_old_input_authorization(machine):
    seed_verified(machine)
    result = desktop.desktop_click({"snapshot_id": machine.state.snapshot["snapshot_id"], "x": 100, "y": 812})
    assert isinstance(result, str) and "1568×782" in result
    assert not machine.state.focus and not machine.state.typed_hash
    machine.gui.click.assert_not_called()


@pytest.mark.parametrize("operation", ["click", "type", "hotkey", "press", "scroll", "showdesktop", "open", "snapshot", "focus"])
def test_halted_turn_blocks_later_side_effects_before_window_preparation(machine, operation):
    machine.state.halt()
    startfile = MagicMock()
    machine.monkeypatch.setattr(desktop.os, "startfile", startfile)
    calls = {
        "click": lambda: desktop.desktop_click({"window_title": "Offline editor"}),
        "type": lambda: desktop.desktop_type({"text": "offline"}),
        "hotkey": lambda: desktop.desktop_hotkey({"keys": ["ctrl", "s"]}),
        "press": lambda: desktop.desktop_press({"key": "tab"}),
        "scroll": lambda: desktop.desktop_scroll({"clicks": 1}),
        "showdesktop": lambda: desktop.desktop_show_desktop({}),
        "open": lambda: desktop.desktop_open_app({"app_name": "offline"}),
        "snapshot": lambda: desktop.desktop_snapshot({"window_title": "Offline editor"}),
        "focus": lambda: desktop.desktop_inspect_focus({}),
    }
    assert_stopped(calls[operation](), machine)
    machine.maximize.assert_not_called()
    startfile.assert_not_called()
    for name in ("click", "doubleClick", "moveTo", "hotkey", "press", "scroll", "screenshot"):
        getattr(machine.gui, name).assert_not_called()


def test_nested_turn_cannot_clear_halt_but_new_user_turn_requires_fresh_evidence(tmp_path):
    scope = "halt-lifecycle-" + str(tmp_path)
    first = begin_turn(scope)
    try:
        state = current_state()
        state.observe(WINDOW, SCREEN, IMAGE, [CONTROL])
        old_id = state.snapshot["snapshot_id"]
        state.halt()
        nested = begin_turn(scope)
        try:
            assert current_state() is state and state.halted
            with pytest.raises(DesktopHalted):
                state.evidence({"snapshot_id": old_id})
        finally:
            end_turn(nested)
    finally:
        end_turn(first)
    second = begin_turn(scope)
    try:
        assert current_state() is state and not state.halted
        assert not state.snapshot and not state.focus
        with pytest.raises(EvidenceError, match="desktop_snapshot"):
            state.evidence({"snapshot_id": old_id})
        state.observe(WINDOW, SCREEN, IMAGE, [CONTROL])
        assert state.snapshot["snapshot_id"] != old_id
    finally:
        end_turn(second)


def test_clipboard_readback_failsafe_preserves_stop_and_restores_original_clipboard(machine):
    seed_verified(machine)
    machine.box["value_supported"] = False
    original_hotkey = machine.gui.hotkey.side_effect
    def fail_copy(*keys):
        if keys == ("ctrl", "c"):
            raise FAILSAFE("offline only")
        original_hotkey(*keys)
    machine.gui.hotkey.side_effect = fail_copy
    result = desktop.desktop_type({"text": "already sent before readback", "press_enter": True})
    assert_stopped(result, machine)
    assert machine.box["value"] == "already sent before readback"
    assert machine.box["clipboard"] == "original"
    machine.gui.press.assert_not_called()


def test_readback_restore_failure_cannot_mask_trusted_failsafe(machine):
    machine.gui.hotkey.side_effect = FAILSAFE("offline only")
    original_copy = machine.clip.copy
    def restore_fails(value):
        if value == "original":
            raise RuntimeError("offline restoration failure")
        original_copy(value)
    machine.clip.copy = restore_fails
    with pytest.raises(DesktopHalted):
        desktop._clipboard_readback()
    assert machine.state.halted and not machine.state.snapshot


@pytest.mark.parametrize("action", ["click", "type", "submit"])
def test_post_action_capture_failsafe_does_not_claim_prior_effects_were_unsent(machine, action):
    seed_verified(machine)
    machine.gui.screenshot.side_effect = FAILSAFE("offline only")
    if action == "click":
        result = desktop.desktop_click({"snapshot_id": machine.state.snapshot["snapshot_id"], "control_ref": "uia:editor"})
        machine.gui.click.assert_called_once()
    elif action == "type":
        result = desktop.desktop_type({"text": "already pasted", "press_enter": True})
        assert machine.box["value"] == "already pasted"
        machine.gui.press.assert_not_called()
    else:
        result = desktop.desktop_press({"key": "enter"})
        machine.gui.press.assert_called_once_with("enter")
    assert_stopped(result, machine)


def test_open_app_inner_handlers_do_not_swallow_real_stop(machine):
    window = SimpleNamespace(_hWnd=100, title="Offline editor", activate=MagicMock())
    machine.monkeypatch.setitem(sys.modules, "pygetwindow", SimpleNamespace(getAllWindows=lambda: [window]))
    machine.maximize.side_effect = FAILSAFE("offline only")
    launch = MagicMock()
    machine.monkeypatch.setattr(desktop.os, "startfile", launch)
    assert_stopped(desktop.desktop_open_app({"app_name": "Offline editor"}), machine)
    launch.assert_not_called()


def test_focus_observation_without_successful_click_remains_explicitly_disallowed(machine):
    result = desktop.desktop_inspect_focus({})
    verification = result.metadata["desktop_verification"]
    assert verification["focus"]["editable"] is True
    assert verification["focus_verified"] is False and verification["input_allowed"] is False
    assert verification["verification_reason"] == "no_successful_click"
    assert "只是观察值" in result.content and "需新截图和成功点击" in result.content


def test_post_action_does_not_maximize_or_activate_after_validating_focus(machine):
    seed_verified(machine)
    result = desktop.desktop_inspect_focus({})
    assert result.metadata["desktop_verification"]["focus_verified"]
    assert machine.state.focus
    machine.maximize.assert_not_called()


@pytest.mark.parametrize("action", ["focus", "type"])
def test_final_state_invalidated_by_geometry_change_downgrades_report_and_prevents_submit(machine, action):
    seed_verified(machine)
    original_capture = desktop._capture
    def moved(args, **kwargs):
        assert kwargs["prepare_window"] is False
        machine.box["window"]["rect"]["right"] -= 100
        return original_capture(args, **kwargs)
    machine.monkeypatch.setattr(desktop, "_capture", moved)
    submit = MagicMock()
    machine.monkeypatch.setattr(desktop, "desktop_press", submit)
    result = (desktop.desktop_inspect_focus({}) if action == "focus"
              else desktop.desktop_type({"text": "offline", "press_enter": True}))
    verification = result.metadata["desktop_verification"]
    assert not machine.state.focus and not machine.state.typed_hash
    assert verification["input_allowed"] is False
    assert verification["focus_verified" if action == "focus" else "input_verified"] is False
    assert verification["verification_reason"] in {"window_geometry_changed", "final_input_state_invalidated"}
    submit.assert_not_called()


def test_real_dimensions_project_uia_screen_rect_to_image_with_separate_axis_scales(machine):
    result = desktop.desktop_snapshot({"window_title": "Offline editor"})
    observation = result.metadata["desktop_observation"]
    control = observation["controls"][0]
    assert control["rect"] == CONTROL["rect"]
    assert control["image_rect"] == screen_rect_to_image(CONTROL["rect"], SCREEN, IMAGE)
    assert control["image_rect"]["top"] == pytest.approx(818 * 782 / 958, abs=1e-6)
    assert abs(control["image_rect"]["top"] - 818 * 1568 / 1920) > .1
    assert observation["coordinate_spaces"] == {"rect": "screen", "image_rect": "image", "click_x_y": "image"}
    assert observation["image_size"] == list(IMAGE) and observation["screen_size"] == list(SCREEN)
    assert observation["image_coordinate_range"] == {"x": [0, 1567], "y": [0, 781]}
    assert "image_rect" not in machine.state.snapshot["controls"][0]
    sid = observation["snapshot_id"]
    assert machine.state.click_point({"snapshot_id": sid, "control_ref": "uia:editor"}) == (1100, 830)
    projected = control["image_rect"]
    assert machine.state.click_point({"snapshot_id": sid, "x": (projected["left"] + projected["right"]) / 2,
                                      "y": (projected["top"] + projected["bottom"]) / 2, "scale": .8167}) == (1100, 830)


@pytest.mark.parametrize("y", [798, 812, 830])
def test_physical_or_old_image_coordinate_is_not_guessed_and_error_gives_current_range(machine, y):
    with pytest.raises(EvidenceError) as error:
        machine.state.click_point({"snapshot_id": machine.state.snapshot["snapshot_id"], "x": 1000, "y": y})
    text = str(error.value)
    assert "1568×782" in text and "y=0..781" in text and "control_ref" in text


@pytest.mark.parametrize("scale", [.5, .8168, .817, float("nan"), float("inf")])
def test_old_or_invalid_scale_is_rejected_even_for_control_reference(machine, scale):
    with pytest.raises(EvidenceError, match="scale"):
        machine.state.click_point({"snapshot_id": machine.state.snapshot["snapshot_id"], "control_ref": "uia:editor", "scale": scale})


@pytest.mark.parametrize("point", [(0, 0), (0, 99), (99, 0), (99, 99)])
def test_all_screen_corner_click_targets_are_rejected_before_gui(point):
    state = DesktopState()
    window = {"hwnd": 1, "rect": {"left": 0, "top": 0, "right": 100, "bottom": 100}}
    state.observe(window, (100, 100), (100, 100))
    with pytest.raises(EvidenceError, match="屏幕四角"):
        state.click_point({"snapshot_id": state.snapshot["snapshot_id"], "x": point[0], "y": point[1]})
    assert not state.halted


@pytest.mark.parametrize("point", [(1567.999, 500), (500, 781.999)])
def test_rounding_cannot_create_an_out_of_screen_target(machine, point):
    with pytest.raises(EvidenceError, match="取整后坐标超出屏幕"):
        machine.state.click_point({"snapshot_id": machine.state.snapshot["snapshot_id"], "x": point[0], "y": point[1]})


def test_tiny_uia_control_center_cannot_round_to_failsafe_corner():
    state = DesktopState()
    control = {"control_ref": "tiny", "enabled": True, "rect": {"left": 0, "top": 0, "right": .8, "bottom": .8}}
    state.observe({"hwnd": 1, "rect": {"left": 0, "top": 0, "right": 100, "bottom": 100}},
                  (100, 100), (100, 100), [control])
    with pytest.raises(EvidenceError, match="屏幕四角"):
        state.click_point({"snapshot_id": state.snapshot["snapshot_id"], "control_ref": "tiny"})
