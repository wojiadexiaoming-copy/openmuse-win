"""策略层接线测试：模板注入 + 适配器注入 + 恢复建议。"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


def test_match_template():
    from agent.strategy import match_template
    t = match_template("打开浏览器用临时邮箱注册 Agnes")
    assert t is not None and t.name == "email_verification_registration"
    t2 = match_template("今天天气怎么样")
    assert t2 is None
    t3 = match_template("用 OAuth 第三方登录")
    assert t3 is not None and t3.name == "oauth_login"


def test_inject_flow_template_new_task():
    from agent.orchestrator import Orchestrator
    from agent.models import Message
    orch = Orchestrator.__new__(Orchestrator)
    orch.history = []
    orch._inject_flow_template("用临时邮箱注册某网站，邮箱验证码")
    assert len(orch.history) == 1
    assert "email_verification_registration" in orch.history[0].content
    assert orch.history[0].name == "flow_template"


def test_inject_flow_template_no_match():
    from agent.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.history = []
    orch._inject_flow_template("今天天气怎么样")
    assert len(orch.history) == 0


def test_navigate_injects_adapter():
    from tools.browser.worker import BrowserWorker
    # 建一个匹配的适配器
    import agent.strategy.site_adapter as sa_mod
    tmp = Path(tempfile.mkdtemp()) / "sites"
    tmp.mkdir()
    site_d = tmp / "testsite"
    site_d.mkdir()
    (site_d / "site.yaml").write_text(
        'domains:\n  - navigatetest.com\nstate_hints:\n  - "看到X→已登录"\n', encoding="utf-8")
    (site_d / "quirks.md").write_text("## 怪癖\n按钮是图片\n", encoding="utf-8")
    orig_init = sa_mod.SiteAdapterRegistry.__init__
    sa_mod.SiteAdapterRegistry.__init__ = lambda self, r=None: (
        setattr(self, "root", tmp) or setattr(self, "_adapters", None))
    try:
        w = BrowserWorker.__new__(BrowserWorker)
        w._do_guard = lambda: None
        w._ensure_maximized = lambda: None
        w._layout_note = lambda: ""
        w._norm_url = lambda u: u

        class FakePage:
            url = "https://navigatetest.com/login"
            def goto(self, url, wait_until=None, timeout=None):
                self.url = url
            def title(self):
                return "Test"
        w._raw_page = FakePage()
        w._url_state = {}
        w._tabs = {}
        w._human_delay = lambda: None
        out = w._do_navigate({"url": "https://navigatetest.com/login"})
        assert "[已打开]" in out
        assert "按钮是图片" in out  # quirks 注入了
        assert "看到X" in out  # state_hints 注入了
    finally:
        sa_mod.SiteAdapterRegistry.__init__ = orig_init


def test_execute_tool_appends_recovery():
    from agent.orchestrator import Orchestrator
    from agent.models import Message
    orch = Orchestrator.__new__(Orchestrator)
    orch._get_terminal_control = lambda: None
    orch._notice_tool_control = lambda m: None
    orch._pending_tool_results = []
    # 模拟工具返回错误
    err_msg = Message(role="tool", name="browser_click",
                      content="[点击失败] button: Timeout 10000ms exceeded")
    orch.tools = MagicMock()
    orch.tools.execute.return_value = err_msg
    import agent.orchestrator as orch_mod
    # 需要 _span 上下文
    orch._span = lambda kind, name: _DummySpan()
    out = orch._execute_tool("browser_click", {"selector": "button"})
    assert "[恢复建议]" in out.content
    assert "browser_find_text" in out.content or "snapshot" in out.content


class _DummySpan:
    def __enter__(self):
        return {}
    def __exit__(self, *a):
        return False


def test_native_set_value():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)

    class FakeLoc:
        def evaluate(self, js, text):
            assert "HTMLInputElement" in js
            assert "dispatchEvent" in js
            return "ok"
    assert w._native_set_value(FakeLoc(), "hello") is True

    class BadLoc:
        def evaluate(self, js, text):
            return "no-setter"
    assert w._native_set_value(BadLoc(), "hello") is False


def test_type_react_fallback():
    """fill 后读回不符 → 原生 setter 重填。"""
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._native_called = False
    orig_native = w._native_set_value

    class FakeLoc:
        def __init__(self):
            self._val = ""
            self._react_mode = True  # 第一次 fill 读回是空的

        def is_visible(self, timeout=None):
            return True

        def fill(self, text, timeout=None):
            # 模拟 React：fill 后读回是空的
            self._val = "" if self._react_mode else text

        def input_value(self, timeout=None):
            return self._val

    fake_loc = FakeLoc()

    def fake_native(loc, text):
        w._native_called = True
        fake_loc._react_mode = False
        fake_loc._val = text
        return True
    w._native_set_value = fake_native

    class FakeLocator:
        @property
        def first(self):
            return fake_loc
    w._find_locator = lambda cand, frame_ref=None: (None, FakeLocator(), "f0")
    # task_state 桩
    import agent.task_state as ts_mod
    orig_cur = getattr(ts_mod, "current_task_state", None)

    out = w._do_type_text({"selector": "input", "text": "hello"})
    assert w._native_called  # 原生 setter 被调用了
    assert "React兼容" in out
