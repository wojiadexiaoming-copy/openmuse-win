"""v1.5.28 §39.25：锁/解锁工具必须编组到 Playwright 线程；CDP 能力缺失不算锁定失败。

背景（2026-10-28 生产日志）：模型调用 browser_unlock_input 时出现
`greenlet.error: Cannot switch to a different thread` 与
"Task exception was never retrieved ... Emulation.setIgnoreInputEvents wasn't found"。
根因：两个工具的 func 是裸 lambda，直接在工作线程（asyncio 线程池）里调用了
worker.lock_input_for_agent()/unlock_input_for_human()，而 Playwright sync 对象
只能在创建它的 pw_* 线程使用；同时 Edge 内核缺少该 CDP 命令，旧实现把
"CDP 不支持" 当成整体失败，误报 "[解锁失败] 无可用页面"。

本文件锁定：
1. browser_lock_input / browser_unlock_input 的工具函数必须经 worker._run 编组；
2. _guard_locked 只在 DOM 锁真正生效后才置 True；
3. CDP 命令缺失（wasn't found）只探测一次，之后跳过，锁/解锁仍返回成功；
4. 非能力性 CDP 错误仍允许下次重试；
5. 真实 Edge headless：DOM 遮罩照常切换，工具不再误报失败。
"""
from unittest.mock import MagicMock

import pytest

from tools.browser.input_guard import BrowserInputGuard
from tools.browser.worker import BrowserWorker, make_browser_tools


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
    page.set_content("<html><body><h1>输入锁目标页</h1></body></html>")
    yield page
    page.close()


def _tool_by_name(worker, name):
    return {t.name: t for t in make_browser_tools(worker)}[name]


def test_lock_tool_goes_through_run():
    worker = BrowserWorker(None, None, "default")
    guard = MagicMock()
    guard.lock_for_agent.return_value = True
    worker._input_guard = guard

    calls = []

    def fake_run(fn, *args, **kwargs):
        calls.append(fn.__name__)
        return fn(*args, **kwargs)

    worker._run = fake_run
    out = _tool_by_name(worker, "browser_lock_input").func({})

    assert calls == ["_do_lock_input_tool"]
    guard.lock_for_agent.assert_called_once()
    assert "已启用" in out


def test_unlock_tool_goes_through_run():
    worker = BrowserWorker(None, None, "default")
    guard = MagicMock()
    guard.unlock_for_human.return_value = True
    worker._input_guard = guard

    calls = []

    def fake_run(fn, *args, **kwargs):
        calls.append(fn.__name__)
        return fn(*args, **kwargs)

    worker._run = fake_run
    out = _tool_by_name(worker, "browser_unlock_input").func({})

    assert calls == ["_do_unlock_input_tool"]
    guard.unlock_for_human.assert_called_once()
    assert "已解除" in out


def test_lock_flag_only_set_after_success():
    worker = BrowserWorker(None, None, "default")
    worker._guard_locked = False
    guard = MagicMock()
    worker._input_guard = guard

    guard.lock_for_agent.return_value = False
    assert worker.lock_input_for_agent() is False
    assert worker._guard_locked is False

    guard.lock_for_agent.return_value = True
    assert worker.lock_input_for_agent() is True
    assert worker._guard_locked is True


def test_tool_reports_failure_when_overlay_rejected():
    worker = BrowserWorker(None, None, "default")
    guard = MagicMock()
    guard.lock_for_agent.return_value = False
    worker._input_guard = guard
    worker._run = lambda fn, *a, **k: fn(*a, **k)

    out = _tool_by_name(worker, "browser_lock_input").func({})
    assert "锁定失败" in out and "遮罩注入被拒" in out

    worker._input_guard = None
    out = _tool_by_name(worker, "browser_unlock_input").func({})
    assert out == "[解锁失败] 无可用页面"


def test_missing_cdp_method_does_not_fail_lock():
    page = MagicMock()
    cdp = MagicMock()
    cdp.send.side_effect = Exception(
        "CDPSession.send: Protocol error (Emulation.setIgnoreInputEvents): "
        "'Emulation.setIgnoreInputEvents' wasn't found")
    page.context.new_cdp_session.return_value = cdp

    guard = BrowserInputGuard(page)
    assert guard.lock_for_agent() is True
    assert guard.mode == "AGENT_ACTIVE"
    assert guard._cdp_unsupported is True
    assert cdp.send.call_count == 1

    assert guard.unlock_for_human(reason="用户接管", task_id="t1") is True
    assert guard.release_all() is True
    # 已确认不支持后不再新建会话、不再发 CDP 命令
    assert page.context.new_cdp_session.call_count == 1
    assert cdp.send.call_count == 1


def test_transient_cdp_error_can_retry():
    page = MagicMock()
    cdp = MagicMock()
    cdp.send.side_effect = Exception("Target closed")
    page.context.new_cdp_session.return_value = cdp

    guard = BrowserInputGuard(page)
    assert guard.lock_for_agent() is True
    assert guard._cdp_unsupported is False

    assert guard.lock_for_agent() is True
    assert cdp.send.call_count == 2
    assert page.context.new_cdp_session.call_count == 2


def test_real_edge_lock_survives_cdp_gap(edge_page):
    guard = BrowserInputGuard(edge_page)

    assert guard.lock_for_agent() is True
    assert edge_page.evaluate("window.__alterego_mode") == "AGENT_ACTIVE"
    assert guard.is_locked is True

    assert guard.unlock_for_human(reason="真实 Edge 验证", task_id="t_edge") is True
    assert edge_page.evaluate("window.__alterego_mode") == "HUMAN_TAKEOVER"
    assert guard.is_locked is False

    assert guard.release_all() is True
    assert guard.mode == "IDLE"
