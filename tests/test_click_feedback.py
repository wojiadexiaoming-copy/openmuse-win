"""点击反馈保真度（s-cdf93b36b0 第二轮实测教训）：

[124] 点发送验证码按钮返回"[已点击]"，但码实际没发出去，模型信了"成功"
去邮箱空轮询。点击成功只证明 Playwright 点到了元素，不等于业务效果达成。
_do_click 在点击前后取页面指纹（URL+标题+DOM哈希），完全一致时追加明确提示，
让模型截图验证而不是假设成功。
"""
from unittest.mock import MagicMock

from tools.browser.worker import BrowserWorker


class FakeResponse:
    def __init__(self, status, url, resource_type):
        self.status = status
        self.url = url
        self.request = FakeRequest(resource_type)


class FakeRequest:
    def __init__(self, resource_type, method="POST"):
        self.resource_type = resource_type
        self.method = method


class FakePageWithEvaluate:
    """支持 evaluate 的最小 Page 替身；dom_version 模拟点击是否改变页面。"""

    def __init__(self, url="https://example.com/login"):
        self.url = url
        self.name = ""  # frame 名（替身里 page 即主 frame）
        self.clicked = []
        self.typed = []
        self.dom_version = 1
        self.change_on_click = True
        self._response_handlers = []
        self.pending_responses = []  # 点击时触发的模拟响应：(status, url, resource_type)
        self.main_frame = self
        self.frames = [self]

    def evaluate(self, js):
        return f"Login|{100 + self.dom_version}|{self.dom_version}"

    def on(self, event, handler):
        if event == "response":
            self._response_handlers.append(handler)

    def remove_listener(self, event, handler):
        if event == "response" and handler in self._response_handlers:
            self._response_handlers.remove(handler)

    @property
    def response_listeners(self):
        return list(self._response_handlers)

    def click(self, selector, timeout=None):
        self.clicked.append(selector)
        if self.change_on_click:
            self.dom_version += 1
        for status, url, rtype in self.pending_responses:
            resp = FakeResponse(status, url, rtype)
            for h in list(self._response_handlers):
                h(resp)
        self.pending_responses = []

    @property
    def viewport_size(self):
        return {"width": 1000, "height": 800}

    @property
    def mouse(self):
        page = self

        class Mouse:
            def click(self, x, y):
                page.clicked.append((round(x, 1), round(y, 1)))
                if page.change_on_click:
                    page.dom_version += 1

        return Mouse()

    def locator(self, selector):
        page = self

        class Loc:
            first = None

            def count(self):
                return 1

            def is_disabled(self, timeout=None):
                return False

            def is_visible(self, timeout=None):
                return True

            def fill(self, text, timeout=None):
                page.typed.append((selector, text))

            def click(self, timeout=None):
                page.clicked.append(selector)
                # 模拟真实点击副作用：DOM 变化 + 触发待处理的模拟响应
                if page.change_on_click:
                    page.dom_version += 1
                for status, url, rtype in page.pending_responses:
                    resp = FakeResponse(status, url, rtype)
                    for h in list(page._response_handlers):
                        h(resp)
                page.pending_responses = []

        loc = Loc()
        loc.first = loc
        return loc


def make_worker(page):
    w = BrowserWorker(profile_mgr=MagicMock(), circuit=MagicMock(), profile="test",
                      headless=True)
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._raw_page = page
    return w


def test_click_with_page_change_returns_plain_success():
    """点击后页面有变化 → 普通[已点击]，不打扰模型。"""
    w = make_worker(FakePageWithEvaluate())
    out = w._do_click({"selector": "#send"})
    assert out.startswith("[已点击]")
    assert "无任何变化" not in out


def test_click_without_page_change_adds_warning_hint():
    """点击前后指纹完全一致 → 追加[提示]，要求截图验证而非假设成功。"""
    page = FakePageWithEvaluate()
    page.change_on_click = False
    w = make_worker(page)
    out = w._do_click({"selector": "#send"})
    assert out.startswith("[已点击]")
    assert "无任何变化" in out
    assert "browser_screenshot" in out


