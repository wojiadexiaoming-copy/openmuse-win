"""v1.5.22 P0：富文本输入状态真实性与 C3 原语顺序。

背景（2026-10-27 抖音评论实测）：browser_fill_input 把文字写进 Draft.js 的 DOM，
回读 innerText 也看得到，于是返回"[输入成功]"；但编辑器 React state 仍为空
（data-block=0、占位符还在），发送按钮不渲染、评论发不出去。随后 browser_paste
连续失败两次就被判 C3 2/2 → 强制 C4，把用户拖进来手动操作。

本文件锁定：
1. state 门控编辑器的落地判据必须读编辑器自身状态，DOM 假文字不算；
2. 真实逐键优先，type 失败先换 paste，两个原语都失败才升级 C4；
3. 纯 contenteditable / input 的旧路径不受影响。
"""
import pytest

from tools.browser.worker import BrowserWorker, _EDITOR_STATE_JS


class FillLocator:
    def __init__(self):
        self.first = self

    def count(self):
        return 1

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None):
        pass


def make_fill_worker(monkeypatch, *, typing_lands=False, paste_lands=False,
                     dom_text_after_typing=True):
    monkeypatch.setattr("tools.browser.worker.time.sleep", lambda _: None)
    from unittest.mock import MagicMock
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = MagicMock()
    state = {"blocks": 0, "text": "", "placeholder_visible": True}
    calls = {"typed": 0, "pasted": 0}

    def type_text(text, delay=0):
        calls["typed"] += 1
        if typing_lands:
            state.update(blocks=1, text=text, placeholder_visible=False)
        elif dom_text_after_typing:
            # DOM 有字、state 仍空：Draft.js 假草稿
            state.update(blocks=0, text=text, placeholder_visible=True)

    def os_paste(text, clear_before=True):
        calls["pasted"] += 1
        if paste_lands:
            state.update(blocks=1, text=text, placeholder_visible=False)
        return True

    w._raw_page.keyboard.type.side_effect = type_text
    w._os_paste = os_paste
    w._do_guard = lambda: None
    w._human_delay = lambda: None
    w.smart_zoom = None
    w._detect_active_modal = lambda: {"has_modal": False}
    w._find_locator = lambda selector, frame_ref=None, scope_to_modal=True: (
        None, FillLocator(), "f0")
    w._mark_fill_target = lambda loc: {
        "ok": True, "tag": "div", "is_input": False, "is_contenteditable": True}
    w._focus_fill_target = lambda frame=None, target_loc=None: {
        "found": True, "focused": True, "tag": "div", "active_tag": "div",
        "is_input": False, "is_contenteditable": True}
    w._fill_target_state = lambda frame=None: {
        "found": True, "focused": True, "tag": "div", "active_tag": "div",
        "current_value": state["text"], "is_input": False, "is_contenteditable": True}
    w._editor_state = lambda frame=None: {
        "found": True, "is_input": False, "state_gated": True,
        "blocks": state["blocks"], "placeholder_visible": state["placeholder_visible"],
        "text": state["text"]}
    w._clear_fill_marker = lambda frame=None: None
    return w, state, calls


def test_editor_landed_rejects_dom_only_fake_draft():
    fake = {"found": True, "state_gated": True, "blocks": 0,
            "placeholder_visible": True, "text": "这也太真实了吧"}
    assert BrowserWorker._editor_landed(fake, "这也太真实了吧") is False
    real = dict(fake, blocks=2, placeholder_visible=False)
    assert BrowserWorker._editor_landed(real, "这也太真实了吧") is True
    # 有块但占位符仍可见 = 编辑器仍认为自己是空的（Draft.js 假草稿）
    assert BrowserWorker._editor_landed(dict(real, placeholder_visible=True), "这也太真实了吧") is False
    plain = {"found": True, "state_gated": False, "blocks": 0, "text": "hi"}
    assert BrowserWorker._editor_landed(plain, "hi") is True
    assert BrowserWorker._editor_landed({"found": False}, "hi") is False
    assert BrowserWorker._editor_landed(real, "别的文本") is False


def test_state_gated_editor_rejects_dom_only_fake_draft(monkeypatch):
    w, state, _ = make_fill_worker(monkeypatch, typing_lands=False, paste_lands=False)
    out = w._do_fill_input({"selector": ".public-DraftEditor-content", "text": "这也太真实了吧"})
    assert "[输入成功]" not in out
    assert "[输入未落地]" in out
    assert "data-block=0" in out
    assert "占位符仍在" in out
    assert "真实逐键与系统粘贴都未落地" in out


def test_state_gated_editor_types_for_real_and_reports_editor_state(monkeypatch):
    w, state, calls = make_fill_worker(monkeypatch, typing_lands=True)
    out = w._do_fill_input({"selector": ".public-DraftEditor-content", "text": "这也太真实了吧"})
    assert "[输入成功]" in out
    assert "编辑器状态: data-block=1" in out and "占位符已隐藏" in out
    assert "C3(OS键鼠)/type 有进展" in out
    assert calls["typed"] == 1 and calls["pasted"] == 0


