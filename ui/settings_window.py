"""AlterEgo 独立大屏设置子窗口 (pywebview 原生桌面窗口)。

通过独立子进程拉起 1040x780 独立 pywebview 原生窗口展示 settings.html，
GUI 线程完全隔离，不阻塞主窗口消息循环，杜绝弹出外部浏览器。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import subprocess
from pathlib import Path

log = logging.getLogger("alterego.settings_window")


def show_settings_window(port: int = 5000, token: str = "") -> None:
    """创建并运行 pywebview 独立大屏设置窗口。"""
    try:
        from ui.launcher import _ensure_windows_desktop_runtime
        _ensure_windows_desktop_runtime()
        import webview
    except ImportError:
        log.warning("[settings_window] pywebview 未安装")
        return

    target_url = f"http://127.0.0.1:{port}/settings?token={token}"
    icon_path = Path(__file__).resolve().parent / "static" / "alterego.ico"
    icon_arg = str(icon_path) if icon_path.exists() else None

    try:
        window = webview.create_window(
            title="AlterEgo - 系统设置与模型密钥配置中心",
            url=target_url,
            width=1040,
            height=780,
            min_size=(760, 560),
            resizable=True,
            shadow=True,
        )

        def close():
            try:
                window.destroy()
            except Exception:
                pass

        if hasattr(window, "expose"):
            try:
                window.expose(close)
            except Exception:
                pass

        webview.start(debug=False)
    except Exception as e:
        log.warning("[settings_window] 打开独立设置窗口失败: %s", e)


def open_settings_window_async(port: int = 5000, token: str = "") -> subprocess.Popen | None:
    """在后台独立子进程中弹出原生设置桌面窗口，GUI 进程完全隔离。"""
    if os.environ.get("ALTEREGO_HEADLESS") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("[settings_window] 测试/无头环境，跳过子进程弹窗")
        return None

    repo_root = Path(__file__).resolve().parent.parent
    cmd = [
        sys.executable,
        "-m",
        "ui.settings_window",
        "--port", str(port),
    ]
    if token:
        cmd.extend(["--token", token])

    log_file = repo_root / "logs" / "settings_window.log"
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        f_out = open(log_file, "a", encoding="utf-8")
    except Exception:
        f_out = subprocess.DEVNULL

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=f_out,
            stderr=subprocess.STDOUT,
        )
        return proc
    except Exception as e:
        log.warning("[settings_window] 启动设置窗口子进程失败: %s", e)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="AlterEgo 独立大屏设置窗口")
    parser.add_argument("--port", type=int, default=5000, help="Web 服务端口")
    parser.add_argument("--token", default="", help="Auth Token")
    args = parser.parse_args()

    show_settings_window(port=args.port, token=args.token)


if __name__ == "__main__":
    main()
