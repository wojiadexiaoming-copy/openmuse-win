"""v1.5.26：SoM 编号框改为瞬态——出现约 1.8s 后淡出，截图前按需回显。

背景（2026-10-28 携程登录页反馈）：编号框在 browser_snapshot 时画上后就一直留在页面上，
接管或用户旁观时满屏蓝框，碍眼且遮挡页面；其实只需要「显示一下」用来核对位置。

本文件锁定：
1. 框选层出现后自动淡出，但保留在 DOM 里（不丢 ref 编号）；
2. __ae_som_pulse(ms) 可瞬时回显并重新计时；
3. 下一次快照会清掉旧层，不会叠加重复框；
4. browser_screenshot 默认路径（clean=False）截图前自动 pulse，画面里仍有编号；
5. __ae_som_off() 可彻底移除。
"""
import time

import pytest

from tools.browser.worker import BrowserWorker, INTERACTIVE_TREE_JS


SOM_FIXTURE = """
<style>
  body { margin: 0; font: 14px sans-serif; }
  #panel { padding: 40px; }
  #panel a, #panel button { display: block; margin: 12px 0; }
</style>
<div id="panel">
  <a href="#a">第一个链接</a>
  <button>第一个按钮</button>
  <input placeholder="一个输入框">
  <button>第二个按钮</button>
</div>
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
    page = browser.new_page(viewport={"width": 900, "height": 700})
    yield page
    browser.close()
    pw.stop()


def _fresh(page, html=SOM_FIXTURE):
    page.goto("about:blank")
    page.set_content(html)


def _layer_state(page):
    return page.evaluate("""() => {
        const l = document.getElementById('__ae_som');
        if (!l) return {exists: false};
        const cs = getComputedStyle(l);
        return {
            exists: true,
            visible: cs.visibility === 'visible' && parseFloat(cs.opacity) > 0.05,
            opacity: cs.opacity,
            boxes: l.querySelectorAll('.ae-som-box').length,
            badges: l.querySelectorAll('.ae-som-badge').length,
        };
    }""")


def _wait_until(page, pred, timeout=2.0, step=0.08):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def test_som_layer_appears_then_fades(edge_page):
    _fresh(edge_page)
    els = edge_page.evaluate(INTERACTIVE_TREE_JS, ["", 250])
    assert els and len(els) >= 4

    st = _layer_state(edge_page)
    assert st["exists"] and st["visible"], "快照后框选层应立即可见"
    assert st["boxes"] >= 4 and st["badges"] == st["boxes"]

    assert _wait_until(edge_page, lambda: not _layer_state(edge_page)["visible"], timeout=2.0)
    st2 = _layer_state(edge_page)
    assert st2["exists"], "淡出后层应保留在 DOM（ref 编号仍有效）"
    assert not st2["visible"]


def test_som_pulse_reveals_then_hides_again(edge_page):
    _fresh(edge_page)
    edge_page.evaluate(INTERACTIVE_TREE_JS, ["", 150])
    assert _wait_until(edge_page, lambda: not _layer_state(edge_page)["visible"], timeout=2.0)

    assert edge_page.evaluate("window.__ae_som_pulse(300)") is True
    assert _layer_state(edge_page)["visible"]
    assert _wait_until(edge_page, lambda: not _layer_state(edge_page)["visible"], timeout=2.5)


def test_next_snapshot_replaces_old_layer(edge_page):
    _fresh(edge_page)
    first = edge_page.evaluate(INTERACTIVE_TREE_JS, ["", 5000])
    edge_page.evaluate(INTERACTIVE_TREE_JS, ["", 5000])
    st = _layer_state(edge_page)
    assert st["exists"] and st["visible"]
    assert st["boxes"] == len(first) >= 4, "第二次快照必须清掉旧层，不能重复叠加"
    assert edge_page.evaluate("document.querySelectorAll('#__ae_som').length") == 1


def test_off_removes_layer(edge_page):
    _fresh(edge_page)
    edge_page.evaluate(INTERACTIVE_TREE_JS, ["", 5000])
    assert _layer_state(edge_page)["exists"]
    edge_page.evaluate("window.__ae_som_off()")
    assert not _layer_state(edge_page)["exists"]


def test_clean_screenshot_pulses_hidden_overlay(edge_page):
    _fresh(edge_page)
    edge_page.evaluate(INTERACTIVE_TREE_JS, ["", 150])
    assert _wait_until(edge_page, lambda: not _layer_state(edge_page)["visible"], timeout=2.0)

    worker = BrowserWorker.__new__(BrowserWorker)
    worker._raw_page = edge_page
    raw = worker._clean_screenshot()
    assert raw, "截图应返回非空字节"
    assert raw[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1")
    assert _layer_state(edge_page)["visible"], "默认截图前必须 pulse 回显编号框"


def test_clean_true_does_not_pulse(edge_page):
    _fresh(edge_page)
    edge_page.evaluate(INTERACTIVE_TREE_JS, ["", 150])
    assert _wait_until(edge_page, lambda: not _layer_state(edge_page)["visible"], timeout=2.0)

    worker = BrowserWorker.__new__(BrowserWorker)
    worker._raw_page = edge_page
    raw = worker._clean_screenshot(clean=True)
    assert raw
    assert not _layer_state(edge_page)["visible"], "clean=True 截图不能把框选层叫回来"


def test_js_contains_transient_contract():
    assert "__ae_som_pulse" in INTERACTIVE_TREE_JS
    assert "aeSomIn" in INTERACTIVE_TREE_JS
    assert "ae-som-box" in INTERACTIVE_TREE_JS and "ae-som-badge" in INTERACTIVE_TREE_JS
    src = BrowserWorker._clean_screenshot.__doc__ or ""
    assert "pulse" in src
