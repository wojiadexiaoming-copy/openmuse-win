"""Tests for desktop UIA Pattern direct invocations, ref handles c1..c64, and new interaction primitives."""
from copy import deepcopy
import json
from unittest.mock import MagicMock
from PIL import Image
import pytest

from agent.models import Message
from tools import desktop
from tools.desktop_runtime import begin_turn, current_state, end_turn


@pytest.fixture
def uia_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    token = begin_turn("uia-patterns-" + str(tmp_path))
    state = current_state()
    state.invalidate()
    
    gui = MagicMock()
    gui.screenshot.return_value = Image.new("RGB", (1920, 1080), "white")
    monkeypatch.setattr(desktop, "pyautogui", gui)
    
    # Default probe mock for live window verification
    window_data = {
        "hwnd": 1234, "title": "Target App", "foreground": True,
        "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}
    }
    monkeypatch.setattr(desktop, "_probe", lambda window_title="", hwnd=None: {
        "ok": True, "supported": True, "window": deepcopy(window_data), "controls": []
    })
    
    yield state, gui
    end_turn(token)


def test_ref_handles_assignment_and_lookup(uia_session):
    state, gui = uia_session
    raw_controls = [
        {"control_ref": f"uia:{i}", "name": f"Item {i}", "control_type": "Button",
         "rect": {"left": 100, "top": 100 + i * 20, "right": 200, "bottom": 120 + i * 20},
         "enabled": True, "focusable": True, "patterns": ["Invoke"]}
        for i in range(10)
    ]
    projected, defaults = desktop._controls_for_model(raw_controls, limit=5)
    assert len(projected) == 5
    assert projected[0]["ref"] == "c1"
    assert projected[4]["ref"] == "c5"
    assert projected[0]["patterns"] == ["Invoke"]

    # Test observe stores ref_map and click_point resolves by ref
    ref_map = {c["ref"]: c for c in projected}
    state.observe({"hwnd": 100, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}},
                  (1920, 1080), (1920, 1080), raw_controls, ref_map=ref_map)

    # Click point by short ref "c1"
    pt = state.click_point({"snapshot_id": state.snapshot["snapshot_id"], "ref": "c1"})
    assert pt == (150, 110)

    # Click point by short ref "c2"
    pt2 = state.click_point({"snapshot_id": state.snapshot["snapshot_id"], "ref": "c2"})
    assert pt2 == (150, 130)


