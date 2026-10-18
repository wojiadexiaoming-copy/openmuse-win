"""单元测试：Milestone 4 - 浏览器全流程录屏与生命周期录像工具（browser_record_*）。
验证：
1. 录屏启动 (record_start) 与停止 (record_stop) 流程；
2. 状态机防护（重复启动、未启动即停止）；
3. auto_record 自动录屏生命周期联动；
4. 工具注册表完整暴露 browser_record_start / browser_record_stop / browser_fill_input。
"""
from pathlib import Path
from unittest.mock import MagicMock
from tools.browser.worker import BrowserWorker, make_browser_tools


class FakeController:
    def __init__(self, output_mp4="artifacts/videos/test.mp4"):
        self.output_mp4 = output_mp4
        self._recording = False
        self.started_pages = []

    def start_recording(self, page):
        self._recording = True
        self.started_pages.append(page)
        return True

    def stop_recording(self):
        self._recording = False
        Path(self.output_mp4).parent.mkdir(parents=True, exist_ok=True)
        Path(self.output_mp4).write_bytes(b"FAKE_MP4_HEADER")
        return self.output_mp4


def make_worker_for_record(tmp_path, auto_record=True):
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = MagicMock()
    w.headless = True
    w.split_screen = False
    w.auto_record = auto_record
    w.multitab_controller = FakeController(output_mp4=str(tmp_path / "session_123.mp4"))
    w.smart_zoom = MagicMock()
    w._input_guard = None
    w._guard_locked = False
    w.circuit = MagicMock()
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._ensure_maximized = lambda: None
    w._layout_note = lambda: ""
    w._apply_adapter_context = lambda u, m: m
    w._url_state = {}
    w._sync_tabs = lambda: None
    w.lock_input_for_agent = lambda: True
    w._tabs = {}
    return w


def test_record_start_and_stop_lifecycle(tmp_path):
    w = make_worker_for_record(tmp_path)
    res_start = w._do_record_start({"name": "xhs_demo"})
    assert "[录屏已启动]" in res_start
    assert w.multitab_controller._recording is True

    # 重复启动防护
    res_start2 = w._do_record_start({})
    assert "[录屏已在进行中]" in res_start2

    # 停止录屏
    res_stop = w._do_record_stop({})
    assert "[录屏已完成]" in res_stop
    assert "xhs_demo" in res_stop
    assert w.multitab_controller._recording is False

    # 未录制时停止防护
    res_stop2 = w._do_record_stop({})
    assert "[录屏未在进行]" in res_stop2


def test_auto_record_on_navigate(tmp_path):
    w = make_worker_for_record(tmp_path, auto_record=True)
    assert w.multitab_controller._recording is False

    w._raw_page.url = "https://example.com"
    w._raw_page.goto = MagicMock()
    w._raw_page.title = MagicMock(return_value="Example")

    w._do_navigate({"url": "https://example.com"})
    assert w.multitab_controller._recording is True
    assert len(w.multitab_controller.started_pages) == 1


def test_auto_record_disabled_on_navigate(tmp_path):
    w = make_worker_for_record(tmp_path, auto_record=False)
    assert w.multitab_controller._recording is False

    w._raw_page.url = "https://example.com"
    w._raw_page.goto = MagicMock()
    w._raw_page.title = MagicMock(return_value="Example")

    w._do_navigate({"url": "https://example.com"})
    assert w.multitab_controller._recording is False


def test_tools_registered_in_make_browser_tools(tmp_path):
    w = make_worker_for_record(tmp_path)
    tools = make_browser_tools(w)
    tool_names = {t.name for t in tools}

    assert "browser_fill_input" in tool_names
    assert "browser_record_start" in tool_names
    assert "browser_record_stop" in tool_names
    assert "browser_type" in tool_names
    assert "browser_snapshot" in tool_names
