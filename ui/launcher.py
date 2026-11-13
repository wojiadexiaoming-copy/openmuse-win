"""Windows desktop entry with startup phases and a verified local backend."""
from __future__ import annotations

import os
import sys

# USB/外置固定盘冷启动性能优化：
# 1. 禁用 Pydantic v2 插件扫描（避免首次导入时枚举 site-packages 所有 .dist-info）
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "1")

# 2. 将 Python 字节码缓存重定向到本机快速 SSD（减少对外置盘的 __pycache__ 读写）
# 3. 窗口创建后后台预热 openai/httpx/pydantic 等重依赖，避免首次聊天卡顿
try:
    from pathlib import Path
    _local_cache = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "AlterEgo" / "pycache"
    _local_cache.mkdir(parents=True, exist_ok=True)
    if "PYTHONPYCACHEPREFIX" not in os.environ:
        os.environ["PYTHONPYCACHEPREFIX"] = str(_local_cache)
    if getattr(sys, "pycache_prefix", None) is None:
        sys.pycache_prefix = str(_local_cache)
except Exception:
    pass

import http.client
import json
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


def _ensure_windows_desktop_runtime() -> None:
    """Ensure pythonnet / CLR can load when running from network/UNC mapped shares."""
    if sys.platform != "win32":
        return

    try:
        import atexit
        import shutil
        import tempfile
        import clr_loader
        import pythonnet

        if getattr(pythonnet, "_LOADED", False):
            return

        temp_dir = Path(tempfile.gettempdir()) / "alterego_desktop_runtime"
        temp_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = temp_dir / "alterego.config"
        cfg_file.write_text(
            '<?xml version="1.0" encoding="utf-8" ?>\n'
            '<configuration>\n'
            '  <runtime>\n'
            '    <loadFromRemoteSources enabled="true"/>\n'
            '  </runtime>\n'
            '</configuration>\n',
            encoding="utf-8",
        )

        rt = clr_loader.get_netfx(domain="alterego_domain", config_file=str(cfg_file))
        src_dll = Path(pythonnet.__file__).parent / "runtime" / "Python.Runtime.dll"
        local_dll = temp_dir / "Python.Runtime.dll"
        if not local_dll.exists() or local_dll.stat().st_size != src_dll.stat().st_size:
            shutil.copyfile(src_dll, local_dll)

        asm = rt.get_assembly(str(local_dll))
        fn = asm.get_function("Python.Runtime.Loader.Initialize")
        if fn(b"") == 0:
            pythonnet._LOADED = True
            pythonnet._RUNTIME = rt
            pythonnet._LOADER_ASSEMBLY = asm
            atexit.register(pythonnet.unload)
            import clr  # noqa: F401
    except Exception:
        try:
            import clr  # noqa: F401
        except Exception:
            pass


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("launcher.py requires Windows")
    from agent.paths import resolve_data_dir
    from agent.singleton import SingleInstanceLock
    data_dir = resolve_data_dir()
    lock_file = data_dir / "app.lock"
    instance_lock = SingleInstanceLock(lock_file)
    if not instance_lock.acquire():
        print("[AlterEgo] 检测到已有正在运行的 AlterEgo 实例 (app.lock)，为避免双实例冲突本次启动已停止。")
        sys.exit(1)

    # Import this stdlib-only module before pywebview, app modules or SQLite.
    from ui.startup import StartupDiagnostics
    diagnostics = StartupDiagnostics(data_dir)
    httpd = None
    thread = None
    server = None
    try:
        _ensure_windows_desktop_runtime()
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
        server.port = bound_port
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
                easy_drag=False,
                shadow=True,
                min_size=(240, 480))
        else:
            window = webview.create_window(
                window_title,
                target_url,
                width=1280, height=800,
                frameless=frameless,
                easy_drag=False,
                shadow=True,
                min_size=(240, 480))

        if hasattr(server, "set_ui_window"):
            server.set_ui_window(window)
        if hasattr(window, "events") and hasattr(window.events, "loaded"):
            window.events.loaded += diagnostics.ready

        # T06 性能优化：窗口创建后，在后台线程中预热重型 Python 依赖（openai/httpx/pydantic），
        # 避免首轮请求交互时产生首次文件导入延迟
        def _prewarm_modules():
            try:
                import openai  # noqa: F401
                import httpx   # noqa: F401
                import pydantic  # noqa: F401
            except Exception:
                pass

        threading.Thread(target=_prewarm_modules, daemon=True, name="alterego-prewarm").start()

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
        instance_lock.release()


if __name__ == "__main__":
    main()