def test_state_gated_editor_switches_primitive_after_typing_fails(monkeypatch):
    w, state, calls = make_fill_worker(monkeypatch, typing_lands=False, paste_lands=True)
    out = w._do_fill_input({"selector": ".public-DraftEditor-content", "text": "这也太真实了吧"})
    assert "[输入成功]" in out
    assert "C3(OS键鼠)/paste 有进展" in out
    assert "编辑器状态: data-block=1" in out
    assert calls["typed"] == 1 and calls["pasted"] == 1


def test_state_gated_editor_locks_c4_after_both_primitives_fail(monkeypatch):
    w, _, calls = make_fill_worker(monkeypatch, typing_lands=False, paste_lands=False)
    out = w._do_fill_input({"selector": ".public-DraftEditor-content", "text": "这也太真实了吧"})
    assert "type+paste 均未落地" in out
    assert "C4(人工)" in out
    assert calls["typed"] == 1 and calls["pasted"] == 1


def test_c3_type_failure_requires_paste_before_c4():
    w = BrowserWorker.__new__(BrowserWorker)
    note = w._channel_note(3, "target", "input", False, variant="type")
    assert "下次改用 C3(OS键鼠)/paste" in note
    assert "C3/paste" in w._channel_guard(3, "target", "input", variant="type")
    assert w._channel_guard(3, "target", "input", variant="paste") == ""


def test_c3_both_primitives_fail_then_c4_locked():
    w = BrowserWorker.__new__(BrowserWorker)
    w._channel_note(3, "target", "input", False, variant="type")
    note = w._channel_note(3, "target", "input", False, variant="paste")
    assert "type+paste 均未落地" in note
    assert "C4(人工)" in w._channel_guard(3, "target", "input", variant="paste")
    assert "C4(人工)" in w._channel_guard(3, "target", "input")


def test_c3_success_clears_primitive_lock_and_failure_count():
    w = BrowserWorker.__new__(BrowserWorker)
    w._channel_note(3, "target", "input", False, variant="type")
    assert "有进展" in w._channel_note(3, "target", "input", True, variant="paste")
    assert w._channel_guard(3, "target", "input", variant="type") == ""


def test_prompts_forbid_delegating_automatable_actions_to_user():
    from pathlib import Path
    system = Path("prompts/system.md").read_text(encoding="utf-8")
    assert "禁止把自己能做的操作甩给用户" in system
    assert "browser_request_takeover" in system
    browser = Path("prompts/skills/browser.md").read_text(encoding="utf-8")
    assert "禁止让用户手动操作" in browser
    assert "data-block=0" in browser
    assert "C3/type" in browser and "C3/paste" in browser


# ---------- 真实 DOM：Draft.js / ProseMirror 结构 ----------

DRAFT_FIXTURE = """
<style>.public-DraftEditorPlaceholder-root { color: #999; }</style>
<div class="DraftEditor-root">
  <div class="DraftEditor-editorContainer">
    <div class="public-DraftEditor-content" contenteditable="true" data-ae-fill-target="1">
      <div data-contents="true">
        <div data-block="true"><div><span>这也太真实了吧</span></div></div>
      </div>
    </div>
  </div>
  <div class="public-DraftEditorPlaceholder-root">说点什么</div>
</div>
"""


@pytest.fixture(scope="module")
def draft_page():
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


def test_draft_state_js_reads_placeholder_outside_content(draft_page):
    """Draft.js 的占位符在 content 之外，scope 必须取 DraftEditor-root。"""
    draft_page.set_content(DRAFT_FIXTURE)
    state = draft_page.evaluate(_EDITOR_STATE_JS)
    assert state["state_gated"] is True
    assert state["blocks"] >= 1
    assert state["placeholder_visible"] is True
    assert BrowserWorker._editor_landed(state, "这也太真实了吧") is False

    draft_page.eval_on_selector(
        ".public-DraftEditorPlaceholder-root", "n => { n.style.display = 'none'; }")
    state = draft_page.evaluate(_EDITOR_STATE_JS)
    assert state["placeholder_visible"] is False
    assert BrowserWorker._editor_landed(state, "这也太真实了吧") is True


def test_draft_state_js_rejects_dom_written_fake_draft(draft_page):
    draft_page.set_content(DRAFT_FIXTURE)
    draft_page.eval_on_selector(
        ".public-DraftEditor-content",
        "n => { n.innerText = '这也太真实了吧'; }")
    state = draft_page.evaluate(_EDITOR_STATE_JS)
    assert state["state_gated"] is True
    assert state["blocks"] == 0
    assert state["placeholder_visible"] is True
    assert BrowserWorker._editor_landed(state, "这也太真实了吧") is False


def test_prosemirror_is_treated_as_state_gated(draft_page):
    draft_page.set_content(
        '<div class="ProseMirror" contenteditable="true" data-ae-fill-target="1">'
        "<p>这也太真实了吧</p></div>")
    state = draft_page.evaluate(_EDITOR_STATE_JS)
    assert state["state_gated"] is True
    assert state["blocks"] >= 1
    assert state["placeholder_visible"] is False
    assert BrowserWorker._editor_landed(state, "这也太真实了吧") is True
