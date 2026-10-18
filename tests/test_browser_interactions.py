"""交互原语测试：upload / scroll / hover / select。"""


class FakeLoc:
    def __init__(self, fail_click=False):
        self._uploaded = None
        self._hovered = False
        self._selected = None
        self._scrolled = False

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def set_input_files(self, path):
        self._uploaded = path

    def scroll_into_view_if_needed(self, timeout=None):
        self._scrolled = True

    def hover(self, timeout=None):
        self._hovered = True

    def select_option(self, value=None, label=None, index=None, timeout=None):
        self._selected = value or label or index
        return [str(self._selected)]


class FakeFrame:
    def __init__(self):
        self._loc = FakeLoc()

    def locator(self, selector):
        return self._loc


class FakePage:
    def __init__(self):
        self.url = "https://example.com/form"
        self.main_frame = FakeFrame()
        self.frames = [self.main_frame]
        self._scrolled_js = None

    def evaluate(self, js):
        self._scrolled_js = js
        return None


def make_worker():
    import tempfile, os
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = FakePage()
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    # 建一个真实临时文件供上传测试
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    w._tmp = tmp
    return w


def test_upload():
    w = make_worker()
    out = w._do_upload({"selector": "input[type=file]", "path": w._tmp})
    assert "[已上传]" in out
    assert w._raw_page.main_frame._loc._uploaded == w._tmp


def test_upload_missing_file():
    w = make_worker()
    out = w._do_upload({"selector": "input[type=file]", "path": "/nope/missing.xyz"})
    assert "文件不存在" in out


def test_upload_missing_args():
    w = make_worker()
    out = w._do_upload({"selector": "input"})
    assert "缺少参数" in out


def test_scroll_direction():
    w = make_worker()
    out = w._do_scroll({"direction": "down", "amount": 800})
    assert "[已滚动] down" in out
    assert "800" in w._raw_page._scrolled_js


def test_scroll_to_top():
    w = make_worker()
    w._do_scroll({"direction": "top"})
    assert "scrollTo(0, 0)" in w._raw_page._scrolled_js


def test_scroll_to_element():
    w = make_worker()
    out = w._do_scroll({"selector": ".footer"})
    assert "滚进视口" in out
    assert w._raw_page.main_frame._loc._scrolled


def test_hover():
    w = make_worker()
    out = w._do_hover({"selector": ".menu"})
    assert "[已悬停]" in out
    assert w._raw_page.main_frame._loc._hovered


def test_select_by_value():
    w = make_worker()
    out = w._do_select({"selector": "select.country", "value": "CN"})
    assert "[已选择]" in out and "CN" in out


def test_select_by_label():
    w = make_worker()
    out = w._do_select({"selector": "select.country", "label": "中国"})
    assert "中国" in out


def test_select_no_option():
    w = make_worker()
    out = w._do_select({"selector": "select.country"})
    assert "缺少选项" in out


def test_interaction_tools_are_daily():
    from agent.risk import _DAILY_TOOLS
    for t in ("browser_upload", "browser_scroll", "browser_hover", "browser_select"):
        assert t in _DAILY_TOOLS
