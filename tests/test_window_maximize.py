"""Window sizing is verified with fake native/CDP backends, never a real desktop."""
from copy import deepcopy
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image
import pytest

from agent.models import Message
from tools import desktop
from tools.desktop_runtime import DesktopState, begin_turn, current_state, end_turn
from tools.browser.worker import BrowserWorker


NORMAL = {"left": 100, "top": 100, "right": 1100, "bottom": 800}
MAXIMIZED = {"left": -8, "top": -8, "right": 1928, "bottom": 1048}
WORK = {"left": 0, "top": 0, "right": 1920, "bottom": 1040}


@pytest.fixture
def fake_desktop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    token = begin_turn("maximize-" + str(tmp_path))
    state = current_state()
    state.invalidate()
    events = []
    info = {1: {"hwnd": 1, "rect": deepcopy(NORMAL), "work_area": deepcopy(WORK),
                "maximizable": True, "maximized": False, "minimized": False, "foreground": True},
            2: {"hwnd": 2, "rect": deepcopy(NORMAL), "work_area": deepcopy(WORK),
                "maximizable": True, "maximized": False, "minimized": False, "foreground": False}}
    def window(hwnd, title):
        def activate():
            events.append(("activate", hwnd))
            for value in info.values():
                value["foreground"] = value["hwnd"] == hwnd
        return SimpleNamespace(_hWnd=hwnd, title=title, activate=activate)
    windows = [window(1, "Target"), window(2, "Unrelated")]
    monkeypatch.setitem(sys.modules, "pygetwindow", SimpleNamespace(getAllWindows=lambda: windows))
    def native(hwnd):
        events.append(("geometry", hwnd))
        return deepcopy(info[hwnd])
    def show(hwnd, command):
        events.append(("show", hwnd, command))
        if command == 3:
            info[hwnd].update(rect=deepcopy(MAXIMIZED), maximized=True, minimized=False)
        elif command == 9:
            info[hwnd]["minimized"] = False
    controls = [{"control_ref": "uia:" + str(index), "name": "node " + str(index),
                 "control_type": "Document", "rect": {"left": 200, "top": 300, "right": 400, "bottom": 500},
                 "enabled": True, "focusable": False, "is_password": False,
                 "has_keyboard_focus": False} for index in range(200)]
    controls[198].update(control_type="Edit", has_keyboard_focus=True, focusable=True, patterns={"Value": True})
    controls[199].update(control_type="Button", name="Send", focusable=True)
    def probe(**kwargs):
        events.append(("probe", 1))
        return {"ok": True, "supported": True, "window": {"hwnd": 1, "title": "Target",
                "foreground": info[1]["foreground"], "rect": deepcopy(info[1]["rect"])},
                "controls": deepcopy(controls)}
    gui = MagicMock()
    def screenshot():
        events.append(("pixels", 1))
        return Image.new("RGB", (1920, 1080), "white")
    gui.screenshot.side_effect = screenshot
    monkeypatch.setattr(desktop, "_native_window_info", native)
    monkeypatch.setattr(desktop, "_show_target_window", show)
    monkeypatch.setattr(desktop, "_probe", probe)
    monkeypatch.setattr(desktop, "pyautogui", gui)
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    yield SimpleNamespace(state=state, info=info, windows=windows, events=events,
                          controls=controls, gui=gui, show=show, window=window)
    end_turn(token)


def test_only_target_maximized_before_controls_and_pixels_and_projection_is_bounded(fake_desktop):
    fake = fake_desktop
    unrelated = deepcopy(fake.info[2])
    result = desktop.desktop_snapshot({"window_title": "Target"})
    assert isinstance(result, Message)
    assert fake.info[2] == unrelated
    assert fake.events.index(("show", 1, 3)) < fake.events.index(("probe", 1)) < fake.events.index(("pixels", 1))
    observation = result.metadata["desktop_observation"]
    assert observation["window_layout"]["geometry_verified"]
    assert observation["window"]["rect"] == MAXIMIZED
    assert len(fake.state.snapshot["controls"]) == 200
    assert observation["controls_total"] == 200 and observation["controls_shown"] == 64
    assert observation["controls"][0]["control_ref"] == "uia:198"
    assert observation["controls"][0]["patterns"] == {"Value": True}
    assert observation["controls"][1]["control_ref"] == "uia:199"
    assert observation["controls"][1]["rect"] == fake.controls[199]["rect"]
    raw_bytes = len(json.dumps(fake.controls, ensure_ascii=False).encode())
    model_bytes = len(json.dumps(observation, ensure_ascii=False, separators=(",", ":")).encode())
    assert model_bytes < raw_bytes * .4
    # Hidden projected nodes remain available to the actual evidence validator.
    assert fake.state.click_point({"snapshot_id": observation["snapshot_id"], "control_ref": "uia:197"}) == (300, 400)


