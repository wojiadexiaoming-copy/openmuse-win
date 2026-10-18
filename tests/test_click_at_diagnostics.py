"""_do_click_at run5 升级测试：
1. 落点元素探针（elementFromPoint）——红圈正中但按钮没反应时，必须知道打中了谁；
2. 零接口请求提示——点击后无任何 XHR/fetch 是"按钮没响应"的关键信号；
3. 红圈图落盘——复盘时必须看得见（run5 教训：只进消息 images 没落盘）。
"""
import io
from unittest.mock import MagicMock

import pytest

from tools.browser.worker import BrowserWorker


def _png_bytes(w=200, h=100):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


class FakePage:
    def __init__(self, hit_element="svg.send-icon", dom_version=1):
        self.url = "https://platform.agnes-ai.com/login"
        self.clicked = []
        self.dom_version = dom_version
        self.change_on_click = True
        self._hit_element = hit_element
        self._response_handlers = []
        self.viewport_size = {"width": 1000, "height": 800}

    def evaluate(self, js, *args):
        if "elementFromPoint" in js:
            return self._hit_element
        return f"Login|{100 + self.dom_version}|{self.dom_version}"

    def screenshot(self):
        return _png_bytes()

    def on(self, event, handler):
        if event == "response":
            self._response_handlers.append(handler)

    def remove_listener(self, event, handler):
        if handler in self._response_handlers:
            self._response_handlers.remove(handler)

    @property
    def mouse(self):
        page = self

        class Mouse:
            def click(self, x, y):
                page.clicked.append((round(x, 1), round(y, 1)))
                if page.change_on_click:
                    page.dom_version += 1

        return Mouse()


def make_worker(page):
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = page
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    return w


def test_click_at_reports_hit_element():
    """落点元素进返回文本：知道点击到底被谁吃掉了。"""
    w = make_worker(FakePage(hit_element="svg.send-icon"))
    out = w._do_click_at({"x": 357, "y": 469})
    text = out if isinstance(out, str) else out.content  # Message.content 是 str
    assert "[落点元素] svg.send-icon" in text


def test_click_at_warns_when_no_network_activity():
    """指纹无变化 + 零接口请求 → 明确说按钮可能没响应。"""
    page = FakePage()
    page.change_on_click = False  # 指纹不变
    w = make_worker(page)
    out = w._do_click_at({"x": 357, "y": 469})
    text = out if isinstance(out, str) else out.content
    assert "未捕获到任何 XHR/fetch 请求" in text
    assert "没响应" in text


def test_marked_click_shot_saved_to_disk(tmp_path, monkeypatch):
    """红圈图落盘：复盘证据不能只活在消息里。"""
    monkeypatch.chdir(tmp_path)
    w = make_worker(FakePage())
    jpeg = w._marked_click_shot(357.0, 469.0)
    assert jpeg and jpeg[:2] == b"\xff\xd8"
    shots = list((tmp_path / "shots").glob("click_at_*.jpg"))
    assert len(shots) == 1
    assert shots[0].read_bytes()[:2] == b"\xff\xd8"


def test_element_at_point_fallback_on_error():
    page = FakePage()
    page.evaluate = MagicMock(side_effect=RuntimeError("boom"))
    w = make_worker(page)
    assert w._element_at_point(1, 2) == "unknown"
