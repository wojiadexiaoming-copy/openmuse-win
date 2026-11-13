"""原生置顶接管小浮窗（spec §31.5）。

通过 pywebview 创建无边框、置顶浮窗，内嵌 static/float.html
（接管卡片模式，高度由 float.js 经 api.resize 自适应）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from typing import Any

log = logging.getLogger("alterego.takeover_window")


def show_takeover_window(
    task_id: str,
    target: str = "Chrome",
    reason: str = "",
    port: int = 5000,
    token: str = "",
) -> None:
    """创建并运行 pywebview 置顶接管小浮窗。

    注意：此函数会阻塞当前线程直至窗口关闭。
    若需异步弹出，请使用 show_takeover_window_async。

    附3.7 铁律：pywebview 的 webview.start() 必须在 Python 主线程调用，
    在后台线程调用会直接报错。在非主线程中调用本函数时，自动降级为
    show_takeover_window_async（独立子进程），避免崩溃。
    """
    if threading.current_thread() is not threading.main_thread():
        log.warning(
            "[takeover_window] 非主线程调用 show_takeover_window，"
            "自动降级为子进程版（show_takeover_window_async）")
        show_takeover_window_async(task_id, target, reason, port, token)
        return
    try:
        import webview
    except ImportError:
        log.warning("[takeover_window] pywebview 未安装，跳过桌面浮窗弹出")
        return

    from urllib.parse import quote
    url = (f"http://127.0.0.1:{port}/static/float.html"
           f"?task_id={quote(task_id)}&target={quote(target)}&reason={quote(reason or '')}")
    if token:
        url += f"&token={token}"

    try:
        window = webview.create_window(
            title="AlterEgo",
            url=url,
            width=380,
            height=230,   # 初始高度 ≈ run 卡片；JS 按模式经 api.resize 自适应
            frameless=True,
            on_top=True,
            resizable=False,
        )

        def close():
            try:
                window.destroy()
            except Exception:
                pass

        def resize(height):
            """浮窗卡片高度自适应（float.js 按模式/胶囊调用）。"""
            try:
                h = max(90, min(430, int(height)))
                window.resize(380, h)
            except Exception:
                pass

        # 暴露 pywebview.api.close / api.resize 给前端 JS 调用
        # 注意：expose 只接受函数本身（按函数名注册），多传别名会抛 "Parameter must be a function"
        window.expose(close)
        window.expose(resize)
        webview.start(debug=False)
    except Exception as e:
        log.warning(f"[takeover_window] 创建浮窗失败（可能是无显示环境）: {e}")


def show_takeover_window_async(
    task_id: str,
    target: str = "Chrome",
    reason: str = "",
    port: int = 5000,
    token: str = "",
):
    """在后台独立子进程中弹出接管浮窗，不阻塞当前流程，GUI 进程完全隔离。"""
    import os
    import subprocess

    if os.environ.get("ALTEREGO_HEADLESS") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("[takeover_window] 测试/无头环境，跳过子进程弹窗")
        return None

    cmd = [
        sys.executable,
        "-m",
        "ui.takeover_window",
        "--task-id", task_id,
        "--target", target,
        "--reason", reason,
        "--port", str(port),
    ]
    if token:
        cmd.extend(["--token", token])

    try:
        # 子进程输出落日志（排障：浮窗起不来时能看到 traceback）
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_fp = open(os.path.join(log_dir, "float-subprocess.log"), "ab")
            proc = subprocess.Popen(cmd, stdout=log_fp, stderr=log_fp)
        except Exception:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info(f"[takeover_window] 浮窗子进程已启动 pid={proc.pid}")
        return proc
    except Exception as e:
        log.warning(f"[takeover_window] 启动浮窗子进程失败: {e}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="AlterEgo 接管置顶浮窗")
    parser.add_argument("--task-id", default="demo", help="任务 ID")
    parser.add_argument("--target", default="Chrome", help="接管目标窗口/软件名")
    parser.add_argument("--reason", default="", help="接管原因")
    parser.add_argument("--port", type=int, default=5000, help="Web 服务端口")
    parser.add_argument("--token", default="", help="Auth Token")
    args = parser.parse_args()

    show_takeover_window(
        task_id=args.task_id,
        target=args.target,
        reason=args.reason,
        port=args.port,
        token=args.token,
    )


if __name__ == "__main__":
    main()
