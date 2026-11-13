"""v1.5.25 P0：可编辑目标排除 file/非文本 input + 懒挂载编辑器原生事件链激活。

背景（2026-10-28 抖音评论事故）：评论容器里只有一个隐藏的 input[type=file]（选图上传），
browser_fill_input 把它当成评论框：既点不开 Draft.js 编辑器，也拿不到焦点；
随后子 Agent 误判为「未登录」，把用户拖进人工接管。

本文件锁定：
1. hidden/file/checkbox/radio 等控件永不被当作 fill 目标；
2. 占位容器点击后编辑器仍未挂载时，补完整 pointer/mouse/click 链并重新标记；
3. 激活只作用于显式目标/落点，页面其它输入框（如背景搜索框）不被写入；
4. 编辑器始终不挂载时失败文案诚实，不宣称成功。
"""
from pathlib import Path

import pytest

from tools.browser.worker import (
    BrowserWorker,
    _ACTIVATE_NODE_JS,
    _FILL_MARK_JS,
    _FILL_POINT_MARK_JS,
)


LAZY_COMMENT_FIXTURE = """
<style>
  #box { width: 360px; height: 76px; background: #f5f5f5; border: 1px solid #ccc; }
  #box .ph { color: #999; font: 14px sans-serif; padding: 26px 12px; }
  #box [contenteditable="true"] { width: 320px; min-height: 36px; margin: 14px; background: #fff;
    outline: none; font: 14px sans-serif; }
</style>
<input id="bg-search" placeholder="背景搜索框">
<div id="box" class="comment-input-inner-container">
  <span class="ph">说点什么...</span>
  <input id="picker" type="file" style="display:none">
  <input id="check" type="checkbox" style="display:none">
</div>
<script>
(() => {
  const box = document.getElementById('box');
  const want = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
  const seen = [];
  const mount = () => {
    if (box.querySelector('[contenteditable="true"]')) return;
    const ed = document.createElement('div');
    ed.setAttribute('contenteditable', 'true');
    box.appendChild(ed);
    ed.focus();
  };
  // 只信合成事件：模拟真实站点里 Playwright 点击被隐藏控件/覆盖层吞掉后编辑器不挂载
  for (const t of want) {
    box.addEventListener(t, (e) => {
      if (e.isTrusted) return;
      seen.push(t);
      if (want.every(x => seen.includes(x))) mount();
    }, true);
  }
})();
</script>
"""


@pytest.fixture(scope="module")
def lazy_page():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - 环境缺失时跳过
        pytest.skip(f"playwright 不可用: {exc}")
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(channel="msedge", headless=True)
    except Exception as exc:  # pragma: no cover - 无 Edge 时跳过
        pytest.skip(f"Edge headless 不可用: {exc}")
    page = browser.new_page(viewport={"width": 1000, "height": 800})
    yield page
    browser.close()
    pw.stop()


def _fresh(page):
    page.set_content(LAZY_COMMENT_FIXTURE)
    return page.locator("#box")


def _real_page_worker(page):
    """只接管真实 DOM 的 worker：其余方法保持生产实现。"""
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = page
    w.smart_zoom = None
    w._channel_progress = {}
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w._os_paste = lambda text, clear_before=True: False
    return w


def test_hidden_file_input_is_never_a_fill_target(lazy_page):
    _fresh(lazy_page)
    mark = lazy_page.locator("#picker").evaluate(_FILL_MARK_JS)
    assert mark.get("ok") is False
    assert mark.get("reason") == "not_editable"


def test_placeholder_container_with_only_file_input_is_not_editable(lazy_page):
    box = _fresh(lazy_page)
    mark = box.evaluate(_FILL_MARK_JS)
    assert mark.get("ok") is False
    assert "input" not in (mark.get("tag") or "")

    point = lazy_page.evaluate(_FILL_POINT_MARK_JS, [180, 38])
    assert point.get("ok") is False


def test_activation_chain_mounts_editor_then_mark_succeeds(lazy_page):
    box = _fresh(lazy_page)
    assert box.evaluate(_FILL_MARK_JS).get("ok") is False

    act = box.evaluate(_ACTIVATE_NODE_JS)
    assert act.get("ok") is True
    lazy_page.wait_for_selector('#box [contenteditable="true"]', timeout=2000)

    mark = box.evaluate(_FILL_MARK_JS)
    assert mark.get("ok") is True
    assert mark.get("is_contenteditable") is True
    assert mark.get("focused") is True


def test_fill_input_activates_lazy_editor_and_writes(lazy_page):
    box = _fresh(lazy_page)
    w = _real_page_worker(lazy_page)

    out = w._do_fill_input({"selector": ".comment-input-inner-container", "text": "今天天气不错"})

    assert "[输入成功]" in out
    assert "原生事件链激活" in out
    assert "今天天气不错" in box.locator('[contenteditable="true"]').inner_text()
    # 背景搜索框绝不能被改写
    assert lazy_page.locator("#bg-search").input_value() == ""