def test_click_fingerprint_failure_degrades_gracefully():
    """evaluate 不可用（旧替身/异常页面）→ 指纹为空，退化为原来的普通返回，不崩。"""
    class NoEvaluatePage(FakePageWithEvaluate):
        def evaluate(self, js):
            raise RuntimeError("closed")

    w = make_worker(NoEvaluatePage())
    out = w._do_click({"selector": "#send"})
    assert out == "[已点击] #send"


def test_click_timeout_adds_icon_button_hint():
    """超时失败 → 追加图标按钮定位提示（v3 实测：SVG 纸飞机按钮连跪 5 次）。"""

    class TimeoutPage(FakePageWithEvaluate):
        def locator(self, selector):
            loc = super().locator(selector)
            orig_click = loc.click
            def raising_click(timeout=None):
                raise Exception(f"Page.click: Timeout {timeout}ms exceeded.")
            loc.click = raising_click
            loc.first.click = raising_click
            return loc

    w = make_worker(TimeoutPage())
    out = w._do_click({"selector": "div > svg"})
    assert out.startswith("[点击失败]")
    assert "browser_click_at" in out
    assert "browser_screenshot" in out


def test_click_syntax_error_has_no_timeout_hint():
    """选择器语法错误 → 错误本身已明确，不加超时提示。"""

    class SyntaxErrorPage(FakePageWithEvaluate):
        def locator(self, selector):
            loc = super().locator(selector)
            def raising_click(timeout=None):
                raise Exception("Unexpected token \")\" while parsing css selector")
            loc.click = raising_click
            loc.first.click = raising_click
            return loc

    w = make_worker(SyntaxErrorPage())
    out = w._do_click({"selector": "div:has(> div))"})
    assert out.startswith("[点击失败]")
    assert "可访问名称" not in out


def test_click_exposes_failed_api_response():
    """第三层验证：点击触发的 XHR 回 429 → [警告]直接暴露，不让模型信按钮状态。"""
    page = FakePageWithEvaluate()
    page.pending_responses = [
        (429, "https://platform.agnes-ai.com/api/send-code", "xhr"),
    ]
    w = make_worker(page)
    out = w._do_click({"selector": "#send-code"})
    assert "[警告]" in out
    assert "429" in out
    assert "倒计时" in out  # 明确不让模型以按钮倒计时为准
    assert page.response_listeners == [], "监听器必须摘掉，不泄漏"


def test_click_ignores_successful_api_response():
    """接口全 2xx → 不打扰，普通[已点击]。"""
    page = FakePageWithEvaluate()
    page.pending_responses = [
        (200, "https://platform.agnes-ai.com/api/send-code", "xhr"),
    ]
    w = make_worker(page)
    out = w._do_click({"selector": "#send-code"})
    assert out.startswith("[已点击]")
    assert "[警告]" not in out


def test_click_ignores_non_api_failed_response():
    """图片/css 等静态资源 5xx → 忽略（不是业务接口）。"""
    page = FakePageWithEvaluate()
    page.pending_responses = [
        (500, "https://example.com/banner.png", "image"),
    ]
    w = make_worker(page)
    out = w._do_click({"selector": "#send-code"})
    assert "[警告]" not in out


def test_click_detaches_listener_on_failure():
    """点击失败路径也要摘掉监听器。"""

    class TimeoutPage(FakePageWithEvaluate):
        def locator(self, selector):
            loc = super().locator(selector)
            def raising_click(timeout=None):
                raise Exception(f"Page.click: Timeout {timeout}ms exceeded.")
            loc.click = raising_click
            loc.first.click = raising_click
            return loc

    page = TimeoutPage()
    w = make_worker(page)
    w._do_click({"selector": "#send-code"})
    assert page.response_listeners == []


