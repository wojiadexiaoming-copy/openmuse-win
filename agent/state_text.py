"""Agent 状态与工具调用的人话文字集中管理（spec §31 / ui-redesign）。

供后端 SSE 事件广播使用，前端统一使用 detail / summary 字段，严禁向用户直接抛出裸 JSON。
"""
from __future__ import annotations

from typing import Any

from .models import Message, terminal_tool_control


ACTION_CN_MAP: dict[str, str] = {
    # 桌面自动化
    "desktop_show_desktop": "切回并显示桌面",
    "desktop_list_items": "获取桌面所有项目",
    "desktop_list_shortcuts": "扫描桌面快捷方式",
    "desktop_open_app": "打开桌面应用",
    "desktop_open": "打开桌面应用",
    "desktop_list_windows": "查找桌面窗口",
    "desktop_click": "点击桌面目标",
    "desktop_type": "输入键盘文本",
    "desktop_hotkey": "按下系统快捷键",
    "desktop_screenshot": "截取全屏桌面",
    "desktop_snapshot": "观察桌面窗口状态",
    "desktop_inspect_focus": "验证输入焦点",
    "desktop_wait": "等待桌面响应",
    "desktop_press": "按下桌面按键",
    "desktop_scroll": "滚动目标窗口",
    # 终端与文件
    "shell_exec": "执行系统命令",
    "file_read": "读取文件内容",
    "file_write": "保存文件",
    "file_edit": "修改文件",
    # 浏览器导航与标签页
    "browser_navigate": "打开目标网页",
    "browser_new_tab": "新建浏览器标签页",
    "browser_switch_tab": "切换网页标签页",
    "browser_close_tab": "关闭网页标签页",
    "browser_click": "点击网页按钮",
    "browser_click_at": "精准点击网页位置",
    "browser_probe_at": "探查页面元素",
    "browser_type": "填写网页表单",
    "browser_screenshot": "截取网页画面",
    "browser_snapshot": "分析页面元素结构",
    "browser_scroll": "滚动浏览网页",
    "browser_wait": "等待网页加载",
    "browser_evaluate": "执行网页交互动作",
    "browser_find_text": "查找页面文字位置",
    "browser_inspect": "检查页面元素状态",
    "browser_console": "诊断网页控制台",
    "browser_network_log": "核验网络请求",
    "browser_request_takeover": "请求人工协同接管",
    # 状态与持久化
    "state_check": "核验任务执行进度",
    "state_pin": "锁定任务关键数据",
    "state_clear": "清理任务临时状态",
    # 定时调度
    "cron_add": "创建定时任务",
    "cron_remove": "取消定时任务",
    "cron_list": "查看定时任务",
    # 保险箱与记忆
    "vault_get": "安全读取保险箱密钥",
    "vault_set": "安全存储保险箱密钥",
    "memory_read": "读取知识记忆库",
    "memory_write": "沉淀关键经验至记忆库",
    # 多模态
    "image_generate": "生成图像素材",
    "video_generate": "生成视频素材",
    # 子 Agent
    "spawn_subagent": "分发协同子任务",
    "poll_subagent": "查看协同子任务进展",
    # 人工接管
    "takeover_request": "请求人工协同接管",
    "ask_user": "向用户确认信息",
}


