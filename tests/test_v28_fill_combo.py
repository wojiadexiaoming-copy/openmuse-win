"""v2.8 §39.7：联想框输入回读、DOM 兜底、SoM 候选排除、通道锁半开。"""
import time

from tools.browser.worker import BrowserWorker


class FakeLoc:
    def __init__(self):
        self.value = ""
        self.filled = 0

    def scroll_into_view_if_needed(self, timeout=0):
        return None

    def bounding_box(self):
        return {"x": 40, "y": 40, "width": 300, "height": 36}

    def click(self, timeout=0):
        return None

    def focus(self):
        return None

    def fill(self, text, timeout=0):
        self.filled += 1
        self.value = text

    def evaluate(self, script, arg=None):
        return self.value

    def input_value(self, timeout=0):
        return self.value


class FakeLocator:
    def __init__(self):
        self.first = FakeLoc()

    def count(self):
        return 1


class FakeKeyboard:
    def press(self, key):
        return None

    def type(self, text, delay=0):
        return None


class FakePage:
    url = "https://example.test/search"

    def __init__(self):
        self.keyboard = FakeKeyboard()


def _worker(fake_loc: FakeLoc):
    worker = BrowserWorker.__new__(BrowserWorker)
    worker._raw_page = FakePage()
    worker._channel_progress = {}
    worker._do_guard = lambda: ""
    worker._find_locator = lambda selector, frame_ref, scope_to_modal=False: (
        None, FakeLocator(), "f0")
    worker._human_like_click = lambda x, y: None
    worker._human_delay = lambda: None
    worker._verify_expect = lambda expect: ""
    return worker


def test_input_not_landed_returns_marker_without_channel_lock():
    fake_loc = FakeLoc()
    fake_loc.fill = lambda text, timeout=0: None  # fill 兜底也写不进去
    worker = _worker(fake_loc)
    worker._find_locator = lambda selector, frame_ref, scope_to_modal=False: (
        None, _LocatorWith(fake_loc), "f0")
    worker._combo_options = lambda q, box: []

    result = worker._do_fill_combo({"ref": "r1", "text": "天津"})

    assert "[输入未落地]" in result
    assert "browser_click_at" in result
    assert worker._channel_progress == {}


class _LocatorWith:
    def __init__(self, first):
        self.first = first

    def count(self):
        return 1


def test_dom_fill_fallback_commits_candidate():
    fake_loc = FakeLoc()
    worker = _worker(fake_loc)
    worker._find_locator = lambda selector, frame_ref, scope_to_modal=False: (
        None, _LocatorWith(fake_loc), "f0")
    candidates = [
        [{"text": "天津", "x": 100, "y": 90, "hit": True, "semantic": True}],
        [],
    ]
    worker._combo_options = lambda q, box: candidates.pop(0) if candidates else []

    result = worker._do_fill_combo({"ref": "r1", "text": "天津"})

    assert fake_loc.filled == 1
    assert "[联想已提交]" in result
    assert "天津" in result


def test_combo_options_excludes_agent_overlays():
    worker = BrowserWorker.__new__(BrowserWorker)
    seen = {}

    class Page:
        def evaluate(self, script, args):
            seen["js"] = script
            return []

    worker._raw_page = Page()
    worker._combo_options("天津", {"x": 0, "y": 0, "width": 100, "height": 30})
    assert "#__ae_som" in seen["js"]
    assert "#__alterego_root" in seen["js"]


def test_channel_lock_half_opens_after_ttl():
    worker = BrowserWorker.__new__(BrowserWorker)
    worker._channel_progress = {}

    for _ in range(2):
        worker._channel_note(1, "target", "input", False)
    assert "[通道锁定" in worker._channel_guard(1, "target", "input")

    key = worker._channel_key(1, "target", "input")
    worker._channel_progress[key]["forced_at"] = time.time() - worker._CHANNEL_LOCK_TTL - 1
    assert worker._channel_guard(1, "target", "input") == ""
    assert worker._channel_progress[key]["forced"] == 0
    assert worker._channel_progress[key]["fails"] == 1