def test_type_email_into_password_field_warns():
    """v4 实测：邮箱填进密码框 → [警告]要求截图确认框是否填对。"""
    page = FakePageWithEvaluate()
    w = make_worker(page)
    out = w._do_type_text({
        "selector": 'input[type="password"]',
        "text": "john1792037591267@temp.inodetree.com",
    })
    assert "[警告]" in out
    assert "密码框" in out
    assert "browser_screenshot" in out


def test_type_email_into_email_field_no_warning():
    """正常填邮箱框 → 不打扰。"""
    page = FakePageWithEvaluate()
    w = make_worker(page)
    out = w._do_type_text({
        "selector": 'input[placeholder*="邮箱"]',
        "text": "john1792037591267@temp.inodetree.com",
    })
    assert "[警告]" not in out


def test_click_at_converts_relative_coords():
    """browser_click_at：0~1000 相对坐标按视口换算后 mouse.click。"""
    page = FakePageWithEvaluate()
    w = make_worker(page)
    out = w._do_click_at({"x": 500, "y": 250})
    assert "[已点击]" in out
    assert "坐标(500, 250)" in out
    # 视口 1000x800 → 绝对 (500, 200)
    assert page.clicked == [(500.0, 200.0)]


def test_click_at_rejects_bad_coords():
    """越界/非数字坐标直接拒绝，不碰鼠标。"""
    page = FakePageWithEvaluate()
    w = make_worker(page)
    assert "越界" in w._do_click_at({"x": 1200, "y": 50})
    assert "数字坐标" in w._do_click_at({"x": "abc", "y": 50})
    assert page.clicked == []


def test_click_css_engine_prefix_normalized():
    """run4 [99]：`div >> css:svg[...]` 误写 css: → 自动纠正为 css=，不再 SyntaxError。"""
    page = FakePageWithEvaluate()
    w = make_worker(page)
    out = w._do_click({"selector": 'div >> css:svg[aria-label="send"]'})
    assert "SyntaxError" not in out
    assert "css=svg" in page.clicked[0]


def test_click_at_is_L1_daily_tool():
    """browser_click_at 与 browser_click 同级：普通点击，不弹审批卡。"""
    from agent.risk import classify_tool_risk
    assert classify_tool_risk("browser_click_at", {"x": 1, "y": 2}) == "L1"


def _png_bytes(w=1000, h=800):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_click_at_returns_marked_screenshot():
    """落点回显：成功点击返回带红圈截图的 Message，模型可见自己点偏了多少。"""
    from agent.models import Message

    class ShotPage(FakePageWithEvaluate):
        def screenshot(self):
            return _png_bytes()

    w = make_worker(ShotPage())
    out = w._do_click_at({"x": 357, "y": 466})
    assert isinstance(out, Message)
    assert "[已点击]" in out.content
    assert "红圈" in out.content
    assert len(out.images) == 1
    assert out.images[0].data[:2] == b"\xff\xd8"  # JPEG


def test_click_at_still_works_without_screenshot():
    """截图失败不阻断点击主流程：降级为纯文本返回。"""

    class NoShotPage(FakePageWithEvaluate):
        def screenshot(self):
            raise RuntimeError("nope")

    w = make_worker(NoShotPage())
    out = w._do_click_at({"x": 357, "y": 466})
    assert isinstance(out, str)
    assert "[已点击]" in out


def test_marked_shot_draws_at_click_point():
    """红圈画在点击位置附近（截图物理像素 = CSS 坐标 × 缩放比）。"""
    import io
    from PIL import Image

    class ShotPage(FakePageWithEvaluate):
        def screenshot(self):
            return _png_bytes(1000, 800)

    w = make_worker(ShotPage())
    jpeg = w._marked_click_shot(500.0, 400.0)  # 视口恰为 1000x800 → 落点 (500,400)
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    # 圆心附近应有红色像素
    reds = sum(1 for dx in range(-20, 21) for dy in range(-20, 21)
               if img.getpixel((500 + dx, 400 + dy))[0] > 200
               and img.getpixel((500 + dx, 400 + dy))[1] < 100)
    assert reds > 0
