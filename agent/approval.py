"""§36 审批架构：三级粒度 + 决策树 + 白名单 + 疲劳对抗（§36.9）。

M2 实现核心逻辑。UI 卡片渲染在 ui/（H5 原型已有样式）。
§36.5 异步挂起：check_async() 抛出 ApprovalPendingInterrupt 而非阻塞主线程；
主循环捕获后保存上下文快照并 yield，用户决策到达时作为唤醒事件恢复。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field

from .risk import classify_tool_risk


class ApprovalPendingInterrupt(Exception):
    """§36.5 审批挂起中断：工具需要用户审批时抛出，而非阻塞主线程。

    主循环捕获此异常后应：
    1. 保存当前 run_turn() 的上下文快照（已执行的工具结果、待执行的工具调用）
    2. 立即退出本轮循环，释放主线程继续处理 message_queue
    3. 将 card_id 注册到唤醒表；用户点击 [批准]/[拒绝] 或发送自然语言
       （"发吧"、"行"、"按这个来"）时，作为新事件恢复执行。

    属性：
        card_id: 审批卡 ID，用于关联用户决策
        tool: 待执行的工具名
        args: 待执行的工具参数（已脱敏）
        snapshot: 调用方保存的上下文快照（由主循环填充）
    """
    def __init__(self, card_id: str, tool: str, args: dict,
                 snapshot: dict | None = None):
        super().__init__(f"审批挂起: {card_id}（{tool}）")
        self.card_id = card_id
        self.tool = tool
        self.args = args
        self.snapshot = snapshot or {}


def _summarize_shell(cmd: str) -> str:
    """把终端命令翻译成一句话人话（给客户看的摘要，不贴原始命令）。
    规则按具体到通用排序，命中即返回；没命中走通用兜底。"""
    c = cmd.lower()
    # 浏览器进程管理
    if "taskkill" in c and "msedge" in c:
        return "关闭 Edge 浏览器进程"
    if "taskkill" in c:
        return "结束指定的系统进程"
    # 等待/延时
    if "start-sleep" in c or "time.sleep" in c or "sleep(" in c:
        import re
        m = re.search(r"(?:start-sleep\s+-seconds\s+|time\.sleep\(|sleep\()\s*(\d+)", c)
        secs = m.group(1) if m else "若干"
        return f"等待约 {secs} 秒"
    # 文件查看/搜索
    if "dir" in c and ("shot" in c or ".jpg" in c):
        return "查找截图文件"
    if any(k in c for k in ("dir", "ls", "findstr", "grep")) and ".jpg" in c:
        return "在文件夹中搜索图片文件"
    if "dir" in c or ("ls" in c and "grep" in c):
        return "查看文件夹内容"
    # 文件写入
    if c.strip().startswith("echo") and ">" in c:
        return "保存一条文本记录到文件"
    # Python 脚本
    if "python" in c and (".py" in c or "<<" in c):
        return "运行 Python 脚本处理任务"
    # 网络
    if "curl" in c or "wget" in c:
        return "从网络下载文件"
    # 通用兜底
    return "在后台执行一条系统命令"


def _is_dangerous(action: str) -> bool:
    """危险操作（删文件/支付/发邮件等）永不给 always 选项。"""
    try:
        from .risk import _DANGEROUS_TOOLS
        return action in _DANGEROUS_TOOLS
    except Exception:
        return False


def humanize_approval(action: str, args: dict) -> tuple[str, str, str, str]:
    """生成面向普通用户的自然中文操作名称、摘要、详细描述以及【操作目的】。"""
    action_map = {
        "shell_exec": "执行系统终端命令",
        "file_write": "写入/创建本地文件",
        "file_edit": "修改本地文件",
        "file_read": "读取本地文件",
        "image_generate": "调用 API 生成图像",
        "video_generate": "调用 API 生成视频",
        "browser_navigate": "打开目标网页",
        "browser_click": "点击网页元素",
        "browser_click_at": "按坐标点击网页元素",

        "browser_type": "输入网页表单",
        "browser_new_tab": "新建浏览器标签页",
        "browser_switch_tab": "切换浏览器标签页",
        "browser_close_tab": "关闭浏览器标签页",
        "browser_context_new": "创建独立浏览器进程",
        "browser_context_list": "查看浏览器进程列表",
        "browser_context_switch": "切换浏览器进程",
        "browser_context_close": "关闭浏览器进程",
        "browser_context_cookies": "导入/导出浏览器 Cookie",
        "browser_verify_code_countdown": "核验验证码倒计时状态",
        "browser_lock_input": "锁定浏览器输入防误触",
        "browser_unlock_input": "解锁浏览器输入",
        "browser_screenshot": "网页截屏",
        "browser_snapshot": "读取页面结构",
        "browser_save_image": "保存网页图片到本地",
        "browser_request_takeover": "请求人工接管操作",
        "desktop_screenshot": "截取电脑桌面屏幕",
        "desktop_list_windows": "获取桌面运行窗口列表",
        "desktop_open_app": "启动或切换桌面软件",
        "desktop_click": "点击电脑桌面或窗口",
        "desktop_type": "在当前桌面窗口输入文本",
        "desktop_hotkey": "发送桌面系统快捷键",
    }
    action_cn = action_map.get(action, action)
    if action == "shell_exec":
        cmd = args.get("command", "") if isinstance(args, dict) else str(args)
        cmd_lower = cmd.lower()
        if "dir" in cmd_lower or "findstr" in cmd_lower or "ls" in cmd_lower:
            intent_cn = "扫描查找本地文件或下载目录，定位生成的图片/产物"
        elif "curl" in cmd_lower or "wget" in cmd_lower:
            if "9222" in cmd_lower or "127.0.0.1" in cmd_lower or "localhost" in cmd_lower:
                intent_cn = "尝试连接本地浏览器调试接口（注：此类私有端口探测已被系统安全拦截）"
            else:
                intent_cn = "通过网络请求下载远程文件或提取资源"
        elif "echo %" in cmd_lower or "pwd" in cmd_lower or "whoami" in cmd_lower:
            intent_cn = "获取当前系统环境路径（如用户主目录、下载文件夹等）"
        elif "where" in cmd_lower or "which" in cmd_lower:
            intent_cn = "检查系统环境中是否安装了特定命令行工具"
        elif "python" in cmd_lower:
            intent_cn = "运行本地 Python 脚本处理数据或文件"
        elif "git" in cmd_lower:
            intent_cn = "执行 Git 版本管理与代码提交操作"
        else:
            intent_cn = "在计算机后台终端执行指定命令行任务"

        summary_cn = _summarize_shell(cmd)
        detail_cn = (
            f"申请在系统终端执行命令行：\n{cmd}\n\n"
            f"（注：该命令将在您的电脑后台执行。请确认命令安全后再授权）"
        )
        return action_cn, summary_cn, detail_cn, intent_cn
    elif action == "file_write":
        path = args.get("path", "") if isinstance(args, dict) else ""
        content = args.get("content", "") if isinstance(args, dict) else ""
        summary_cn = f"写入本地文件：{path}"
        detail_cn = f"目标文件路径：{path}\n写入内容长度：约 {len(content)} 字符"
        intent_cn = f"将账号信息、海报配置或生成结果持久化保存到本地文件 ({path})"
        return action_cn, summary_cn, detail_cn, intent_cn
    elif action == "file_edit":
        path = args.get("path", "") if isinstance(args, dict) else ""
        summary_cn = f"修改本地文件：{path}"
        detail_cn = f"目标文件路径：{path}"
        intent_cn = f"更新修改项目中的现有文件内容 ({path})"
        return action_cn, summary_cn, detail_cn, intent_cn
    elif action.startswith("desktop_"):
        if action in ("desktop_open_app", "desktop_open"):
            app = (args.get("app_name") or args.get("target") or args.get("name") or "") if isinstance(args, dict) else str(args)
            intent_cn = f"启动或切换 Windows 桌面项目（应用/文件夹/文件）：{app}"
            summary_cn = f"打开目标：{app}"
            detail_cn = f"申请在 Windows 桌面打开目标：{app}"
        elif action in ("desktop_list_items", "desktop_list_shortcuts"):
            intent_cn = "扫描获取 Windows 桌面上的文件与文件夹列表"
            summary_cn = "查看桌面内容"
            detail_cn = "只读枚举桌面上的所有文件、文件夹和快捷方式"
        elif action == "desktop_show_desktop":
            intent_cn = "最小化所有前台窗口切回并显示 Windows 桌面"
            summary_cn = "显示桌面"
            detail_cn = "按下 Win+D 最小化全部前台窗口以展现真实桌面"
        elif action == "desktop_click":
            x, y = (args.get("x"), args.get("y")) if isinstance(args, dict) else (None, None)
            w = args.get("window_title", "") if isinstance(args, dict) else ""
            target_str = f"坐标 ({x}, {y})" if x is not None else f"窗口 [{w}]"
            intent_cn = f"在桌面上点击目标：{target_str}"
            summary_cn = f"桌面点击：{target_str}"
            detail_cn = f"申请模拟鼠标点击桌面目标：{target_str}"
        elif action == "desktop_type":
            txt = args.get("text", "") if isinstance(args, dict) else str(args)
            intent_cn = f"向当前前台桌面窗口输入文本内容"
            summary_cn = f"桌面输入：{txt[:30]}"
            detail_cn = f"申请向当前前台活跃窗口输入内容：\n{txt}"
        else:
            intent_cn = f"执行 {action_cn} 桌面系统控制操作"
            summary_cn = f"{action_cn}"
            detail_cn = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
        return action_cn, summary_cn, detail_cn, intent_cn
    else:
        summary_cn = f"申请操作：{action_cn}"
        detail_cn = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
        intent_cn = f"执行 {action_cn} 操作推进当前任务流程"
        return action_cn, summary_cn, detail_cn, intent_cn


@dataclass
class ApprovalCard:
    approval_id: str
    action: str                        # 工具名，如 "file_write"
    detail: str                        # 参数摘要
    risk: str                          # "L1" | "L2" | "L3"
    action_cn: str = ""                # 中文工具名称
    summary_cn: str = ""               # 中文简要说明
    detail_cn: str = ""                # 中文详细内容
    intent_cn: str = ""                # 中文操作目的与用途解释
    scope: str = "once"                # once | session | always
    batch: list = field(default_factory=list)  # §36.9 合并的同类审批
    created_at: float = field(default_factory=time.time)
    timeout_sec: int = 300
    # §36.11 幂等防重击：pending → consumed|approved|rejected 单向流转
    # 前端点击后立即 disabled 显示"处理中..."，后端 status 置 consumed，
    # 200ms 内到达的重复指令直接抛弃，杜绝双击并发重复发送
    status: str = "pending"
    # §36.12 分布式乐观锁：多端（Web/手机）同时操作同一张卡时，
    # 提交必须携带 version_id；若版本已变，提交失效并提示"已被其他端处理"
    version_id: int = 0

    @property
    def risk_level(self) -> str:
        return self.risk

    @property
    def requested_at(self) -> float:
        return self.created_at

    def consume(self) -> bool:
        """尝试消费此卡：pending→consumed 成功返回 True，已消费返回 False。"""
        if self.status != "pending":
            return False
        self.status = "consumed"
        self.version_id += 1
        return True



class ApprovalManager:
    def __init__(self, db_path: str, timeout_sec: int = 300, settings=None):
        from agent.db import get_db
        self.db = get_db(db_path)
        self._lock = threading.Lock()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS approvals"
            "(id TEXT PRIMARY KEY, action TEXT, risk TEXT, decision TEXT,"
            " scope TEXT, ts REAL, detail TEXT)")
        try:
            self.db.execute("ALTER TABLE approvals ADD COLUMN detail TEXT")
            self.db.commit()
        except Exception:
            pass
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS whitelist"
            "(wkey TEXT PRIMARY KEY, action TEXT, added_ts REAL)")
        self.timeout_sec = timeout_sec
        self.settings = settings  # Settings（§37）：常驻策略先行
        # 待用户决策的卡：approval_id -> {"event": Event, "decision": bool|None}
        self._pending: dict[str, dict] = {}
        # 会话许可：wkey -> True（本任务内同类免批）
        self._session_ok: set[str] = set()

    # ---------- 风险定级 ----------

    def classify(self, tool: str, args: dict) -> str:
        return classify_tool_risk(tool, args)

    def _wkey(self, tool: str, args: dict) -> str:
        sig = hashlib.sha256(
            json.dumps(args, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        return f"{tool}:{sig}"

    # ---------- 主入口 ----------

    def check(self, tool: str, args: dict,
              on_ask=None, prov_level: int = 0) -> bool:
        """返回 True=允许执行，False=拒绝。

        on_ask(card) -> (approved: bool, scope: str)：UI 层实现，同步阻塞等用户；
        为 None 时（无 UI/测试）L2/L3 直接拒绝（Fail Closed）。
        prov_level：指令来源等级（§27.5），L2 来源的 L2 操作强制升 L3。
        """
        # Explicit denial remains authoritative. Routine L1 actions need no card,
        # including installations with a persisted legacy "ask" policy.
        policy = self.settings.get(tool) if self.settings is not None else "ask"
        if policy == "deny":
            self._record_denied(tool, args)
            return False
        risk = self.classify(tool, args)
        if risk == "L1" or (policy == "allow" and risk != "L3"):
            return True
        if prov_level == 2 and risk == "L2":
            risk = "L3"  # §27.5：低信任域来源的敏感操作升级
        if risk == "L1":
            return True
        wkey = self._wkey(tool, args)
        with self._lock:
            if (risk != "L3" and self._in_whitelist(wkey)) or wkey in self._session_ok:
                return True
        if on_ask is None:
            return False  # 无人可问 → Fail Closed

        # §36.9：5 分钟内同工具+同风险的卡合并（L3 永不合并）
        host = self._find_batch_host(tool, risk) if risk != "L3" else None
        if host is not None:
            approved, scope = self._wait_on_host(host)
            # 并入的卡单独记一行（ID 加后缀，不与宿主卡主键冲突）
            merged = ApprovalCard(
                approval_id=host["card"].approval_id + f"#m{wkey[:8]}",
                action=tool, detail=host["card"].detail, risk=risk)
            self._record(merged, approved, scope, wkey)
            return approved

        # 预先拦截绝对危险违规模式，直接拒绝，避免给用户弹出注定失败的无谓骚扰弹窗
        if tool == "shell_exec":
            cmd_check = (args.get("command", "") if isinstance(args, dict) else str(args)).lower()
            if any(bad in cmd_check for bad in ("127.0.0.1:9222", "localhost:9222", ":9222", "rm -rf /")):
                self._record_denied(tool, args)
                return False

        action_cn, summary_cn, detail_cn, intent_cn = humanize_approval(tool, args)
        card = ApprovalCard(
            approval_id=f"ap-{uuid.uuid4().hex[:10]}",
            action=tool, detail=json.dumps(args, ensure_ascii=False)[:500],
            risk=risk, action_cn=action_cn, summary_cn=summary_cn,
            detail_cn=detail_cn, intent_cn=intent_cn, timeout_sec=self.timeout_sec)
        with self._lock:
            self._pending[card.approval_id] = {
                "card": card, "event": threading.Event(),
                "decision": None, "scope": "once",
            }
        try:
            approved, scope = on_ask(card)
        except Exception:  # noqa: BLE001 —— UI 异常按拒绝处理
            approved, scope = False, "once"
        with self._lock:
            p = self._pending.pop(card.approval_id, None)
            if p is not None:
                p["decision"] = approved
                p["scope"] = scope
                p["event"].set()
        self._record(card, approved, scope, wkey)
        return approved

    def check_async(self, tool: str, args: dict, prov_level: int = 0) -> bool:
        """§36.5 异步审批：不阻塞主线程，需要审批时抛出 ApprovalPendingInterrupt。

        与 check() 的区别：
        - check()：同步阻塞等用户（on_ask 回调），用于测试/无 UI 环境
        - check_async()：L1 直接通过；L2/L3 需要审批时抛出中断，
          主循环捕获后挂起本轮，用户决策到达时恢复

        自然语言隐式审批（§36.10）：用户在聊天框直接说"发吧"/"行"/"按这个来"
        时，主循环将其路由为对挂起卡的批准，无需点击审批卡按钮。
        """
        policy = self.settings.get(tool) if self.settings is not None else "ask"
        if policy == "deny":
            self._record_denied(tool, args)
            return False
        risk = self.classify(tool, args)
        if risk == "L1" or (policy == "allow" and risk != "L3"):
            return True
        if prov_level == 2 and risk == "L2":
            risk = "L3"
        if risk == "L1":
            return True

        # 需要审批：创建卡片并抛出中断，不阻塞
        wkey = self._wkey(tool, args)
        with self._lock:
            if wkey in self._session_ok:
                return True

        action_cn, summary_cn, detail_cn, intent_cn = humanize_approval(tool, args)
        card = ApprovalCard(
            approval_id=f"ap-{uuid.uuid4().hex[:10]}",
            action=tool, detail=json.dumps(args, ensure_ascii=False)[:500],
            risk=risk, action_cn=action_cn, summary_cn=summary_cn,
            detail_cn=detail_cn, intent_cn=intent_cn, timeout_sec=self.timeout_sec)
        with self._lock:
            self._pending[card.approval_id] = {
                "card": card, "event": threading.Event(),
                "decision": None, "scope": "once",
            }
        # 抛出中断：主循环捕获后保存快照并 yield
        # 脱敏后的参数（不含密码/token）
        safe_args = {k: ("***" if any(s in k.lower() for s in
                       ("password", "token", "secret", "key") ) else v)
                     for k, v in (args.items() if isinstance(args, dict) else {})}
        # 深层日志：审批挂起（脱敏后）
        import logging as _logging
        _logging.getLogger("alterego.approval").info(
            "[approval_pending] card=%s tool=%s risk=%s",
            card.approval_id, tool, risk)
        raise ApprovalPendingInterrupt(card.approval_id, tool, safe_args)

    def _find_batch_host(self, tool: str, risk: str) -> dict | None:
        """找 5 分钟内同工具+同风险的在途卡。"""
        with self._lock:
            for p in self._pending.values():
                c = p["card"]
                if (c.action == tool and c.risk == risk
                        and time.time() - c.created_at < 300
                        and p["decision"] is None):
                    return p
        return None

    def _wait_on_host(self, host: dict) -> tuple[bool, str]:
        """等宿主卡出结果（超时按拒绝）。"""
        host["event"].wait(timeout=self.timeout_sec)
        with self._lock:
            d = host.get("decision")
            s = host.get("scope", "once")
        return (bool(d), s)

    def _record(self, card: ApprovalCard, approved: bool, scope: str, wkey: str):
        # 用户已批准 L3 放开 always（2026-10-15），但危险操作永不给 always
        try:
            from .risk import _DANGEROUS_TOOLS
            if card.action in _DANGEROUS_TOOLS and scope == "always":
                scope = "once"
        except Exception:
            pass
        with self._lock:
            self.db.execute(
                "INSERT INTO approvals (id, action, risk, decision, scope, ts, detail) VALUES (?,?,?,?,?,?,?)",
                (card.approval_id, card.action, card.risk,
                 "approved" if approved else "rejected", scope, time.time(), getattr(card, "detail", "") or ""))
            if approved and scope == "always":
                self.db.execute(
                    "INSERT OR REPLACE INTO whitelist VALUES (?,?,?)",
                    (wkey, card.action, time.time()))
            if approved and scope == "session":
                self._session_ok.add(wkey)
            self.db.commit()

    def _record_denied(self, tool: str, args: dict):
        """常驻策略 deny 的记录（审计用，不弹卡）。"""
        detail_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
        with self._lock:
            self.db.execute(
                "INSERT INTO approvals (id, action, risk, decision, scope, ts, detail) VALUES (?,?,?,?,?,?,?)",
                (f"deny-{time.time_ns()}", tool, self.classify(tool, args),
                 "rejected", "policy", time.time(), detail_str))
            self.db.commit()

    def _in_whitelist(self, wkey: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM whitelist WHERE wkey=?", (wkey,)).fetchone() is not None

    # ---------- 白名单管理（spec §36.6） ----------

    def revoke(self, wkey: str):
        with self._lock:
            self.db.execute("DELETE FROM whitelist WHERE wkey=?", (wkey,))
            self._session_ok.discard(wkey)
            self.db.commit()

    def list_whitelist(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT wkey, action, added_ts FROM whitelist").fetchall()
        return [{"wkey": k, "action": a, "added_ts": ts} for k, a, ts in rows]

    def stats(self, action: str = "", days: int = 30) -> dict:
        """§36.9 降级建议用：同类操作的批准/拒绝统计。"""
        if action:
            rows = self.db.execute(
                "SELECT decision, COUNT(*) FROM approvals WHERE action=? AND ts>? "
                "GROUP BY decision", (action, time.time() - days * 86400)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT decision, COUNT(*) FROM approvals WHERE ts>? "
                "GROUP BY decision", (time.time() - days * 86400,)).fetchall()
        d = dict(rows)
        return {"approved": d.get("approved", 0), "rejected": d.get("rejected", 0)}

    def list_pending(self) -> list[dict]:
        """UI 获取待审批卡列表。"""
        with self._lock:
            return [
                {
                    "approval_id": p["card"].approval_id,
                    "action": p["card"].action,
                    "action_cn": getattr(p["card"], "action_cn", "") or p["card"].action,
                    "summary_cn": getattr(p["card"], "summary_cn", "") or p["card"].detail,
                    "detail_cn": getattr(p["card"], "detail_cn", "") or p["card"].detail,
                    "intent_cn": getattr(p["card"], "intent_cn", "") or "",
                    "detail": p["card"].detail,
                    "risk_level": p["card"].risk,
                    "scope_options": (["once", "session"] if _is_dangerous(p["card"].action)
                                      else ["once", "session", "always"]),
                    "expires_at": int((p["card"].created_at + p["card"].timeout_sec) * 1000),
                    "status": "pending",
                }
                for p in self._pending.values()
                if p.get("decision") is None
            ]

    def list_history(self, limit: int = 50) -> list[dict]:
        """UI 获取已决断的历史审核审计记录（倒序）。"""
        action_map = {
            "shell_exec": "执行系统终端命令",
            "file_write": "写入/创建本地文件",
            "file_edit": "修改本地文件",
            "file_read": "读取本地文件",
            "image_generate": "调用 API 生成图像",
            "video_generate": "调用 API 生成视频",
            "browser_navigate": "打开目标网页",
            "browser_click": "点击网页元素",
            "browser_click_at": "按坐标点击网页元素",
            "browser_type": "输入网页表单",
            "browser_screenshot": "网页截屏",
            "browser_snapshot": "读取页面结构",
            "browser_save_image": "保存网页图片到本地",
            "browser_request_takeover": "请求人工接管操作",
            "browser_lock_input": "锁定浏览器输入与遮罩",
            "browser_wait": "浏览器等待页面稳定",
            "browser_new_tab": "新建浏览器标签页",
            "browser_switch_tab": "切换浏览器标签页",
            "state_pin": "固定关键状态",
            "wait": "等待指定时间",
            "desktop_screenshot": "截取电脑桌面屏幕",
            "desktop_list_windows": "获取桌面运行窗口列表",
            "desktop_list_shortcuts": "扫描桌面快捷方式",
            "desktop_open_app": "启动或切换桌面软件",
            "desktop_click": "点击电脑桌面或窗口",
            "desktop_type": "在当前桌面窗口输入文本",
            "desktop_hotkey": "发送桌面系统快捷键",
        }
        with self._lock:
            cols = [c[1] for c in self.db.execute("PRAGMA table_info(approvals)").fetchall()]
            has_detail = "detail" in cols
            if has_detail:
                rows = self.db.execute(
                    "SELECT id, action, risk, decision, scope, ts, detail "
                    "FROM approvals ORDER BY rowid DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            else:
                rows = [r + ("",) for r in self.db.execute(
                    "SELECT id, action, risk, decision, scope, ts "
                    "FROM approvals ORDER BY rowid DESC LIMIT ?",
                    (limit,)
                ).fetchall()]

        results = []
        for row in rows:
            aid, action, risk, decision, scope, ts, detail = row
            action_cn = action_map.get(action, action)
            results.append({
                "approval_id": aid,
                "action": action,
                "action_cn": action_cn,
                "risk_level": risk or "L2",
                "decision": decision or "unknown",
                "scope": scope or "once",
                "ts": float(ts) if ts else time.time(),
                "status": decision or "resolved",
                "detail": detail or "",
            })
        return results


    def resolve_card(self, approval_id: str, approved: bool, scope: str = "once",
                     version_id: int | None = None) -> dict:
        """UI 用户解决审批卡（批准/拒绝）。

        §36.11 幂等：已消费的卡重复提交直接返回 {"ok": False, "reason": "already_consumed"}。
        §36.12 乐观锁：version_id 不匹配时返回 {"ok": False, "reason": "version_conflict"}，
        前端弹窗提示"该项已被其他客户端处理"。
        """
        with self._lock:
            p = self._pending.get(approval_id)
            if not p:
                return {"ok": False, "reason": "not_found"}
            card = p["card"]
            # 幂等检查
            if not card.consume():
                return {"ok": False, "reason": "already_consumed",
                        "detail": f"该审批已在 {card.status} 状态，重复提交已抛弃"}
            # 乐观锁检查（调用方提供 version_id 时才校验）
            if version_id is not None and version_id != card.version_id - 1:
                # 回滚 consume（版本号已+1，需恢复）
                card.status = "pending"
                card.version_id -= 1
                return {"ok": False, "reason": "version_conflict",
                        "detail": "该项已被其他客户端处理，请刷新后重试"}
            if card.risk == "L3" and scope == "always":
                card.status = "pending"
                card.version_id -= 1
                raise ValueError("L3 不允许 always")
            p["decision"] = approved
            p["scope"] = scope
            p["event"].set()
            # 深层日志：审批决议（脱敏）
            import logging as _logging
            _logging.getLogger("alterego.approval").info(
                "[approval_resolved] card=%s approved=%s scope=%s tool=%s",
                approval_id, approved, scope, getattr(card, "action", "?"))
            return {"ok": True, "approval_id": approval_id,
                    "approved": approved, "version_id": card.version_id}

