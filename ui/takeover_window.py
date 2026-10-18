"""原生置顶接管小浮窗（spec §31.5）。

通过 pywebview 创建无边框、置顶的迷你小浮窗（340x110），
内嵌 takeover.html，提供【完成】与【取消】按钮及计时器。
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
        show_takeover_window_async(task_id, target, reason, port)
        return
    try:
        import webview
    except ImportError:
        log.warning("[takeover_window] pywebview 未安装，跳过桌面浮窗弹出")
        return

    url = f"http://127.0.0.1:{port}/takeover.html?task_id={task_id}&target={target}"

    try:
        window = webview.create_window(
            title="AlterEgo 接管中",
            url=url,
            width=340,
            height=110,
            frameless=True,
            on_top=True,
            resizable=False,
        )

        def _close_window():
            try:
                window.destroy()
            except Exception:
                pass

        # 暴露 pywebview.api.close 给前端 JS 调用
        window.expose(_close_window, "close")
        webview.start(debug=False)
    except Exception as e:
        log.warning(f"[takeover_window] 创建浮窗失败（可能是无显示环境）: {e}")


def show_takeover_window_async(
    task_id: str,
    target: str = "Chrome",
    reason: str = "",
    port: int = 5000,
):
    """在后台独立子进程中弹出接管浮窗，不阻塞当前流程，GUI 进程完全隔离。"""
    import os
    import subprocess

    if os.environ.get("ALTEREGO_HEADLESS") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("[takeover_window] 测试/无头环境，跳过子进程弹窗")
        return None

    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ui.takeover_window",
                "--task-id", task_id,
                "--target", target,
                "--reason", reason,
                "--port", str(port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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
    args = parser.parse_args()

    show_takeover_window(
        task_id=args.task_id,
        target=args.target,
        reason=args.reason,
        port=args.port,
    )


if __name__ == "__main__":
    main()
