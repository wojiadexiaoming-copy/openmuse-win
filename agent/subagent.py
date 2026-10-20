"""§4.4 子 Agent 管理器 + 结果验收。

- 子 Agent 不共享主 Agent 完整历史，只给任务书 + 最小记忆（防上下文爆炸）
- 子 Agent 不写 MEMORY.md（防并发写冲突），lesson 由主 Agent 提炼
- 结果验收：必须附证据清单；关键任务抽查；打回最多 2 次
- §P0-6：独立进程隔离 + 动态资源配额 + 最低保证(512MB) + 排队与熔断
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger("alterego.subagent")


@dataclass
class SubagentTask:
    brief: str                       # 任务书：含 done 的标准（可验证）
    kind: str = "generic"            # generic | browser | code
    critical: bool = False           # 关键任务 → 主 Agent 抽查证据
    timeout_sec: int = 1800
    lease_timeout_sec: float = 5.0
    memory_snippets: list[str] = field(default_factory=list)
    depth: int = 0                   # 嵌套深度，默认 0；上限 2，防递归死锁
    max_memory_mb: int = 0           # §P0-6 内存配额 (0=动态自动分配，显式指定则硬下限 512MB)
    use_process: bool = True         # §P0-6 优先使用独立进程隔离
    enforce_min_memory: bool = True  # 是否强制最低 512MB 保证（单测可置 False）

    def __post_init__(self):
        if len(self.brief) > 2000:
            self.brief = self.brief[:2000]
        if self.enforce_min_memory and self.max_memory_mb != 0 and self.max_memory_mb < 512:
            raise ValueError(f"子 Agent 内存配额不能低于 512MB (请求: {self.max_memory_mb}MB)")


@dataclass
class ResourceQuota:
    max_memory_bytes: int
    max_fds: int = 1024
    max_cpu_seconds: int = 300
    max_procs: int = 8
    max_concurrent: int = 2


def compute_dynamic_resource_quota(override_mb: int | None = None, enforce_min_memory: bool = True) -> ResourceQuota:
    """计算子 Agent 动态资源配额（基于本机内存与最低 512MB 保证）。"""
    MIN_QUOTA_BYTES = 512 * 1024 * 1024  # 512 MB 硬下限

    try:
        import psutil
        total_mem = psutil.virtual_memory().total
        avail_mem = psutil.virtual_memory().available
    except Exception:
        total_mem = 16 * 1024 * 1024 * 1024
        avail_mem = 8 * 1024 * 1024 * 1024

    if override_mb is not None and override_mb > 0:
        single_quota = override_mb * 1024 * 1024
    else:
        # 1. 单进程上限：总内存 15% 或 4GB，取较小
        single_max = min(total_mem * 0.15, 4 * 1024**3)
        # 2. 单进程下限：512 MB
        single_min = MIN_QUOTA_BYTES
        # 3. 实际单进程配额 = clamp(下限, 上限, 可用内存 30%)
        single_quota = max(single_min, min(single_max, avail_mem * 0.3))

    if enforce_min_memory and single_quota < MIN_QUOTA_BYTES:
        raise ValueError(f"子 Agent 内存配额低于最低保证 512MB ({round(single_quota / 1024 / 1024)}MB)")

    # 4. 最大并发槽位 = 可用内存 / 单进程配额，范围 [1, 4]
    max_concurrent = max(1, min(int(avail_mem / max(1, single_quota)), 4))

    return ResourceQuota(
        max_memory_bytes=int(single_quota),
        max_fds=1024,
        max_cpu_seconds=300,
        max_procs=8,
        max_concurrent=max_concurrent,
    )


@dataclass
class SubagentResult:
    task_id: str
    status: str                      # done | timeout | failed
    summary: str = ""
    evidence: list[dict] = field(default_factory=list)
    # evidence 项：{"type": "file"|"screenshot"|"quote", "ref": "..."}
    confidence: str = "medium"       # high | medium | low（子 Agent 自评）


def _is_picklable(obj) -> bool:
    """探测工厂对象是否能在当前平台上被跨进程序列化。"""
    try:
        import pickle
        pickle.dumps(obj)
        return True
    except Exception:
        return False


def _get_process_memory_bytes(pid: int) -> int:
    """跨平台获取指定进程当前的内存占用（字节数）。"""
    if not pid or pid <= 0:
        return 0
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            handle = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
            if not handle:
                return 0
            try:
                counters = _PROCESS_MEMORY_COUNTERS()
                counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
                if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    return int(counters.PagefileUsage or counters.WorkingSetSize)
                return 0
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return 0
    else:
        try:
            with open(f"/proc/{pid}/statm", "r") as f:
                fields = f.read().split()
                import resource
                return int(fields[1]) * resource.getpagesize()
        except Exception:
            return 0


def _subagent_process_entry(task_id: str, task: SubagentTask, make_fn, result_queue, max_memory_mb: int):
    """独立子进程入口函数。支持 Linux setrlimit 硬件限制与优雅异常捕获。"""
    if sys.platform != "win32":
        try:
            import resource
            bytes_limit = max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
            resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
            if hasattr(resource, "RLIMIT_NPROC"):
                resource.setrlimit(resource.RLIMIT_NPROC, (8, 8))
            if hasattr(resource, "RLIMIT_CORE"):
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if hasattr(resource, "RLIMIT_DATA"):
                resource.setrlimit(resource.RLIMIT_DATA, (bytes_limit, bytes_limit))
            if hasattr(resource, "RLIMIT_RSS"):
                resource.setrlimit(resource.RLIMIT_RSS, (bytes_limit, bytes_limit))
        except Exception as exc:
            logger.warning("设置 Linux rlimit 异常: %s", exc)

    res = None
    try:
        orch = make_fn(task)
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
        res = SubagentResult(
            task_id=task_id, status="done",
            summary=summary, evidence=evidence,
            confidence="high" if evidence else "medium"
        )
    except MemoryError:
        res = SubagentResult(
            task_id=task_id, status="failed",
            summary=f"SubAgent OOM terminated: 内存超出配额 ({max_memory_mb}MB)",
            confidence="low"
        )
    except Exception as e:
        res = SubagentResult(
            task_id=task_id, status="failed",
            summary=f"{type(e).__name__}: {e}",
            confidence="low"
        )
    finally:
        try:
            result_queue.put(res)
        except Exception:
            pass


class SubagentManager:
    # §4.4 铁律：任何浏览器/桌面子任务硬超时上限 20 分钟。
    HARD_TIMEOUT_SEC = 20 * 60
    DEFAULT_MAX_MEMORY_MB = 2048
    WATCHDOG_INTERVAL_SEC = 1.0

    def __init__(self, make_orchestrator, max_memory_mb: int | None = None,
                 on_queue_update=None, max_concurrent: int | None = None,
                 enforce_min_memory: bool = True):
        """make_orchestrator(task) -> Orchestrator：由 main.py 注入工厂。"""
        self._make = make_orchestrator
        self.on_queue_update = on_queue_update
        self.enforce_min_memory = enforce_min_memory
        self.quota = compute_dynamic_resource_quota(
            override_mb=max_memory_mb if (max_memory_mb and max_memory_mb != self.DEFAULT_MAX_MEMORY_MB) else None,
            enforce_min_memory=enforce_min_memory,
        )
        if max_concurrent is not None:
            self.quota.max_concurrent = max(1, max_concurrent)
        self.default_max_memory_mb = int(self.quota.max_memory_bytes / 1024 / 1024)

        self._semaphore = threading.Semaphore(self.quota.max_concurrent)
        self._queue: list[tuple[str, SubagentTask, int]] = []
        self._queue_lock = threading.Lock()
        self._dispatcher_cv = threading.Condition(self._queue_lock)

        self._results: dict[str, SubagentResult | None] = {}
        self._lock = threading.Lock()
        # 看门狗：task_id -> {"thread": Thread|None, "process": Process|None, "queue": Queue|None, "deadline": float, "task": SubagentTask, "orch": Orchestrator|None, "max_memory_mb": int, "slot_released": bool}
        self._watchdog: dict[str, dict] = {}
        self._stop_event = threading.Event()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

        self._dispatcher_thread = threading.Thread(
            target=self._dispatcher_loop, daemon=True)
        self._dispatcher_thread.start()

    def _emit_queue_event(self):
        with self._queue_lock:
            qsize = len(self._queue)
        if self.on_queue_update:
            try:
                self.on_queue_update(qsize)
            except Exception as e:
                logger.debug("on_queue_update error: %s", e)

    @property
    def queue_waiting_count(self) -> int:
        with self._queue_lock:
            return len(self._queue)

    def _release_task_slot(self, task_id: str):
        with self._lock:
            info = self._watchdog.get(task_id)
            if info:
                if not info.get("slot_released", False):
                    info["slot_released"] = True
                    self._semaphore.release()
                    with self._queue_lock:
                        self._dispatcher_cv.notify()

    def _dispatcher_loop(self):
        """按最大并发槽位和可用内存排队分发子任务。"""
        while not self._stop_event.is_set():
            with self._queue_lock:
                while not self._queue and not self._stop_event.is_set():
                    self._dispatcher_cv.wait(timeout=0.5)
                if self._stop_event.is_set():
                    break
                if not self._queue:
                    continue
                item = self._queue[0]

            if not self._semaphore.acquire(timeout=0.5):
                continue

            try:
                import psutil
                avail = psutil.virtual_memory().available
                needed_bytes = self.quota.max_memory_bytes * 1.2
                if self.enforce_min_memory and avail < needed_bytes:
                    self._semaphore.release()
                    time.sleep(2.0)
                    continue
            except Exception:
                pass

            with self._queue_lock:
                if self._queue and self._queue[0][0] == item[0]:
                    self._queue.pop(0)
            self._emit_queue_event()

            task_id, task, max_mem = item
            with self._lock:
                if self._results.get(task_id) is not None:
                    self._semaphore.release()
                    continue

            self._launch_task(task_id, task, max_mem)

    def _watchdog_loop(self):
        """看门狗：定期扫描超时的幽灵任务与超出内存配额 (OOM) 的子进程。"""
        while not self._stop_event.is_set():
            time.sleep(self.WATCHDOG_INTERVAL_SEC)
            now = time.time()
            expired = []
            oom_killed = []
            with self._lock:
                for tid, info in list(self._watchdog.items()):
                    if self._results.get(tid) is not None:
                        continue
                    # 1. 监测子进程内存配额
                    proc = info.get("process")
                    if proc and proc.is_alive():
                        max_mb = info.get("max_memory_mb", self.default_max_memory_mb)
                        mem_bytes = _get_process_memory_bytes(proc.pid)
                        if mem_bytes > max_mb * 1024 * 1024:
                            oom_killed.append((tid, info, mem_bytes))
                            continue
                    # 2. 监测超时
                    if now > info.get("deadline", float("inf")):
                        expired.append((tid, info))

            for tid, info, mem_bytes in oom_killed:
                logger.error("[SubAgent] SubAgent OOM terminated: task_id=%s 内存使用达到 %d MB (配额上限 %d MB)",
                             tid, round(mem_bytes / 1024 / 1024), info.get("max_memory_mb", 2048))
                self._kill_ghost(tid, info, reason="oom")

            for tid, info in expired:
                self._kill_ghost(tid, info, reason="timeout")

    def _kill_ghost(self, task_id: str, info: dict, reason: str = "timeout"):
        """强制回收幽灵任务：强杀独立子进程，避免 OOM 拖垮主进程。"""
        self._release_task_slot(task_id)
        task = info.get("task")
        brief_preview = task.brief[:50] if task else ""
        if reason == "oom":
            logger.warning(f"[watchdog] 子任务 {task_id} 发生 OOM 超额，强制终止（任务书: {brief_preview}...)")
        else:
            logger.warning(f"[watchdog] 子任务 {task_id} 超过硬超时，强制回收（任务书: {brief_preview}...)")

        # 1. 如果有独立子进程，执行强杀 (terminate -> kill)
        proc = info.get("process")
        if proc and proc.is_alive():
            try:
                proc.terminate()
                proc.join(timeout=0.5)
                if proc.is_alive():
                    proc.kill()
            except Exception as e:
                logger.warning(f"[watchdog] 强杀子进程异常: {e}")

        # 2. 线程协程式取消通知
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
                if reason == "oom":
                    max_mem = info.get("max_memory_mb", self.default_max_memory_mb)
                    summary = f"SubAgent OOM terminated: 内存超出配额 ({max_mem}MB)，系统强制终止"
                else:
                    summary = f"硬超时 {self.HARD_TIMEOUT_SEC}s，看门狗强制回收"
                self._results[task_id] = SubagentResult(
                    task_id=task_id, status="failed" if reason == "oom" else "timeout",
                    summary=summary,
                    confidence="low")
            self._watchdog.pop(task_id, None)

    def cancel(self, task_id: str):
        """主动终止指定的子任务。若是独立子进程，强杀子进程。"""
        in_queue = False
        with self._queue_lock:
            for i, item in enumerate(list(self._queue)):
                if item[0] == task_id:
                    self._queue.pop(i)
                    in_queue = True
                    break
        if in_queue:
            self._emit_queue_event()

        with self._lock:
            info = self._watchdog.pop(task_id, None)
            if self._results.get(task_id) is None:
                self._results[task_id] = SubagentResult(
                    task_id=task_id, status="failed",
                    summary="已主动取消", confidence="low")

        if info:
            self._release_task_slot(task_id)
            proc = info.get("process")
            if proc and proc.is_alive():
                try:
                    proc.terminate()
                    proc.join(timeout=0.5)
                    if proc.is_alive():
                        proc.kill()
                except Exception:
                    pass
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
        """终止所有进行中的子任务及排队任务。"""
        with self._queue_lock:
            queued_ids = [item[0] for item in self._queue]
            self._queue.clear()
        self._emit_queue_event()

        with self._lock:
            running_ids = list(self._watchdog.keys())
        for tid in set(queued_ids + running_ids):
            self.cancel(tid)

    def _result_collector_thread(self, task_id: str, proc, q):
        """异步收集独立子进程返回的结果。"""
        try:
            res = q.get(timeout=self.HARD_TIMEOUT_SEC)
            with self._lock:
                if self._results.get(task_id) is None:
                    self._results[task_id] = res
                    self._watchdog.pop(task_id, None)
        except Exception:
            with self._lock:
                if self._results.get(task_id) is None and (not proc.is_alive()):
                    exitcode = getattr(proc, "exitcode", None)
                    if exitcode != 0:
                        self._results[task_id] = SubagentResult(
                            task_id=task_id,
                            status="failed",
                            summary=f"子进程异常退出 (exitcode={exitcode})",
                            confidence="low"
                        )
                        self._watchdog.pop(task_id, None)
        finally:
            self._release_task_slot(task_id)

    def spawn(self, task: SubagentTask) -> str:
        """派发子任务。若当前无空闲并发槽位，则进入排队队列等待派发。"""
        if task.depth >= 2:
            raise ValueError(f"子任务嵌套深度达到上限 ({task.depth} >= 2)，禁止继续派生子 Agent")

        task_mem = getattr(task, "max_memory_mb", 0)
        if getattr(task, "enforce_min_memory", True) and self.enforce_min_memory and task_mem != 0 and task_mem < 512:
            raise ValueError(f"子 Agent 内存配额不能低于 512MB (请求: {task_mem}MB)")

        effective_mem = task_mem if task_mem else self.default_max_memory_mb

        task_id = f"sub-{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._results[task_id] = None

        with self._queue_lock:
            self._queue.append((task_id, task, effective_mem))
            self._dispatcher_cv.notify()
        self._emit_queue_event()

        return task_id

    def _launch_task(self, task_id: str, task: SubagentTask, max_mem: int):
        effective_timeout = min(task.timeout_sec, self.HARD_TIMEOUT_SEC)
        can_process = getattr(task, "use_process", True) and _is_picklable(self._make)

        # SEC-06: 构造纯数据 Task 实例，剥离并清洗任何潜在的敏感密文或对象引用
        from vault.manager import Vault
        safe_task = SubagentTask(
            brief=Vault.scrub_text(task.brief),
            kind=task.kind,
            critical=task.critical,
            timeout_sec=task.timeout_sec,
            lease_timeout_sec=task.lease_timeout_sec,
            memory_snippets=[Vault.scrub_text(s) for s in (task.memory_snippets or [])],
            depth=task.depth,
            max_memory_mb=task.max_memory_mb,
            use_process=task.use_process,
            enforce_min_memory=task.enforce_min_memory,
        )

        if can_process:
            import multiprocessing as mp
            q = mp.Queue()
            p = mp.Process(
                target=_subagent_process_entry,
                args=(task_id, safe_task, self._make, q, max_mem),
                daemon=True,
            )
            with self._lock:
                self._watchdog[task_id] = {
                    "thread": None,
                    "process": p,
                    "queue": q,
                    "deadline": time.time() + effective_timeout,
                    "task": task,
                    "orch": None,
                    "max_memory_mb": max_mem,
                    "slot_released": False,
                }
            p.start()
            collector = threading.Thread(
                target=self._result_collector_thread,
                args=(task_id, p, q),
                daemon=True,
            )
            collector.start()
        else:
            t = threading.Thread(target=self._run, args=(task_id, task), daemon=True)
            with self._lock:
                self._watchdog[task_id] = {
                    "thread": t,
                    "process": None,
                    "queue": None,
                    "deadline": time.time() + effective_timeout,
                    "task": task,
                    "orch": None,
                    "max_memory_mb": max_mem,
                    "slot_released": False,
                }
            t.start()

    def _run(self, task_id: str, task: SubagentTask):
        orch = None
        worker = None
        lease_acquired = False
        from agent.task_state import TaskState, set_current_task_state
        sub_task_state = TaskState()
        state_token = set_current_task_state(sub_task_state)
        threading.current_thread()._lease_caller_id = f"sub:{task_id}"
        try:
            orch = self._make(task)
            threading.current_thread()._orch = orch
            with self._lock:
                if task_id in self._watchdog:
                    self._watchdog[task_id]["orch"] = orch
                    setattr(self._watchdog[task_id]["thread"], "_orch", orch)

            # 浏览器独占仲裁：如果是浏览器任务，尝试申请租约
            if task.kind == "browser" and hasattr(orch, "tools"):
                worker = getattr(orch.tools, "browser_worker", getattr(orch.tools, "_browser_worker", None))
                if worker and hasattr(worker, "acquire_lease"):
                    lease_acquired = worker.acquire_lease(f"sub:{task_id}", timeout_sec=getattr(task, "lease_timeout_sec", 5.0))
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
            self._release_task_slot(task_id)
            set_current_task_state(None)
            if hasattr(threading.current_thread(), "_lease_caller_id"):
                delattr(threading.current_thread(), "_lease_caller_id")
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
        """None=还在跑或排队中。主循环每轮顺手 poll 一次。"""
        with self._lock:
            res = self._results.get(task_id)
            if res is not None:
                return res
            info = self._watchdog.get(task_id)
            if info and info.get("queue"):
                q = info["queue"]
                try:
                    if not q.empty():
                        result = q.get_nowait()
                        self._results[task_id] = result
                        self._watchdog.pop(task_id, None)
                        return result
                except Exception:
                    pass
            return self._results.get(task_id)

    def list_running(self) -> list[str]:
        with self._lock:
            return [tid for tid, r in self._results.items() if r is None]

    def shutdown(self):
        """关闭管理器并清理后台线程。"""
        self._stop_event.set()
        with self._queue_lock:
            self._dispatcher_cv.notify_all()
        self.cancel_all()

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
                    enforce_min_memory=task.enforce_min_memory,
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
            parallel_safe=False,
            is_readonly=False,
            cacheable=False,
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
            parallel_safe=True,
            is_readonly=True,
            cacheable=False,
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
            parallel_safe=False,
            is_readonly=False,
            cacheable=False,
        ),
    ]