def test_fill_input_fails_honestly_when_editor_never_mounts(lazy_page):
    lazy_page.set_content(LAZY_COMMENT_FIXTURE.replace("if (e.isTrusted) return;", "return;"))
    box = lazy_page.locator("#box")
    w = _real_page_worker(lazy_page)

    out = w._do_fill_input({"selector": ".comment-input-inner-container", "text": "写不进去"})

    assert "[输入成功]" not in out
    assert "[输入失败]" in out
    assert "禁止改填其它输入框" in out
    assert box.locator('[contenteditable="true"]').count() == 0
    assert lazy_page.locator("#bg-search").input_value() == ""


def test_prompt_documents_standalone_video_page_activation():
    browser = Path("prompts/skills/browser.md").read_text(encoding="utf-8")
    assert "/video/" in browser
    assert "原生事件链" in browser
    assert "编辑器未挂载" in browser


# ---------- P1-4 接管完成核验 ----------

def _takeover_worker(*, verify_js_value=False, selector_found=False):
    from unittest.mock import MagicMock
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = MagicMock()
    w._raw_page.url = "https://www.douyin.com/video/712345"
    w._raw_page.evaluate = MagicMock(return_value=verify_js_value)
    w._ensure_maximized = MagicMock()
    w._do_focus_window = MagicMock()
    w._clean_screenshot = MagicMock(return_value=b"")
    w.unlock_input_for_human = MagicMock()
    w.lock_input_for_agent = MagicMock()
    w.release_input_guard = MagicMock()
    w._do_snapshot = MagicMock(return_value="[当前页面] 测试快照")
    loc = MagicMock()
    loc.count.return_value = 1 if selector_found else 0
    loc.first.is_visible.return_value = selector_found
    w._find_locator = MagicMock(return_value=(None, loc, "f0"))
    return w


def _patch_takeover_manager(monkeypatch, outcome="completed"):
    from unittest.mock import MagicMock
    import agent.takeover as takeover_mod

    mgr = MagicMock()
    # §39.24：worker 改用泵式等待（poll），不再阻塞 Event.wait
    mgr.poll.return_value = takeover_mod.TakeoverResult(
        task_id="t_v1525", outcome=outcome)
    monkeypatch.setattr(takeover_mod, "get_takeover_manager", lambda: mgr)
    return mgr


def test_takeover_verify_selector_failure_is_reported(monkeypatch):
    w = _takeover_worker(selector_found=False)
    _patch_takeover_manager(monkeypatch)

    out = w._do_request_takeover("评论没发出去", 5.0, verify_selector=".comment-item")

    assert "[接管完成但核验未通过]" in out
    assert "选择器 '.comment-item' 未找到或不可见" in out
    assert "[人工接管已完成]" not in out


def test_takeover_verify_js_failure_is_reported(monkeypatch):
    w = _takeover_worker(verify_js_value=False)
    _patch_takeover_manager(monkeypatch)

    out = w._do_request_takeover(
        "评论没发出去", 5.0, verify_js="document.querySelector('.comment-item') !== null")

    assert "[接管完成但核验未通过]" in out
    assert "核验脚本返回 false" in out
    assert "[人工接管已完成]" not in out


def test_takeover_verify_js_success_marks_verified(monkeypatch):
    w = _takeover_worker(verify_js_value=True, selector_found=True)
    _patch_takeover_manager(monkeypatch)

    out = w._do_request_takeover("评论没发出去", 5.0, verify_selector=".comment-item")

    assert "[人工接管已完成]" in out
    assert "核验通过" in out and "选择器 '.comment-item' 可见" in out
    assert "[接管完成但核验未通过]" not in out


# ---------- P1-5 出口拦截扩展与子 Agent 证据收紧 ----------

def test_manual_offload_gate_catches_login_offload():
    from agent.orchestrator import Orchestrator

    assert Orchestrator._manual_offload_snippet("抖音页面显示未登录，请你扫码登录后再试")
    assert Orchestrator._manual_offload_snippet("这个站不好用，换个网站试试吧")
    assert Orchestrator._manual_offload_snippet("已经登录成功，评论区出现新内容") == ""


def test_subagent_takeover_mention_alone_does_not_exempt_gate():
    from types import SimpleNamespace
    from agent.models import Message
    from agent.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._turn_browser_touched = True
    orch._turn_takeover_used = False
    orch._manual_offload_nudges = 0
    orch._note_turn_tool_usage([], [SimpleNamespace(
        name="wait_subagent",
        content="子 Agent 建议人工接管，但实际未调用；请你扫码登录")])

    assert orch._turn_takeover_used is False
    assert orch._should_block_manual_offload(
        Message(role="assistant", content="抖音没登录，请你扫码登录")) != ""


def test_subagent_takeover_completion_counts_as_used():
    from types import SimpleNamespace
    from agent.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._turn_browser_touched = True
    orch._turn_takeover_used = False
    orch._manual_offload_nudges = 0
    orch._note_turn_tool_usage([], [SimpleNamespace(
        name="wait_subagent",
        content="[人工接管已完成] 用户已完成滑块验证并交还控制权")])

    assert orch._turn_takeover_used is True
