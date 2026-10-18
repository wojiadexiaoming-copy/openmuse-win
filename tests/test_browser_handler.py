"""browser_handler 溯源测试 + 网络响应体捕获测试。"""


class FakeEl:
    def __init__(self, mode="react_catch"):
        self._mode = mode

    def evaluate(self, js):
        if self._mode == "react_catch":
            return {
                "onclickAttr": "", "onclickSrc": "",
                "reactSrc": "onClick = async () => { await fetch('/api/send').catch(e => {}); }",
                "vueInfo": "", "ancestors": [], "framework": "react",
            }
        if self._mode == "dead":
            return {
                "onclickAttr": "", "onclickSrc": "", "reactSrc": "",
                "vueInfo": "", "ancestors": [], "framework": "unknown",
            }
        if self._mode == "native":
            return {
                "onclickAttr": "doSend()",
                "onclickSrc": "", "reactSrc": "", "vueInfo": "",
                "ancestors": ["<div> 有onclick"], "framework": "unknown",
            }
        return {}


class FakeLoc:
    def __init__(self, mode):
        self._mode = mode

    def count(self):
        return 1

    @property
    def first(self):
        return FakeEl(self._mode)


class FakeFrame:
    def __init__(self, mode="react_catch"):
        self._mode = mode

    def locator(self, selector):
        return FakeLoc(self._mode)


class FakePage:
    def __init__(self, mode="react_catch"):
        self.url = "https://example.com/"
        self.main_frame = FakeFrame(mode)
        self.frames = [self.main_frame]


def make_worker(mode="react_catch"):
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = FakePage(mode)
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    return w


def test_handler_react_catch_warns():
    w = make_worker("react_catch")
    out = w._do_handler({"selector": "img.send"})
    assert "React handler 源码" in out
    assert ".catch" in out  # 提醒静默吞异常


def test_handler_dead_element():
    w = make_worker("dead")
    out = w._do_handler({"selector": "img.send"})
    assert "死元素" in out


def test_handler_native_onclick():
    w = make_worker("native")
    out = w._do_handler({"selector": "button"})
    assert "doSend()" in out
    assert "祖先可疑拦截" in out


def test_handler_not_found():
    w = make_worker()
    w._find_locator = lambda s, f=None: (None, None, None)
    out = w._do_handler({"selector": ".nope"})
    assert "未找到" in out


def test_network_body_captured_for_errors():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._do_guard = lambda: None

    class FakeReq:
        url = "https://api.example.com/send"
        method = "POST"
        resource_type = "xhr"

    class FakeResp:
        status = 429
        request = FakeReq()

        @property
        def url(self):
            return self.request.url

        def text(self):
            return '{"code":429,"message":"too many requests"}'

    page_handlers = {}

    class FakePageNet:
        def on(self, event, handler):
            page_handlers[event] = handler

    w._attach_page_diagnostics(FakePageNet())
    req = FakeReq()
    page_handlers["request"](req)
    page_handlers["response"](FakeResp())
    out = w._do_network_log({"errors_only": True, "show_body": True})
    assert "429" in out
    assert "too many requests" in out  # 响应体被存下来了


def test_handler_tool_is_daily():
    from agent.risk import _DAILY_TOOLS
    assert "browser_handler" in _DAILY_TOOLS
