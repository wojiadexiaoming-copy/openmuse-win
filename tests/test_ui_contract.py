# tests/test_ui_contract.py
"""UI 契约测试：文件存在、端点形状未变、事件监听覆盖完整。

P6 转正后 v2 已是唯一 UI（index.html / app.js / style.css），本文件守的是它的外部契约。
"""
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "ui"
APP = (UI / "static" / "app.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "static" / "style.css").read_text(encoding="utf-8")
FLOAT = (UI / "static" / "float.html").read_text(encoding="utf-8")

# 适配层标志：P2.3 的 applyAgentState / STATE_FROM_SERVER 是唯一可靠判据
ADAPTER_READY = "STATE_FROM_SERVER" in APP

needs_adapter = pytest.mark.skipif(
    not ADAPTER_READY,
    reason="适配层未接入（P2 之前只校验文件存在性）",
)


def test_ui_files_present_and_nonempty():
    for name in ("index.html", "static/style.css", "static/app.js",
                 "static/float.html", "static/float.css", "static/float.js"):
        p = UI / name
        assert p.is_file() and p.stat().st_size > 1000, name


@needs_adapter
def test_sse_listeners_cover_backend_events():
    required = {
        "agent_state", "message", "approval_request", "approval_resolved",
        "task_update", "token_usage", "takeover_request", "takeover_update",
        "tool_call_started", "tool_call_done", "subagent_update",
        "plan_update", "task_progress", "turn_notice", "schedule_fire",
        "model_switched", "model_deleted", "weixin_login_progress",
    }
    missing = {e for e in required if f"addEventListener('{e}'" not in APP
               and f'addEventListener("{e}"' not in APP}
    assert not missing, f"SSE 监听缺失: {sorted(missing)}"


@needs_adapter
def test_no_ghost_event_listener():
    assert "addEventListener('memory_write'" not in APP
    assert 'addEventListener("memory_write"' not in APP


@needs_adapter
def test_state_mapping_covers_all_backend_states():
    for st in ("idle", "thinking", "acting", "waiting_subagent",
               "awaiting_approval", "paused_takeover", "waiting_user",
               "done", "cancelled", "incomplete", "error"):
        assert st in APP, f"状态映射缺 {st}"
    assert "waiting_subagent: 'acting'" in APP, "waiting_subagent 必须映射成 acting"


@needs_adapter
def test_stop_endpoint_wired():
    assert "'/api/stop'" in APP or '"/api/stop"' in APP


def test_html_asset_refs_use_canonical_names():
    assert "static/app.js" in HTML and "static/style.css" in HTML
    assert ".v2." not in HTML and "legacy" not in HTML, "主页面仍引用并行期/legacy 资源"
    assert "style.css" in FLOAT and ".v2." not in FLOAT, "浮窗仍引用并行期样式"


@needs_adapter
def test_no_legacy_assets_or_demo_persistence():
    assert "assets/avatar" not in APP
    assert "marked.min.js" not in HTML and "tailwind" not in HTML
    assert "localStorage.setItem('alterego.prototype.state.v1'" not in APP
    assert 'localStorage.setItem("alterego.prototype.state.v1"' not in APP


def test_no_bare_hex_outside_root():
    css = CSS.split(":root", 1)
    tail = css[1].split("}", 1)[1] if len(css) > 1 else CSS
    bad = re.findall(r"#[0-9A-Fa-f]{3,8}\b", tail)
    assert not bad, f":root 之外出现裸 hex: {bad[:10]}"


def test_endpoint_editor_dom_and_js_contract():
    """验证设置面板中的接口列表项支持点击编辑及相关 DOM / 逻辑完整性。"""
    assert 'id="ep-modal-title"' in HTML, "缺少接口弹窗标题元素 #ep-modal-title"
    assert 'id="btn-ep-delete"' in HTML, "缺少删除模型按钮 #btn-ep-delete"
    assert 'openEndpointEditor' in APP, "app.js 缺少 openEndpointEditor 函数"
    assert 'data-model-id' in APP, "renderEndpoints 未为列表项绑定 data-model-id"
    assert '.keyrow' in CSS, "CSS 缺少 .keyrow 样式"
    assert '.keyrow__arrow' in CSS, "CSS 缺少 .keyrow__arrow 箭头图标样式"


def test_fallback_chain_dom_and_js_contract():
    """验证设置面板中的备用链多级降级配置 DOM 与逻辑完整性。"""
    assert 'id="fallback-table"' in HTML, "缺少备用链容器 #fallback-table"
    assert 'id="btn-add-fallback"' in HTML, "缺少添加备用降级模型按钮 #btn-add-fallback"
    assert 'renderFallbackChain' in APP, "app.js 缺少 renderFallbackChain 函数"
    assert 'saveFallbackChainToServer' in APP, "app.js 缺少 saveFallbackChainToServer 函数"
    assert 'data-fallback-select' in APP, "app.js 缺少 data-fallback-select 下拉框标记"
    assert '.fb-badge' in CSS, "CSS 缺少 .fb-badge 顺位徽标样式"
