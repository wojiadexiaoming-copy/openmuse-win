"""M3 测试：熔断器 + profile 隔离（纯逻辑）。浏览器真机测试见下。"""
import time

import pytest

from tools.browser.circuit import CircuitBreaker
from tools.browser.profiles import ProfileManager


def test_circuit_l1_then_recover():
    cb = CircuitBreaker("p")
    cb.record("captcha")
    cb.record("captcha")
    assert cb.level == 1
    assert cb.delay_factor() == 2.0
    assert cb.paused is False


def test_circuit_l2():
    cb = CircuitBreaker("p")
    for _ in range(3):
        cb.record("captcha")
    assert cb.level == 2
    assert cb.paused is True
    assert cb.check_before_action("p") is not None
    cb.manual_resume()
    assert cb.paused is False and cb.level == 0


def test_circuit_l3_banned():
    cb = CircuitBreaker("p")
    cb.record("banned")
    assert cb.level == 3
    assert cb.try_recover() is False


def test_circuit_warning_direct_l2():
    cb = CircuitBreaker("p")
    cb.record("warning_banner")
    assert cb.level == 2


def test_profiles(tmp_path):
    pm = ProfileManager(tmp_path / "profiles.yaml", tmp_path / "browsers")
    pm.add("daily", automation_allowed=False, note="日常")
    pm.add("work", automation_allowed=True)
    assert pm.is_automation_allowed("daily") is False
    assert pm.is_automation_allowed("work") is True
    d = pm.user_data_dir("work")
    assert d.exists() and "profile_work" in str(d)


@pytest.mark.skipif(True, reason="需 Chromium 真机，CI 跳过；本地 pytest -m 'not skip' 可跑")
def test_browser_worker_real():
    pass


def test_browser_snapshot_real(tmp_path):
    """真机：CDP 无障碍树实测（Linux + headless Chromium）。"""
    import os
    import pathlib
    import shutil
    has_chromium = (
        shutil.which("chromium") is not None
        or bool(list(pathlib.Path.home().glob(".cache/ms-playwright/chromium*")))
        or bool(list(pathlib.Path(os.environ.get("LOCALAPPDATA", "")).glob("ms-playwright/chromium*")))
    )
    if not has_chromium:
        pytest.skip("无 Chromium")
    from tools.browser.circuit import CircuitBreaker
    from tools.browser.profiles import ProfileManager
    from tools.browser.worker import BrowserWorker
    pm = ProfileManager(tmp_path / "p.yaml", tmp_path / "b")
    pm.add("test", automation_allowed=True)
    cb = CircuitBreaker("test")
    w = BrowserWorker(pm, cb, "test", headless=True)
    w.start()
    try:
        w._page.set_content("<button>提交</button><a href='#'>链接</a><input type='number' placeholder='年龄' />")
        out = w.snapshot({})
        assert "button" in out and "提交" in out
        assert "年龄" in out
        # 日常 profile 禁止自动化
        pm.add("daily", automation_allowed=False)
        w2 = BrowserWorker(pm, cb, "daily", headless=True)
        with pytest.raises(PermissionError):
            w2.start()
    finally:
        w.stop()


