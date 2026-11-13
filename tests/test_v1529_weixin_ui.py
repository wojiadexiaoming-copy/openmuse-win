# tests/test_v1529_weixin_ui.py
"""v1.5.29 微信绑定入口回归：状态字段 + 三个 UI 面契约。

背景：v1.5.13 v2 转正把微信绑定卡片从主界面移除，独立设置页又无入口且字段名过时
（is_logged_in / qr_img），导致「微信绑定板块看不到」。本文件守住三件事：
1. get_status 透出 token_expired，UI 能区分「授权过期」与「从未绑定」；
2. 主界面设置弹窗有「微信」标签页并接到真实端点与 SSE；
3. 独立设置页使用正确的字段名和二维码图片地址。
"""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from channels.weixin import WEIXIN_TOKEN_TTL_SEC, WeixinChannel

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
APP = (UI / "static" / "app.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
SETTINGS = (UI / "settings.html").read_text(encoding="utf-8")
SERVER = (UI / "server.py").read_text(encoding="utf-8")


def _channel(tmp_path):
    return WeixinChannel(data_dir=tmp_path / "weixin")


def test_status_reports_token_expired(tmp_path):
    ch = _channel(tmp_path)
    fresh = ch.get_status()
    assert fresh["connected"] is False
    assert fresh["token_expired"] is False

    ch.credentials = {
        "bot_token": "tok",
        "connected_at": time.time() - WEIXIN_TOKEN_TTL_SEC - 60,
    }
    st = ch.get_status()
    assert st["connected"] is False
    assert st["token_expired"] is True


def test_status_fresh_token_is_connected(tmp_path):
    ch = _channel(tmp_path)
    ch.credentials = {"bot_token": "tok", "connected_at": time.time(), "ilink_user_id": "u1"}
    st = ch.get_status()
    assert st["connected"] is True
    assert st["token_expired"] is False
    assert st["user_id"] == "u1"


def test_settings_modal_has_weixin_tab_and_panel():
    assert 'data-set-tab="weixin"' in HTML
    assert 'data-set-panel="weixin"' in HTML
    for el in ("weixin-badge", "weixin-status-text", "btn-weixin-qr", "weixin-qr-box"):
        assert f'id="{el}"' in HTML, f"设置弹窗缺少 #{el}"


def test_app_js_wires_weixin_endpoints_and_sse():
    for token in ("/api/weixin/status", "/api/weixin/login", "/api/weixin/qr_img",
                  "weixin_login_progress"):
        assert token in APP, f"app.js 未接线 {token}"
    assert "function loadWeixinStatus" in APP
    assert "function generateWeixinQr" in APP
    assert "name === 'weixin'" in APP, "切到微信标签必须拉取状态"
    assert "qr_img?t=" in APP, "二维码必须走图片地址"
    assert "res.qr_img" not in APP, "后端没有 qr_img 字段"


def test_settings_page_uses_real_fields():
    assert "res.connected" in SETTINGS
    assert "res.token_expired" in SETTINGS
    assert "is_logged_in" not in SETTINGS
    assert "res.qr_img" not in SETTINGS
    assert "/api/weixin/qr_img" in SETTINGS
    assert "already_connected" in SETTINGS


def test_qr_image_route_stays_auth_exempt_for_img_tag():
    assert 'not request.path.startswith("/api/weixin/qr_img")' in SERVER


def test_settings_modal_weixin_flow_in_jsdom():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the jsdom settings regression")
    script = Path(__file__).with_name("test_ui_weixin_binding.js")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
