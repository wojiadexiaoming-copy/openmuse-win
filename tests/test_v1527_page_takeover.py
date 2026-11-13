"""v1.5.27 §39.24：网页内接管按钮不再卡死执行线程；泵式等待可收取页内按钮点击。

背景（2026-10-28 豆包事故）：用户点【👉 申请人工接管】后界面只停在"⏳ 正在切入接管..."，
连正在执行的 browser_evaluate 也永不返回，之后所有浏览器调用全部超时。
根因：Playwright sync 的 expose_binding 回调运行在驱动 greenlet 里，回调内再调用
同步 Playwright API（new_cdp_session / cdp.send / page.evaluate）会把工作线程永久卡死。

本文件锁定：
1. 网页横幅回调零 Playwright 调用（纯内存状态登记）；
2. 真实 Edge：点【申请人工接管】立即本地切到接管态并登记 source=page 请求；
3. 真实 Edge：在 in-flight evaluate 期间点击也不卡死；
4. 真实 Edge：Agent 发起的接管，页内【✓ 完成接管】能被泵式等待即时收到；
5. worker 每次调用前后消费页内请求：完成后回锁、等待中暂停、超时不改写请求；
6. TakeoverManager.poll / source 契约。
"""
import threading
import time
from unittest.mock import MagicMock

import pytest

from agent.takeover import TakeoverManager
from tools.browser.input_guard import BrowserInputGuard, INJECT_OVERLAY_JS
from tools.browser.worker import BrowserWorker


@pytest.fixture(scope="module")
def edge_browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - 环境缺失时跳过
        pytest.skip(f"playwright 不可用: {exc}")
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(channel="msedge", headless=True)
    except Exception as exc:  # pragma: no cover - 无 Edge 时跳过
        pytest.skip(f"Edge headless 不可用: {exc}")
    yield browser
    browser.close()
    pw.stop()


@pytest.fixture
def edge_page(edge_browser):
    page = edge_browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content(
        "<html><body><h1>目标页面</h1><button id='real-btn'>真实按钮</button></body></html>")
    yield page
    page.close()


def _patch_manager(monkeypatch):
    mgr = TakeoverManager(on_event=lambda event, payload: None)
    monkeypatch.setattr("agent.takeover.get_takeover_manager", lambda: mgr)
    return mgr


def _pump_until(page, pred, timeout=3.0, step=50):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        page.wait_for_timeout(step)
    return pred()


def _active_tid(mgr):
    with mgr._lock:
        for tid, req in mgr._requests.items():
            if req.status == "active":
                return tid
    return ""


# ---------- 1. 回调纯内存 ----------

def test_callback_never_touches_playwright(monkeypatch):
    mgr = _patch_manager(monkeypatch)
    page = MagicMock()
    guard = BrowserInputGuard(page)
    page.reset_mock()

    guard._on_human_finished_from_ui(
        source={}, outcome="request_takeover", task_id="t_pure",
        page_url="https://example.com/login")

    assert page.evaluate.call_count == 0
    assert page.context.new_cdp_session.call_count == 0
    assert guard.mode == "HUMAN_TAKEOVER"
    assert guard.is_locked is False
    assert guard.peek_page_request() == "t_pure"
    status = mgr.get_status("t_pure")
    assert status["status"] == "active"
    assert status["source"] == "page"
    assert status["page_url"] == "https://example.com/login"

    guard._on_human_finished_from_ui(
        source={}, outcome="completed", task_id="t_pure")
    assert page.evaluate.call_count == 0
    assert guard.mode == "AGENT_ACTIVE"
    assert guard.peek_page_request() == ""
    assert mgr.get_status("t_pure")["status"] == "completed"


def test_callback_ignores_unknown_outcome_as_completed(monkeypatch):
    mgr = _patch_manager(monkeypatch)
    guard = BrowserInputGuard(MagicMock())
    mgr.request_takeover(task_id="t_odd", reason="r", source="page")

    guard._on_human_finished_from_ui(source={}, outcome="weird", task_id="t_odd")

    assert mgr.get_status("t_odd")["status"] == "completed"
    assert "网页横幅" in mgr.get_status("t_odd")["summary"]


# ---------- 2. 真实 Edge：申请接管 ----------

def test_real_edge_request_takeover_switches_banner_locally(edge_page, monkeypatch):
    mgr = _patch_manager(monkeypatch)
    guard = BrowserInputGuard(edge_page)
    guard.lock_for_agent()  # headless Edge 无 Emulation.setIgnoreInputEvents，返回值不可作为判据
    assert edge_page.evaluate("() => typeof window.__ae_set_mode") == "function"
    assert edge_page.evaluate("() => window.__alterego_mode") == "AGENT_ACTIVE"

    edge_page.click("#__ae_btn_req_takeover")

    # 视觉切换由 JS 本地完成，不依赖 Python 回包
    assert edge_page.evaluate("() => window.__alterego_mode") == "HUMAN_TAKEOVER"
    assert edge_page.evaluate("() => !!document.getElementById('__ae_btn_done')") is True
    assert edge_page.evaluate(
        "() => window.__ae_pending_takeover && window.__ae_pending_takeover.kind") == "request"

    assert _pump_until(edge_page, lambda: bool(_active_tid(mgr)))
    tid = _active_tid(mgr)
    assert edge_page.evaluate("() => window.__alterego_task_id") == tid
    assert mgr.get_status(tid)["source"] == "page"
    assert guard.mode == "HUMAN_TAKEOVER"
    assert guard.peek_page_request() == tid

    edge_page.click("#__ae_btn_done")
    assert _pump_until(edge_page, lambda: mgr.poll(tid) is not None)
    assert mgr.poll(tid).outcome == "completed"
    assert guard.mode == "AGENT_ACTIVE"
    assert guard.peek_page_request() == ""
    assert edge_page.evaluate("() => window.__alterego_mode") == "AGENT_ACTIVE"
    assert edge_page.evaluate("() => window.__ae_pending_takeover") is None


