"""M3 测试：Windows 桌面自动化工具（spec §19）。"""
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from PIL import Image
import pytest

from agent.approval import humanize_approval
from tools.desktop import (
    DESKTOP_TOOLS,
    desktop_screenshot,
    desktop_list_windows,
    desktop_hotkey,
)



WINDOW = {"hwnd": 100, "title": "Test", "foreground": True,
          "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080}}


def fake_gui():
    gui = MagicMock()
    gui.screenshot.side_effect = lambda: Image.new("RGB", (1920, 1080), "white")
    return gui


@pytest.fixture(autouse=True)
def isolated_desktop(tmp_path, monkeypatch):
    import pygetwindow
    from tools import desktop
    from tools.desktop_runtime import begin_turn, end_turn, current_state
    token = begin_turn(str(tmp_path))
    current_state().invalidate()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pygetwindow, "getAllWindows", lambda: [])
    monkeypatch.setattr(desktop, "_probe", lambda **kwargs: {
        "ok": True, "supported": True, "window": deepcopy(WINDOW), "controls": []})
    monkeypatch.setattr(desktop, "_maximize_target_window", lambda **kwargs: {
        "status": "unsupported", "geometry_verified": False, "changed": False})
    monkeypatch.setattr(desktop, "_read_focus", lambda **kwargs: {
        "ok": True, "window": deepcopy(WINDOW), "focus": {
            "hwnd": 100, "control_ref": "uia:input", "editable": True,
            "is_password": False, "value_supported": True, "value": "Hello Agent",
            "rect": deepcopy(WINDOW["rect"])}})
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    yield
    end_turn(token)

def test_desktop_tools_schemas():
    names = {t.name for t in DESKTOP_TOOLS}
    expected = {
        "desktop_screenshot",
        "desktop_list_windows",
        "desktop_open_app",
        "desktop_click",
        "desktop_type",
        "desktop_hotkey",
    }
    assert expected <= names
    for t in DESKTOP_TOOLS:
        assert t.name
        assert t.description
        assert "type" in t.parameters


@pytest.mark.parametrize("screen_size, expected_size, scale", [
    ((800, 600), (800, 600), "1.0000"),
    ((3200, 1800), (1568, 882), "0.4900"),
])
def test_desktop_screenshot(tmp_path, monkeypatch, screen_size, expected_size, scale):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from agent.models import Message
    from tools import desktop

    fake_screen = Image.new("RGB", screen_size, color=(20, 40, 60))
    monkeypatch.setattr(desktop, "pyautogui", SimpleNamespace(screenshot=lambda: fake_screen))
    res = desktop_screenshot({"out_dir": str(tmp_path)})

    assert isinstance(res, Message)
    assert res.role == "tool"
    assert res.name == "desktop_screenshot"
    assert "[桌面截图]" in res.content
    assert f"截图尺寸: {expected_size[0]}x{expected_size[1]}" in res.content
    assert f"scale={scale}" in res.content
    shots = list(tmp_path.glob("desktop_shot_*.jpg"))
    assert len(shots) == 1
    assert len(res.images) == 1
    assert res.images[0].data == shots[0].read_bytes()
    assert Image.open(io.BytesIO(res.images[0].data)).size == expected_size


