"""§4.4 子 Agent 管理器 + 结果验收。

- 子 Agent 不共享主 Agent 完整历史，只给任务书 + 最小记忆（防上下文爆炸）
- 子 Agent 不写 MEMORY.md（防并发写冲突），lesson 由主 Agent 提炼
- 结果验收：必须附证据清单；关键任务抽查；打回最多 2 次
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class SubagentTask:
    brief: str                       # 任务书：含 done 的标准（可验证）
    kind: str = "generic"            # generic | browser | code
    critical: bool = False           # 关键任务 → 主 Agent 抽查证据
    timeout_sec: int = 1800
    memory_snippets: list[str] = field(default_factory=list)
    depth: int = 0                   # 嵌套深度，默认 0；上限 2，防递归死锁

    def __post_init__(self):
        if len(self.brief) > 2000:
            self.brief = self.brief[:2000]



@dataclass
class SubagentResult:
    task_id: str
    status: str                      # done | timeout | failed
    summary: str = ""
    evidence: list[dict] = field(default_factory=list)
    # evidence 项：{"type": "file"|"screenshot"|"quote", "ref": "..."}
    confidence: str = "medium"       # high | medium | low（子 Agent 自评）


class SubagentManager:
    # §4.4 铁律：任何浏览器/桌面子任务硬超时上限 20 分钟。
    # 超时后看门狗强制杀掉线程并回收资源，防止幽灵任务吃光内存。
    HARD_TIMEOUT_SEC = 20 * 60

    def __init__(self, make_orchestrator):
        """make_orchestrator(task) -> Orchestrator：由 main.py 注入工厂。"""
        self._make = make_orchestrator
        self._results: dict[str, SubagentResult | None] = {}
        self._lock = threading.Lock()
        # 看门狗：task_id -> {"thread": Thread, "deadline": float, "task": SubagentTask, "orch": Orchestrator|None}
        self._watchdog: dict[str, dict] = {}
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _watchdog_loop(self):
        """看门狗：每 30 秒扫描一次，杀掉超时的幽灵任务。"""
        import time
        while True:
            time.sleep(30)
            now = time.time()
            expired = []
            with self._lock:
                for tid, info in list(self._watchdog.items()):
                    if now > info["deadline"] and self._results.get(tid) is None:
                        expired.append((tid, info))
            for tid, info in expired:
                self._kill_ghost(tid, info)

    def _kill_ghost(self, task_id: str, info: dict):
        """强制回收幽灵任务：标记超时，尝试清理浏览器资源。"""
        import logging
        log = logging.getLogger("alterego.subagent")
        task = info["task"]
        log.warning(
            f"[watchdog] 子任务 {task_id} 超过硬超时 "
            f"{self.HARD_TIMEOUT_SEC}s，强制回收（任务书: {task.brief[:50]}...)")
        # 尝试调用 cancel 与 cleanup
        try:
            orch = info.get("orch") or getattr(info.get("thread"), "_orch", None)
            if orch:
                if hasattr(orch, "cancel"):
                    orch.cancel()
                if hasattr(orch, "cleanup"):
                    orch.cleanup()
        except Exception:
            pass
        with self._lock:
            if self._results.get(task_id) is None:
                self._results[task_id] = SubagentResult(
                    task_id=task_id, status="timeout",
                    summary=f"硬超时 {self.HARD_TIMEOUT_SEC}s，看门狗强制回收",
                    confidence="low")
            self._watchdog.pop(task_id, None)

    def cancel(self, task_id: str):
        """主动终止指定的子任务。"""
        with self._lock:
            info = self._watchdog.pop(task_id, None)
            if self._results.get(task_id) is None:
                self._results[task_id] = SubagentResult(
                    task_id=task_id, status="failed",
                    summary="已主动取消", confidence="low")
        if info:
            orch = info.get("orch") or getattr(info.get("thread"), "_orch", None)
            if orch:
                if hasattr(orch, "cancel"):
                    try:
                        orch.cancel()
                    except Exception:
                        pass
                if hasattr(orch, "cleanup"):
                    try:
                        orch.cleanup()
                    except Exception:
                        pass

    def cancel_all(self):
        """终止所有进行中的子任务。"""
        with self._lock:
            tids = list(self._watchdog.keys())
        for tid in tids:
            self.cancel(tid)

    def spawn(self, task: SubagentTask) -> str:
        if task.depth >= 2:
            raise ValueError(f"子任务嵌套深度达到上限 ({task.depth} >= 2)，禁止继续派生子 Agent")
        task_id = f"sub-{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._results[task_id] = None
        t = threading.Thread(target=self._run, args=(task_id, task), daemon=True)
        # 注册看门狗：deadline = min(任务自设 timeout, 硬上限)
        effective_timeout = min(task.timeout_sec, self.HARD_TIMEOUT_SEC)
        with self._lock:
            self._watchdog[task_id] = {
                "thread": t,
                "deadline": __import__("time").time() + effective_timeout,
                "task": task,
                "orch": None,
            }
        t.start()
        return task_id

    def _run(self, task_id: str, task: SubagentTask):
        orch = None
        worker = None
        lease_acquired = False
        try:
            orch = self._make(task)
            threading.current_thread()._orch = orch
            with self._lock:
                if task_id in self._watchdog:
                    self._watchdog[task_id]["orch"] = orch
                    setattr(self._watchdog[task_id]["thread"], "_orch", orch)

            # 浏览器独占仲裁：如果是浏览器任务，尝试申请租约
            if task.kind == "browser" and hasattr(orch, "tools"):
                worker = getattr(orch.tools, "_browser_worker", None)
                if worker and hasattr(worker, "acquire_lease"):
                    lease_acquired = worker.acquire_lease(f"sub:{task_id}", timeout_sec=5.0)
                    if not lease_acquired:
                        current = worker.current_lease_owner() if hasattr(worker, "current_lease_owner") else "unknown"
                        result = SubagentResult(
                            task_id=task_id, status="failed",
                            summary=f"无法获取浏览器独占权（当前被 {current} 占用）",
                            confidence="low")
                        with self._lock:
                            self._results[task_id] = result
                            self._watchdog.pop(task_id, None)
                        return

            # 子 Agent 上下文 = 任务书 + 记忆片段（不共享主历史）
            prompt = (f"[子任务]\n{task.brief}\n\n"
                      f"[相关记忆]\n" + "\n".join(task.memory_snippets) +
                      "\n\n完成标准见任务书。结束时用一句话总结+证据清单。")
            summary = orch.handle(prompt)
            evidence = []
            if hasattr(orch, "last_turn_tool_records"):
                for rec in orch.last_turn_tool_records:
                    if rec.get("status") == "ok":
                        ref = (rec.get("args") or {}).get("path") or rec.get("tool", "")
                        evidence.append({"type": "tool_record", "ref": str(ref)})
            result = SubagentResult(task_id=task_id, status="done",
                                   summary=summary, evidence=evidence,
                                   confidence="high" if evidence else "medium")
        except Exception as e:  # noqa: BLE001
            result = SubagentResult(task_id=task_id, status="failed",
                                   summary=f"{type(e).__name__}: {e}")
        finally:
            if lease_acquired and worker and hasattr(worker, "release_lease"):
                try:
                    worker.release_lease(f"sub:{task_id}")
                except Exception:
                    pass
        with self._lock:
            self._results[task_id] = result
            # 正常完成：从看门狗注销
            self._watchdog.pop(task_id, None)

    def poll(self, task_id: str) -> SubagentResult | None:
        """None=还在跑。主循环每轮顺手 poll 一次。"""
        with self._lock:
            return self._results.get(task_id)

    def list_running(self) -> list[str]:
        with self._lock:
            return [tid for tid, r in self._results.items() if r is None]

    def run_with_retry(
        self,
        task: SubagentTask,
        max_retries: int = 2,
        spot_check=None,
        timeout_sec: float = 30.0,
    ) -> tuple[SubagentResult, bool]:
        """运行子 Agent 并验收，打回重做最多 max_retries 次，第 3 次打回主 Agent 接手（spec §4.4）。

        返回 (最终结果, 主Agent是否需要接手)。
        """
        attempts = 0
        current_task = task
        last_res = SubagentResult(task_id="", status="failed", summary="未启动")
        while attempts <= max_retries:
            tid = self.spawn(current_task)
            start_t = time.time()
            res = None
            while time.time() - start_t < timeout_sec:
                res = self.poll(tid)
                if res is not None:
                    break
                time.sleep(0.05)
            if res is None:
                res = SubagentResult(task_id=tid, status="timeout", summary="子任务执行超时")

            last_res = res
            ok, reason = accept_result(res, current_task, spot_check=spot_check)
            if ok:
                return res, False

            attempts += 1
            if attempts <= max_retries:
                current_task = SubagentTask(
                    brief=f"{task.brief}\n[前次打回原因: {reason}，请根据要求补充修正]",
                    kind=task.kind,
                    critical=task.critical,
                    timeout_sec=task.timeout_sec,
                    memory_snippets=task.memory_snippets,
                )

        return last_res, True


def accept_result(result: SubagentResult, task: SubagentTask,
                  spot_check=None) -> tuple[bool, str]:
    """主 Agent 验收。返回 (通过, 原因)。"""
    if result.status != "done":
        return False, f"子任务状态={result.status}，主 Agent 决定重试或放弃"
    if not result.evidence:
        return False, "无证据清单，打回"
    if task.critical and result.confidence != "high":
        if spot_check is None:
            return False, "关键任务且自评非 high，无抽查函数，打回"
        ev = result.evidence[0]
        if not spot_check(ev):
            return False, f"抽查证据失败：{ev.get('ref')}"
    return True, "通过"


def make_subagent_tools(manager: SubagentManager):
    """构建 spawn_subagent、poll_subagent 和 cancel_subagent 工具定义供主 Agent 调用。"""
    from agent.tools_registry import ToolDefinition

    def _spawn(args: dict) -> str:
        depth = int(args.get("depth", 0))
        if depth >= 2:
            return f"[工具执行失败] 子任务嵌套深度达到上限 ({depth} >= 2)，禁止继续派生子 Agent"
        task = SubagentTask(
            brief=args.get("brief", ""),
            kind=args.get("kind", "generic"),
            critical=bool(args.get("critical", False)),
            memory_snippets=args.get("memory_snippets") or [],
            depth=depth,
        )
        try:
            tid = manager.spawn(task)
            return f"[子 Agent 已派发] task_id={tid}，本轮继续其它步骤，用 poll 查结果。"
        except ValueError as e:
            return f"[工具执行失败] {e}"

    def _poll(args: dict) -> str:
        tid = args.get("task_id", "")
        res = manager.poll(tid)
        if res is None:
            return f"[子 Agent 执行中] task_id={tid} 尚未完成，稍后再查"
        return f"[子 Agent 执行完毕] 状态: {res.status} | 总结: {res.summary} | 置信度: {res.confidence} | 证据数: {len(res.evidence)}"

    def _cancel(args: dict) -> str:
        tid = args.get("task_id", "")
        if not tid:
            return "[工具执行失败] 缺少 task_id"
        manager.cancel(tid)
        return f"[子 Agent 已取消] task_id={tid}"

    return [
        ToolDefinition(
            name="spawn_subagent",
            description="派发子 Agent 异步执行子任务（输入 brief 契约，输出总结+证据）。",
            parameters={
                "type": "object",
                "properties": {
                    "brief": {"type": "string", "description": "任务书：明确做什么及验收标准（done 标准）"},
                    "kind": {"type": "string", "enum": ["generic", "browser", "code"], "description": "子任务类型；code 使用代码专用模型角色"},
                    "critical": {"type": "boolean", "description": "是否为关键任务（抽查证据）"},
                    "memory_snippets": {"type": "array", "items": {"type": "string"}, "description": "关联记忆片段"},
                    "depth": {"type": "integer", "default": 0, "description": "嵌套深度，默认 0，上限 2"},
                },
                "required": ["brief"],
            },
            func=_spawn,
        ),
        ToolDefinition(
            name="poll_subagent",
            description="查询已派发子 Agent 的执行状态与产出结果。",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "子任务 task_id"},
                },
                "required": ["task_id"],
            },
            func=_poll,
        ),
        ToolDefinition(
            name="cancel_subagent",
            description="主动终止正在运行的子 Agent 任务并释放其占用的资源。",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "子任务 task_id"},
                },
                "required": ["task_id"],
            },
            func=_cancel,
        ),
    ]

