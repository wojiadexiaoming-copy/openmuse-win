"""Windows desktop entry with startup phases and a verified local backend."""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent
REPO_ROOT = UI_DIR.parent


def _probe_alterego_health(host, port, timeout=1.0):
    """端口被占用时探一下：占着端口的是不是还活着的 AlterEgo 实例。"""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        body = resp.read(4096)
        conn.close()
        return resp.status == 200 and json.loads(body).get("status") == "ok"
    except Exception:
        return False


def _bind_local_port(host="127.0.0.1", first_port=5000, tries=11):
    """按顺序绑定本地端口。

    2026-10-15 用户现场：上一次 launcher 退出后端口 5000 仍被占用
    （残留 TIME_WAIT / 未退干净的进程，Windows 下 SO_EXCLUSIVEADDRUSE 会挡住重绑），
    写死 port=5000 直接 OSError 崩掉，报错信息还没头没尾。

    策略：
    - 5000 被占 → 先探活 /api/health：是活着的 AlterEgo 实例 → 明确拒绝启动，
      避免双实例并发打架；
    - 探活无响应（残留端口/其他程序）→ 自动顺延到下一个端口；
    - 全部被占 → 汇总报错，告诉用户查什么。
    返回 (httpd, 实际绑定的端口)。
    """
    from ui.server import ThreadedWSGIServer
    tried = []
    for port in range(first_port, first_port + tries):
        try:
            httpd = ThreadedWSGIServer(host=host, port=port).create_server(None)
            return httpd, port
        except OSError as exc:
            tried.append(port)
            if _probe_alterego_health(host, port):
                raise RuntimeError(
                    f"端口 {port} 上已有一个正在运行的 AlterEgo 实例，"
                    "为避免双实例冲突本次启动已停止。请先关闭旧实例再启动。"
                ) from exc
            # 残留 TIME_WAIT / 其他程序占用 → 试下一个端口
            continue
    raise OSError(
        f"本地端口 {tried[0]}-{tried[-1]} 全部被占用且无一是 AlterEgo 实例："
        "请检查是否有其他程序占用这些端口，或在任务管理器中结束残留的 "
        "AlterEgo/python 进程后重试。"
    )


def _wait_backend(host, port, thread, *, timeout=10.0):
    """Verify our bound server, without proxy settings, tokens or model calls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("backend thread exited before readiness")
        connection = http.client.HTTPConnection(
            host, port, timeout=min(0.5, max(0.01, deadline - time.monotonic())))
        try:
            connection.request("GET", "/api/health")
            reply = connection.getresponse()
            body = reply.read(4096)
            payload = json.loads(body)
            if reply.status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    raise TimeoutError("backend readiness timed out")


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("launcher.py requires Windows")
    # Import this stdlib-only module before pywebview, app modules or SQLite.
    from ui.startup import StartupDiagnostics
    diagnostics = StartupDiagnostics(Path(os.environ.get("ALTEREGO_DATA", str(REPO_ROOT))))
    httpd = None
    thread = None
    server = None
    try:
        diagnostics.phase("import_webview")
        import webview
        diagnostics.phase("import_backend")
        from ui.server import AgentServer

        diagnostics.phase("bind_local_port")
        # Claim the port before initializing databases and background listeners.
        # No requests are served until the real application is installed below.
        # 5000 被占时自动顺延（见 _bind_local_port）；探活是旧实例则拒绝双开。
        httpd, bound_port = _bind_local_port("127.0.0.1")
        if bound_port != 5000:
            print(f"[AlterEgo] 端口 5000 被占用，已改用 {bound_port}")
        diagnostics.phase("initialize_backend")
        server = AgentServer(startup_progress=diagnostics.phase)
        httpd.set_app(server.app)
        thread = threading.Thread(
            target=lambda: httpd.serve_forever(poll_interval=0.1),
            name="alterego-http", daemon=True)
        thread.start()
        diagnostics.phase("wait_backend_health")
        _wait_backend("127.0.0.1", httpd.server_port, thread)
        diagnostics.phase("create_desktop_window")
        from ui.window_layout import get_split_layout
        cfg = getattr(server, "cfg", {}) or {}
        tools_cfg = cfg.get("tools", {}) or {}
        browser_cfg = tools_cfg.get("browser", {}) or {}
        split_screen = browser_cfg.get("split_screen", True)
        browser_ratio = float(browser_cfg.get("browser_ratio", 0.80))

        server.port = httpd.server_port

        ui_cfg = cfg.get("ui", {}) or {}
        frameless = bool(ui_cfg.get("frameless", True))
        icon_path = Path(__file__).resolve().parent / "static" / "alterego.ico"
        icon_arg = str(icon_path) if icon_path.exists() else None

        window_title = "AlterEgo"
        target_url = f"http://127.0.0.1:{httpd.server_port}/?token={server.api_token}&desktop=1"

        if split_screen:
            layout = get_split_layout(browser_ratio=browser_ratio)
            ui_geo = layout["ui"]
            window = webview.create_window(
                window_title,
                target_url,
                x=ui_geo["x"], y=ui_geo["y"],
                width=ui_geo["width"], height=ui_geo["height"],
                frameless=frameless,
                easy_drag=True,
                shadow=True,
                min_size=(240, 480))
        else:
            window = webview.create_window(
                window_title,
                target_url,
                width=1280, height=800,
                frameless=frameless,
                easy_drag=True,
                shadow=True,
                min_size=(240, 480))

        if hasattr(server, "set_ui_window"):
            server.set_ui_window(window)
        if hasattr(window, "events") and hasattr(window.events, "loaded"):
            window.events.loaded += diagnostics.ready

        if frameless and sys.platform == "win32" and hasattr(window, "events") and hasattr(window.events, "shown"):
            def _enable_native_resize():
                try:
                    import ctypes
                    hwnd = getattr(window, "native", None)
                    if hwnd and hasattr(hwnd, "Handle"):
                        h = hwnd.Handle.ToInt32()
                        GWL_STYLE = -16
                        WS_THICKFRAME = 0x00040000
                        style = ctypes.windll.user32.GetWindowLongW(h, GWL_STYLE)
                        ctypes.windll.user32.SetWindowLongW(h, GWL_STYLE, style | WS_THICKFRAME)
                        ctypes.windll.user32.SetWindowPos(h, 0, 0, 0, 0, 0, 0x0027)
                except Exception:
                    pass
            window.events.shown += _enable_native_resize

        diagnostics.phase("start_desktop_runtime")
        webview.start(debug=False, icon=icon_arg)
    except Exception as exc:
        diagnostics.failure(exc)
        raise SystemExit(1) from None
    finally:
        if httpd is not None:
            if thread is not None and thread.is_alive():
                httpd.shutdown()
            httpd.server_close()
        # A failed native window must not leave another active Weixin listener
        # or memory scheduler behind in this process.
        if server is not None:
            # Playwright 的 node 驱动会活过窗口关闭：必须先关浏览器、停驱动，
            # 否则它对着已断开的管道写事件，控制台甩一段 EPIPE 崩溃堆栈
            # （2026-10-17 双机实测）。
            browser_worker = getattr(getattr(server, "registry", None), "browser_worker", None)
            stop_browser = getattr(browser_worker, "stop", None)
            if callable(stop_browser):
                try:
                    stop_browser()
                except Exception:
                    pass
            for owner_name, method_name in (("weixin_channel", "stop_message_loop"),
                                           ("scheduler", "shutdown")):
                method = getattr(getattr(server, owner_name, None), method_name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:
                        pass
        diagnostics.close()


if __name__ == "__main__":
    main()
