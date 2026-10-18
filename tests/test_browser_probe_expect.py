"""P1 地基测试：browser_probe_at（点前探针）与 expect（效果核验框架）。

probe_at：run4 教训——模型目测坐标差 140px 却不自知。先探后点。
expect：run5 教训——点了纸飞机没变倒计时，模型却宣布成功。期望写进参数，
框架自动核验。两者皆通用能力，非 Agnes 专用。
"""
import io

from tools.browser.worker import BrowserWorker


def _png_bytes(w=400, h=300):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 200, 200)).save(buf, "PNG")
    return buf.getvalue()


class FakePage:
    def __init__(self, url="https://example.com/", body_text="hello", hit="button.submit"):
        self.url = url
        self.name = ""
        self._body_text = body_text
        self._hit = hit
        self.main_frame = self
        self.frames = [self]
        self.viewport_size = {"width": 1000, "height": 800}

    def evaluate(self, js, *args):
        if "elementFromPoint" in js:
            return self._hit
        if "innerText" in js:
            return self._body_text
        return ""

    def screenshot(self):
        return _png_bytes()

    def locator(self, selector):
        page = self

        class Loc:
            def count(self):
                return 1 if "gone" not in selector else 0

            @property
            def first(self):
                return self

            def is_visible(self, timeout=None):
                return "gone" not in selector

        return Loc()


def make_worker(page):
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = page
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    return w


def test_probe_at_reports_element_and_zoom():
    w = make_worker(FakePage(hit="svg.send-icon"))
    out = w._do_probe_at({"x": 357, "y": 469})
    assert not isinstance(out, str)  # 应返回带图的 Message
    assert "svg.send-icon" in out.content
    assert "357" in out.content and "469" in out.content
    assert len(out.images) == 1


def test_probe_at_rejects_bad_coords():
    w = make_worker(FakePage())
    assert "0~1000" in w._do_probe_at({"x": 1500, "y": 10})
    assert "x/y" in w._do_probe_at({"x": "abc", "y": 10})


def test_probe_at_zoom_clamped():
    w = make_worker(FakePage())
    out = w._do_probe_at({"x": 500, "y": 500, "zoom": 99})
    assert "放大 5 倍" in out.content


def test_verify_expect_all_pass():
    w = make_worker(FakePage(url="https://example.com/dashboard", body_text="欢迎回来"))
    out = w._verify_expect({"url_contains": "/dashboard", "text_visible": "欢迎"}, timeout=1)
    assert "[期望核验通过]" in out


def test_verify_expect_failure_reported():
    w = make_worker(FakePage(url="https://example.com/login", body_text="请登录"))
    out = w._verify_expect({"url_contains": "/dashboard", "text_visible": "欢迎"}, timeout=1)
    assert "[期望核验未通过]" in out
    assert "url_contains(/dashboard): 未通过" in out


def test_verify_expect_selector_conditions():
    w = make_worker(FakePage())
    out = w._verify_expect({"selector_visible": "button.submit",
                            "selector_hidden": "div.gone-xyz"}, timeout=1)
    assert "[期望核验通过]" in out


def test_verify_expect_empty_is_noop():
    w = make_worker(FakePage())
    assert w._verify_expect({}) == ""
    assert w._verify_expect(None) == ""


def test_probe_at_is_daily_tool():
    from agent.risk import _DAILY_TOOLS
    assert "browser_probe_at" in _DAILY_TOOLS


def test_verify_expect_unknown_condition_reported():
    w = make_worker(FakePage())
    out = w._verify_expect({"url_contain": "/x"}, timeout=1)  # 拼写错误
    assert "未知条件" in out
    assert "[期望核验未通过]" in out
