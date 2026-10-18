"""定位增强测试：browser_find_text + 请求体/请求头捕获 + inspect 坐标。"""


class FakeReq:
    def __init__(self):
        self.url = "https://api.example.com/send"
        self.method = "POST"
        self.resource_type = "xhr"
        self.headers = {"authorization": "Bearer secret_token_1234567890",
                        "content-type": "application/json"}
        self.post_data = '{"email":"a@b.c","purpose":"register"}'


class FakeResp:
    def __init__(self, req):
        self.status = 200
        self.request = req

    @property
    def url(self):
        return self.request.url

    def text(self):
        return "ok"


class FakePage:
    def __init__(self):
        self.url = "https://example.com/"
        self.main_frame = self
        self.frames = [self]
        self.viewport_size = {"width": 1280, "height": 800}
        self._handlers = {}

    def on(self, event, handler):
        self._handlers[event] = handler

    def evaluate(self, js, *a):
        if "createTreeWalker" in js:
            t = str(a[0]) if a else ""
            if "不存在" in t:
                return []
            return [
                {"x": 640, "y": 400, "w": 100, "h": 30, "text": "发送验证码"},
                {"x": 640, "y": 500, "w": 100, "h": 30, "text": "重新发送验证码"},
            ]
        return None

    def locator(self, selector):
        class Loc:
            def count(self):
                return 1

            @property
            def first(self):
                return self

            def evaluate(self, js):
                return {
                    "tag": "button", "id": "", "cls": "",
                    "attrs": {}, "text": "发送", "value": "",
                    "box": {"x": 590, "y": 385, "w": 100, "h": 30},
                    "style": {"color": "rgb(0,0,0)", "background": "rgb(255,255,255)",
                              "opacity": "1", "display": "inline-block",
                              "visibility": "visible", "cursor": "pointer",
                              "pointerEvents": "auto"},
                    "disabled": False, "ariaDisabled": "", "hasOnClick": True,
                }

            def screenshot(self, timeout=None):
                raise RuntimeError("no shot")

        return Loc()


def make_worker():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = FakePage()
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    return w


def test_find_text_returns_coords():
    w = make_worker()
    out = w._do_find_text({"text": "发送验证码"})
    # (640/1280*1000, 400/800*1000) = (500, 500)
    assert "(500, 500)" in out
    assert "browser_click_at" in out


def test_find_text_not_found():
    w = make_worker()
    out = w._do_find_text({"text": "不存在的文字XYZ"})
    assert "未找到" in out


def test_find_text_missing():
    w = make_worker()
    out = w._do_find_text({})
    assert "缺少 text" in out


def test_network_post_body_captured():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._do_guard = lambda: None
    page = FakePage()
    w._attach_page_diagnostics(page)
    req = FakeReq()
    page._handlers["request"](req)
    page._handlers["response"](FakeResp(req))
    out = w._do_network_log({"show_post": True})
    assert "a@b.c" in out  # 请求体
    assert "Bearer secret_to" in out  # 请求头（截断到16字符）
    assert "1234567890" not in out  # 尾部已脱敏


def test_inspect_includes_click_coords():
    w = make_worker()
    out = w._do_inspect({"selector": "button"})
    # box 中心 (640, 400) → (500, 500)
    assert "(500, 500)" in out.content


def test_find_text_tool_is_daily():
    from agent.risk import _DAILY_TOOLS
    assert "browser_find_text" in _DAILY_TOOLS
