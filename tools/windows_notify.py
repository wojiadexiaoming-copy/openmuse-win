"""Windows 10/11 原生交互式 Toast 通知模块。

支持在 Windows 屏幕右下角弹出带有 [✔ 批准授权] / [✕ 拒绝] 或 [👉 聚焦浏览器] 按钮的系统通知。
通过自定义协议 alterego:// + pythonw.exe 后台静默处理决断，彻底告别浏览器闪烁与多余标签页弹窗。
"""
from __future__ import annotations

import logging
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any
import urllib.parse
import urllib.request

logger = logging.getLogger("alterego.notify")


def _xml_esc(s: str) -> str:
    """转义 XML 特殊字符。"""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def ensure_protocol_registered() -> bool:
    """注册 alterego:// 自定义协议到 Windows 当前用户注册表，绑定 pythonw.exe 实现后台零弹窗静默决断。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        # 寻找静默无黑框控制台的 pythonw.exe
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        if not pythonw.exists():
            pythonw = Path(sys.executable)

        handler_script = Path(__file__).resolve()
        cmd = f'"{pythonw}" "{handler_script}" handle "%1"'

        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\alterego")
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:AlterEgo Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        cmd_key = winreg.CreateKey(key, r"shell\open\command")
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(cmd_key)
        winreg.CloseKey(key)
        return True
    except Exception as e:
        logger.debug("Failed to register alterego protocol: %s", e)
        return False


def focus_edge_browser_window():
    """将后台或被遮挡的 Edge 自动化浏览器窗口推到桌面最前台。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _enum_proc(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                    if "Edge" in title or "个人" in title or "InPrivate" in title or "Work" in title:
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        user32.SetForegroundWindow(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_proc), 0)
    except Exception:
        pass


def handle_protocol(uri: str):
    """静默处理 Windows 协议回调，绝不弹出或闪烁任何浏览器窗口。"""
    try:
        uri = uri.strip("\"' ")
        parsed = urllib.parse.urlparse(uri)
        # 支持 alterego://resolve?id=...&approved=1&port=5000&token=...
        # 或 alterego://takeover_focus?tid=...&port=5000&token=...
        host = (parsed.netloc or parsed.path).strip("/")
        params = urllib.parse.parse_qs(parsed.query)

        port = params.get("port", ["5000"])[0]
        token = params.get("token", [""])[0]

        if host == "resolve":
            aid = params.get("id", [""])[0]
            approved = params.get("approved", ["1"])[0]
            scope = params.get("scope", ["once"])[0]
            target_url = (
                f"http://127.0.0.1:{port}/api/approvals/toast_resolve"
                f"?id={aid}&approved={approved}&scope={scope}&token={token}"
            )
            req = urllib.request.Request(
                target_url,
                headers={"X-AlterEgo-Token": token, "Accept": "application/json"}
            )
            urllib.request.urlopen(req, timeout=3)
        elif host in ("takeover_focus", "takeover"):
            tid = params.get("tid", [""])[0]
            target_url = f"http://127.0.0.1:{port}/api/takeover/{tid}/focus?token={token}"
            req = urllib.request.Request(
                target_url,
                headers={"X-AlterEgo-Token": token, "Accept": "application/json"}
            )
            try:
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass
            focus_edge_browser_window()
    except Exception as e:
        logger.debug("Failed to handle protocol %s: %s", uri, e)