def test_fixed_size_dialog_is_observed_without_forcing_maximize(fake_desktop):
    fake = fake_desktop
    fake.info[1]["maximizable"] = False
    result = desktop.desktop_snapshot({"window_title": "Target"})
    assert isinstance(result, Message)
    assert result.metadata["desktop_observation"]["window_layout"]["status"] == "fixed_size"
    assert not any(event[0] == "show" for event in fake.events)
    assert fake.info[1]["rect"] == NORMAL


def test_minimized_fixed_dialog_is_restored_without_resizing(fake_desktop):
    fake = fake_desktop
    fake.info[1].update(maximizable=False, minimized=True)
    result = desktop.desktop_snapshot({"window_title": "Target"})
    assert isinstance(result, Message)
    assert ("show", 1, 9) in fake.events and ("show", 1, 3) not in fake.events
    assert fake.info[1]["rect"] == NORMAL


def test_ambiguous_title_never_changes_any_window(fake_desktop):
    fake = fake_desktop
    fake.windows.append(fake.window(2, "Target"))
    assert desktop._maximize_target_window(window_title="Target")["status"] == "unresolved"
    assert fake.events == []


def test_untargeted_overview_does_not_resize_foreground_window(fake_desktop):
    fake = fake_desktop
    result = desktop.desktop_screenshot({})
    assert isinstance(result, Message)
    assert result.metadata["desktop_observation"]["window_layout"]["status"] == "untargeted"
    assert "未指定目标窗口，本次未请求最大化" in result.content
    assert "请指定 window_title" in result.content
    assert not any(event[0] in {"show", "activate"} for event in fake.events)


def test_failed_maximize_cannot_claim_success_or_keep_old_coordinates(fake_desktop, monkeypatch):
    fake = fake_desktop
    fake.state.observe({"hwnd": 1, "rect": NORMAL}, (1920, 1080), (1568, 882))
    monkeypatch.setattr(desktop, "_show_target_window", lambda hwnd, command: None)
    result = desktop.desktop_snapshot({"window_title": "Target"})
    assert isinstance(result, str) and "失败" in result
    assert not fake.state.snapshot
    fake.gui.screenshot.assert_not_called()


def test_geometry_change_during_capture_rejects_evidence(fake_desktop):
    fake = fake_desktop
    def moved_during_pixels():
        fake.info[1]["rect"]["right"] -= 100
        return Image.new("RGB", (1920, 1080), "white")
    fake.gui.screenshot.side_effect = moved_during_pixels
    result = desktop.desktop_snapshot({"window_title": "Target"})
    assert isinstance(result, str) and "尺寸变化" in result
    assert not fake.state.snapshot


def test_old_snapshot_action_is_rejected_before_any_automatic_resize(fake_desktop):
    fake = fake_desktop
    result = desktop.desktop_snapshot({"window_title": "Target"})
    old_id = result.metadata["desktop_observation"]["snapshot_id"]
    fake.info[1].update(rect=deepcopy(NORMAL), maximized=False)
    before = list(fake.events)
    rejected = desktop.desktop_click({"snapshot_id": old_id, "x": 300, "y": 300})
    assert isinstance(rejected, str) and "失败" in rejected
    fake.gui.click.assert_not_called()
    assert not any(event[0] == "show" for event in fake.events[len(before):])
    fresh = desktop.desktop_snapshot({"window_title": "Target"})
    assert fresh.metadata["desktop_observation"]["snapshot_id"] != old_id


def test_geometry_change_clears_preserved_input_and_click_evidence():
    state = DesktopState()
    state.observe({"hwnd": 1, "rect": NORMAL}, (1920, 1080), (1568, 882))
    state.focus = {"control_ref": "uia:old"}
    state.typed_hash, state.typed_control = "typed", "uia:old"
    state.clicked_point, state.expected_control = (300, 400), "uia:old"
    state.observe({"hwnd": 1, "rect": MAXIMIZED}, (1920, 1080), (1568, 882), preserve_input=True)
    assert not state.focus and not state.typed_hash and not state.typed_control
    assert state.clicked_point is None and not state.expected_control


