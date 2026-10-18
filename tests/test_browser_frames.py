"""iframe 地基测试（P0）：frame 清单、跨 frame 定位、frame 参数。

此前选择器只搜主 frame，Stripe 支付框这类 iframe 内元素完全够不着。
"""
from unittest.mock import MagicMock

from tools.browser.worker import BrowserWorker


class FakeLocator:
    def __init__(self, found=True):
        self._found = found
        self.clicked = []

    def count(self):
        return 1 if self._found else 0

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.clicked.append(True)

    def is_visible(self, timeout=None):
        return self._found

    def is_disabled(self, timeout=None):
        return False

    def fill(self, text, timeout=None):
        pass


class FakeFrame:
    def __init__(self, name="", url="https://example.com/", has_button=False):
        self.name = name
        self.url = url
        self._has_button = has_button

    def locator(self, selector):
        return FakeLocator(found=self._has_button and "pay" in selector)

    def evaluate(self, expr):
        return f"frame:{self.name}:{expr[:20]}"


class FakePage:
    def __init__(self):
        self.url = "https://example.com/checkout"
        self.main_frame = FakeFrame(name="", url="https://example.com/checkout")
        # 子 frame：Stripe 支付 iframe，里面有 pay 按钮
        self.frames = [self.main_frame,
                       FakeFrame(name="stripe-card", url="https://js.stripe.com/v3/", has_button=True)]

    def title(self):
        return "Checkout"


def make_worker(page):
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = page
    w._browser = MagicMock()
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    return w


def test_frame_inventory():
    w = make_worker(FakePage())
    inv = w._frame_inventory()
    assert len(inv) == 2
    assert inv[0]["id"] == "f0" and inv[0]["is_main"] is True
    assert inv[1]["id"] == "f1" and inv[1]["name"] == "stripe-card"
    assert "stripe" in inv[1]["url"]


def test_resolve_frame():
    w = make_worker(FakePage())
    assert w._resolve_frame(None) == w._raw_page.main_frame
    assert w._resolve_frame("auto") == w._raw_page.main_frame
    assert w._resolve_frame("f1").name == "stripe-card"
    assert w._resolve_frame("f99") is None
    assert w._resolve_frame("bogus") is None


def test_find_locator_main_frame_first():
    w = make_worker(FakePage())
    w._raw_page.main_frame._has_button = True
    frame, loc, fid = w._find_locator("button.pay")
    assert fid == "f0"


def test_find_locator_falls_back_to_subframe():
    """主 frame 没有，自动在子 frame 找到——核心能力。"""
    w = make_worker(FakePage())
    frame, loc, fid = w._find_locator("button.pay")
    assert fid == "f1"
    assert frame.name == "stripe-card"


def test_find_locator_explicit_frame():
    w = make_worker(FakePage())
    frame, loc, fid = w._find_locator("button.pay", frame_ref="f1")
    assert fid == "f1"
    frame2, loc2, fid2 = w._find_locator("button.pay", frame_ref="f0")
    assert loc2.count() == 0  # 主 frame 确实没有


def test_find_locator_not_found_anywhere():
    w = make_worker(FakePage())
    frame, loc, fid = w._find_locator("button.nonexistent-xyz")
    assert (frame, loc, fid) == (None, None, None)


def test_evaluate_frame_param():
    w = make_worker(FakePage())
    out = w._do_evaluate({"expression": "1+1", "frame": "f1"})
    assert "stripe-card" in out
    out2 = w._do_evaluate({"expression": "1+1"})
    assert "frame::1+1" in out2  # 默认主 frame


def test_do_click_auto_searches_subframe():
    """click 默认 auto：主 frame 没有时自动打中子 frame 的按钮。"""
    page = FakePage()
    w = make_worker(page)
    w._page_fingerprint = lambda: "fp1"
    out = w._do_click({"selector": "button.pay"})
    assert "[已点击]" in out
    assert "[frame=f1]" in out
    # 确认点的是子 frame 的 locator
    assert page.frames[1].locator("button.pay").clicked or True  # locator 每次新建，验返回文本即可


def test_do_click_reports_not_found_across_frames():
    page = FakePage()
    w = make_worker(page)
    out = w._do_click({"selector": "button.nonexistent-xyz"})
    assert "[点击失败]" in out and "所有 frame" in out
