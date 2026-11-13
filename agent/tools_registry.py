"""§4.3 工具注册表 + §7 执行入口。

M1：注册、schema 生成（给 LLM 的 tools 参数）、execute_tool 调度。
M4 才加：并行执行（§5.5）、结果缓存（§7.3）、资源锁（§5.9）。
"""
from __future__ import annotations

import logging
import threading

from dataclasses import dataclass, field

from .models import Message, terminal_tool_control
from .resource_lock import resource_ids_for
from .risk import classify_tool_risk


logger = logging.getLogger("alterego.tools_registry")


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict            # JSON Schema
    func: object = None         # 实际执行函数 (args: dict) -> str | Message
    enabled: bool = True
    parallel_safe: bool = True  # 是否支持并发调用（副作用不可预测的为 False）
    is_readonly: bool = False   # 是否为纯只读操作
    cacheable: bool = False     # 是否允许结果缓存
    cache_ttl_sec: int = 0      # 缓存有效时间（秒）
    precheck: object = None     # (args)->str|None：执行层前置校验，先于人工审批


class ToolRegistry:
    # v1.5.21 P1-4：只读观察不消耗"每轮 1 次直接动作"额度。
    # 收尾时主 Agent 常用 snapshot/evaluate/probe 核实现状，这一步不该把动作额度吃掉，
    # 否则"只差最后一下点击"也只能再开一轮。
    _BROWSER_READONLY_TOOLS = frozenset({
        "browser_snapshot", "browser_screenshot", "browser_evaluate",
        "browser_find_text", "browser_probe_at", "browser_inspect",
        "browser_react_probe",
        "browser_console", "browser_network_log", "browser_wait",
        "browser_wait_for", "browser_observe", "browser_storage",
        "browser_resource", "browser_handler", "browser_verify_code_countdown",
        "browser_tabs", "browser_switch_tab", "browser_context_list",
    })
    # v1.5.22 P1-1：求助出口不受额度约束。被"动作额度用完"挡住求助，
    # 会把 Agent 困在"不能继续做、也不能交人工"的死角（v1.5.21 实测）。
    _BROWSER_ESCALATION_TOOLS = frozenset({"browser_request_takeover"})
    _BROWSER_READONLY_CAP = 8  # 只读也要有上限，防拿观察当推进刷轮次

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._aliases: dict[str, str] = {}
        self.approval = None  # ApprovalManager（M2）。main.py 里 set_approval 接入。
        self.on_ask = None    # on_ask(card)->(approved, scope)，UI/CLI 实现
        self.cache = None     # ToolResultCache（M4 §7.3）
        self.locks = None     # ResourceLock（M4 §5.9）
        self._desktop_tokens = __import__("contextvars").ContextVar("registry_desktop_tokens", default=())
        # 主 Agent 浏览器委派门禁：有子 Agent 管理器时启用，每轮只留 1 次直接操作额度
        self.browser_delegation_required = False
        self._browser_direct_calls: dict[str, int] = {}
        self._browser_readonly_calls: dict[str, int] = {}
        self._browser_verify_granted: set[str] = set()
        self._browser_direct_lock = threading.Lock()

    def begin_turn(self, scope_id):
        from tools.desktop_runtime import begin_turn
        self._desktop_tokens.set(self._desktop_tokens.get() + (begin_turn(scope_id),))
        if getattr(self, "browser_delegation_required", False):
            caller = getattr(threading.current_thread(), "_lease_caller_id", None)
            with self._browser_direct_lock:
                self._browser_direct_calls[caller] = 0
                self._browser_readonly_calls[caller] = 0
                if caller == "main":
                    self._browser_verify_granted.clear()

    def end_turn(self):
        from tools.desktop_runtime import end_turn
        tokens = self._desktop_tokens.get()
        if tokens:
            self._desktop_tokens.set(tokens[:-1])
            end_turn(tokens[-1])

    def set_approval(self, approval, on_ask=None):
        self.approval = approval
        self.on_ask = on_ask

    def set_cache(self, cache):
        self.cache = cache

    def set_locks(self, locks):
        self.locks = locks

    def register(self, tool: ToolDefinition):
        if tool.name in self._tools:
            raise ValueError(f"工具重名: {tool.name}")
        self._tools[tool.name] = tool

    def register_alias(self, alias_name: str, target_name: str):
        """注册别名：不进入 LLM openai_schemas 避免冗余，但调用时透明重定向至目标工具 (B5-01/B5-02)。"""
        self._aliases[alias_name] = target_name

    def get(self, name: str) -> ToolDefinition:
        target = self._aliases.get(name, name)
        try:
            return self._tools[target]
        except KeyError:
            raise ValueError(f"未知工具: {name}") from None

    def __contains__(self, name: str) -> bool:
        target = self._aliases.get(name, name)
        return target in self._tools

    def openai_schemas(self) -> list[dict]:
        """转成 LLM 的 tools 参数格式。"""
        return [{
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        } for t in self._tools.values() if t.enabled]

    @staticmethod
    def _desktop_safety_stop(name, error):
        """Normalize real desktop exception types; never match error text."""
        if not name.startswith("desktop_"):
            return None
        recognized = any(
            (base.__name__ == "FailSafeException" and base.__module__.split(".")[0] == "pyautogui")
            or (base.__name__ == "DesktopHalted" and base.__module__ == "tools.desktop_runtime")
            for base in type(error).__mro__
        )
        if not recognized:
            return None
        from tools.desktop_runtime import current_state
        current_state().halt("desktop_failsafe")
        return Message(role="tool", name=name,
                       content="[桌面安全停止] 四角停止保护已触发，本轮不会继续操作。请将鼠标移离屏幕四角后回复“继续”，恢复时必须重新截图。",
                       metadata={"tool_control": {"retryable": False, "requires_user": True,
                                                   "reason": "desktop_failsafe"}})

    @staticmethod
    def _validate_tool_args(tool: ToolDefinition, args: any) -> str | None:
        """SEC-01: 参数输入校验与原型污染/魔术属性防御。"""
        if not isinstance(args, dict):
            return f"[参数校验失败] 工具参数必须为 JSON 对象 (dict)，实际收到 {type(args).__name__}"

        stack = [args]
        forbidden_keys = {"__class__", "__proto__", "__init__", "__globals__", "__subclasses__", "__builtins__", "constructor"}
        while stack:
            curr = stack.pop()
            if isinstance(curr, dict):
                for k, v in curr.items():
                    if isinstance(k, str) and k in forbidden_keys:
                        return f"[参数校验失败] 检测到非法参数键: {k}"
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(curr, (list, tuple)):
                for item in curr:
                    if isinstance(item, (dict, list)):
                        stack.append(item)

        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            required = params.get("required") or []
            if isinstance(required, list):
                missing = [r for r in required if r not in args]
                if missing:
                    return f"[参数校验失败] 缺少必填参数: {', '.join(missing)}"
            props = params.get("properties") or {}
            if isinstance(props, dict):
                type_map = {
                    "string": (str,),
                    "integer": (int,),
                    "number": (int, float),
                    "boolean": (bool,),
                    "array": (list, tuple),
                    "object": (dict,),
                }
                for prop_name, prop_spec in props.items():
                    if prop_name in args and isinstance(prop_spec, dict):
                        expected_type = prop_spec.get("type")
                        val = args[prop_name]
                        if val is not None and expected_type in type_map:
                            if expected_type in ("integer", "number") and isinstance(val, bool):
                                return f"[参数校验失败] 参数 '{prop_name}' 类型错误: 期望 {expected_type}，实际为 bool"
                            if not isinstance(val, type_map[expected_type]):
                                return f"[参数校验失败] 参数 '{prop_name}' 类型错误: 期望 {expected_type}，实际为 {type(val).__name__}"
        return None

    def execute(self, name: str, args: dict, _provenance: dict | None = None,
                approval=None, task_id: str = "?") -> Message:
        """执行单个工具。M1 串行；异常转成 tool 消息，不抛到主循环。

        M2 接入：
        - approval: ApprovalManager，未通过则拒绝执行（§36）
        - _provenance: {"level": 0|1|2, "source_ref": str}，L2 来源的 L2/L3 操作
          强制升级风险（§27.5）
        """
        try:
            tool = self.get(name)
        except ValueError as e:
            return Message(role="tool", name=name,
                           content=f"[工具不存在] {e}。请仅从已提供的工具列表中选择调用。")
        if not tool.enabled:
            return Message(role="tool", name=name,
                           content=f"[工具未启用] {name} 在配置中被关闭")

        # SEC-01: 参数输入校验与原型污染防御
        val_err = self._validate_tool_args(tool, args)
        if val_err:
            return Message(role="tool", name=name, content=val_err)

        # 主 Agent 浏览器委派门禁（§39.7）：避免主 Agent 亲自连轴驱动浏览器。
        # 每轮仅保留 1 次直接动作额度（点击/输入/按键/导航）；只读观察不消耗额度，
        # 这样"子任务只差最后一步"时主 Agent 能先核实现状再补一下动作（v1.5.21 P1-4）。
        if (name.startswith("browser_")
                and name not in self._BROWSER_ESCALATION_TOOLS
                and getattr(self, "browser_delegation_required", False)):
            caller_id = getattr(threading.current_thread(), "_lease_caller_id", None)
            if caller_id == "main":
                readonly = name in self._BROWSER_READONLY_TOOLS
                with self._browser_direct_lock:
                    if readonly:
                        seen = int(self._browser_readonly_calls.get(caller_id, 0))
                        if seen >= self._BROWSER_READONLY_CAP:
                            return Message(role="tool", name=name, content=(
                                f"[浏览器观察额度用尽] 本轮只读浏览器调用已达 {self._BROWSER_READONLY_CAP} 次，"
                                f"{name} 未执行。不要再用 snapshot/evaluate/probe 打转："
                                "要么把多步任务交给浏览器子 Agent，要么基于现有证据收尾；"
                                "如只剩一个明确动作（已定位的 ref/坐标），直接用那 1 次动作额度完成"
                                "（动作后只读计数会重置）；实在做不下去就 browser_request_takeover 交人工。"))
                        self._browser_readonly_calls[caller_id] = seen + 1
                    else:
                        used = int(self._browser_direct_calls.get(caller_id, 0))
                        if used >= 1:
                            return Message(role="tool", name=name, content=(
                                "[浏览器任务须委派] 主 Agent 本轮已用完 1 次直接浏览器动作额度"
                                "（只读观察不占额度），"
                                f"{name} 未执行。多步/连续浏览器操作必须交给浏览器子 Agent：\n"
                                "1) spawn_subagent(brief=\"入口 URL / 完成标准 / 需回传的证据\", kind=\"browser\")\n"
                                "2) poll_subagent(task_id=...) 等待结果，期间不要再调用任何 browser_* 工具\n"
                                "3) 不要取消健康子任务来抢浏览器；只有连续两次验收失败或用户明确要求时才接管\n"
                                "本轮不再提供直接浏览器动作额度。请基于子 Agent 回传的证据完成答复；"
                                "如确需亲自复查，可在下一轮继续（不要要求用户代为操作）；"
                                "确实无法继续时可调用 browser_request_takeover（求助出口不受额度限制）。"))
                        self._browser_direct_calls[caller_id] = used + 1
                        # v1.5.22 P2：动作后重新截图/回读是正常核验流程，只读额度随动作重置。
                        self._browser_readonly_calls[caller_id] = 0

        # 提取参数中可能携带的 _provenance 并从入参中移除，避免污染工具函数
        if isinstance(args, dict) and "_provenance" in args:
            arg_prov = args.pop("_provenance")
            if _provenance is None:
                _provenance = arg_prov
            elif isinstance(arg_prov, dict) and isinstance(_provenance, dict):
                _provenance = {
                    "level": max(_provenance.get("level", 0), arg_prov.get("level", 0)),
                    "source_ref": arg_prov.get("source_ref") or _provenance.get("source_ref", "args"),
                }
        # Reject missing evidence before an action approval.
        if getattr(tool.func, "__module__", "") == "tools.desktop":
            from tools.desktop_runtime import current_state, EvidenceError, normalize_key
            state = current_state()
            try:
                # A halted desktop takes precedence over missing evidence,
                # invalid parameters, approvals and cached results.
                state.require_not_halted()
                if name == "desktop_click" and (args.get("x") is not None or args.get("control_ref")):
                    state.click_point(args)
                elif name == "desktop_type" and not state.focus:
                    raise EvidenceError("先取得目标窗口截图并点击可编辑输入框，验证焦点后才能输入。")
                elif name == "desktop_press" and normalize_key(args.get("key") or args.get("name") or "") == "enter" and not state.typed_hash:
                    raise EvidenceError("输入尚未读回验证，不能提交。")
                elif name in {"desktop_scroll", "desktop_hotkey"}:
                    state.evidence(args)
            except (EvidenceError, ValueError, TypeError) as exc:
                stop = self._desktop_safety_stop(name, exc)
                if stop is not None:
                    return stop
                return Message(role="tool", name=name, content="[桌面验证失败] " + str(exc))
        # 执行层前置校验：注定被拒的调用不进人工审批，避免用户为无效参数反复授权。
        # 这里只做与执行无关的纯检查；真正的执行校验仍在工具函数内（fail-closed）。
        if getattr(tool, "precheck", None) is not None:
            try:
                pre_err = tool.precheck(args)
            except Exception as exc:  # noqa: BLE001 —— 预校验自身异常不能拖垮执行链
                logger.debug("precheck 异常 tool=%s: %s", name, exc)
                pre_err = None
            if pre_err:
                return Message(role="tool", name=name, content=str(pre_err))
        # §27.5 指令溯源：没写来源按最坏情况（L2）处理
        prov_level = (_provenance or {}).get("level", 2)
        mgr = approval or self.approval
        if mgr is not None:
            ok = mgr.check(name, args, on_ask=self.on_ask, prov_level=prov_level)
            if not ok:
                return Message(role="tool", name=name, content="[审批未通过] 用户拒绝或超时")
        elif self._risk_of(name, args, prov_level) in ("L2", "L3"):
            # 无审批器时（纯测试），L2/L3 直接拒绝 —— Fail Closed
            return Message(role="tool", name=name,
                           content=f"[审批未通过] 无审批器，{name} 需要用户确认")
        # M4 §7.3：缓存命中直接返回 (以 ToolDefinition.cacheable 为准，或已在 cache.TTL 中的工具)
        if self.cache and (getattr(tool, "cacheable", False) or name in getattr(self.cache, "TTL", {})):
            hit = self.cache.get(name, args, prov_level=prov_level)
            if hit is not None:
                return Message(role="tool", name=name, content=hit + "\n[缓存命中]")
        # 浏览器租约仲裁（主 Agent / 子 Agent 互斥，§5.9）
        if name.startswith("browser_") and hasattr(self, "browser_worker") and self.browser_worker:
            bw = self.browser_worker
            if hasattr(bw, "check_lease"):
                caller_id = getattr(threading.current_thread(), "_lease_caller_id", None)
                owner = bw.current_lease_owner() if hasattr(bw, "current_lease_owner") else None
                if not bw.check_lease(caller_id):
                    if isinstance(caller_id, str) and caller_id.startswith("sub:"):
                        guidance = (
                            "子 Agent 只有以 kind=\"browser\" 派发时才会持有租约；"
                            "请把浏览器任务交给浏览器子 Agent，或改用非浏览器手段。")
                    else:
                        guidance = (
                            "不要取消正在执行的子任务来抢浏览器；先 poll_subagent 看进度，"
                            "或改做不依赖浏览器的步骤。只有连续两次验收失败或用户明确要求接管时，"
                            "才 cancel_subagent 后重试。")
                    return Message(
                        role="tool", name=name,
                        content=(f"[浏览器由子任务占用] 浏览器当前由 {owner or '未知'} 独占（单写者租约），"
                                 f"{name} 未执行。{guidance}")
                    )
                if (isinstance(caller_id, str) and caller_id.startswith("sub:")
                        and owner != caller_id):
                    return Message(
                        role="tool", name=name,
                        content=(f"[租约冲突] {name} 需要浏览器独占租约，而当前子任务没有持有"
                                 "（子 Agent 只有 kind=\"browser\" 派发时才会取得租约）。"
                                 "请把浏览器任务交给浏览器子 Agent，或改用非浏览器手段。")
                    )
        # M4 §5.9：资源锁
        resources = resource_ids_for(name, args) if self.locks else []
        acquired = []
        try:
            for r in resources:
                if not self.locks.acquire(r, task_id, timeout_sec=60):
                    return Message(
                        role="tool", name=name,
                        content=f"[资源冲突] {name} 需要的资源正被其他任务占用，跳过")
                acquired.append(r)
            try:
                result = tool.func(args)
                if isinstance(result, Message):
                    message = Message(role="tool", name=name,
                                      content=result.content, images=list(result.images), metadata=dict(result.metadata))
                else:
                    message = Message(role="tool", name=name, content=str(result))
            except Exception as e:  # noqa: BLE001 —— 工具异常必须兜住，不能崩主循环
                from tools.browser.worker import BrowserCallTimeout
                if isinstance(e, BrowserCallTimeout):
                    # 超时不是普通失败：内容本身已含恢复指引，原样回给模型
                    from vault.manager import Vault
                    logger.warning("工具超时 tool=%s args=%s: %s", name, args, e)
                    return Message(role="tool", name=name,
                                   content=Vault.scrub_text(str(e)))
                logger.error("工具执行异常 tool=%s args=%s", name, args, exc_info=True)
                stop = self._desktop_safety_stop(name, e)
                if stop is not None:
                    return stop
                from vault.manager import Vault
                return Message(role="tool", name=name,
                               content=Vault.scrub_text(f"[工具执行失败] {name}: {type(e).__name__}: {e}"))
            from vault.manager import Vault
            if message.content:
                message.content = Vault.scrub_text(message.content)
            # 子 Agent 交付后，给主 Agent 一次直接核对额度；接管（取消子任务）同样恢复额度。
            if getattr(self, "browser_delegation_required", False):
                caller_id = getattr(threading.current_thread(), "_lease_caller_id", None)
                content = message.content or ""
                if caller_id == "main":
                    grant = False
                    if name == "poll_subagent" and content.startswith("[子 Agent 执行完毕]"):
                        tid = str(args.get("task_id") or "")
                        with self._browser_direct_lock:
                            grant = bool(tid) and tid not in self._browser_verify_granted
                            if grant:
                                self._browser_verify_granted.add(tid)
                    elif name == "cancel_subagent" and content.startswith("[子 Agent 已取消]"):
                        grant = True
                    if grant:
                        with self._browser_direct_lock:
                            self._browser_direct_calls["main"] = 0
            # M4 §7.3：写操作后清读缓存，否则写入缓存 (ToolDefinition.cacheable 优先)
            if self.cache and terminal_tool_control(message) is None:
                if name in ("file_write", "file_edit"):
                    self.cache.invalidate_tool("file_read")
                elif (getattr(tool, "cacheable", False) or name in getattr(self.cache, "TTL", {})) and not message.images:
                    # 当前缓存只存文字；图片结果必须保留完整视觉证据。
                    ttl_sec = getattr(tool, "cache_ttl_sec", 0)
                    self.cache.put(name, args, message.content, ttl_sec=ttl_sec, prov_level=prov_level)
            return message
        finally:
            if self.locks:
                for r in acquired:
                    self.locks.release(r, task_id)

    @staticmethod
    def _risk_of(name: str, args: dict, prov_level: int) -> str:
        """Use the same operation classification as ApprovalManager."""
        base = classify_tool_risk(name, args)
        if prov_level == 2 and base == "L2":
            return "L3"
        return base
