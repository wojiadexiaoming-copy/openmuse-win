"""§4.3 工具注册表 + §7 执行入口。

M1：注册、schema 生成（给 LLM 的 tools 参数）、execute_tool 调度。
M4 才加：并行执行（§5.5）、结果缓存（§7.3）、资源锁（§5.9）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Message, terminal_tool_control
from .resource_lock import resource_ids_for
from .risk import classify_tool_risk


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


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self.approval = None  # ApprovalManager（M2）。main.py 里 set_approval 接入。
        self.on_ask = None    # on_ask(card)->(approved, scope)，UI/CLI 实现
        self.cache = None     # ToolResultCache（M4 §7.3）
        self.locks = None     # ResourceLock（M4 §5.9）
        self._desktop_tokens = __import__("contextvars").ContextVar("registry_desktop_tokens", default=())

    def begin_turn(self, scope_id):
        from tools.desktop_runtime import begin_turn
        self._desktop_tokens.set(self._desktop_tokens.get() + (begin_turn(scope_id),))

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

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError:
            raise ValueError(f"未知工具: {name}") from None

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
        # M4 §7.3：缓存命中直接返回
        if self.cache:
            hit = self.cache.get(name, args)
            if hit is not None:
                return Message(role="tool", name=name, content=hit + "\n[缓存命中]")
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
                stop = self._desktop_safety_stop(name, e)
                if stop is not None:
                    return stop
                return Message(role="tool", name=name,
                               content=f"[工具执行失败] {name}: {type(e).__name__}: {e}")
            # M4 §7.3：写操作后清读缓存，否则写入缓存
            if self.cache and terminal_tool_control(message) is None:
                if name in ("file_write", "file_edit"):
                    self.cache.invalidate_tool("file_read")
                elif not message.images:
                    # 当前缓存只存文字；图片结果必须保留完整视觉证据。
                    self.cache.put(name, args, message.content)
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