def show_approval_toast(
    card: Any,
    port: int = 5000,
    api_token: str = "",
) -> bool:
    """向 Windows 屏幕右下角发送带交互按钮的系统审批授权通知。

    点击按钮通过 alterego:// 协议在后台静默发起 HTTP 决断，绝不弹窗或打开浏览器标签页。
    """
    if sys.platform != "win32":
        return False

    ensure_protocol_registered()

    if isinstance(card, dict):
        aid = card.get("approval_id") or "unknown"
        action_cn = card.get("action_cn") or card.get("action") or "敏感操作"
        summary_cn = card.get("summary_cn") or card.get("detail") or ""
        detail = card.get("detail") or summary_cn or ""
        intent_cn = card.get("intent_cn") or summary_cn or ""
        risk = card.get("risk_level") or card.get("risk") or "L2"
    else:
        aid = getattr(card, "approval_id", "unknown")
        action_cn = getattr(card, "action_cn", "") or getattr(card, "action", "敏感操作")
        summary_cn = getattr(card, "summary_cn", "") or getattr(card, "detail", "")
        detail = getattr(card, "detail", "") or summary_cn or ""
        intent_cn = getattr(card, "intent_cn", "") or summary_cn or ""
        risk = getattr(card, "risk_level", getattr(card, "risk", "L2"))

    # 系统信息完整呈现：第 1 行标题等级，第 2 行操作意图，第 3 行真实具体指令/参数明细
    title_xml = _xml_esc(f"🛡️ AlterEgo 权限审批 ({risk}) · {action_cn}")
    intent_text = intent_cn or summary_cn or action_cn
    intent_xml = _xml_esc(f"意图：{intent_text}")
    clean_detail = str(detail).replace("\r", " ").replace("\n", " ").strip()
    if len(clean_detail) > 180:
        clean_detail = clean_detail[:180] + "…"
    detail_xml = _xml_esc(f"明细：{clean_detail}")

    token_arg = f"&amp;token={api_token}" if api_token else ""
    approve_url = f"alterego://resolve?id={aid}&amp;approved=1&amp;scope=once&amp;port={port}{token_arg}"
    reject_url = f"alterego://resolve?id={aid}&amp;approved=0&amp;scope=once&amp;port={port}{token_arg}"

    ps_code = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

$xml = @"
<toast duration="long">
    <visual>
        <binding template="ToastGeneric">
            <text>{title_xml}</text>
            <text>{intent_xml}</text>
            <text>{detail_xml}</text>
        </binding>
    </visual>
    <actions>
        <action content="✔ 批准授权" arguments="{approve_url}" activationType="protocol"/>
        <action content="✕ 拒绝" arguments="{reject_url}" activationType="protocol"/>
    </actions>
</toast>
"@

$doc = [Windows.Data.Xml.Dom.XmlDocument]::new()
$doc.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe")
$notifier.Show($toast)
"""

    def _async_send():
        try:
            CREATE_NO_WINDOW = 0x08000000
            subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps_code],
                creationflags=CREATE_NO_WINDOW,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as e:
            logger.warning("Windows Toast 发送失败: %s", e)

    threading.Thread(target=_async_send, daemon=True, name="toast-sender").start()
    return True


def show_takeover_toast(
    req: Any,
    port: int = 5000,
    api_token: str = "",
) -> bool:
    """向 Windows 屏幕右下角发送人工协同通知。

    点击按钮仅将已打开的 Edge 自动化浏览器窗口推到前台，绝不弹窗或新开 H5 会话页。
    用户直接在左侧浏览器内操作，并在顶部横幅点击【完成接管】即可。
    """
    if sys.platform != "win32":
        return False

    ensure_protocol_registered()

    if isinstance(req, dict):
        tid = req.get("task_id") or req.get("id") or "takeover"
        reason = req.get("reason") or "检测到验证码或安全验证需要人工介入"
    else:
        tid = getattr(req, "task_id", getattr(req, "id", "takeover"))
        reason = getattr(req, "reason", "检测到验证码或安全验证需要人工介入")

    title_xml = _xml_esc("⚠️ AlterEgo 申请人工接管 (验证码/滑块)")
    reason_xml = _xml_esc(f"原因：{reason}")
    hint_xml = _xml_esc("提示：请直接在左侧已打开的 Edge 浏览器窗口中完成操作")
    token_arg = f"&amp;token={api_token}" if api_token else ""
    focus_url = f"alterego://takeover_focus?tid={tid}&amp;port={port}{token_arg}"

    ps_code = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

$xml = @"
<toast duration="long">
    <visual>
        <binding template="ToastGeneric">
            <text>{title_xml}</text>
            <text>{reason_xml}</text>
            <text>{hint_xml}</text>
        </binding>
    </visual>
    <actions>
        <action content="👉 聚焦浏览器 / 我去处理" arguments="{focus_url}" activationType="protocol"/>
    </actions>
</toast>
"@

$doc = [Windows.Data.Xml.Dom.XmlDocument]::new()
$doc.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe")
$notifier.Show($toast)
"""

    def _async_send():
        try:
            CREATE_NO_WINDOW = 0x08000000
            subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps_code],
                creationflags=CREATE_NO_WINDOW,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as e:
            logger.warning("Windows Takeover Toast 发送失败: %s", e)

    threading.Thread(target=_async_send, daemon=True, name="toast-takeover-sender").start()
    return True


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "handle":
        handle_protocol(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1].startswith("alterego://"):
        handle_protocol(sys.argv[1])
