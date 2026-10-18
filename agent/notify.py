"""§31.4 系统通知推送（Windows Toast / UI Push / 日志）。

重要事件弹系统通知（用户不盯着 Web UI 时）：
1. 审批请求（ApprovalCard）
2. Hook / 定时任务异常触发
3. 关键任务完成/失败
4. Kill Switch 触发

实现：
- Windows 系统 Toast 原生通知（通过 PowerShell Windows.UI.Notifications 异步无阻触发）
- 关键/紧急（urgent=True）同时写入系统关键日志
- 支持向前端或外部注册的回调广播（如 EventBus SSE 推送）
"""
from __future__ import annotations

import logging
import platform
import subprocess
import threading
from typing import Callable

logger = logging.getLogger("alterego.notify")

_LISTENERS: list[Callable[[str, str, bool], None]] = []
_LOCK = threading.Lock()


def register_notification_listener(listener: Callable[[str, str, bool], None]):
    """注册全局通知监听器（供 UI 服务或外部总线使用）。"""
    with _LOCK:
        if listener not in _LISTENERS:
            _LISTENERS.append(listener)


def unregister_notification_listener(listener: Callable[[str, str, bool], None]):
    with _LOCK:
        if listener in _LISTENERS:
            _LISTENERS.remove(listener)


def _send_windows_toast(title: str, message: str):
    """利用 Windows 运行时 ToastNotificationManager 原生触发无依赖弹窗。"""
    if platform.system().lower() != "windows":
        return

    # 转义双引号与重音符
    t_clean = title.replace('"', '""').replace("`", "``")
    m_clean = message.replace('"', '""').replace("`", "``")
    ps_cmd = (
        f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        f"$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f"$textNodes = $template.GetElementsByTagName('text'); "
        f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{t_clean}')) | Out-Null; "
        f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{m_clean}')) | Out-Null; "
        f"$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AlterEgo').Show($toast);"
    )

    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        logger.debug("Windows toast 通知触发异常: %s", e)


def notify(title: str, message: str, urgent: bool = False):
    """发送系统通知。

    Args:
        title: 通知标题
        message: 通知内容
        urgent: 是否为紧急通知（写 CRITICAL 级日志）
    """
    if urgent:
        logger.critical(f"[URGENT] {title}: {message}")
    else:
        logger.info(f"[NOTIFY] {title}: {message}")

    # 1. 触发 Windows 系统桌面通知
    try:
        threading.Thread(target=_send_windows_toast, args=(title, message), daemon=True).start()
    except Exception:
        pass

    # 2. 广播给已注册的监听器（如 Web UI SSE）
    with _LOCK:
        listeners = list(_LISTENERS)
    for fn in listeners:
        try:
            fn(title, message, urgent)
        except Exception as e:
            logger.debug("Notification listener error: %s", e)
