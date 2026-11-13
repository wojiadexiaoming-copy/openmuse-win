# tests/test_v1530_weixin_transport.py
"""v1.5.30 微信通道传输层回归：Content-Length 与实发字节一致、空响应/非 JSON 语义。

背景：2026-10-28 生产日志 `Too little data for declared Content-Length`。根因是
WeixinClient 用默认 json.dumps 算长度、却让 httpx 用紧凑分隔符重编码 `json=body`，
手填头被 setdefault 保留，h11 发现少 6 字节直接拒绝请求——扫码能成功，但
sendmessage / getupdates 100% 失败。本文件守住四件事：
1. 请求体与 Content-Length 必须来自同一份字节串（含中文）；
2. sendmessage 的空串 / {} 响应视为成功；
3. 非 JSON 响应必须报错，不能伪装成功；
4. 最近一次发送/长轮询错误透出到 get_status，供 UI 显示「已连接 · 通道异常」。
"""
import time

import httpx
import pytest

import channels.weixin as weixin_mod
from channels.weixin import WeixinChannel, WeixinClient

ROOT_UI = weixin_mod.Path(__file__).resolve().parent.parent / "ui"
APP = (ROOT_UI / "static" / "app.js").read_text(encoding="utf-8")
SETTINGS = (ROOT_UI / "settings.html").read_text(encoding="utf-8")


def _client(monkeypatch, handler):
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(weixin_mod.httpx, "Client", factory)
    return WeixinClient()


def test_send_message_content_length_matches_body(monkeypatch):
    seen = {}

    def handler(request):
        seen["declared"] = int(request.headers["content-length"])
        seen["actual"] = len(request.content)
        seen["body"] = request.content
        return httpx.Response(200, text="{}")

    client = _client(monkeypatch, handler)
    assert client.send_message("tok", "u1", text="你好，微信") == {"ret": 0}
    assert seen["declared"] == seen["actual"]
    assert "你好".encode("utf-8") in seen["body"]
    assert client.last_send_error == ""


def test_get_updates_content_length_matches_body(monkeypatch):
    seen = {}

    def handler(request):
        seen["declared"] = int(request.headers["content-length"])
        seen["actual"] = len(request.content)
        return httpx.Response(200, json={"msgs": [], "get_updates_buf": "b1"})

    client = _client(monkeypatch, handler)
    data = client.get_updates("tok", get_updates_buf="b0")
    assert data["get_updates_buf"] == "b1"
    assert seen["declared"] == seen["actual"]
    assert client.last_poll_error == ""


def test_real_h11_loopback_accepts_declared_length():
    """真实 h11 序列化（MockTransport 不校验帧格式）：声明长度必须与实发字节一致。"""
    import http.server
    import threading

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            seen["body"] = self.rfile.read(n)
            seen["path"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = WeixinClient(base_url=f"http://127.0.0.1:{server.server_port}")
        assert client.send_message("tok", "u1", text="中文回环测试") == {"ret": 0}
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert seen["path"] == "/ilink/bot/sendmessage"
    assert "中文回环测试".encode("utf-8") in seen["body"]


def test_get_updates_timeout_is_normal_idle(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("long poll timeout", request=request)

    client = _client(monkeypatch, handler)
    data = client.get_updates("tok", get_updates_buf="b0", timeout_sec=1)
    assert data == {"ret": 0, "msgs": [], "get_updates_buf": "b0"}
    assert client.last_poll_error == ""


@pytest.mark.parametrize("body", ["", "{}", "  \n"])
def test_empty_send_response_is_success(monkeypatch, body):
    client = _client(monkeypatch, lambda request: httpx.Response(200, text=body))
    assert client.send_message("tok", "u1", text="hi") == {"ret": 0}
    assert client.last_send_error == ""


def test_non_json_response_is_error_for_send(monkeypatch):
    client = _client(
        monkeypatch,
        lambda request: httpx.Response(200, text="<html>bad gateway</html>"),
    )
    with pytest.raises(ValueError, match="非 JSON"):
        client.send_message("tok", "u1", text="hi")
    assert "非 JSON" in client.last_send_error
    assert client.last_error_source == "send"


def test_non_json_response_is_error_for_poll(monkeypatch):
    client = _client(
        monkeypatch,
        lambda request: httpx.Response(200, text="<html>bad gateway</html>"),
    )
    with pytest.raises(ValueError, match="非 JSON"):
        client.get_updates("tok")
    assert "非 JSON" in client.last_poll_error
    assert client.last_error_source == "poll"


def test_business_error_sets_and_success_clears_last_error(monkeypatch):
    responses = [
        httpx.Response(200, json={"ret": -1, "errmsg": "invalid request"}),
        httpx.Response(200, text="{}"),
    ]

    def handler(request):
        return responses.pop(0)

    client = _client(monkeypatch, handler)
    assert client.send_message("tok", "u1", text="hi")["ret"] == -1
    assert "ret=-1" in client.last_send_error
    client.send_message("tok", "u1", text="hi")
    assert client.last_send_error == ""


def test_status_exposes_last_transport_error(tmp_path):
    ch = WeixinChannel(data_dir=tmp_path / "weixin")
    ch.credentials = {
        "bot_token": "tok",
        "connected_at": time.time(),
        "ilink_user_id": "u1",
    }
    ch.client.last_send_error = "发送失败: boom"
    ch.client.last_error_source = "send"
    ch.client.last_error_at = time.time()
    st = ch.get_status()
    assert st["connected"] is True
    assert st["last_error"] == "发送失败: boom"
    assert st["last_error_source"] == "send"
    assert st["last_error_at"] is not None

    ch.client.last_send_error = ""
    ch.client.last_poll_error = "长轮询失败: eof"
    ch.client.last_error_source = "poll"
    st = ch.get_status()
    assert st["last_error"] == "长轮询失败: eof"
    assert st["last_error_source"] == "poll"


def test_ui_surfaces_connected_channel_error():
    assert "last_error" in APP and "通道异常" in APP
    assert "last_error" in SETTINGS and "通道异常" in SETTINGS