def test_desktop_screenshot_failure_stays_text(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tools import desktop

    def failed_capture():
        raise RuntimeError("screen grab failed")

    monkeypatch.setattr(desktop, "pyautogui", SimpleNamespace(screenshot=failed_capture))
    res = desktop_screenshot({"out_dir": str(tmp_path)})
    assert isinstance(res, str)
    assert "[桌面截图失败]" in res
    assert not list(tmp_path.glob("desktop_shot_*.jpg"))


def test_desktop_list_windows():
    res = desktop_list_windows({})
    assert "[当前桌面窗口列表]" in res or "[桌面窗口]" in res


def test_desktop_hotkey_empty():
    res = desktop_hotkey({"keys": []})
    assert "失败" in res or "不能为空" in res


def test_desktop_humanize_approval():
    act, sum_cn, det_cn, intent = humanize_approval("desktop_open_app", {"app_name": "notepad"})
    assert "桌面" in act
    assert "notepad" in sum_cn
    assert "启动或切换" in intent

    act, sum_cn, det_cn, intent = humanize_approval("desktop_click", {"x": 100, "y": 200})
    assert "100" in sum_cn and "200" in sum_cn
    assert "点击" in intent


def test_desktop_click_scale_conversion(monkeypatch):
    from unittest.mock import MagicMock
    from tools import desktop

    mock_pyautogui = fake_gui()
    monkeypatch.setattr(desktop, "pyautogui", mock_pyautogui)

    # 4K 屏缩放至 1568 后 scale 约为 0.4083，传入截图坐标 (640, 360)
    shot = desktop.desktop_screenshot({})
    shot_id = shot.metadata["desktop_observation"]["snapshot_id"]
    res = desktop.desktop_click({"snapshot_id": shot_id, "x": 640, "y": 360})
    assert "物理屏幕坐标: (784, 441)" in res.content
    mock_pyautogui.click.assert_called_with(784, 441)


def test_desktop_click_double_click(monkeypatch):
    from unittest.mock import MagicMock
    from tools import desktop

    mock_pyautogui = fake_gui()
    monkeypatch.setattr(desktop, "pyautogui", mock_pyautogui)

    # 1. clicks=2
    shot_id = desktop.desktop_screenshot({}).metadata["desktop_observation"]["snapshot_id"]
    res = desktop.desktop_click({"snapshot_id": shot_id, "x": 100, "y": 200, "clicks": 2})
    assert "[桌面双击已发送]" in res.content
    mock_pyautogui.doubleClick.assert_called_with(122, 245)

    # 2. double_click=True
    shot_id = res.metadata["desktop_observation"]["snapshot_id"]
    res2 = desktop.desktop_click({"snapshot_id": shot_id, "x": 300, "y": 400, "double_click": True})
    assert "[桌面双击已发送]" in res2.content
    mock_pyautogui.doubleClick.assert_called_with(367, 490)


def test_desktop_show_desktop(monkeypatch):
    from unittest.mock import MagicMock
    from tools import desktop

    mock_pyautogui = fake_gui()
    monkeypatch.setattr(desktop, "pyautogui", mock_pyautogui)

    res = desktop.desktop_show_desktop({})
    assert "[已显示桌面]" in res
    mock_pyautogui.hotkey.assert_called_with("win", "d")


def test_desktop_list_shortcuts():
    from tools import desktop
    res = desktop.desktop_list_shortcuts({})
    assert "[当前桌面内容列表" in res or "[桌面内容]" in res


def test_desktop_list_items():
    from tools import desktop
    res = desktop.desktop_list_items({})
    assert "[当前桌面内容列表" in res or "[桌面内容]" in res


def test_desktop_open_app_shortcut(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    from tools import desktop

    # 模拟桌面快捷方式
    mock_startfile = MagicMock()
    monkeypatch.setattr(desktop.os, "startfile", mock_startfile)
    mock_items = [{"name": "Clash Verge", "type": "shortcut", "path": tmp_path / "Clash Verge.lnk", "location": "desktop"}]
    monkeypatch.setattr(desktop, "find_desktop_items", lambda: mock_items)

    res = desktop.desktop_open_app({"app_name": "Clash Verge"})
    assert "[已成功打开]" in res
    assert "已请求最大化，截图前将验证" in res
    mock_startfile.assert_called_once_with(str(tmp_path / "Clash Verge.lnk"), show_cmd=3)


def test_desktop_open_app_folder(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    from tools import desktop

    # 模拟桌面真实存在的文件夹 (如 ffmeng)
    mock_startfile = MagicMock()
    monkeypatch.setattr(desktop.os, "startfile", mock_startfile)
    folder_path = tmp_path / "ffmeng"
    mock_items = [{"name": "ffmeng", "type": "folder", "path": folder_path, "location": "desktop"}]
    monkeypatch.setattr(desktop, "find_desktop_items", lambda: mock_items)

    res = desktop.desktop_open_app({"app_name": "ffmeng"})
    assert "[已成功打开]" in res
    assert "文件夹" in res
    assert "已请求最大化，截图前将验证" in res
    mock_startfile.assert_called_once_with(str(folder_path), show_cmd=3)


def test_desktop_type_clipboard_restore(monkeypatch):
    from unittest.mock import MagicMock
    from tools import desktop

    mock_pyautogui = fake_gui()
    mock_pyperclip = MagicMock()
    mock_pyperclip.paste.return_value = "用户原有的剪贴板内容"
    monkeypatch.setattr(desktop, "pyautogui", mock_pyautogui)
    monkeypatch.setattr(desktop, "pyperclip", mock_pyperclip)

    shot_id = desktop.desktop_screenshot({}).metadata["desktop_observation"]["snapshot_id"]
    desktop.desktop_click({"snapshot_id": shot_id, "x": 500, "y": 500})
    res = desktop.desktop_type({"text": "Hello Agent", "press_enter": True})
    assert "[桌面按键已发送] enter" in res.content
    mock_pyperclip.copy.assert_any_call("Hello Agent")
    assert mock_pyperclip.copy.call_args[0][0] == "用户原有的剪贴板内容"



