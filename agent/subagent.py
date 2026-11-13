"""§4.4 子 Agent 管理器 + 结果验收。

- 子 Agent 不共享主 Agent 完整历史，只给任务书 + 最小记忆（防上下文爆炸）
- 子 Agent 不写 MEMORY.md（防并发写冲突），lesson 由主 Agent 提炼
- 结果验收：必须附证据清单；关键任务抽查；打回最多 2 次
- §P0-6：独立进程隔离 + 动态资源配额 + 最低保证(512MB) + 排队与熔断
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .models import Message

logger = logging.getLogger("alterego.subagent")


@dataclass
class SubagentTask:
    brief: str                       # 任务书：含 done 的标准（可验证）
    kind: str = "generic"            # generic | browser | code
    critical: bool = False           # 关键任务 → 主 Agent 抽查证据
    timeout_sec: int = 1800
    lease_timeout_sec: float = 300.0  # §5.9：浏览器租约排队上限 5 分钟
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
    status: str                      # done | partial | timeout | failed
    summary: str = ""
    evidence: list[dict] = field(default_factory=list)
    # evidence 项：{"type": "tool_record", "tool": ..., "args": ..., "result": ...}
    confidence: str = "medium"       # high | medium | low（子 Agent 自评）
    # 终态竞态审计：迟到结果覆盖了哪个已交付状态（用于复盘，不改变父级已看到的说明）
    late: bool = False
    superseded_status: str = ""


_PARTIAL_MARKERS = ("任务尚未完成", "[已超时]", "[部分交付]", "尚未完成：")


_EVIDENCE_FAILURE_MARKERS = ("[输入失败]", "[输入未落地]", "[点击失败]", "[ref 失效]",
                             "[超时]", "[等待超时]", "[无候选]", "[失败]", "[错误]",
                             "[通道锁定")


def _evidence_status(rec: dict) -> str:
    status = str(rec.get("status") or "")
    if status != "ok":
        return status or "error"
    content = str(rec.get("result") or "")
    if any(m in content for m in _EVIDENCE_FAILURE_MARKERS):
        return "suspicious"
    return "ok"


def _head_tail_pick(indices: list[int], limit: int) -> list[int]:
    if limit <= 0 or not indices:
        return []
    if len(indices) <= limit:
        return indices
    head = max(1, limit // 3)
    return indices[:head] + indices[-(limit - head):]


def _build_evidence(orch, limit: int = 12) -> list[dict]:
    """把工具记录整理成可回传的证据摘要（工具名 + 参数 + 结果原文片段）。

    只回传工具名等于没回传：主 Agent 看不到子 Agent 查到的数据，
    就无法综合，只能对用户说"没查到"。这里保留结果片段，让证据可引用。
    失败与非 ok 记录同样入证（此前只收 ok，失败根因全被丢弃）。
    """
    records = getattr(orch, "last_turn_tool_records", None) or []
    evidence: list[dict] = []
    seen: set[tuple[str, str]] = set()
    try:
        from .orchestrator import _safe_args_for_log
    except Exception:  # pragma: no cover - 防御循环导入
        _safe_args_for_log = lambda _name, args: args if isinstance(args, dict) else {}

    for rec in records:
        tool = str(rec.get("tool") or "")
        content = str(rec.get("result") or "").strip()
        if not tool:
            continue
        status = _evidence_status(rec)
        key = (tool, content[:200])
        if key in seen:
            continue
        seen.add(key)
        args = rec.get("args") or {}
        safe_args = _safe_args_for_log(tool, args)
        try:
            args_text = json.dumps(safe_args, ensure_ascii=False,
                                   sort_keys=True, default=str)[:240]
        except Exception:
            args_text = str(safe_args)[:240]
        ref = safe_args.get("path") or safe_args.get("url") or tool
        evidence.append({
            "type": "tool_record",
            "tool": tool,
            "status": status,
            "ref": str(ref)[:240],
            "args": args_text,
            "result": content[:300],
        })
    limit = max(1, limit)
    if len(evidence) <= limit:
        return evidence
    # 失败/可疑记录优先保留：失败任务的复盘价值就在"哪一步开始出错"，
    # 不能被大量成功探针挤出；剩余名额按"头部起步 + 尾部卡点"分配。
    fail_idx = [i for i, e in enumerate(evidence) if e.get("status") != "ok"]
    ok_idx = [i for i, e in enumerate(evidence) if e.get("status") == "ok"]
    if not fail_idx:
        return [evidence[i] for i in _head_tail_pick(list(range(len(evidence))), limit)]
    fail_budget = min(len(fail_idx), max(1, limit // 2))
    if len(fail_idx) <= fail_budget:
        chosen_fail = fail_idx
    elif fail_budget == 1:
        chosen_fail = fail_idx[-1:]
    else:
        chosen_fail = fail_idx[:1] + fail_idx[-(fail_budget - 1):]
    chosen = _head_tail_pick(ok_idx, limit - len(chosen_fail))
    return [evidence[i] for i in sorted(set(chosen_fail) | set(chosen))]


def build_subagent_result(task_id: str, orch, summary: str | Message) -> SubagentResult:
    """根据子 Agent 的真实收尾状态生成结果；未完成不得伪装成 done。"""
    # 取消路径历史上会回传 Message；统一取正文，避免 "Message is not iterable"
    text = summary.content if isinstance(summary, Message) else str(summary or "")
    partial = bool(getattr(orch, "_turn_exhausted", False))
    if getattr(orch, "last_turn_status", "") in ("incomplete", "cancelled", "waiting_user"):
        partial = True
    if any(marker in text for marker in _PARTIAL_MARKERS):
        partial = True
    evidence = _build_evidence(orch)
    return SubagentResult(
        task_id=task_id,
        status="partial" if partial else "done",
        summary=text,
        evidence=evidence,
        confidence="high" if evidence else "medium",
    )


def format_subagent_result(result: SubagentResult, max_evidence: int = 3,
                           header: str = "[子 Agent 执行完毕]",
                           acceptance: str = "") -> str:
    """poll/自动回传统一格式：状态 + 总结 + 置信度 + 关键证据原文。"""
    summary = " ".join((result.summary or "").split())
    if len(summary) > 1200:
        summary = summary[:1200] + "…"
    line = (f"{header} 状态: {result.status} | 总结: {summary} | "
            f"置信度: {result.confidence} | 证据数: {len(result.evidence)}")
    if acceptance:
        line += f" | {acceptance}"
    items = result.evidence[-max_evidence:] if max_evidence > 0 else []
    if max_evidence > 0:
        failed = [ev for ev in result.evidence if str(ev.get("status") or "ok") != "ok"]
        oks = [ev for ev in result.evidence if str(ev.get("status") or "ok") == "ok"]
        if failed:
            # 失败证据优先出镜；名额有余再补最新成功记录（起点/终点都有据可查）
            picked = failed[-max_evidence:]
            if len(picked) < max_evidence and oks:
                picked = picked + oks[-(max_evidence - len(picked)):]
            chosen = {id(ev) for ev in picked}
            items = [ev for ev in result.evidence if id(ev) in chosen]
        elif len(result.evidence) > max_evidence:
            # 尾部探针容易把早期登录/提交证据挤出；保留首条 + 尾 N-1 条，让主 Agent
            # 既能看到任务怎么起步，也能看到最后卡在哪。
            items = result.evidence[:1] + result.evidence[-(max_evidence - 1):]
    if items:
        lines = []
        for ev in items:
            tool = ev.get("tool") or ev.get("ref") or "?"
            args = " ".join(str(ev.get("args") or "").split())[:160]
            text = " ".join(str(ev.get("result") or "").split())[:240]
            lines.append(f"- {tool}({args}): {text}")
        line += "\n关键证据:\n" + "\n".join(lines)
    return line


_RESTART_SUMMARY = ("[进程重启] 子任务在上一进程退出时尚未完成，结果不可用；"
                    "请重新 spawn_subagent 派发。")
_TERMINAL_STATUSES = ("done", "partial", "timeout", "failed", "cancelled")


class SubagentStore:
    """子任务终态持久化：进程重启后仍能回答"结果是什么"，而不是假装还在执行。"""

    MAX_ROWS = 500

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = None
        self._closed = False
        try:
            import sqlite3
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS subagent_tasks (
                task_id TEXT PRIMARY KEY,
                brief TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'generic',
                status TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'medium',
                evidence TEXT NOT NULL DEFAULT '[]',
                pid INTEGER NOT NULL DEFAULT 0,
                stale INTEGER NOT NULL DEFAULT 0,
                late INTEGER NOT NULL DEFAULT 0,
                superseded_status TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL)""")
            # 旧库平滑迁移：迟到结果审计列
            columns = {row[1] for row in conn.execute("PRAGMA table_info(subagent_tasks)")}
            if "late" not in columns:
                conn.execute("ALTER TABLE subagent_tasks "
                             "ADD COLUMN late INTEGER NOT NULL DEFAULT 0")
            if "superseded_status" not in columns:
                conn.execute("ALTER TABLE subagent_tasks "
                             "ADD COLUMN superseded_status TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subagent_updated "
                         "ON subagent_tasks(updated_at)")
            conn.execute("""DELETE FROM subagent_tasks WHERE task_id NOT IN (
                SELECT task_id FROM subagent_tasks
                ORDER BY updated_at DESC LIMIT ?)""", (self.MAX_ROWS,))
            conn.commit()
            self._conn = conn
        except Exception as exc:
            logger.warning("SubagentStore 初始化失败(%s): %s", self.path, exc)
            self._conn = None

    @property
    def enabled(self) -> bool:
        return self._conn is not None and not self._closed

    def _run(self, sql, params=()):
        if not self.enabled:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.fetchall()
            except Exception as exc:
                logger.warning("SubagentStore SQL 失败: %s", exc)
                return []

    @staticmethod
    def _row_to_result(row) -> SubagentResult:
        task_id, status, summary, confidence, evidence = row
        try:
            evidence_list = json.loads(evidence or "[]")
        except Exception:
            evidence_list = []
        if not isinstance(evidence_list, list):
            evidence_list = []
        return SubagentResult(
            task_id=task_id, status=status, summary=summary or "",
            evidence=evidence_list, confidence=confidence or "medium")

    def create(self, task_id: str, brief: str, kind: str = "generic"):
        now = time.time()
        self._run(
            "INSERT INTO subagent_tasks "
            "(task_id,brief,kind,status,summary,confidence,evidence,pid,stale,late,"
            "superseded_status,created_at,updated_at) "
            "VALUES (?,?,?,'queued','','medium','[]',?,0,0,'',?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET brief=excluded.brief, kind=excluded.kind, "
            "status='queued', summary='', confidence='medium', evidence='[]', "
            "pid=excluded.pid, stale=0, late=0, superseded_status='', "
            "updated_at=excluded.updated_at",
            (task_id, str(brief or "")[:2000], kind or "generic",
             os.getpid(), now, now))

    def mark_running(self, task_id: str):
        self._run("UPDATE subagent_tasks SET status='running', pid=?, updated_at=? "
                  "WHERE task_id=? AND status IN ('queued','running')",
                  (os.getpid(), time.time(), task_id))

    def mark_stale(self, task_id: str):
        self._run("UPDATE subagent_tasks SET stale=1, updated_at=? WHERE task_id=?",
                  (time.time(), task_id))

    def save_result(self, result: SubagentResult):
        now = time.time()
        evidence = json.dumps(result.evidence or [], ensure_ascii=False, default=str)
        self._run(
            "INSERT INTO subagent_tasks "
            "(task_id,brief,kind,status,summary,confidence,evidence,pid,late,"
            "superseded_status,created_at,updated_at) "
            "VALUES (?,'','generic',?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, "
            "summary=excluded.summary, confidence=excluded.confidence, "
            "evidence=excluded.evidence, pid=excluded.pid, late=excluded.late, "
            "superseded_status=excluded.superseded_status, "
            "updated_at=excluded.updated_at",
            (result.task_id, result.status, result.summary or "",
             result.confidence or "medium", evidence, os.getpid(),
             1 if result.late else 0, result.superseded_status or "", now, now))

    def exists(self, task_id: str) -> bool:
        return bool(self._run(
            "SELECT 1 FROM subagent_tasks WHERE task_id=?", (task_id,)))

    def is_stale(self, task_id: str) -> bool:
        return bool(self._run(
            "SELECT 1 FROM subagent_tasks WHERE task_id=? AND stale=1", (task_id,)))

    def recover(self):
        """返回 (全部结果, 上一进程遗留未完成的 task_id 列表)。

        未完成的任务没有真实结果，只能明确标记失败；已完成的照常恢复，
        这样重启后主 Agent 还能拿到昨天的查询结果。
        """
        rows = self._run(
            "SELECT task_id,status,summary,confidence,evidence,stale FROM subagent_tasks "
            "ORDER BY updated_at ASC")
        results: list[SubagentResult] = []
        interrupted: list[str] = []
        for row in rows:
            res = self._row_to_result(row[:5])
            stale = bool(row[5]) if len(row) > 5 else False
            if res.status not in _TERMINAL_STATUSES:
                res = SubagentResult(
                    task_id=res.task_id, status="failed",
                    summary=_RESTART_SUMMARY, confidence="low")
                self.save_result(res)
                self.mark_stale(res.task_id)
                interrupted.append(res.task_id)
            elif stale:
                interrupted.append(res.task_id)
            results.append(res)
        return results, interrupted

    def close(self):
        self._closed = True
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


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
                  "\n\n完成标准见任务书。结束时用一句话总结+证据清单；"
                  "若任务只差最后一个动作即可完成，必须在总结末尾单列"
                  "【下一步可执行动作】：工具名+关键参数（ref/0~1000 坐标/选择器）+"
                  "前置核验结论（如 probe 命中了什么元素），让接手方不用重新摸索。")
        summary = orch.handle(prompt)
        res = build_subagent_result(task_id, orch, summary)
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
    # P0-2：连续 N 秒没有新的成功操作 → 判定卡死并回收，不让父级空等
    DEFAULT_STALL_TIMEOUT_SEC = 420.0
    STALL_SCAN_INTERVAL_SEC = 5.0
    # 收尾阶段（结论已产出，只剩落库/事件/返回）不产生工具记录，
    # 必须用独立阈值；卡住时优先把已产出结论按部分交付回传。
    DEFAULT_CLOSEOUT_STALL_SEC = 180.0
    _CLOSEOUT_PHASES = {"post_turn", "verify", "close_out", "checkpoint",
                        "tracker_finish", "emit_state", "span_exit"}
    _STATUS_RANK = {"failed": 0, "timeout": 1, "partial": 2, "done": 3}
    # 等待类/纯观察类不算"实质进展"；合法长等待不按卡死处理
    _STALL_IGNORE_TOOLS = {"wait_subagent", "poll_subagent", "desktop_wait", "state_check"}
    _STALL_EXEMPT_TOOLS = {"browser_request_takeover", "shell_exec", "desktop_wait"}

    def __init__(self, make_orchestrator, max_memory_mb: int | None = None,
                 on_queue_update=None, max_concurrent: int | None = None,
                 enforce_min_memory: bool = True, store_path=None):
        """make_orchestrator(task) -> Orchestrator：由 main.py 注入工厂。

        store_path 非空时把任务终态写入 SQLite，并在启动时恢复上一进程的结果；
        上一进程遗留的 queued/running 任务明确标为失败，绝不冒充"执行中"。
        """
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
        # 收尾阶段已产出的候选结论：终态被外部清退时优先按 partial 交付
        self._provisional: dict[str, dict] = {}
        self._store = SubagentStore(store_path) if store_path else None
        self._interrupted_ids: set[str] = set()
        if self._store:
            restored, interrupted = self._store.recover()
            for res in restored:
                self._results[res.task_id] = res
            self._interrupted_ids = set(interrupted)
            logger.info("[SubAgent] 已恢复 %d 条历史结果（其中 %d 条因重启中断）",
                        len(restored), len(interrupted))
        # 看门狗：task_id -> {"thread": Thread|None, "process": Process|None, "queue": Queue|None, "deadline": float, "task": SubagentTask, "orch": Orchestrator|None, "max_memory_mb": int, "slot_released": bool}
        self._watchdog: dict[str, dict] = {}
        self._stop_event = threading.Event()
        try:
            env_stall = float(os.environ.get("ALTEREGO_SUBAGENT_STALL_SEC", ""))
        except (TypeError, ValueError):
            env_stall = 0.0
        self.stall_timeout_sec = (env_stall if env_stall > 0
                                  else self.DEFAULT_STALL_TIMEOUT_SEC)
        try:
            env_closeout = float(os.environ.get("ALTEREGO_SUBAGENT_CLOSEOUT_SEC", ""))
        except (TypeError, ValueError):
            env_closeout = 0.0
        self.closeout_stall_sec = (env_closeout if env_closeout > 0
                                   else self.DEFAULT_CLOSEOUT_STALL_SEC)

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

    def _remember_result(self, task_id: str, result: SubagentResult):
        """写内存 + 落盘；内存是快路径，SQLite 负责跨进程恢复。

        终态竞态：外部清退可能先写入 partial/timeout，子线程随后才返回真实终态。
        迟到结果覆盖时保留审计字段；done 是粘性的，绝不被迟到的失败降级。
        """
        with self._lock:
            previous = self._results.get(task_id)
            if previous is not None and previous is not result:
                if (previous.status == "done"
                        or self._STATUS_RANK.get(result.status, 0)
                        < self._STATUS_RANK.get(previous.status, 0)):
                    logger.warning("[SubAgent] %s 迟到结果 %s 不覆盖已交付终态 %s",
                                   task_id, result.status, previous.status)
                    result = previous
                else:
                    result.late = True
                    result.superseded_status = previous.status
            self._results[task_id] = result
            getattr(self, "_provisional", {}).pop(task_id, None)
        if self._store:
            self._store.save_result(result)

    def _stash_provisional(self, task_id: str, content: str, status: str = "running"):
        """回合结论已产出但收尾未完成：先暂存，清退时不用 failed 掩盖真实结论。"""
        text = (content or "").strip()
        if len(text) < 40:
            return
        with self._lock:
            if self._results.get(task_id) is not None:
                return
            provisional = getattr(self, "_provisional", None)
            if provisional is None:
                provisional = self._provisional = {}
            provisional[task_id] = {"content": text, "at": time.time(),
                                    "status": status}
            info = self._watchdog.get(task_id)
            if info is not None:
                info["reply_ready_at"] = time.time()
                info["provisional_chars"] = len(text)

    def _provisional_result(self, task_id: str, reason: str,
                            info: dict | None = None) -> SubagentResult | None:
        with self._lock:
            prov = getattr(self, "_provisional", {}).get(task_id)
        if not prov:
            return None
        text = (prov.get("content") or "").strip()
        if len(text) < 40:
            return None
        evidence = []
        orch = (info or {}).get("orch") or getattr((info or {}).get("thread"), "_orch", None)
        if orch is not None:
            try:
                evidence = _build_evidence(orch)
            except Exception:
                evidence = []
        note = (f"[交付说明] 子任务在收尾阶段被终止（{reason}）。以上为已产出结论，"
                "未经最终核验；引用时请注明为部分交付。")
        return SubagentResult(
            task_id=task_id, status="partial",
            summary=f"{text}\n\n{note}", evidence=evidence, confidence="medium")

    def knows(self, task_id: str) -> bool:
        """task_id 是否属于本管理器（含排队/运行/已完成/历史持久化记录）。"""
        if not task_id:
            return False
        with self._lock:
            if task_id in self._results or task_id in self._watchdog:
                return True
        with self._queue_lock:
            if any(item[0] == task_id for item in self._queue):
                return True
        return bool(self._store and self._store.exists(task_id))

    def is_stale(self, task_id: str) -> bool:
        """上一进程遗留、已被标为中断（结果不可复用）的 task_id。"""
        if task_id in self._interrupted_ids:
            return True
        return bool(self._store and self._store.is_stale(task_id))

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
        last_stall_scan = 0.0
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

            if now - last_stall_scan >= self.STALL_SCAN_INTERVAL_SEC:
                last_stall_scan = now
                self._scan_stalled(now)

    def _task_records(self, info: dict) -> list[dict]:
        orch = info.get("orch") or getattr(info.get("thread"), "_orch", None)
        if orch is None:
            return []
        try:
            return list(getattr(orch, "last_turn_tool_records", None) or [])
        except Exception:
            return []

    def _progress_digest(self, info: dict) -> tuple:
        """成功且非等待类的工具记录指纹；变化即视为子任务有实质推进。"""
        digest = []
        for rec in self._task_records(info):
            tool = str(rec.get("tool") or "")
            if not tool or str(rec.get("status") or "") != "ok":
                continue
            if tool in self._STALL_IGNORE_TOOLS:
                continue
            digest.append((tool, hash(str(rec.get("result") or "")[:300])))
        return tuple(digest)

    def _scan_stalled(self, now: float):
        """无新成功操作的子任务：超过阈值即强制回收，父级可据此部分交付。

        收尾阶段（结论已产出、只剩落库/事件/返回）不产生工具记录，
        按独立的 closeout_stall_sec 判定，并转储全线程堆栈供复盘。
        """
        stalled = []
        closeout_stalled = []
        closeout_limit = getattr(self, "closeout_stall_sec",
                                 self.DEFAULT_CLOSEOUT_STALL_SEC)
        with self._lock:
            for tid, info in list(self._watchdog.items()):
                if self._results.get(tid) is not None:
                    continue
                orch = info.get("orch") or getattr(info.get("thread"), "_orch", None)
                # 进程模式看不到内存中的工具记录，不能用"无进展"误杀
                if orch is None:
                    continue
                phase = str(getattr(orch, "_turn_phase", "") or "")
                phase_ts = float(getattr(orch, "_turn_phase_ts", 0.0) or 0.0)
                info["closeout_phase"] = phase
                if phase in self._CLOSEOUT_PHASES and phase_ts > 0:
                    closeout_for = now - phase_ts
                    info["closeout_sec"] = closeout_for
                    if closeout_for >= closeout_limit:
                        closeout_stalled.append((tid, info, closeout_for, phase))
                    continue
                info["closeout_sec"] = 0.0
                digest = self._progress_digest(info)
                if digest != info.get("progress_digest"):
                    info["progress_digest"] = digest
                    info["last_progress_ts"] = now
                last = float(info.get("last_progress_ts") or info.get("started_ts") or now)
                idle = now - last
                info["idle_sec"] = idle
                current = str(getattr(orch, "_current_tool", "") or "")
                info["current_tool"] = current
                current_set = {name.strip() for name in current.split(",") if name.strip()}
                # 没有在跑工具时，子任务多在等 LLM 推理（httpx read 上限 600s），
                # 不能用工具卡死阈值误杀，放宽到双倍。
                limit = (self.stall_timeout_sec if current_set
                         else self.stall_timeout_sec * 2)
                if idle >= limit and not (current_set & self._STALL_EXEMPT_TOOLS):
                    stalled.append((tid, info, idle, current))
        for tid, info, idle, phase in closeout_stalled:
            task = info.get("task")
            brief_preview = (getattr(task, "brief", "") or "")[:60]
            dump_path = self._dump_thread_stacks(tid, phase)
            logger.error("[watchdog] 子任务 %s 收尾阶段 %s 已卡死 %.0f 秒，"
                         "判定收尾阻塞并回收（任务书: %s...，堆栈: %s）",
                         tid, phase or "未知", idle, brief_preview,
                         dump_path or "转储失败")
            self._kill_ghost(tid, info, reason="closeout_stuck",
                             idle_sec=idle, phase=phase)
        for tid, info, idle, current in stalled:
            task = info.get("task")
            brief_preview = (getattr(task, "brief", "") or "")[:60]
            logger.warning("[watchdog] 子任务 %s 已 %.0f 秒无新成功操作（当前工具: %s），"
                           "判定卡死并回收（任务书: %s...）",
                           tid, idle, current or "无", brief_preview)
            self._kill_ghost(tid, info, reason="stuck", idle_sec=idle)

    def _dump_thread_stacks(self, task_id: str, phase: str) -> str:
        """收尾卡死时转储全线程栈：下一次能直接看到阻塞在哪个调用。"""
        import datetime
        import traceback
        try:
            from .paths import resolve_sub_dir
            log_dir = Path(resolve_sub_dir("logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            target = log_dir / f"closeout_stall_{task_id}_{int(time.time())}.log"
            names = {t.ident: t.name for t in threading.enumerate()}
            lines = [
                f"# closeout stall task={task_id} phase={phase} "
                f"at {datetime.datetime.now().isoformat(timespec='seconds')}",
            ]
            for ident, frame in sys._current_frames().items():
                lines.append(f"\n--- thread {names.get(ident, ident)} ---")
                lines.extend(line.rstrip("\n") for line in traceback.format_stack(frame))
            target.write_text("\n".join(lines), encoding="utf-8")
            return str(target)
        except Exception as exc:
            logger.warning("[watchdog] 收尾堆栈转储失败 task=%s: %s", task_id, exc)
            return ""

    def _kill_ghost(self, task_id: str, info: dict, reason: str = "timeout",
                    idle_sec: float = 0.0, phase: str = ""):
        """强制回收幽灵任务：强杀独立子进程，避免 OOM 拖垮主进程。"""
        self._release_task_slot(task_id)
        self._release_browser_lease(task_id, info)
        task = info.get("task")
        brief_preview = task.brief[:50] if task else ""
        if reason == "oom":
            logger.warning(f"[watchdog] 子任务 {task_id} 发生 OOM 超额，强制终止（任务书: {brief_preview}...)")
        elif reason == "closeout_stuck":
            logger.error(f"[watchdog] 子任务 {task_id} 收尾阶段 {phase or '未知'} "
                         f"卡死 {int(idle_sec)}s，强制回收（任务书: {brief_preview}...)")
        elif reason == "stuck":
            logger.warning(f"[watchdog] 子任务 {task_id} 无进展 {int(idle_sec)}s，强制回收（任务书: {brief_preview}...)")
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

        provisional = None
        if reason in ("timeout", "stuck", "closeout_stuck"):
            if reason == "closeout_stuck":
                why = (f"收尾阶段 {phase or '未知'} 卡死 {int(idle_sec)} 秒，"
                       "系统强制回收")
            elif reason == "stuck":
                why = (f"连续 {int(idle_sec)} 秒没有新的成功操作"
                       f"（卡在 {info.get('current_tool') or '未知工具'}），判定卡死并强制回收")
            else:
                why = f"硬超时 {self.HARD_TIMEOUT_SEC}s，看门狗强制回收"
            provisional = self._provisional_result(task_id, why, info)

        result = None
        with self._lock:
            if self._results.get(task_id) is None:
                if provisional is not None:
                    result = provisional
                elif reason == "oom":
                    max_mem = info.get("max_memory_mb", self.default_max_memory_mb)
                    summary = f"SubAgent OOM terminated: 内存超出配额 ({max_mem}MB)，系统强制终止"
                    result = SubagentResult(
                        task_id=task_id, status="failed", summary=summary,
                        confidence="low")
                elif reason == "closeout_stuck":
                    summary = (f"收尾阶段（{phase or '未知'}）卡死 {int(idle_sec)} 秒，"
                               "没有可交付的结论，系统强制回收。")
                    result = SubagentResult(
                        task_id=task_id, status="timeout", summary=summary,
                        confidence="low")
                elif reason == "stuck":
                    tool = str(info.get("current_tool") or "未知工具")
                    summary = (f"子任务连续 {int(idle_sec)} 秒没有新的成功操作（卡在 {tool}），"
                               "判定卡死并强制回收。中间结果见工具记录，未核验的部分不得当作已完成。")
                    result = SubagentResult(
                        task_id=task_id, status="timeout", summary=summary,
                        confidence="low")
                else:
                    summary = f"硬超时 {self.HARD_TIMEOUT_SEC}s，看门狗强制回收"
                    result = SubagentResult(
                        task_id=task_id, status="timeout", summary=summary,
                        confidence="low")
                self._results[task_id] = result
                getattr(self, "_provisional", {}).pop(task_id, None)
            self._watchdog.pop(task_id, None)
        if result is not None and self._store:
            self._store.save_result(result)

    def _release_browser_lease(self, task_id: str, info: dict):
        """取消/回收子任务时立刻交还浏览器租约，不等子线程 finally。"""
        try:
            orch = info.get("orch") or getattr(info.get("thread"), "_orch", None)
            tools = getattr(orch, "tools", None) if orch is not None else None
            worker = (getattr(tools, "browser_worker", None)
                      or getattr(tools, "_browser_worker", None))
            if worker is not None and hasattr(worker, "release_lease"):
                worker.release_lease(f"sub:{task_id}")
        except Exception as exc:
            logger.debug("[SubAgent] 释放浏览器租约失败 task=%s: %s", task_id, exc)

    def cancel(self, task_id: str, reason: str = ""):
        """主动终止指定的子任务。若是独立子进程，强杀子进程。

        reason 用于区分"用户主动停止"与"回合超时系统清退"，终态说明必须诚实。
        返回写入内存的终态（便于调用方直接使用）；任务已有终态时返回 None。
        """
        in_queue = False
        with self._queue_lock:
            for i, item in enumerate(list(self._queue)):
                if item[0] == task_id:
                    self._queue.pop(i)
                    in_queue = True
                    break
        if in_queue:
            self._emit_queue_event()

        result = None
        with self._lock:
            info = self._watchdog.get(task_id)
        if info is not None:
            # 收尾阶段可能已产出结论：优先交付 partial，不能用"失败/零证据"掩盖
            result = self._provisional_result(
                task_id, reason or "主动取消", info)
        with self._lock:
            if task_id in self._results and self._results.get(task_id) is None:
                if result is None:
                    summary = "已主动取消" + (f"（{reason}）" if reason else "")
                    result = SubagentResult(
                        task_id=task_id, status="failed",
                        summary=summary, confidence="low")
                self._results[task_id] = result
                getattr(self, "_provisional", {}).pop(task_id, None)
            else:
                result = None
        if result is not None and self._store:
            self._store.save_result(result)

        if info:
            # 先释放槽位（依赖 watchdog 条目），再注销；否则 release 变成 no-op
            self._release_task_slot(task_id)
            self._release_browser_lease(task_id, info)
            with self._lock:
                self._watchdog.pop(task_id, None)
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
        return result

    def cancel_all(self, reason: str = ""):
        """终止所有进行中的子任务及排队任务。"""
        with self._queue_lock:
            queued_ids = [item[0] for item in self._queue]
            self._queue.clear()
        self._emit_queue_event()

        with self._lock:
            running_ids = list(self._watchdog.keys())
        for tid in set(queued_ids + running_ids):
            self.cancel(tid, reason=reason)

    def _result_collector_thread(self, task_id: str, proc, q):
        """异步收集独立子进程返回的结果。"""
        result = None
        try:
            res = q.get(timeout=self.HARD_TIMEOUT_SEC)
            with self._lock:
                pending = self._results.get(task_id) is None
            if pending:
                self._remember_result(task_id, res)
                result = res
        except Exception:
            exitcode = getattr(proc, "exitcode", None)
            if not proc.is_alive() and exitcode != 0:
                with self._lock:
                    pending = self._results.get(task_id) is None
                if pending:
                    result = SubagentResult(
                        task_id=task_id,
                        status="failed",
                        summary=f"子进程异常退出 (exitcode={exitcode})",
                        confidence="low"
                    )
                    self._remember_result(task_id, result)
        finally:
            # 释放槽位必须在 watchdog 注销之前，否则 release 找不到条目直接 no-op
            self._release_task_slot(task_id)
            with self._lock:
                self._watchdog.pop(task_id, None)

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
        if self._store:
            self._store.create(task_id, task.brief, task.kind)

        with self._queue_lock:
            self._queue.append((task_id, task, effective_mem))
            self._dispatcher_cv.notify()
        self._emit_queue_event()

        return task_id

    def _launch_task(self, task_id: str, task: SubagentTask, max_mem: int):
        effective_timeout = min(task.timeout_sec, self.HARD_TIMEOUT_SEC)
        can_process = getattr(task, "use_process", True) and _is_picklable(self._make)
        started_ts = time.time()

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
                    "started_ts": started_ts,
                    "last_progress_ts": started_ts,
                    "progress_digest": (),
                    "idle_sec": 0.0,
                    "current_tool": "",
                }
            p.start()
            if self._store:
                self._store.mark_running(task_id)
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
                    "started_ts": started_ts,
                    "last_progress_ts": started_ts,
                    "progress_digest": (),
                    "idle_sec": 0.0,
                    "current_tool": "",
                }
            t.start()
            if self._store:
                self._store.mark_running(task_id)

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
            try:
                orch.on_turn_reply_ready = (
                    lambda content, status: self._stash_provisional(
                        task_id, content, status))
            except Exception as exc:
                logger.debug("[SubAgent] 安装结论暂存回调失败 task=%s: %s", task_id, exc)
            threading.current_thread()._orch = orch
            with self._lock:
                if task_id in self._watchdog:
                    self._watchdog[task_id]["orch"] = orch
                    setattr(self._watchdog[task_id]["thread"], "_orch", orch)

            # 浏览器独占仲裁：如果是浏览器任务，尝试申请租约
            if task.kind == "browser" and hasattr(orch, "tools"):
                worker = getattr(orch.tools, "browser_worker", getattr(orch.tools, "_browser_worker", None))
                if worker and hasattr(worker, "acquire_lease"):
                    lease_acquired = worker.acquire_lease(f"sub:{task_id}", timeout_sec=getattr(task, "lease_timeout_sec", 300.0))
                    if not lease_acquired:
                        current = worker.current_lease_owner() if hasattr(worker, "current_lease_owner") else "unknown"
                        waited = float(getattr(task, "lease_timeout_sec", 300.0) or 0)
                        result = SubagentResult(
                            task_id=task_id, status="failed",
                            summary=(f"无法获取浏览器独占权（已排队等待 {waited:.0f} 秒，当前被 {current} 占用）。"
                                     "浏览器同一时刻只允许一个写者：请先 poll 现有任务或等其结束，"
                                     "不要并发派发第二个浏览器任务。"),
                            confidence="low")
                        self._release_task_slot(task_id)
                        self._remember_result(task_id, result)
                        with self._lock:
                            self._watchdog.pop(task_id, None)
                        return

            # 子 Agent 上下文 = 任务书 + 记忆片段（不共享主历史）
            prompt = (f"[子任务]\n{task.brief}\n\n"
                      f"[相关记忆]\n" + "\n".join(task.memory_snippets) +
                      "\n\n完成标准见任务书。结束时用一句话总结+证据清单；"
                      "若任务只差最后一个动作即可完成，必须在总结末尾单列"
                      "【下一步可执行动作】：工具名+关键参数（ref/0~1000 坐标/选择器）+"
                      "前置核验结论（如 probe 命中了什么元素），让接手方不用重新摸索。")
            summary = orch.handle(prompt)
            result = build_subagent_result(task_id, orch, summary)
        except Exception as e:  # noqa: BLE001
            provisional = self._provisional_result(
                task_id, f"执行出错（{type(e).__name__}: {e}）")
            result = provisional or SubagentResult(
                task_id=task_id, status="failed",
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
        self._remember_result(task_id, result)
        # 正常完成：从看门狗注销
        with self._lock:
            self._watchdog.pop(task_id, None)

    def poll(self, task_id: str) -> SubagentResult | None:
        """None=还在跑或排队中；未知 id 立即返回 failed，不再冒充执行中。"""
        if not task_id:
            return SubagentResult(
                task_id="", status="failed",
                summary="[任务不存在或已随进程重启失效] 缺少 task_id。",
                confidence="low")
        with self._lock:
            res = self._results.get(task_id)
            info = self._watchdog.get(task_id)
            queue = info.get("queue") if info else None
        if res is not None:
            return res
        if queue is not None:
            try:
                result = queue.get_nowait()
            except Exception:
                result = None
            if result is not None:
                self._remember_result(task_id, result)
                # 先释放槽位再注销；poll 是完成态，不能把并发额度漏掉
                self._release_task_slot(task_id)
                with self._lock:
                    self._watchdog.pop(task_id, None)
                return result
        with self._lock:
            res = self._results.get(task_id)
            if res is not None:
                return res
            known = task_id in self._results or task_id in self._watchdog
        if not known:
            with self._queue_lock:
                known = any(item[0] == task_id for item in self._queue)
        if not known and self._store and self._store.exists(task_id):
            known = True
        if not known:
            return SubagentResult(
                task_id=task_id, status="failed",
                summary=("[任务不存在或已随进程重启失效] 该 task_id 不属于当前进程，"
                         "结果不可用。需要结果请重新 spawn_subagent 派发。"),
                confidence="low")
        return None

    def wait(self, task_id: str, timeout_sec: float = 60.0) -> SubagentResult | None:
        """阻塞等待结果；None=超时仍未完成。替代主 Agent 的 poll+desktop_wait 轮询。"""
        try:
            timeout = max(0.0, min(float(timeout_sec or 0), 180.0))
        except (TypeError, ValueError):
            timeout = 60.0
        deadline = time.time() + timeout
        while True:
            res = self.poll(task_id)
            if res is not None:
                return res
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    def list_running(self) -> list[str]:
        with self._lock:
            return [tid for tid, r in self._results.items() if r is None]

    def progress_snapshot(self) -> list[dict]:
        """运行中子任务的实时进度快照（主 Agent 判断墙钟续期/用户提示用）。"""
        with self._lock:
            snapshot = []
            for tid, info in self._watchdog.items():
                if self._results.get(tid) is not None:
                    continue
                task = info.get("task")
                orch = info.get("orch")
                records: list[dict] = []
                step = 0
                if orch is not None:
                    try:
                        records = list(getattr(orch, "last_turn_tool_records", []) or [])
                        step = int(getattr(orch, "_turn_step", 0) or 0)
                    except Exception:
                        records, step = [], 0
                snapshot.append({
                    "task_id": tid,
                    "brief": (getattr(task, "brief", "") or "")[:120],
                    "kind": getattr(task, "kind", "") if task else "",
                    "step": step,
                    "records": records[-40:],
                    "deadline": info.get("deadline"),
                    "current_tool": str(getattr(orch, "_current_tool", "") or "") if orch else "",
                    "idle_sec": float(info.get("idle_sec") or 0.0),
                    "started_ts": float(info.get("started_ts") or 0.0),
                    "reply_ready_at": float(info.get("reply_ready_at") or 0.0),
                    "closeout_phase": str(info.get("closeout_phase") or ""),
                    "closeout_sec": float(info.get("closeout_sec") or 0.0),
                })
            return snapshot

    def shutdown(self):
        """关闭管理器并清理后台线程。"""
        self._stop_event.set()
        with self._queue_lock:
            self._dispatcher_cv.notify_all()
        self.cancel_all()
        if self._store:
            self._store.close()

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
    if result.status == "partial":
        return False, "子任务未完成，仅返回部分结果；主 Agent 应基于证据整合或补派，不得当作已查到"
    if result.status != "done":
        return False, f"子任务状态={result.status}，主 Agent 决定重试或放弃"
    if any(marker in (result.summary or "") for marker in _PARTIAL_MARKERS):
        return False, "子任务总结仍标记未完成，不能判为通过"
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
            return (f"[子 Agent 已派发] task_id={tid}，本轮继续其它步骤；"
                    "需要结果时用 wait_subagent 阻塞等待，不要用 desktop_wait 空转。")
        except ValueError as e:
            return f"[工具执行失败] {e}"

    def _poll(args: dict) -> str:
        tid = args.get("task_id", "")
        res = manager.poll(tid)
        if res is None:
            return f"[子 Agent 执行中] task_id={tid} 尚未完成，稍后再查"
        return format_subagent_result(res)

    def _wait(args: dict) -> str:
        tid = args.get("task_id", "")
        if not tid:
            return "[工具执行失败] 缺少 task_id"
        timeout = args.get("timeout_sec", 60)
        res = manager.wait(tid, timeout)
        if res is None:
            return (f"[子 Agent 仍在执行] task_id={tid} 等待 {int(timeout)} 秒仍未完成。"
                    "继续 wait_subagent 或基于已有信息收尾，不要改用 desktop_wait 空转。")
        return format_subagent_result(res)

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
                    "kind": {"type": "string", "enum": ["generic", "browser", "code"], "description": "子任务类型；browser=浏览器任务（自动取得浏览器独占租约，最长排队 300 秒）；code 使用代码专用模型角色"},
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
            name="wait_subagent",
            description=("阻塞等待子 Agent 完成并返回结果+证据（默认 60 秒，最长 180 秒）。"
                         "派发子任务后优先用本工具等待；不要用 desktop_wait 轮询空转。"),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "子任务 task_id"},
                    "timeout_sec": {"type": "integer",
                                    "description": "最长等待秒数，默认 60，最大 180"},
                },
                "required": ["task_id"],
            },
            func=_wait,
            parallel_safe=False,
            is_readonly=False,
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
