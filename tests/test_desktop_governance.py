"""Tests for desktop governance: focus contention, secure desktop, irreversible actions, and lossless clipboard."""
from copy import deepcopy
from unittest.mock import MagicMock
from PIL import Image
import pytest

from agent.models import Message
from tools import desktop
from tools.desktop_runtime import begin_turn, current_state, end_turn


@pytest.fixture
def gov_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    token = begin_turn("desktop-gov-" + str(tmp_path))
    state = current_state()
    state.invalidate()
    
    gui = MagicMock()
    gui.screenshot.return_value = Image.new("RGB", (1920, 1080), "white")
    monkeypatch.setattr(desktop, "pyautogui", gui)
    
    yield state, gui
    end_turn(token)


def test_focus_contention_detection_pauses_automation(gov_session, monkeypatch):
    state, gui = gov_session
    
    controls = [{"control_ref": "uia:btn", "name": "Test", "control_type": "Button",
                 "rect": {"left": 100, "top": 100, "right": 200, "bottom": 200}, "enabled": True}]
    state.observe({"hwnd": 1234, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
                   "title": "Target App", "foreground": True},
                  (1920, 1080), (1920, 1080), controls)

    # Simulate physical user activity within 0.5s (< 2.0s threshold)
    monkeypatch.setattr(desktop, "check_focus_contention", lambda threshold=2.0: {"contention": True, "idle_sec": 0.5})

    res = desktop.desktop_click({"snapshot_id": state.snapshot["snapshot_id"], "x": 150, "y": 150})
    assert isinstance(res, Message)
    assert "[桌面已让出]" in res.content
    assert res.metadata.get("tool_control", {}).get("requires_user") is True
    # Physical mouse should not click
    gui.click.assert_not_called()


def test_secure_desktop_uac_intercepts_automation(gov_session, monkeypatch):
    state, gui = gov_session
    
    controls = [{"control_ref": "uia:btn", "name": "Test", "control_type": "Button",
                 "rect": {"left": 100, "top": 100, "right": 200, "bottom": 200}, "enabled": True}]
    state.observe({"hwnd": 1234, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
                   "title": "Target App", "foreground": True},
                  (1920, 1080), (1920, 1080), controls)

    # Simulate Secure Desktop (UAC active)
    monkeypatch.setattr(desktop, "is_secure_desktop", lambda: True)

    res = desktop.desktop_click({"snapshot_id": state.snapshot["snapshot_id"], "x": 150, "y": 150})
    assert isinstance(res, Message)
    assert "[桌面安全拦截]" in res.content
    assert "UAC 凭据请求" in res.content
    assert res.metadata.get("tool_control", {}).get("requires_user") is True
    gui.click.assert_not_called()


def test_irreversible_action_protection(gov_session, monkeypatch):
    state, gui = gov_session
    
    controls = [
        {"control_ref": "uia:btn_format", "name": "格式化磁盘", "control_type": "Button",
         "rect": {"left": 100, "top": 100, "right": 200, "bottom": 200}, "enabled": True}
    ]
    ref_map = {"c1": controls[0]}
    controls[0]["ref"] = "c1"
    
    state.observe({"hwnd": 1234, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
                   "title": "Disk Tool", "foreground": True},
                  (1920, 1080), (1920, 1080), controls, ref_map=ref_map)

    # Attempt to click without confirmed=True
    res = desktop.desktop_click({"snapshot_id": state.snapshot["snapshot_id"], "ref": "c1"})
    assert isinstance(res, Message)
    assert "[桌面高风险操作拦截]" in res.content
    assert "格式化磁盘" in res.content
    assert res.metadata.get("tool_control", {}).get("requires_user") is True
    gui.click.assert_not_called()

    # With confirmed=True, the risk interception is passed
    monkeypatch.setattr(desktop, "_probe", lambda window_title="", hwnd=None: {
        "ok": True, "supported": True,
        "window": {"hwnd": 1234, "title": "Disk Tool", "foreground": True,
                   "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}},
        "controls": controls
    })
    monkeypatch.setattr(desktop, "_read_focus", lambda hwnd: {
        "ok": True, "window": {"hwnd": 1234, "foreground": True},
        "focus": {"control_ref": "uia:btn_format", "editable": False,
                  "rect": {"left": 100, "top": 100, "right": 200, "bottom": 200}}
    })
    res_confirmed = desktop.desktop_click({"snapshot_id": state.snapshot["snapshot_id"], "ref": "c1", "confirmed": True})
    assert isinstance(res_confirmed, Message)
    assert "[桌面点击已发送]" in res_confirmed.content
    gui.click.assert_called_once_with(150, 150)


def test_lossless_clipboard_backup_and_restore(gov_session, monkeypatch):
    state, gui = gov_session
    
    controls = [
        {"control_ref": "uia:edit", "name": "Text Box", "control_type": "Edit",
         "rect": {"left": 100, "top": 100, "right": 400, "bottom": 200},
         "enabled": True, "focusable": True, "editable": True, "value_supported": False}
    ]
    state.observe({"hwnd": 5555, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
                   "title": "Text Editor", "foreground": True},
                  (1920, 1080), (1920, 1080), controls)

    focus_data = {
        "ok": True, "window": {"hwnd": 5555, "foreground": True},
        "focus": {"control_ref": "uia:edit", "editable": True, "value_supported": False,
                  "rect": {"left": 100, "top": 100, "right": 400, "bottom": 200}}
    }
    state.mark_click((250, 150), "uia:edit")
    state.set_focus(focus_data)

    clipboard_holder = ["USER_ORIGINAL_CLIPBOARD_CONTENT"]
    
    class FakePyperclip:
        def paste(self):
            return clipboard_holder[0]
        def copy(self, text):
            clipboard_holder[0] = text

    monkeypatch.setattr(desktop, "pyperclip", FakePyperclip())
    monkeypatch.setattr(desktop, "_probe", lambda window_title="", hwnd=None: {
        "ok": True, "supported": True,
        "window": {"hwnd": 5555, "title": "Text Editor", "foreground": True,
                   "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}},
        "controls": controls
    })
    monkeypatch.setattr(desktop, "_read_focus", lambda hwnd: deepcopy(focus_data))
    monkeypatch.setattr(desktop, "_clipboard_readback", lambda: "New Input Text")

    res = desktop.desktop_type({"text": "New Input Text"})
    assert isinstance(res, Message)
    assert "[桌面输入已验证]" in res.content
    # After desktop_type completes, original clipboard must be losslessly restored!
    assert clipboard_holder[0] == "USER_ORIGINAL_CLIPBOARD_CONTENT"