def test_browser_click_exact_and_type_enter(tmp_path):
    """验证 :has-text 模糊匹配自动纠正为 :text-is 精确匹配，以及 type_text press_enter。"""
    import os
    import pathlib
    import shutil
    has_chromium = (
        shutil.which("chromium") is not None
        or bool(list(pathlib.Path.home().glob(".cache/ms-playwright/chromium*")))
        or bool(list(pathlib.Path(os.environ.get("LOCALAPPDATA", "")).glob("ms-playwright/chromium*")))
    )
    if not has_chromium:
        pytest.skip("无 Chromium")
    from tools.browser.circuit import CircuitBreaker
    from tools.browser.profiles import ProfileManager
    from tools.browser.worker import BrowserWorker
    pm = ProfileManager(tmp_path / "p.yaml", tmp_path / "b")
    pm.add("test", automation_allowed=True)
    cb = CircuitBreaker("test")
    w = BrowserWorker(pm, cb, "test", headless=True)
    w.start()
    try:
        # 1. 验证精确点击防串扰：页面同时有“使用电话号码继续”和“继续”
        html = """
        <html><body>
          <button id="phone-btn" onclick="window.clicked = 'phone'">使用电话号码继续</button>
          <button id="continue-btn" onclick="window.clicked = 'continue'">继续</button>
        </body></html>
        """
        w._page.set_content(html)
        res = w.click({"selector": 'button:has-text("继续")'})
        assert "已点击" in res
        assert w._page.evaluate("() => window.clicked") == "continue"

        # 2. 验证 type_text 支持 press_enter
        form_html = """
        <html><body>
          <form onsubmit="window.submitted = true; return false;">
            <input id="email" type="email" />
          </form>
        </body></html>
        """
        w._page.set_content(form_html)
        res_type = w.type_text({"selector": "#email", "text": "test@example.com", "press_enter": True})
        assert "已按回车提交" in res_type
        assert w._page.evaluate("() => window.submitted") is True
    finally:
        w.stop()


def test_browser_save_image_and_url_header(tmp_path):
    """验证快照返回 URL 头以及 browser_save_image 本地落盘能力。"""
    import os
    import pathlib
    import shutil
    has_chromium = (
        shutil.which("chromium") is not None
        or bool(list(pathlib.Path.home().glob(".cache/ms-playwright/chromium*")))
        or bool(list(pathlib.Path(os.environ.get("LOCALAPPDATA", "")).glob("ms-playwright/chromium*")))
    )
    if not has_chromium:
        pytest.skip("无 Chromium")
    from tools.browser.circuit import CircuitBreaker
    from tools.browser.profiles import ProfileManager
    from tools.browser.worker import BrowserWorker, make_browser_tools
    pm = ProfileManager(tmp_path / "p.yaml", tmp_path / "b")
    pm.add("test", automation_allowed=True)
    cb = CircuitBreaker("test")
    w = BrowserWorker(pm, cb, "test", headless=True)
    w.start()
    try:
        # 验证 make_browser_tools 包含 browser_save_image
        tools = make_browser_tools(w)
        tool_names = [t.name for t in tools]
        assert "browser_save_image" in tool_names

        # 验证 snapshot 包含 [当前页面] URL
        html = """
        <html><head><title>海报页面</title></head><body>
          <img id="poster" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" alt="2027蛇年海报" />
          <button>下载</button>
        </body></html>
        """
        w._page.set_content(html)
        snap = w.snapshot({})
        assert "[当前页面]" in snap
        assert "URL:" in snap

        # 验证 save_image
        out_img = tmp_path / "downloads" / "poster.png"
        res_save = w.save_image({"selector": "#poster", "out_path": str(out_img)})
        assert "图片保存成功" in res_save
        assert out_img.exists()
        assert out_img.stat().st_size > 0
    finally:
        w.stop()


def test_browser_worker_navigate_url_cleanup(tmp_path):
    from unittest.mock import MagicMock
    from tools.browser.circuit import CircuitBreaker
    from tools.browser.profiles import ProfileManager
    from tools.browser.worker import BrowserWorker
    pm = ProfileManager(tmp_path / "p.yaml", tmp_path / "b")
    pm.add("test", automation_allowed=True)
    cb = CircuitBreaker("test")
    w = BrowserWorker(pm, cb, "test", headless=True)
    w._do_guard = MagicMock(return_value=None)
    w._human_delay = MagicMock()
    mock_page = MagicMock()
    mock_page.title.return_value = "Gemini"
    w._raw_page = mock_page

    # 1. 域名幻觉纠正
    w._do_navigate({"url": "https://geminiapi.com"})
    mock_page.goto.assert_called_with(
        "https://gemini.google.com", wait_until="domcontentloaded", timeout=15000
    )

    # 2. 补全协议头
    w._do_navigate({"url": "gemini.google.com"})
    mock_page.goto.assert_called_with(
        "https://gemini.google.com", wait_until="domcontentloaded", timeout=15000
    )

    # 3. 空 URL
    res = w._do_navigate({"url": ""})
    assert "URL 不能为空" in res


