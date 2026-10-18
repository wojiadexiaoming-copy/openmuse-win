"""鲁棒性测试：对话框/弹窗/点击降级/DOM 观察。"""


class FakeDialog:
    def __init__(self, dtype="confirm", message="确定吗？"):
        self.type = dtype
        self.message = message
        self.default_value = ""
        self.handled = None

    def accept(self, value=""):
        self.handled = ("accept", value)

    def dismiss(self):
        self.handled = ("dismiss",)


class FakePopup:
    def __init__(self):
        self.url = "https://example.com/oauth"
        self._handlers = {}

    def on(self, event, handler):
        self._handlers[event] = handler


class FakePage:
    def __init__(self):
        self.url = "https://example.com/"
        self.main_frame = self
        self.frames = [self]
        self._handlers = {}

    def on(self, event, handler):
        self._handlers[event] = handler

    def locator(self, selector):
        return self

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        raise TimeoutError("Timeout 10000ms exceeded")

    def evaluate(self, js, *a):
        if "el.click()" in js:
            return None  # JS 点击成功
        if "MutationObserver" in js:
            return "OK"
        if "__ae_obs" in js:
            return ["childList +<div> -0节点", "文本变更: 29"]
        if "innerText" in js:
            return "page text"
        return None

    def is_disabled(self, timeout=None):
        return False


def make_worker():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = FakePage()
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._tabs = {}
    # 桩掉指纹/API 监听等
    w._page_fingerprint = lambda: "fp1"
    w._begin_api_watch = lambda: ([], [], lambda: None)
    return w


def test_dialog_auto_accept_and_logged():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._do_guard = lambda: None
    page = FakePage()
    w._attach_page_diagnostics(page)
    dlg = FakeDialog("confirm", "确定提交吗？")
    page._handlers["dialog"](dlg)
    assert dlg.handled[0] == "accept"
    out = w._do_console_log({"type": "dialog"})
    assert "确定提交吗" in out and "已自动接受" in out


def test_dialog_beforeunload_dismissed():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._do_guard = lambda: None
    page = FakePage()
    w._attach_page_diagnostics(page)
    dlg = FakeDialog("beforeunload", "要离开吗？")
    page._handlers["dialog"](dlg)
    assert dlg.handled[0] == "dismiss"  # 留在当前页


def test_popup_auto_registered():
    from tools.browser.worker import BrowserWorker
    w = make_worker()
    page = w._raw_page
    w._attach_page_diagnostics(page)
    popup = FakePopup()
    page._handlers["popup"](popup)
    assert "popup_1" in w._tabs
    assert w._tabs["popup_1"]["page"] is popup


def test_click_escalates_to_js_click():
    w = make_worker()
    out = w._do_click({"selector": "button.send"})
    assert "[JS降级点击]" in out  # Playwright 超时后自动 JS 点击成功


def test_observe_reports_mutations():
    w = make_worker()
    out = w._do_observe({"duration": 1})
    assert "DOM 突变" in out
    assert "<div>" in out


def test_observe_tool_is_daily():
    from agent.risk import _DAILY_TOOLS
    assert "browser_observe" in _DAILY_TOOLS