def tool_summary(tool: str, args: dict[str, Any] | None = None) -> str:
    """生成工具调用的人话摘要（如：'正在打开应用 ClashVerge.exe'），杜绝英文代码裸露。"""
    args = args or {}
    cn_name = ACTION_CN_MAP.get(tool, tool)

    if tool == "desktop_show_desktop":
        return "正在切回并显示 Windows 桌面"
    elif tool in ("desktop_list_items", "desktop_list_shortcuts"):
        return "正在感知并扫描桌面上的文件与文件夹"
    elif tool in ("desktop_open_app", "desktop_open"):
        target = args.get("app_name") or args.get("target") or args.get("name") or "桌面程序"
        return f"正在打开应用 {target}" if tool == "desktop_open_app" else f"正在打开桌面目标 {target}"
    elif tool == "desktop_list_windows":
        return "正在扫描桌面当前可见窗口"
    elif tool == "desktop_click":
        win = args.get("window_title")
        clicks = int(args.get("clicks", 2 if args.get("double_click") or args.get("double") else 1))
        action_word = "双击" if clicks >= 2 else "点击"
        if win:
            return f"正在{action_word}窗口「{win}」"
        x, y = args.get("x"), args.get("y")
        if x is not None and y is not None:
            return f"正在{action_word}屏幕目标位置"
        return f"正在{action_word}桌面位置"
    elif tool == "desktop_type":
        text = str(args.get("text", "")).strip()
        display_text = (text[:25] + "…") if len(text) > 25 else text
        return f"正在输入文本 \"{display_text}\""
    elif tool == "desktop_hotkey":
        keys = args.get("keys", [])
        keys_str = "+".join(keys) if isinstance(keys, list) else str(keys)
        return f"正在按下快捷键 {keys_str}"
    elif tool == "desktop_screenshot":
        return "正在截取桌面屏幕画面"
    elif tool == "shell_exec":
        cmd = str(args.get("command", "")).strip()
        display_cmd = (cmd[:35] + "…") if len(cmd) > 35 else cmd
        return f"正在执行系统命令: {display_cmd}"
    elif tool == "file_read":
        path = args.get("path") or args.get("file_path") or "指定文件"
        return f"正在读取文件 {path}"
    elif tool == "file_write":
        path = args.get("path") or args.get("file_path") or "本地文件"
        return f"正在保存文件 {path}"
    elif tool == "file_edit":
        path = args.get("path") or args.get("file_path") or "本地文件"
        return f"正在修改文件 {path}"
    elif tool == "browser_navigate":
        url = str(args.get("url", "")).strip()
        from urllib.parse import urlparse
        try:
            domain = urlparse(url).netloc or url
        except Exception:
            domain = url
        return f"正在打开网页 {domain}"
    elif tool == "browser_new_tab":
        url = str(args.get("url", "")).strip()
        role = args.get("role", "")
        role_tip = f"（{role}）" if role else ""
        return f"正在新建标签页打开网页{role_tip}"
    elif tool == "browser_switch_tab":
        role = args.get("role") or args.get("tab_id") or "目标标签页"
        role_name = {"mailbox": "邮箱页面", "target": "注册目标页面"}.get(role, role)
        return f"正在切换到「{role_name}」标签页"
    elif tool == "browser_close_tab":
        return "正在关闭当前标签页"
    elif tool == "browser_click":
        elem = args.get("selector") or args.get("text") or "网页按钮"
        import re
        m = re.search(r':(?:has-text|text-is)\(["\']?([^"\']+)["\']?\)', elem)
        clean_name = m.group(1) if m else elem
        if len(clean_name) > 25: clean_name = clean_name[:25] + "…"
        return f"正在点击页面「{clean_name}」"
    elif tool == "browser_click_at":
        return "正在点击页面指定按钮位置"
    elif tool == "browser_probe_at":
        return "正在定位探查页面按钮"
    elif tool == "browser_type":
        text = str(args.get("text", "")).strip()
        disp = (text[:16] + "…") if len(text) > 16 else text
        return f"正在填写输入框: {disp}" if text else "正在填写网页表单"
    elif tool == "browser_screenshot":
        return "正在截取当前网页画面"
    elif tool == "browser_snapshot":
        return "正在分析当前页面结构与元素"
    elif tool == "browser_scroll":
        return "正在滚动浏览网页内容"
    elif tool == "browser_wait":
        return "正在等待页面响应"
    elif tool == "browser_find_text":
        txt = args.get("text", "")
        return f"正在查找页面文字「{txt}」"
    elif tool == "browser_inspect":
        return "正在检查页面按钮可点击状态"
    elif tool == "browser_console":
        return "正在核查网页控制台日志"
    elif tool == "browser_network_log":
        return "正在核查网页网络请求结果"
    elif tool == "state_check":
        return "正在核查当前任务执行进度"
    elif tool == "state_pin":
        key = args.get("key", "关键凭据")
        val = str(args.get("value", ""))
        disp = (val[:20] + "…") if len(val) > 20 else val
        return f"正在记录任务凭据: {key}={disp}" if val else f"正在记录任务关键数据: {key}"
    elif tool == "state_clear":
        return "正在清理任务临时状态"
    elif tool == "vault_get":
        k = args.get("key", "密钥")
        return f"正在从硬件保险箱安全读取「{k}」"
    elif tool == "vault_set":
        k = args.get("key", "密钥")
        return f"正在将「{k}」安全存入硬件保险箱"
    elif tool == "image_generate":
        prompt = str(args.get("prompt", "")).strip()
        disp = (prompt[:25] + "…") if len(prompt) > 25 else prompt
        return f"正在调用 AI 绘图: {disp}"
    elif tool == "video_generate":
        return "正在调用 AI 生成视频"
    elif tool == "spawn_subagent":
        brief = str(args.get("brief", "")).strip()
        disp = (brief[:30] + "…") if len(brief) > 30 else brief
        return f"正在派发协同子任务: {disp}"
    elif tool == "poll_subagent":
        return "正在查询协同子任务进展"
    elif tool == "takeover_request":
        reason = args.get("reason", "需要人工操作")
        return f"请求人工协同接管: {reason}"

    return f"正在推进：{cn_name}"


