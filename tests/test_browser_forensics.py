"""取证工具测试：browser_inspect + browser_resource。"""
import io
from tools.browser.worker import BrowserWorker
from agent.models import Message


class FakeEl:
    def __init__(self, png_bytes):
        self._png = png_bytes

    def screenshot(self, timeout=None):
        return self._png

    def evaluate(self, js):
        return {
            "tag": "img", "id": "", "cls": "send-btn",
            "attrs": {"alt": "can send", "src": "/send.png"},
            "text": "", "value": "",
            "box": {"x": 350, "y": 460, "w": 24, "h": 24},
            "style": {"color": "rgb(0, 0, 0)", "background": "rgba(0, 0, 0, 0)",
                      "opacity": "1", "display": "inline",
                      "visibility": "visible", "cursor": "pointer",
                      "pointerEvents": "auto"},
            "disabled": False, "ariaDisabled": "",
            "hasOnClick": True,
        }


class FakeLoc:
    def __init__(self, png_bytes):
        self._el = FakeEl(png_bytes)

    def count(self):
        return 1

    @property
    def first(self):
        return self._el


class FakeFrame:
    def __init__(self, png_bytes):
        self._png = png_bytes

    def locator(self, selector):
        return FakeLoc(self._png)


class FakePage:
    def __init__(self):
        self.url = "https://example.com/"
        self.main_frame = FakeFrame(None)
        self.frames = [self.main_frame]

    def evaluate(self, js, *a):
        if "performance.getEntriesByType" in js:
            return ["https://example.com/app.abc123.js", "https://example.com/style.css"]
        if "fetch" in js:
            url = a[0] if a else ""
            if "missing.js" in url:
                return "HTTP_ERROR:404"
            return "const API='/api/verification';\nfunction send(){fetch(API)}\n// end"
        return None


def make_worker(png_bytes=None):
    from PIL import Image
    if png_bytes is None:
        img = Image.new("RGB", (24, 24), (44, 44, 44))  # 深灰 = 活按钮
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = FakePage()
    w._raw_page.main_frame = FakeFrame(png_bytes)
    w._raw_page.frames = [w._raw_page.main_frame]
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    return w


def test_inspect_reports_state_and_color():
    w = make_worker()
    out = w._do_inspect({"selector": "img.send-btn"})
    assert isinstance(out, Message)
    assert "<img>" in out.content
    assert "#2c2c2c" in out.content  # 深灰主色
    assert "can send" in out.content
    assert out.images  # 附元素图


def test_inspect_not_found():
    w = make_worker()
    w._find_locator = lambda s, f=None: (None, None, None)
    out = w._do_inspect({"selector": ".nope"})
    assert "未找到" in out.content


def test_inspect_disabled_clues():
    w = make_worker()
    orig = FakeEl.evaluate
    def fake_eval(self, js):
        d = orig(self, js)
        d["disabled"] = True
        d["style"]["pointerEvents"] = "none"
        return d
    FakeEl.evaluate = fake_eval
    try:
        out = w._do_inspect({"selector": "button"})
    finally:
        FakeEl.evaluate = orig
    assert "已禁用" in out.content
    assert "pointer-events:none" in out.content


def test_resource_list():
    w = make_worker()
    out = w._do_resource({})
    assert "app.abc123.js" in out


def test_resource_search():
    w = make_worker()
    out = w._do_resource({"url": "https://example.com/app.js", "search": "verification"})
    assert "/api/verification" in out
    assert "第 1 行" in out


def test_resource_search_no_match():
    w = make_worker()
    out = w._do_resource({"url": "https://example.com/app.js", "search": "zzz_nope"})
    assert "无匹配" in out


def test_resource_404():
    w = make_worker()
    out = w._do_resource({"url": "https://example.com/missing.js"})
    assert "404" in out


def test_forensics_tools_are_daily():
    from agent.risk import _DAILY_TOOLS
    assert "browser_inspect" in _DAILY_TOOLS
    assert "browser_resource" in _DAILY_TOOLS
