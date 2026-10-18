"""§16 后台 Job 架构 [必选]

设计原则：
1. Job = 主 Agent 的"夜班分身"：同一套 Orchestrator 代码，新实例，无对话历史
2. 通过文件交接：Job 读文件写文件，不直接碰主 Agent 内存
3. 失败隔离：单个 Job 崩了只记日志，不影响主循环和其他 Job
4. 幂等：Job 可重跑，用 last_run 水位线防重复
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("jobs")


class JobScheduler:
    """后台 Job 调度器（基于 APScheduler，带失败隔离+水位线）。"""

    def __init__(self, config: dict | None = None,
                 state_path: str | None = None):
        from apscheduler.schedulers.background import BackgroundScheduler
        self.config = config or {}
        tz = self.config.get("jobs", {}).get("timezone", "Asia/Shanghai")
        self.sched = BackgroundScheduler(timezone=tz)
        if state_path is None:
            state_path = os.path.join(os.path.expanduser("~"),
                                       ".alterego", "jobs", "state.json")
        self.state_path = state_path
        os.makedirs(os.path.dirname(state_path), exist_ok=True)

    # ---- 水位线（幂等） ----
    def get_watermark(self, job_id: str) -> float:
        """读上次运行时间戳，无则返回 0。"""
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f).get(job_id, 0)
        except Exception:
            return 0

    def set_watermark(self, job_id: str, ts: float | None = None):
        """写水位线。"""
        ts = ts or time.time()
        try:
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                state = {}
            state[job_id] = ts
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.warning(f"[jobs] 水位线写入失败 {job_id}: {e}")

    # ---- 失败隔离 ----
    def _safe_run(self, job_id: str, fn):
        """包装：捕获所有异常，只记日志不抛（§16.1 失败隔离）。"""
        def wrapper(*args, **kwargs):
            try:
                log.info(f"[jobs] Job 开始：{job_id}")
                fn(*args, **kwargs)
                self.set_watermark(job_id)
                log.info(f"[jobs] Job 完成：{job_id}")
            except Exception as e:
                log.error(f"[jobs] Job 失败：{job_id}，{e}", exc_info=True)
                # 不抛，隔离失败
        wrapper.__name__ = f"safe_{job_id}"
        return wrapper

    def add_job(self, job_id: str, fn, trigger: str = "interval",
                max_instances: int = 1, coalesce: bool = True, **trigger_args):
        """注册 Job（自动包 _safe_run）。"""
        self.sched.add_job(
            self._safe_run(job_id, fn),
            trigger,
            id=job_id,
            max_instances=max_instances,  # 防重叠
            coalesce=coalesce,            # 错过的只补跑一次
            **trigger_args)

    def start(self):
        self.sched.start()
        log.info("[jobs] JobScheduler 启动")

    def shutdown(self, wait: bool = False):
        self.sched.shutdown(wait=wait)
