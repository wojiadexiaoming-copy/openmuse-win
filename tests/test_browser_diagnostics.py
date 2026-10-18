"""诊断地基测试：console 捕获 + 网络日志 + 失败时自动附带。

run5 教训：Agnes 的 .catch() 静默吞异常，页面无变化、无 toast——
只有 console/pageerror 和网络日志能说清发生了什么。这是通用诊断能力。
"""
from tools.browser.worker import BrowserWorker


class FakeConsoleMsg:
    def __init__(self, type_, text):
        self.type = type_
        self.text = text


class FakeRequest:
    def __init__(self, url, method="GET", resource_type="xhr"):
        self.url = url
        self.method = method
        self.resource_type = resource_type


class FakeResponse:
    def __init__(self, url, status, method="GET", resource_type="xhr"):
        self.url = url
        self.status = status
        self.request = FakeRequest(url, method, resource_type)


class FakePage:
    def __init__(self):
        self.url = "https://example.com/"
        self.name = ""
        self._handlers = {}
        self.main_frame = self
        self.frames = [self]

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for h in self._handlers.get(event, []):
            h(*args)

    def locator(self, selector):
        class Loc:
            def count(self):
                return 1

            @property
            def first(self):
                return self

            def is_visible(self, timeout=None):
                return True

            def click(self, timeout=None):
                pass

        return Loc()


def make_worker():
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = None
    w._browser = None
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    return w


def test_console_capture_and_read():
    w = make_worker()
    page = FakePage()
    w._attach_page_diagnostics(page)
    page.emit("console", FakeConsoleMsg("error", "Uncaught TypeError: foo is null"))
    page.emit("console", FakeConsoleMsg("log", "just info"))
    page.emit("pageerror", ValueError("boom"))
    out = w._do_console_log({})
    assert "TypeError" in out and "boom" in out and "just info" in out
    out_err = w._do_console_log({"type": "error"})
    assert "TypeError" in out_err and "just info" not in out_err


def test_console_dedup_page_attach():
    w = make_worker()
    page = FakePage()
    w._attach_page_diagnostics(page)
    w._attach_page_diagnostics(page)  # 重复挂载不应重复记录
    page.emit("console", FakeConsoleMsg("error", "once"))
    out = w._do_console_log({"type": "error"})
    assert out.count("once") == 1


def test_network_log_capture_and_filter():
    w = make_worker()
    page = FakePage()
    w._attach_page_diagnostics(page)
    req = FakeRequest("https://api.example.com/verification?email=a@b.c", "GET", "xhr")
    page.emit("request", req)
    page.emit("response", FakeResponse(req.url, 200, "GET", "xhr"))
    req2 = FakeRequest("https://api.example.com/send", "POST", "xhr")
    page.emit("request", req2)
    page.emit("response", FakeResponse(req2.url, 429, "POST", "xhr"))
    # 图片请求应被降噪
    req3 = FakeRequest("https://example.com/logo.png", "GET", "image")
    page.emit("request", req3)
    page.emit("response", FakeResponse(req3.url, 200, "GET", "image"))

    out = w._do_network_log({})
    assert "verification" in out and "429" in out
    assert "logo.png" not in out  # 降噪
    out_f = w._do_network_log({"filter": "verification"})
    assert "verification" in out_f and "send" not in out_f
    out_e = w._do_network_log({"errors_only": True})
    assert "429" in out_e and "verification" not in out_e


def test_recent_errors_helpers():
    w = make_worker()
    page = FakePage()
    w._attach_page_diagnostics(page)
    assert w._recent_console_errors() == ""
    assert w._recent_network_errors() == ""
    page.emit("console", FakeConsoleMsg("error", "silent catch ate it"))
    req = FakeRequest("https://api.example.com/x", "GET", "xhr")
    page.emit("request", req)
    page.emit("response", FakeResponse(req.url, 500, "GET", "xhr"))
    assert "silent catch" in w._recent_console_errors()
    assert "500" in w._recent_network_errors()


def test_diag_tools_are_daily():
    from agent.risk import _DAILY_TOOLS
    assert "browser_console" in _DAILY_TOOLS
    assert "browser_network_log" in _DAILY_TOOLS
