"""browser_evaluate（run5 Agnes 沉默失败教训）：页面内执行 JS 表达式并返回结果，
用于 UI 点击静默失败时直接调页面背后的接口读返回 JSON。"""
from unittest.mock import MagicMock

from tools.browser.worker import BrowserWorker


class FakePage:
    def __init__(self, result="ok"):
        self.url = "https://example.com/"
        self.name = ""
        self._result = result
        self.main_frame = self
        self.frames = [self]

    def evaluate(self, expression):
        assert expression
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def make_worker(page):
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = page
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    return w


def test_evaluate_returns_json_result():
    w = make_worker(FakePage({"code": 200, "message": "ok"}))
    out = w._do_evaluate({"expression": "fetch('/api/x').then(r=>r.json())"})
    assert "[执行结果]" in out and '"message": "ok"' in out


def test_evaluate_empty_expression_rejected():
    w = make_worker(FakePage())
    assert "expression 为空" in w._do_evaluate({"expression": "  "})


def test_evaluate_js_error_reported():
    w = make_worker(FakePage(RuntimeError("boom")))
    assert "[执行失败]" in w._do_evaluate({"expression": "bad("})


def test_evaluate_long_result_truncated():
    w = make_worker(FakePage("x" * 5000))
    out = w._do_evaluate({"expression": "document.body.innerHTML"})
    assert "截断" in out and len(out) < 4500


def test_evaluate_is_daily_tool_no_approval():
    from agent.risk import _DAILY_TOOLS
    assert "browser_evaluate" in _DAILY_TOOLS
