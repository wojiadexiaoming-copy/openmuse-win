"""单元测试：Milestone 1 - 浮层作用域感知与优先检索（Active Modal Scope）。
验证对标准 dialog、ARIA 弹窗及 SPA 浮层容器的识别，以及定位器优先限制在浮层内。
"""
from unittest.mock import MagicMock
from tools.browser.worker import BrowserWorker


class FakeLocator:
    def __init__(self, count_val=1, visible=True):
        self._count = count_val
        self._visible = visible

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return self._visible

    def locator(self, sub_sel):
        # 模拟在浮层内查找子元素
        if "textarea" in sub_sel:
            return FakeLocator(count_val=1)
        return FakeLocator(count_val=0)


class FakePageWithModal:
    def __init__(self, has_modal=True):
        self.url = "https://example.com/explore"
        self._has_modal = has_modal
        self.main_frame = self
        self.frames = [self]

    def title(self):
        return "Example Community"

    def evaluate(self, js, *args):
        # 模拟 _detect_active_modal 返回
        if "standardSelectors" in js:
            if self._has_modal:
                return {
                    "has_modal": True,
                    "selector": ".note-detail-mask",
                    "title": "笔记详情浮层",
                    "rect": {"x": 100, "y": 50, "width": 800, "height": 700},
                    "zIndex": 100,
                }
            return {"has_modal": False}
        if "document.createTreeWalker" in js:
            # 模拟 find_text
            text_arg, modal_sel = args[0] if args and isinstance(args[0], list) else ("", "")
            return [
                {"x": 500, "y": 600, "w": 200, "h": 40, "text": "评论：说点什么", "inModal": bool(modal_sel)},
                {"x": 100, "y": 20, "w": 100, "h": 30, "text": "背景搜索框", "inModal": False},
            ]
        return None

    def locator(self, selector):
        if selector == ".note-detail-mask":
            return FakeLocator(count_val=1)
        # 背景层全局 textarea
        return FakeLocator(count_val=1)


def make_worker(has_modal=True):
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = FakePageWithModal(has_modal=has_modal)
    w.headless = True
    w.split_screen = False
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._layout_note = lambda: ""
    w._ensure_maximized = lambda: None
    w._sync_tabs = lambda: None
    w._tabs = {}
    w._get_viewport = lambda: {"width": 1280, "height": 800}
    return w


def test_detect_active_modal_present():
    w = make_worker(has_modal=True)
    modal = w._detect_active_modal()
    assert modal["has_modal"] is True
    assert modal["selector"] == ".note-detail-mask"
    assert modal["title"] == "笔记详情浮层"


def test_detect_active_modal_absent():
    w = make_worker(has_modal=False)
    modal = w._detect_active_modal()
    assert modal["has_modal"] is False


def test_find_locator_scoped_to_modal():
    w = make_worker(has_modal=True)
    frame, loc, frame_id = w._find_locator("textarea")
    assert frame_id == "f0_modal"
    assert loc is not None
    assert loc.count() == 1


def test_find_locator_fallback_when_no_modal():
    w = make_worker(has_modal=False)
    frame, loc, frame_id = w._find_locator("textarea")
    assert frame_id == "f0"
    assert loc is not None


def test_find_text_prioritizes_modal():
    w = make_worker(has_modal=True)
    out = w._do_find_text({"text": "评论"})
    assert "[文本定位]" in out
    assert "优先置顶浮层内匹配" in out
    assert "[浮层内]" in out


def test_snapshot_includes_modal_header():
    w = make_worker(has_modal=True)
    # mock _do_layout_tree to avoid complex tree traversal
    w._do_layout_tree = lambda args: "  1. [文本框] 说点什么"
    snap = w._do_snapshot({})
    assert "[活跃浮层] 笔记详情浮层 (容器: .note-detail-mask)" in snap
