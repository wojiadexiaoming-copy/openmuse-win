"""v1.5.21 浏览器地基：图标控件感知、富文本提交判据、只读观察不计失败。

真实用例来源（2026-10-27 抖音评论）：评论文案已写进 Draft.js 编辑器，
但回车只换行、发送键是无文字的 div>svg 图标，ref 树里没有它，
最后一步"点发送"始终没做成。
"""
import pytest

from agent.tools_registry import ToolRegistry
from tools.browser.worker import INTERACTIVE_TREE_JS, BrowserWorker


# ---------- P0-2：图标控件进入 SoM/ref 树 ----------

FIXTURE_HTML = """
<style>
  body { margin: 0; }
  #editor { width: 400px; height: 96px; border: 1px solid #ccc; }
  .icon { display: inline-block; width: 32px; height: 32px; cursor: pointer; }
  #far { position: fixed; left: 900px; top: 760px; width: 32px; height: 32px; cursor: pointer; }
  #plain { display: inline-block; width: 32px; height: 32px; }
</style>
<div id="editor" contenteditable="true">草稿文字</div>
<div id="send" class="icon">
  <svg width="32" height="32" viewBox="0 0 32 32"><circle cx="16" cy="16" r="14"/></svg>
</div>
<div id="far"><svg width="32" height="32" viewBox="0 0 32 32"><rect width="32" height="32"/></svg></div>
<button id="btn"><svg width="20" height="20" viewBox="0 0 20 20"><rect width="20" height="20"/></svg></button>
<div id="plain"><svg width="32" height="32" viewBox="0 0 32 32"><rect width="32" height="32"/></svg></div>
<div id="label"><svg width="20" height="20" viewBox="0 0 20 20"><rect width="20" height="20"/></svg>发送</div>
"""


@pytest.fixture(scope="module")
def edge_page():
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
    page.set_content(FIXTURE_HTML)
    yield page
    browser.close()
    pw.stop()


def test_tree_exposes_icon_only_send_control(edge_page):
    results = edge_page.evaluate(INTERACTIVE_TREE_JS, ["", 5000])

    icons = [r for r in results if r.get("icon_only")]
    assert len(icons) == 1, results
    icon = icons[0]
    assert icon["name"] == "[图标按钮]"
    assert icon["tag"] == "div"
    bound_id = edge_page.evaluate(
        "(ref) => { const el = document.querySelector('[data-ae-ref=\"' + ref + '\"]');"
        " return el ? el.id : ''; }", icon["ref"])
    assert bound_id == "send"

    # 编辑器本身仍以可编辑节点身份出现
    assert any(r.get("is_editable") for r in results)
    # 包裹 svg 的 button 由主通道收录，图标通道不得重复编号
    assert sum(1 for r in results if r["tag"] == "button") == 1


# ---------- P0-3：富文本 Enter 提交判据 ----------

class FakeFocusPage:
    def __init__(self, before, after):
        self.keyboard = self
        self.pressed = []
        self._snapshots = [before, after]

    def press(self, key):
        self.pressed.append(key)

    def evaluate(self, js):
        if "activeElement" in js and "contenteditable" in js:
            return self._snapshots.pop(0) if len(self._snapshots) > 1 else self._snapshots[0]
        return None


def _worker(monkeypatch, page):
    import tools.browser.worker as worker_mod
    monkeypatch.setattr(worker_mod, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    w = BrowserWorker.__new__(BrowserWorker)
    w._raw_page = page
    w._do_guard = lambda: None
    return w


def test_enter_in_contenteditable_reports_not_submitted(monkeypatch):
    page = FakeFocusPage(
        {"editable": True, "tag": "div", "text": "这也太真了吧"},
        {"editable": True, "tag": "div", "text": "这也太真了吧\n"},
    )
    out = _worker(monkeypatch, page)._do_press({"key": "Enter"})
    assert "[Enter 未提交]" in out
    assert "只换行" in out
    assert "browser_probe_at" in out
    assert "不要再按 Enter" in out


def test_enter_after_editor_cleared_is_only_suspected(monkeypatch):
    page = FakeFocusPage(
        {"editable": True, "tag": "div", "text": "这也太真了吧"},
        {"editable": False, "tag": "body", "text": ""},
    )
    out = _worker(monkeypatch, page)._do_press({"key": "Enter"})
    assert "疑似已提交" in out
    assert "不得直接宣称成功" in out


def test_enter_in_plain_input_keeps_simple_ack(monkeypatch):
    page = FakeFocusPage(
        {"editable": False, "tag": "input", "text": "muse"},
        {"editable": False, "tag": "input", "text": "muse"},
    )
    out = _worker(monkeypatch, page)._do_press({"key": "Enter"})
    assert out == "[按键] Enter"


# ---------- P1-4：只读空探测不计入"失败累积" ----------

def _orchestrator():
    from agent.orchestrator import Orchestrator
    return Orchestrator(llm=object(), tools=ToolRegistry(), ctx=object())


def test_readonly_probe_failures_do_not_count_as_stall():
    orch = _orchestrator()
    orch.last_turn_tool_records = [
        {"tool": "browser_find_text", "status": "error", "result": "[执行结果] 命中 0 个"},
        {"tool": "browser_snapshot", "status": "error", "result": "[工具执行失败] 浏览器未就绪"},
        {"tool": "browser_click", "status": "error", "result": "[未找到] 发送"},
    ]
    assert orch._unresolved_tool_failures() == ["browser_click"]


def test_evaluate_script_error_still_counts_as_failure():
    orch = _orchestrator()
    orch.last_turn_tool_records = [
        {"tool": "browser_evaluate", "status": "ok",
         "result": "[执行结果] TypeError: x is not a function"},
    ]
    assert orch._unresolved_tool_failures() == ["browser_evaluate"]


# ---------- P0-1：提示词一致性 ----------

def test_browser_skill_has_single_coordinate_channel():
    from pathlib import Path
    text = Path("prompts/skills/browser.md").read_text(encoding="utf-8")
    assert "坐标通道只有一条路" in text
    assert "ref → find_text → probe_at → click_at" in text
    assert "严禁用 `browser_screenshot` 估算坐标" not in text
    assert "[图标按钮]" in text
    assert "[Enter 未提交]" in text
    assert "续接任务" in text


def test_system_prompt_forbids_banning_fallback_channels():
    from pathlib import Path
    text = Path("prompts/system.md").read_text(encoding="utf-8")
    assert "任务书不得禁用技能允许的兜底通道" in text
    assert "续接/收尾 brief 不得重新导航或刷新" in text
    assert "只读观察" in text