def approval_summary(action: str, args: dict[str, Any] | None = None) -> str:
    """生成审批卡片的人话摘要。"""
    args = args or {}
    cn_name = ACTION_CN_MAP.get(action, action)
    if action == "shell_exec":
        cmd = str(args.get("command", "")).strip()
        disp = (cmd[:40] + "…") if len(cmd) > 40 else cmd
        return f"执行系统终端命令: {disp}"
    elif action in ("file_write", "file_edit"):
        path = args.get("path") or "本地文件"
        return f"修改本地关键文件: {path}"
    elif action == "desktop_click":
        return "在屏幕上执行自动化鼠标点击"
    return f"需要确认授权：{cn_name}"


def result_summary(result: str | Message, max_len: int = 60) -> str:
    """提取工具返回结果的一句话人话摘要，不包含冗长代码堆栈或长日志。"""
    if isinstance(result, Message):
        control = terminal_tool_control(result)
        if result.metadata.get("tool_cancelled") is True:
            return "本轮已安全暂停，该调用未执行"
        if control is not None:
            return ("桌面四角停止保护已触发，等待用户恢复" if control["reason"] == "desktop_failsafe"
                    else "工具无法自动重试，本轮已停止")
        verification = result.metadata.get("desktop_verification")
        if isinstance(verification, dict) and verification.get("focus_verified") is False:
            return "目标输入焦点未验证，不能输入或提交"
        if (result.name == "desktop_inspect_focus" and isinstance(verification, dict)
                and verification.get("focus_verified") is True):
            return "已验证目标窗口的可编辑输入焦点"
        result = result.content
    if not result:
        return "执行完毕"
    text = str(result).strip()
    if text.startswith("[桌面焦点验证失败]"):
        return "目标输入焦点未验证，不能输入或提交"
    if text.startswith("[桌面安全停止]"):
        return "桌面四角停止保护已触发，等待用户恢复"
    if text.startswith("[拒绝]"):
        return "用户拒绝了该项操作"
    if text.startswith("[工具执行失败]"):
        first_line = text.split("\n", 1)[0].replace("[工具执行失败]", "").strip()
        return f"执行出错: {first_line[:max_len]}"
    if "exit=1" in text or "Error" in text[:40]:
        first_line = text.split("\n", 1)[0].strip()
        return f"执行失败: {first_line[:max_len]}"
    if text.startswith("[已点击]"):
        return "点击操作已执行"
    if text.startswith("[已输入]"):
        return "表单内容填写完成"
    if text.startswith("[探针]"):
        return "元素位置探查完成"
    if text.startswith("[截图]"):
        return "页面画面已核验"
    if text.startswith("[当前页面]") or text.startswith("[无障碍树]"):
        return "页面元素分析完毕"
    if text.startswith("[已切换]"):
        return "标签页切换成功"
    if text.startswith("[已钉死]") or text.startswith("[钉死检查通过]"):
        return "关键数据核验锁定完成"
    first_line = text.split("\n", 1)[0].strip()
    clean = (first_line
             .replace("[桌面截图]", "")
             .replace("[桌面已点击]", "")
             .replace("[桌面已输入]", "")
             .replace("[截图]", "")
             .replace("[已点击]", "")
             .replace("[已输入]", "")
             .strip())
    if not clean:
        clean = "成功完成"
    return (clean[:max_len] + "…") if len(clean) > max_len else clean
