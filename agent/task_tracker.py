"""§31.6 任务进度弹窗后端：TaskTracker。

每个任务维护步骤清单；状态变化 → 调 on_update(task_progress) 推 SSE。
M2 建表结构 + 逻辑；SSE 推送在 ui 层对接（M3）。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TaskStep:
    step_id: str
    name: str
    status: str = "pending"  # pending | running | done | failed | skipped
    started_at: float = 0.0
    ended_at: float = 0.0
    detail: str = ""


@dataclass
class TaskProgress:
    task_id: str
    name: str
    status: str = "running"  # running | paused_takeover | done | failed
    steps: list[TaskStep] = field(default_factory=list)

    @property
    def progress(self) -> float:
        done = sum(1 for s in self.steps if s.status in ("done", "skipped"))
        return done / len(self.steps) * 100 if self.steps else 0.0


class TaskTracker:
    def __init__(self, on_update=None):
        """on_update(progress: TaskProgress)：SSE 推送回调，UI 层注入。"""
        self.on_update = on_update
        self._tasks: dict[str, TaskProgress] = {}
        self._lock = threading.Lock()

    def new_task(self, name: str) -> str:
        tid = f"task-{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._tasks[tid] = TaskProgress(task_id=tid, name=name)
        return tid

    def _emit(self, tid: str):
        if self.on_update:
            with self._lock:
                p = self._tasks[tid]
            self.on_update(p)

    def step_start(self, task_id: str, name: str) -> str:
        sid = f"st-{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._tasks[task_id].steps.append(
                TaskStep(step_id=sid, name=name, status="running",
                         started_at=time.time()))
        self._emit(task_id)
        return sid

    def step_done(self, task_id: str, step_id: str):
        self._set(task_id, step_id, "done")

    def step_failed(self, task_id: str, step_id: str, reason: str):
        self._set(task_id, step_id, "failed", detail=reason)

    def _set(self, task_id: str, step_id: str, status: str, detail: str = ""):
        with self._lock:
            for s in self._tasks[task_id].steps:
                if s.step_id == step_id:
                    s.status = status
                    s.ended_at = time.time()
                    s.detail = detail
        self._emit(task_id)

    def finish(self, task_id: str, status: str):
        with self._lock:
            self._tasks[task_id].status = status
        self._emit(task_id)

    def get(self, task_id: str) -> TaskProgress | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_all(self) -> list[TaskProgress]:
        with self._lock:
            return list(self._tasks.values())