def test_desktop_click_pattern_direct_invocation_avoids_mouse_jump(uia_session, monkeypatch):
    state, gui = uia_session
    
    controls = [
        {"control_ref": "uia:btn_submit", "name": "提交", "control_type": "Button",
         "rect": {"left": 200, "top": 300, "right": 300, "bottom": 350},
         "enabled": True, "focusable": True, "patterns": ["Invoke"]}
    ]
    ref_map = {"c1": controls[0]}
    controls[0]["ref"] = "c1"
    
    state.observe({"hwnd": 1234, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
                   "title": "Target App", "foreground": True},
                  (1920, 1080), (1920, 1080), controls, ref_map=ref_map)

    monkeypatch.setattr(desktop, "_probe", lambda window_title="", hwnd=None: {
        "ok": True, "supported": True,
        "window": {"hwnd": 1234, "title": "Target App", "foreground": True,
                   "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}},
        "controls": controls
    })

    invoked = []
    def fake_invoke(hwnd, ref, pattern, value=""):
        invoked.append((hwnd, ref, pattern))
        return {"ok": True, "supported": True, "action": "invoked"}

    from tools import desktop_accessibility
    monkeypatch.setattr(desktop_accessibility, "invoke_pattern", fake_invoke)
    monkeypatch.setattr(desktop, "_read_focus", lambda hwnd: {
        "ok": True, "window": {"hwnd": 1234, "foreground": True},
        "focus": {"control_ref": "uia:btn_submit", "editable": True, "rect": {"left": 200, "top": 300, "right": 300, "bottom": 350}}
    })

    res = desktop.desktop_click({"snapshot_id": state.snapshot["snapshot_id"], "ref": "c1"})
    assert isinstance(res, Message)
    assert "[桌面Pattern直调成功: Invoke]" in res.content
    assert (1234, "uia:btn_submit", "Invoke") in invoked
    # Physical mouse click should NOT be called because pattern direct invocation succeeded!
    gui.click.assert_not_called()


def test_desktop_click_fallback_to_physical_mouse_when_pattern_unsupported(uia_session, monkeypatch):
    state, gui = uia_session
    
    controls = [
        {"control_ref": "uia:custom_box", "name": "自绘框", "control_type": "Pane",
         "rect": {"left": 200, "top": 300, "right": 300, "bottom": 350},
         "enabled": True, "focusable": True, "patterns": []}  # No patterns
    ]
    ref_map = {"c1": controls[0]}
    controls[0]["ref"] = "c1"
    
    state.observe({"hwnd": 1234, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
                   "title": "Target App", "foreground": True},
                  (1920, 1080), (1920, 1080), controls, ref_map=ref_map)

    monkeypatch.setattr(desktop, "_probe", lambda window_title="", hwnd=None: {
        "ok": True, "supported": True,
        "window": {"hwnd": 1234, "title": "Target App", "foreground": True,
                   "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}},
        "controls": controls
    })
    monkeypatch.setattr(desktop, "_read_focus", lambda hwnd: {
        "ok": True, "window": {"hwnd": 1234, "foreground": True},
        "focus": {"control_ref": "uia:custom_box", "editable": False, "rect": {"left": 200, "top": 300, "right": 300, "bottom": 350}}
    })

    res = desktop.desktop_click({"snapshot_id": state.snapshot["snapshot_id"], "ref": "c1"})
    assert isinstance(res, Message)
    assert "[桌面点击已发送]" in res.content
    # Fallback to physical click at center (250, 325)
    gui.click.assert_called_once_with(250, 325)


def test_desktop_type_value_pattern_direct_write(uia_session, monkeypatch):
    state, gui = uia_session
    
    controls = [
        {"control_ref": "uia:input_box", "name": "用户名", "control_type": "Edit",
         "rect": {"left": 100, "top": 100, "right": 400, "bottom": 140},
         "enabled": True, "focusable": True, "editable": True, "value_supported": True,
         "patterns": ["Value"]}
    ]
    state.observe({"hwnd": 9999, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
                   "title": "Target App", "foreground": True},
                  (1920, 1080), (1920, 1080), controls)

    monkeypatch.setattr(desktop, "_probe", lambda window_title="", hwnd=None: {
        "ok": True, "supported": True,
        "window": {"hwnd": 9999, "title": "Target App", "foreground": True,
                   "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}},
        "controls": controls
    })

    # Fake initial focus
    focus_data = {
        "ok": True, "window": {"hwnd": 9999, "foreground": True},
        "focus": {"control_ref": "uia:input_box", "editable": True, "value_supported": True,
                  "value": "", "rect": {"left": 100, "top": 100, "right": 400, "bottom": 140}}
    }
    state.mark_click((250, 120), "uia:input_box")
    state.set_focus(focus_data)

    written_values = []
    def fake_invoke(hwnd, ref, pattern, value=""):
        written_values.append((hwnd, ref, pattern, value))
        focus_data["focus"]["value"] = value
        return {"ok": True, "supported": True, "action": "value_set"}

    from tools import desktop_accessibility
    monkeypatch.setattr(desktop_accessibility, "invoke_pattern", fake_invoke)
    monkeypatch.setattr(desktop, "_read_focus", lambda hwnd: deepcopy(focus_data))

    res = desktop.desktop_type({"text": "Hello AlterEgo"})
    assert isinstance(res, Message)
    assert "(ValuePattern直写)" in res.content
    assert (9999, "uia:input_box", "Value", "Hello AlterEgo") in written_values


def test_desktop_hover_and_drag(uia_session, monkeypatch):
    state, gui = uia_session
    
    controls = [
        {"control_ref": "uia:item_a", "name": "Item A", "control_type": "ListItem",
         "rect": {"left": 100, "top": 100, "right": 200, "bottom": 140}, "enabled": True},
        {"control_ref": "uia:item_b", "name": "Item B", "control_type": "ListItem",
         "rect": {"left": 100, "top": 300, "right": 200, "bottom": 340}, "enabled": True},
    ]
    ref_map = {"c1": controls[0], "c2": controls[1]}
    controls[0]["ref"] = "c1"
    controls[1]["ref"] = "c2"
    
    state.observe({"hwnd": 8888, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
                   "title": "Target App", "foreground": True},
                  (1920, 1080), (1920, 1080), controls, ref_map=ref_map)

    monkeypatch.setattr(desktop, "_probe", lambda window_title="", hwnd=None: {
        "ok": True, "supported": True,
        "window": {"hwnd": 8888, "title": "Target App", "foreground": True,
                   "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}},
        "controls": controls
    })

    # Test Hover
    hover_res = desktop.desktop_hover({"snapshot_id": state.snapshot["snapshot_id"], "ref": "c1", "duration": 0.1})
    assert isinstance(hover_res, Message)
    assert "[桌面悬停已完成]" in hover_res.content
    gui.moveTo.assert_called_with(150, 120, duration=0.1)

    # Test Drag
    drag_res = desktop.desktop_drag({
        "snapshot_id": state.snapshot["snapshot_id"],
        "start_ref": "c1", "end_ref": "c2", "duration": 0.3
    })
    assert isinstance(drag_res, Message)
    assert "[桌面拖拽已完成]" in drag_res.content
    gui.mouseDown.assert_called_once()
    gui.mouseUp.assert_called_once()


def test_desktop_probe_app(monkeypatch):
    from tools import desktop
    
    monkeypatch.setattr(desktop, "_probe", lambda window_title="", hwnd=None: {
        "ok": True, "supported": True,
        "window": {"hwnd": 7777, "title": "Chrome Browser"},
        "controls": [
            {"control_ref": "uia:1", "patterns": ["Invoke"]},
            {"control_ref": "uia:2", "patterns": ["Value"]},
        ]
    })
    monkeypatch.setattr(desktop, "_get_process_id_from_hwnd", lambda hwnd: 1234)
    monkeypatch.setattr(desktop, "classify_app", lambda pid, hwnd: {
        "pid": pid, "hwnd": hwnd, "process_name": "chrome.exe",
        "window_class": "Chrome_WidgetWin_1", "framework": "chromium",
        "recommendations": ["检测到 Chromium 内核浏览器；如需高精度的页面自动化，强烈建议使用 browser_* 系列工具。"]
    })

    report = desktop.desktop_probe_app({"window_title": "Chrome Browser"})
    assert "[应用框架诊断报告]" in report
    assert "chrome.exe" in report
    assert "chromium" in report
    assert "browser_*" in report
