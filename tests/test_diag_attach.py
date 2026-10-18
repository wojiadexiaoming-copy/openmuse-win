"""诊断监听挂载测试：所有 page 赋值路径都走 setter 自动挂载。"""


class FakePage:
    def __init__(self, url="https://example.com/"):
        self.url = url
        self._handlers = {}

    def on(self, event, handler):
        self._handlers[event] = handler

    def is_closed(self):
        return False

    def title(self):
        return "Test"

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        return None


def make_worker():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._contexts = {"default": {"context": None, "tabs": {}}}
    w._active_context = "default"
    w._url_state = {}
    w._layout_note = lambda: ""
    w._norm_url = lambda u: u
    w._ensure_maximized = lambda: None
    return w


def test_setter_attaches_diagnostics():
    w = make_worker()
    page = FakePage()
    w._page = page  # 走 setter
    assert "console" in page._handlers
    assert "pageerror" in page._handlers
    assert "request" in page._handlers
    assert "response" in page._handlers
    assert "dialog" in page._handlers
    assert "popup" in page._handlers


def test_new_tab_attaches_diagnostics():
    w = make_worker()

    class FakeBrowser:
        def new_page(self):
            return FakePage("https://newsite.com/")
    w._browser = FakeBrowser()
    out = w._do_new_tab({"role": "test", "url": "https://newsite.com/"})
    assert "[标签页已开]" in out
    page = w._tabs["test"]["page"]
    assert "console" in page._handlers, "新 tab 必须自动挂诊断监听"
    assert "request" in page._handlers


def test_switch_tab_attaches_diagnostics():
    w = make_worker()
    page1 = FakePage("https://a.com/")
    page2 = FakePage("https://b.com/")
    # page2 之前没挂过监听（模拟旧 tab）
    assert "console" not in page2._handlers
    w._tabs["a"] = {"page": page1, "url_prefix": "https://a.com",
                    "title_kw": "", "pinned": False, "last_used": 0}
    w._tabs["b"] = {"page": page2, "url_prefix": "https://b.com",
                    "title_kw": "", "pinned": False, "last_used": 0}
    out = w._do_switch_tab({"role": "b"})
    assert "[已切换]" in out
    assert "console" in page2._handlers, "切换到的 tab 必须补挂诊断监听"


def test_console_captures_on_new_tab():
    """端到端：新 tab 的 console 错误能被捕获。"""
    w = make_worker()

    class FakeBrowser:
        def new_page(self):
            return FakePage("https://newsite.com/")
    w._browser = FakeBrowser()
    w._do_new_tab({"role": "test", "url": "https://newsite.com/"})
    page = w._tabs["test"]["page"]

    class FakeMsg:
        type = "error"
        text = "Something broke"
    page._handlers["console"](FakeMsg())
    out = w._do_console_log({"type": "error"})
    assert "Something broke" in out