def test_reused_open_app_maximizes_only_uniquely_matched_window(fake_desktop):
    fake = fake_desktop
    result = desktop.desktop_open_app({"app_name": "Target"})
    assert "已激活" in result and "最大化并验证" in result
    assert ("show", 1, 3) in fake.events
    assert not any(event[0] == "show" and event[1] == 2 for event in fake.events)


def test_ambiguous_open_app_does_not_activate_or_launch(fake_desktop, monkeypatch):
    fake = fake_desktop
    fake.windows[:] = [fake.window(1, "Target document 1"), fake.window(2, "Target document 2")]
    launch = MagicMock()
    monkeypatch.setattr(desktop.os, "startfile", launch)
    result = desktop.desktop_open_app({"app_name": "Target"})
    assert "匹配多个" in result and fake.events == []
    launch.assert_not_called()


def test_shell_open_requests_maximized_start(fake_desktop, monkeypatch):
    fake_desktop.windows.clear()
    monkeypatch.setattr(desktop, "find_desktop_items", lambda: [])
    launch = MagicMock()
    monkeypatch.setattr(desktop.subprocess, "Popen", launch)
    result = desktop.desktop_open_app({"app_name": "offline-nonexistent-app"})
    assert "已通过系统命令启动" in result
    assert "已请求最大化，截图前将验证" in result and "最大化并验证" not in result
    assert launch.call_args.args[0] == 'start "" /max "offline-nonexistent-app"'


def test_control_projection_keeps_disabled_password_and_unknown_flags():
    raw = [{"control_ref": "password", "name": "", "control_type": "Edit", "rect": NORMAL,
            "enabled": False, "is_password": True},
           {"control_ref": "unknown", "name": "x", "control_type": "Button", "rect": NORMAL}]
    projected, defaults = desktop._controls_for_model(raw)
    assert projected[0]["enabled"] is False and projected[0]["is_password"] is True
    assert projected[1]["enabled"] is None
    assert defaults["is_password"] is False
    assert raw[1].get("enabled") is None


def test_action_button_is_not_displaced_by_large_named_sidebar():
    controls = [{"control_ref": "sidebar:" + str(index), "name": "History " + str(index),
                 "control_type": "Button", "rect": NORMAL, "enabled": True,
                 "focusable": True, "is_password": False, "has_keyboard_focus": False}
                for index in range(200)]
    controls[-1].update(control_ref="send", name="Send")
    selected, _ = desktop._controls_for_model(controls)
    assert selected[0]["control_ref"] == "send" and len(selected) == 64


def test_maximized_state_alone_does_not_validate_oversized_or_invalid_geometry():
    from tools.desktop_runtime import maximized_geometry_verified
    assert not maximized_geometry_verified({"maximized": True, "rect": NORMAL, "work_area": WORK})
    oversized = {"left": -5000, "top": -5000, "right": 5000, "bottom": 5000}
    assert not maximized_geometry_verified({"maximized": True, "rect": oversized, "work_area": WORK})
    assert maximized_geometry_verified({"maximized": True, "rect": MAXIMIZED, "work_area": WORK})


@pytest.fixture
def fake_browser(monkeypatch, tmp_path):
    events = []
    box = {"state": "normal", "resize_works": True}
    class Session:
        def send(self, method, params=None):
            events.append((method, deepcopy(params)))
            if method == "Browser.getWindowForTarget":
                return {"windowId": 42, "bounds": bounds()}
            if method == "Browser.getWindowBounds":
                return {"bounds": bounds()}
            if method == "Browser.setWindowBounds" and box["resize_works"]:
                assert params["windowId"] == 42
                box["state"] = params["bounds"]["windowState"]
            if method == "Accessibility.getFullAXTree":
                return {"nodes": [{"role": {"value": "button"}, "name": {"value": "Send"}, "nodeId": 1}]}
            return {}
        def detach(self):
            events.append(("detach", None))
    def bounds():
        return {"windowState": box["state"], "left": 0, "top": 0, "width": 1920, "height": 1040}
    page = MagicMock()
    page.url, page.title.return_value, page.is_closed.return_value = "about:blank", "Offline", False
    page.context.new_cdp_session.side_effect = lambda target: Session()
    browser = SimpleNamespace(pages=[page], new_cdp_session=page.context.new_cdp_session,
                              close=lambda: None, add_init_script=lambda script: None)
    worker = BrowserWorker(SimpleNamespace(is_automation_allowed=lambda name: True,
                                          user_data_dir=lambda name: tmp_path),
                           SimpleNamespace(check_before_action=lambda name: None), "offline", headless=False)
    worker._browser, worker._raw_page = browser, page
    worker._human_delay = lambda: None
    monkeypatch.setattr("tools.browser.worker.time.sleep", lambda _: None)
    yield SimpleNamespace(worker=worker, browser=browser, page=page, box=box, events=events)
    worker._executor.shutdown(wait=True, cancel_futures=True)


