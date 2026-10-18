"""测试：浏览器控制架构升级 - ref 元素句柄与失效自愈机制。"""
import pytest
from tools.browser.worker import BrowserWorker, make_browser_tools


class FakeLoc:
    def __init__(self, exists=True, is_input=False, val="", page=None):
        self._exists = exists
        self._is_input = is_input
        self._val = val
        self._page = page

    def count(self):
        return 1 if self._exists else 0

    @property
    def first(self):
        return self

    def click(self, **kwargs):
        if not self._exists:
            raise RuntimeError("Element not found")
        return None

    def hover(self, **kwargs):
        if not self._exists:
            raise RuntimeError("Element not found")
        return None

    def select_option(self, **kwargs):
        if not self._exists:
            raise RuntimeError("Element not found")
        return ["selected_val"]

    def fill(self, val, **kwargs):
        self._val = val
        if self._page:
            self._page.input_val = val
        return None

    def input_value(self, timeout=None):
        return self._val

    def evaluate(self, js, *args):
        return self._val or "val"

    def locator(self, sub_sel):
        return self

    def scroll_into_view_if_needed(self, timeout=None):
        return None


class FakeAccessibility:
    def snapshot(self):
        return {
            "role": "WebArea",
            "name": "Test Page",
            "children": [
                {"role": "button", "name": "提交", "nodeId": 1},
                {"role": "textbox", "name": "邮箱", "nodeId": 2},
            ],
        }


class FakeCDPSession:
    def send(self, cmd, *args):
        if cmd == "Accessibility.getFullAXTree":
            return {
                "nodes": [
                    {"nodeId": 1, "role": {"value": "button"}, "name": {"value": "提交"}},
                    {"nodeId": 2, "role": {"value": "textbox"}, "name": {"value": "邮箱"}},
                ]
            }
        return {}

    def detach(self):
        pass


class FakeContext:
    def __init__(self, page):
        self.pages = [page]

    def new_cdp_session(self, page):
        return FakeCDPSession()


class FakePage:
    def __init__(self, existing_refs=("ae1", "ae2")):
        self.url = "https://example.com/test"
        self.main_frame = self
        self.frames = [self]
        self.viewport_size = {"width": 1000, "height": 800}
        self.existing_refs = set(existing_refs)
        self.clicked_selectors = []
        self._handlers = {}
        self.input_val = ""
        self.accessibility = FakeAccessibility()
        self.context = FakeContext(self)

    def title(self):
        return "Test Title"

    def on(self, event, handler):
        self._handlers[event] = handler

    def evaluate(self, js, *a):
        # 页面指纹返回
        if "document.documentElement" in js or "document.title" in js:
            return "Test Title|100|12345"
        if "is_editable" in js:
            return {
                "is_editable": True,
                "tag": "input#email",
                "is_input": True,
                "is_contenteditable": False,
                "current_value": self.input_val or "",
            }
        if "return el.value" in js or "activeElement" in js:
            if a and isinstance(a[0], str):
                self.input_val = a[0]
            return self.input_val or "hello_alterego"
        return None

    def locator(self, selector):
        self.clicked_selectors.append(selector)
        if '[data-ae-ref="' in selector:
            ref_name = selector.split('[data-ae-ref="')[1].split('"]')[0]
            if ref_name in self.existing_refs:
                return FakeLoc(exists=True, is_input=(ref_name == "ae2"), page=self)
            return FakeLoc(exists=False, page=self)
        return FakeLoc(exists=True, page=self)


def make_test_worker(existing_refs=("ae1", "ae2")):
    w = BrowserWorker.__new__(BrowserWorker)
    w.headless = True
    w._window_layout = {"status": "headless"}
    w._ensure_maximized = lambda: None
    w._raw_page = FakePage(existing_refs=existing_refs)
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._human_like_click = lambda x, y: None
    w._detect_active_modal = lambda: {"has_modal": False, "selector": ""}
    w._page_fingerprint = lambda: "fingerprint_1"
    w._verify_expect = lambda exp, tout=5.0: (True, "")
    w._begin_api_watch = lambda: ([], [], lambda: None)
    w._interactive_tree_with_coords = lambda modal_sel: [
        {
            "ref": "ae1",
            "id": 1,
            "tag": "button",
            "role": "button",
            "is_editable": False,
            "in_modal": False,
            "name": "提交",
            "x": 500,
            "y": 400,
            "w": 100,
            "h": 40,
        },
        {
            "ref": "ae2",
            "id": 2,
            "tag": "input",
            "role": "textbox",
            "is_editable": True,
            "in_modal": False,
            "name": "[输入框]",
            "x": 500,
            "y": 300,
            "w": 200,
            "h": 30,
        },
    ]
    w._get_viewport = lambda: {"width": 1000, "height": 800}
    return w


