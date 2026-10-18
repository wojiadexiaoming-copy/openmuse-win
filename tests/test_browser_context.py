"""进程级隔离测试：多账号独立 browser 进程 + 指纹区分 + eTLD 路由。"""


class FakePage:
    def __init__(self, url="https://example.com/"):
        self.url = url
        self._handlers = {}
        self._closed = False

    def on(self, event, handler):
        self._handlers[event] = handler

    def is_closed(self):
        return self._closed

    def title(self):
        return "Test"

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url


class FakeContext:
    """模拟 Playwright BrowserContext：独立 cookie jar + 指纹参数。"""
    def __init__(self, **fp_kwargs):
        self._cookies = []
        self.pages = []
        self.closed = False
        self.fp_kwargs = fp_kwargs

    def new_page(self):
        p = FakePage()
        self.pages.append(p)
        return p

    def add_cookies(self, cookies):
        for c in cookies:
            if not all(k in c for k in ("name", "value", "domain")):
                raise ValueError(f"cookie 缺字段: {c}")
        self._cookies.extend(cookies)

    def cookies(self):
        return list(self._cookies)

    def add_init_script(self, script):
        self._init_script = script

    def close(self):
        self.closed = True


class FakeBrowserProcess:
    """模拟独立 browser 进程。"""
    def __init__(self, **launch_kwargs):
        self.launch_kwargs = launch_kwargs
        self.closed = False
        self._ctx = None

    def new_context(self, **fp_kwargs):
        self._ctx = FakeContext(**fp_kwargs)
        return self._ctx

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.launched = []

    def launch(self, **kwargs):
        b = FakeBrowserProcess(**kwargs)
        self.launched.append(b)
        return b


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


class FakeBrowser:
    """default profile 的 persistent context（本身就是 context）。"""
    def __init__(self):
        self._pages = []

    def new_page(self):
        p = FakePage()
        self._pages.append(p)
        return p

    def cookies(self):
        return []

    def add_cookies(self, cookies):
        pass


def make_worker():
    from tools.browser.worker import BrowserWorker
    w = BrowserWorker.__new__(BrowserWorker)
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._layout_note = lambda: ""
    w._norm_url = lambda u: u
    w._ensure_maximized = lambda: None
    w._browser = FakeBrowser()
    w._pw = FakePlaywright()
    w.headless = True
    w._profiles = {"default": {"browser": None, "context": None, "tabs": {},
                                "domains": set(), "fingerprint": {}}}
    w._active_profile = "default"
    w._fp_index = 0
    w._url_state = {}
    return w


def test_etld1():
    from tools.browser.worker import _etld1
    assert _etld1("https://mail.google.com/a/b") == "google.com"
    assert _etld1("https://platform.agnes-ai.com/login") == "agnes-ai.com"
    assert _etld1("https://www.example.co.uk/x") == "example.co.uk"
    assert _etld1("not-a-url") == ""


def test_fingerprint_pool():
    from tools.browser.worker import _FINGERPRINTS
    assert len(_FINGERPRINTS) >= 2
    uas = {f["user_agent"] for f in _FINGERPRINTS}
    vps = {(f["viewport"]["width"], f["viewport"]["height"]) for f in _FINGERPRINTS}
    assert len(uas) >= 2 or len(vps) >= 2  # 指纹有区分


def test_context_new_launches_process():
    w = make_worker()
    out = w._do_context_new({"name": "acct1"})
    assert "Profile 已建" in out and "acct1" in out
    assert "独立进程" in out
    prof = w._profiles["acct1"]
    assert prof["browser"] is not None
    assert isinstance(prof["browser"], FakeBrowserProcess)
    assert len(w._pw.chromium.launched) == 1
    assert prof["fingerprint"]["viewport"]["width"] in (1920, 1366, 1536)


def test_fingerprint_rotates():
    w = make_worker()
    w._do_context_new({"name": "a1"})
    w._do_context_new({"name": "a2"})
    fp1 = w._profiles["a1"]["fingerprint"]
    fp2 = w._profiles["a2"]["fingerprint"]
    assert fp1 != fp2  # 轮换出不同指纹


def test_context_duplicate_rejected():
    w = make_worker()
    w._do_context_new({"name": "acct1"})
    out = w._do_context_new({"name": "acct1"})
    assert "已存在" in out


def test_tabs_isolated_per_profile():
    w = make_worker()
    w._do_context_new({"name": "acct1"})
    w._do_context_new({"name": "acct2"})

    w._do_new_tab({"role": "mail", "url": "https://a.com/"})
    assert "mail" in w._tabs

    w._do_context_switch({"name": "acct1"})
    assert "mail" not in w._tabs
    w._do_new_tab({"role": "work", "url": "https://b.com/"})
    assert "work" in w._tabs

    w._do_context_switch({"name": "default"})
    assert "mail" in w._tabs
    assert "work" not in w._tabs


def test_new_tab_uses_profile_process():
    w = make_worker()
    w._do_context_new({"name": "acct1"})
    w._do_context_switch({"name": "acct1"})
    w._do_new_tab({"role": "x", "url": "https://x.com/"})
    prof = w._profiles["acct1"]
    assert w._tabs["x"]["page"] in prof["context"].pages
    assert "x.com" in prof["domains"]


def test_cookie_login_import():
    w = make_worker()
    cookies = [{"name": "sessionid", "value": "abc123",
                "domain": ".example.com", "path": "/"}]
    out = w._do_context_new({"name": "acct1", "cookies": cookies})
    assert "已导入 1 个 cookies" in out
    prof = w._profiles["acct1"]
    assert prof["context"].cookies()[0]["name"] == "sessionid"

    out = w._do_context_cookies({"name": "acct1"})
    assert "共 1 个" in out
    assert ".example.com" in out
    assert "abc123" not in out  # 值不落日志


def test_cookie_import_bad_format():
    w = make_worker()
    out = w._do_context_new(
        {"name": "bad", "cookies": [{"name": "x"}]})
    assert "失败" in out
    assert "bad" not in w._profiles


def test_context_close_kills_process():
    w = make_worker()
    w._do_context_new({"name": "acct1"})
    w._do_context_switch({"name": "acct1"})
    w._do_new_tab({"role": "t", "url": "https://t.com/"})
    browser = w._profiles["acct1"]["browser"]
    out = w._do_context_close({"name": "acct1"})
    assert "[已关闭]" in out
    assert "acct1" not in w._profiles
    assert w._active_profile == "default"
    assert browser.closed  # 进程已回收


def test_default_cannot_close():
    w = make_worker()
    out = w._do_context_close({"name": "default"})
    assert "不能关" in out


def test_switch_unknown():
    w = make_worker()
    out = w._do_context_switch({"name": "nope"})
    assert "不存在" in out


def test_list_shows_fingerprint():
    w = make_worker()
    w._do_context_new({"name": "acct1"})
    out = w._do_context_list({})
    assert "acct1" in out
    assert "独立进程" in out
    assert "指纹:" in out