def test_browser_maximizes_only_current_target_and_verifies_stable_bounds(fake_browser):
    fake = fake_browser
    result = fake.worker._ensure_maximized()
    assert result["geometry_verified"] and result["window_id"] == 42
    changes = [params for method, params in fake.events if method == "Browser.setWindowBounds"]
    assert changes == [{"windowId": 42, "bounds": {"windowState": "maximized"}}]
    assert fake.events[-1][0] == "detach"


@pytest.mark.parametrize("state", ["minimized", "fullscreen"])
def test_browser_restores_before_maximizing(fake_browser, state):
    fake = fake_browser
    fake.box["state"] = state
    assert fake.worker._ensure_maximized()["geometry_verified"]
    assert [params["bounds"]["windowState"] for method, params in fake.events
            if method == "Browser.setWindowBounds"] == ["normal", "maximized"]


def test_browser_headless_does_not_change_real_windows(fake_browser):
    fake = fake_browser
    fake.worker.headless = True
    assert fake.worker._ensure_maximized()["status"] == "headless"
    assert fake.events == []


def test_reused_browser_is_maximized_before_accessibility_observation(fake_browser):
    fake = fake_browser
    result = fake.worker._do_snapshot({})
    assert "窗口已最大化并验证" in result and "Send" in result
    methods = [method for method, _ in fake.events]
    assert methods.index("Browser.setWindowBounds") < methods.index("Accessibility.getFullAXTree")
    fake.box["state"] = "normal"
    fake.events.clear()
    fake.worker._do_snapshot({})
    assert any(method == "Browser.setWindowBounds" for method, _ in fake.events)


def test_browser_screenshot_prepares_window_before_pixels(fake_browser, tmp_path):
    fake = fake_browser
    image = io.BytesIO()
    Image.new("RGB", (1920, 1080), "white").save(image, "PNG")
    def capture():
        fake.events.append(("pixels", None))
        return image.getvalue()
    fake.page.screenshot.side_effect = capture
    result = fake.worker._do_screenshot({"out_dir": str(tmp_path)})
    # §39.6：截图返回带 images 的 Message（不再是裸路径字符串）
    assert isinstance(result, Message)
    assert "窗口已最大化并验证" in result.content
    assert result.images and result.images[0].data
    methods = [method for method, _ in fake.events]
    assert methods.index("Browser.setWindowBounds") < methods.index("pixels")


def test_browser_actions_do_not_silently_resize_existing_observation(fake_browser):
    fake = fake_browser
    maximize = MagicMock()
    fake.worker._ensure_maximized = maximize
    assert "已点击" in fake.worker._do_click({"selector": "#send"})
    assert "已输入" in fake.worker._do_type_text({"selector": "#editor", "text": "offline"})
    maximize.assert_not_called()


def test_browser_failed_maximize_is_reported_without_false_verified_status(fake_browser):
    fake = fake_browser
    fake.box["resize_works"] = False
    result = fake.worker._do_snapshot({})
    assert "窗口最大化尚未验证" in result
    assert fake.worker._window_layout["geometry_verified"] is False


@pytest.mark.parametrize("headless", [False, True])
def test_browser_launch_uses_real_viewport_only_when_headed(fake_browser, monkeypatch, headless):
    import playwright.sync_api
    fake = fake_browser
    launches = []
    def launch(path, **kwargs):
        launches.append(kwargs)
        return fake.browser
    pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: pw))
    fake.worker.headless = headless
    fake.worker._do_start()
    assert "--start-maximized" in launches[0]["args"]
    assert launches[0].get("no_viewport", False) is (not headless)
