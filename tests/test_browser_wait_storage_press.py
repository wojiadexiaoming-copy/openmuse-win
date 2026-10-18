"""新工具测试：browser_wait_for + browser_storage + browser_press。"""


class FakePage:
    def __init__(self):
        self.url = "https://example.com/inbox"
        self.main_frame = self
        self.frames = [self]
        self.context = self
        self.keyboard = self
        self.pressed = []
        self._text = "收件箱（0）"
        self._cookies = [{"name": "session", "value": "abc123xyz789secret", "domain": ".example.com"}]
        self._storage = {"token": "tok_12345"}

    def locator(self, selector):
        class Loc:
            def count(self):
                return 1

            @property
            def first(self):
                return self

            def is_visible(self, timeout=None):
                return True

        return Loc()

    def evaluate(self, js):
        if "innerText" in js:
            return self._text
        if "localStorage" in js or "sessionStorage" in js:
            return [[k, v] for k, v in self._storage.items()]
        return None

    def cookies(self):
        return self._cookies

    def press(self, key):
        self.pressed.append(key)


def make_worker():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = FakePage()
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    return w


def test_wait_for_text_immediate():
    w = make_worker()
    out = w._do_wait_for({"text_visible": "收件箱", "timeout": 5})
    assert "[等待满足]" in out and "text_visible" in out


def test_wait_for_timeout():
    w = make_worker()
    out = w._do_wait_for({"text_visible": "绝不会出现的文字XYZ", "timeout": 1})
    assert "[等待超时]" in out


def test_wait_for_url():
    w = make_worker()
    out = w._do_wait_for({"url_contains": "/inbox", "timeout": 5})
    assert "[等待满足]" in out


def test_wait_for_no_condition():
    w = make_worker()
    out = w._do_wait_for({"timeout": 5})
    assert "缺少条件" in out


def test_wait_for_becomes_true():
    w = make_worker()
    import threading
    def later():
        import time
        time.sleep(0.8)
        w._raw_page._text = "收件箱（1）验证码"
    threading.Thread(target=later, daemon=True).start()
    out = w._do_wait_for({"text_visible": "验证码", "timeout": 5})
    assert "[等待满足]" in out


def test_storage_cookies_masked():
    w = make_worker()
    out = w._do_storage({"action": "cookies"})
    assert "session=" in out
    assert "abc123xyz789secret" not in out  # 脱敏
    assert "abc123xyz789…" in out


def test_storage_empty_cookies():
    w = make_worker()
    w._raw_page._cookies = []
    out = w._do_storage({"action": "cookies"})
    assert "无 cookie" in out


def test_storage_local():
    w = make_worker()
    out = w._do_storage({"action": "local"})
    assert "token = tok_12345" in out


def test_press():
    w = make_worker()
    out = w._do_press({"key": "Enter"})
    assert "[按键] Enter" in out
    assert w._raw_page.pressed == ["Enter"]


def test_press_missing_key():
    w = make_worker()
    out = w._do_press({})
    assert "缺少 key" in out


def test_new_tools_are_daily():
    from agent.risk import _DAILY_TOOLS
    for t in ("browser_wait_for", "browser_storage", "browser_press"):
        assert t in _DAILY_TOOLS