def test_snapshot_includes_ref_tags():
    w = make_test_worker()
    out = w._do_snapshot({})
    assert "[ref=ae1]" in out
    assert "[ref=ae2]" in out
    assert "[ref 规则]" in out
    assert "browser_click(ref=\"aeN\")" in out


def test_click_with_valid_ref():
    w = make_test_worker(existing_refs=["ae1"])
    res = w._do_click({"ref": "ae1"})
    assert "data-ae-ref=\"ae1\"" in str(w._raw_page.clicked_selectors)
    assert "点击" in res or "click" in res.lower()


def test_click_with_stale_ref_returns_recovery_instruction():
    w = make_test_worker(existing_refs=["ae1"])
    res = w._do_click({"ref": "ae999"})
    assert "[ref 失效]" in res
    assert "ae999" in res
    assert "browser_snapshot" in res


def test_fill_input_with_valid_ref():
    w = make_test_worker(existing_refs=["ae2"])
    res = w._do_fill_input({"ref": "ae2", "text": "hello_alterego"})
    assert '[data-ae-ref="ae2"]' in res
    assert "14" in res


def test_fill_input_with_stale_ref_returns_recovery_instruction():
    w = make_test_worker(existing_refs=["ae2"])
    res = w._do_fill_input({"ref": "ae888", "text": "test"})
    assert "[ref 失效]" in res
    assert "ae888" in res
    assert "browser_snapshot" in res


def test_hover_with_ref():
    w = make_test_worker(existing_refs=["ae1"])
    res = w._do_hover({"ref": "ae1"})
    assert "悬停" in res or "hover" in res.lower()

    stale_res = w._do_hover({"ref": "ae_expired"})
    assert "[ref 失效]" in stale_res


def test_select_with_ref():
    w = make_test_worker(existing_refs=["ae1"])
    res = w._do_select({"ref": "ae1", "value": "val1"})
    assert "selected_val" in res or "选定" in res

    stale_res = w._do_select({"ref": "ae_expired", "value": "val1"})
    assert "[ref 失效]" in stale_res


def test_tool_schemas_contain_ref_parameter():
    w = make_test_worker()
    tools = {t.name: t for t in make_browser_tools(w)}
    
    assert "browser_click" in tools
    click_props = tools["browser_click"].parameters.get("properties", {})
    assert "ref" in click_props
    assert "首选" in click_props["ref"].get("description", "")

    assert "browser_fill_input" in tools
    fill_props = tools["browser_fill_input"].parameters.get("properties", {})
    assert "ref" in fill_props
    assert "首选" in fill_props["ref"].get("description", "")

    assert "browser_hover" in tools
    hover_props = tools["browser_hover"].parameters.get("properties", {})
    assert "ref" in hover_props

    assert "browser_select" in tools
    select_props = tools["browser_select"].parameters.get("properties", {})
    assert "ref" in select_props


def test_clean_screenshot_som_toggle():
    w = make_test_worker()
    eval_calls = []

    def fake_eval(js, *a):
        eval_calls.append((js, a))
        return None

    def fake_screenshot(**kw):
        return b"fake_screenshot_bytes"

    w._raw_page.evaluate = fake_eval
    w._raw_page.screenshot = fake_screenshot

    # clean=False (默认): evaluate 传 clean=False (保留 SoM)
    w._clean_screenshot(clean=False)
    assert len(eval_calls) >= 2
    assert eval_calls[0][1] == (False,)

    # clean=True: evaluate 传 clean=True (隐藏 SoM)
    eval_calls.clear()
    w._clean_screenshot(clean=True)
    assert len(eval_calls) >= 2
    assert eval_calls[0][1] == (True,)

    # Tool schema 包含 clean 参数
    tools = {t.name: t for t in make_browser_tools(w)}
    shot_props = tools["browser_screenshot"].parameters.get("properties", {})
    assert "clean" in shot_props