def test_real_edge_click_during_inflight_evaluate_does_not_deadlock(edge_page, monkeypatch):
    mgr = _patch_manager(monkeypatch)
    guard = BrowserInputGuard(edge_page)
    guard.lock_for_agent()

    started = time.time()
    out = edge_page.evaluate("""async () => {
        await new Promise(r => setTimeout(r, 200));
        document.getElementById('__ae_btn_req_takeover').click();
        await new Promise(r => setTimeout(r, 300));
        return 'alive';
    }""")
    elapsed = time.time() - started

    assert out == "alive"
    assert elapsed < 10  # 旧实现会在这里永久卡死
    assert guard.mode == "HUMAN_TAKEOVER"
    assert guard.peek_page_request()
    assert mgr.get_status(guard.peek_page_request())["status"] == "active"


# ---------- 3. 真实 Edge：泵式等待收取页内完成点击 ----------

def test_pump_wait_receives_inpage_done_click(edge_page, monkeypatch):
    mgr = _patch_manager(monkeypatch)
    guard = BrowserInputGuard(edge_page)
    guard.lock_for_agent()

    tid = "t_pump_done"
    guard.unlock_for_human(reason="需要人工验证", task_id=tid)
    mgr.request_takeover(task_id=tid, reason="需要人工验证", timeout_sec=30.0)
    edge_page.evaluate(
        "() => setTimeout(() => document.getElementById('__ae_btn_done').click(), 300)")

    worker = BrowserWorker.__new__(BrowserWorker)
    worker._raw_page = edge_page
    started = time.time()
    res = worker._pump_wait_takeover(mgr, tid, 8.0)
    elapsed = time.time() - started

    assert res.outcome == "completed"
    assert elapsed < 5  # 旧实现要等满 8s 超时
    assert guard.mode == "AGENT_ACTIVE"


# ---------- 4. worker 前后消费页内请求 ----------

def _page_worker(mgr, tid):
    worker = BrowserWorker.__new__(BrowserWorker)
    worker._raw_page = MagicMock()
    guard = MagicMock()
    guard.peek_page_request.return_value = tid
    guard.take_page_request.return_value = tid
    worker._input_guard = guard
    worker.lock_input_for_agent = MagicMock(return_value=True)
    worker.release_input_guard = MagicMock(return_value=True)
    worker.PAGE_TAKEOVER_WAIT_SEC = 5.0
    return worker, guard


def test_worker_drains_page_request_and_relocks(monkeypatch):
    mgr = _patch_manager(monkeypatch)
    worker, guard = _page_worker(mgr, "t_drain")
    mgr.request_takeover(task_id="t_drain", reason="用户申请", source="page")

    t = threading.Thread(
        target=lambda: (time.sleep(0.2), mgr.resolve("t_drain", "completed", "用户完成")))
    t.start()
    out = worker._call_with_page_takeover(120.0, lambda: "工具原始结果")
    t.join()

    assert out == "工具原始结果"
    guard.take_page_request.assert_called()
    worker.lock_input_for_agent.assert_called()
    worker.release_input_guard.assert_not_called()


def test_worker_pauses_action_while_user_takeover_active(monkeypatch):
    mgr = _patch_manager(monkeypatch)
    worker, guard = _page_worker(mgr, "t_busy")
    worker.PAGE_TAKEOVER_WAIT_SEC = 0.3
    mgr.request_takeover(task_id="t_busy", reason="用户申请", source="page")

    ran = []
    out = worker._call_with_page_takeover(120.0, lambda: ran.append(1) or "不该执行")

    assert ran == []
    assert "[用户正在接管中]" in out
    # 超时只暂停自动化，不改写用户仍在进行的接管请求
    assert mgr.get_status("t_busy")["status"] == "active"
    guard.take_page_request.assert_not_called()


def test_worker_releases_on_page_cancel(monkeypatch):
    mgr = _patch_manager(monkeypatch)
    worker, guard = _page_worker(mgr, "t_cancel")
    mgr.request_takeover(task_id="t_cancel", reason="用户申请", source="page")
    mgr.resolve("t_cancel", "cancelled", "用户放弃")

    out = worker._call_with_page_takeover(120.0, lambda: "不应执行")

    assert "[人工接管已取消]" in out
    worker.release_input_guard.assert_called()
    worker.lock_input_for_agent.assert_not_called()


# ---------- 5. 管理器契约 ----------

def test_takeover_manager_poll_and_source():
    mgr = TakeoverManager(on_event=lambda event, payload: None)
    req = mgr.request_takeover(task_id="p1", reason="r", source="page")
    assert req.source == "page"
    assert mgr.poll("p1") is None
    assert mgr.poll("unknown") is None

    mgr.resolve("p1", "completed", "done")
    res = mgr.poll("p1")
    assert res is not None and res.outcome == "completed"
    assert mgr.get_status("p1")["source"] == "page"
    assert mgr.get_status("p1")["status"] == "completed"


def test_overlay_js_exposes_pure_frontend_state_machine():
    assert "window.__ae_set_mode" in INJECT_OVERLAY_JS
    assert "__ae_pending_takeover" in INJECT_OVERLAY_JS
    assert "location.href" in INJECT_OVERLAY_JS
