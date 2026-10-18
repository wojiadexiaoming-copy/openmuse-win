"""ui/launcher.py 端口绑定策略测试。

2026-10-15 用户现场：launcher 写死 port=5000，上次退出后端口残留被占
（Windows SO_EXCLUSIVEADDRUSE 连 TIME_WAIT 都挡），直接 OSError 崩掉。
现策略：被占先探活 /api/health → 是活着的实例则拒绝双开；
残留/其他程序占用则自动顺延端口；全被占则汇总报错。
逻辑与平台无关，Linux 可跑；main() 本体仍要求 Windows。
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ui.launcher import _bind_local_port, _probe_alterego_health


def _free_port():
    """向 OS 借一个空闲端口号，立即释放供后续测试使用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _occupy(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def test_port_fallback_when_busy():
    """first_port 被占（探活无响应）→ 自动顺延到 first_port+1。"""
    p0 = _free_port()
    s = _occupy(p0)
    try:
        httpd, port = _bind_local_port("127.0.0.1", first_port=p0, tries=3)
        assert port == p0 + 1
        httpd.server_close()
    finally:
        s.close()


def test_refuse_second_instance_when_live():
    """first_port 上是活着的 AlterEgo（/api/health 返回 ok）→ 拒绝双开。"""
    import time as _time

    p0 = _free_port()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/health":
                body = json.dumps({"status": "ok"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", p0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    # 等服务器进入 accept 循环
    _time.sleep(0.2)
    try:
        assert _probe_alterego_health("127.0.0.1", p0) is True
        with pytest.raises(RuntimeError, match="已有一个正在运行的 AlterEgo 实例"):
            _bind_local_port("127.0.0.1", first_port=p0, tries=3)
    finally:
        srv.shutdown()


def test_all_busy_reports_clearly():
    """端口全被占 → OSError 信息说清范围和排查方向。"""
    p0, p1 = _free_port(), _free_port()
    # 保证 p1 == p0+1（顺延逻辑依赖连续端口）
    p0 = min(p0, p1)
    p1 = p0 + 1
    holders = [_occupy(p0), _occupy(p1)]
    try:
        with pytest.raises(OSError, match="{}-{}".format(p0, p1)):
            _bind_local_port("127.0.0.1", first_port=p0, tries=2)
    finally:
        for s in holders:
            s.close()
