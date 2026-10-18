"""Tests for window split layout and split_screen mode in BrowserWorker."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from ui.window_layout import get_work_area, get_split_layout
from tools.browser.worker import BrowserWorker


def test_get_work_area_returns_valid_rect():
    wa = get_work_area()
    assert isinstance(wa, dict)
    assert wa["right"] > wa["left"]
    assert wa["bottom"] > wa["top"]


def test_get_split_layout_70_30_exact():
    fake_wa = {"left": 0, "top": 0, "right": 1920, "bottom": 1000}
    layout = get_split_layout(browser_ratio=0.70, work_area=fake_wa)
    b = layout["browser"]
    u = layout["ui"]
    assert b["x"] == 0
    assert b["y"] == 0
    assert b["width"] == 1344
    assert b["height"] == 1000

    assert u["x"] == 1344
    assert u["y"] == 0
    assert u["width"] == 576
    assert u["height"] == 1000

    assert b["width"] + u["width"] == 1920
    assert layout["browser_ratio"] == 0.70


def test_get_split_layout_ratio_clamping():
    fake_wa = {"left": 100, "top": 50, "right": 1100, "bottom": 850}
    # clamped to 0.85
    layout_high = get_split_layout(browser_ratio=0.99, work_area=fake_wa)
    assert layout_high["browser_ratio"] == 0.85
    assert layout_high["browser"]["width"] == int(1000 * 0.85)

    # clamped to 0.40
    layout_low = get_split_layout(browser_ratio=0.10, work_area=fake_wa)
    assert layout_low["browser_ratio"] == 0.40
    assert layout_low["browser"]["width"] == int(1000 * 0.40)


def test_browser_worker_split_screen_initialization(monkeypatch, tmp_path):
    events = []
    box = {"state": "normal", "left": 0, "top": 0, "width": 1344, "height": 918}

    class Session:
        def send(self, method, params=None):
            events.append((method, deepcopy(params)))
            if method == "Browser.getWindowForTarget":
                return {"windowId": 99, "bounds": {"windowState": box["state"], **box}}
            if method == "Browser.getWindowBounds":
                return {"bounds": {"windowState": box["state"], **box}}
            if method == "Browser.setWindowBounds":
                assert params["windowId"] == 99
                b = params["bounds"]
                if "windowState" in b:
                    box["state"] = b["windowState"]
                for k in ("left", "top", "width", "height"):
                    if k in b:
                        box[k] = b[k]
                return {}
            return {}

        def detach(self):
            events.append(("detach", None))

    page = MagicMock()
    page.url, page.title.return_value, page.is_closed.return_value = "about:blank", "Offline", False
    page.context.new_cdp_session.side_effect = lambda target: Session()
    browser = SimpleNamespace(pages=[page], new_cdp_session=page.context.new_cdp_session,
                              close=lambda: None, add_init_script=lambda script: None)
    worker = BrowserWorker(
        SimpleNamespace(is_automation_allowed=lambda name: True, user_data_dir=lambda name: tmp_path),
        SimpleNamespace(check_before_action=lambda name: None),
        "offline",
        headless=False,
        split_screen=True,
        browser_ratio=0.70,
    )
    worker._browser, worker._raw_page = browser, page
    worker._human_delay = lambda: None
    monkeypatch.setattr("tools.browser.worker.time.sleep", lambda _: None)

    res = worker._ensure_maximized()
    assert res["geometry_verified"] is True
    assert res["status"] == "split_screen"
    assert "窗口分屏已就绪 (70%)" in worker._layout_note()

    # User manually resized the browser window:
    box["width"] = 1200
    box["height"] = 800
    res2 = worker._ensure_maximized()
    assert res2["geometry_verified"] is True
    # Worker must NOT revert the user's manual resize back to 1344
    assert res2["bounds"]["width"] == 1200
    assert res2["bounds"]["height"] == 800

    # Test dynamic set_split_ratio to 80% (when right drawer is closed)
    b_geo_80 = worker.set_split_ratio(0.80)
    assert worker.browser_ratio == 0.80
    assert box["width"] == b_geo_80["width"]
    assert "窗口分屏已就绪 (80%)" in worker._layout_note()

    # Test dynamic set_split_ratio back to 70% (when right drawer is expanded)
    b_geo_70 = worker.set_split_ratio(0.70)
    assert worker.browser_ratio == 0.70
    assert box["width"] == b_geo_70["width"]
    assert "窗口分屏已就绪 (70%)" in worker._layout_note()

    worker._executor.shutdown(wait=True, cancel_futures=True)

