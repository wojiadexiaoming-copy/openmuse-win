"""§10 定时任务：once / interval / cron。

APScheduler + SQLite 持久化（jobstore）。每个 Job 跑起来是一个独立 Agent run。
结果投递：routine 成功安静；需要用户注意才通知；出错必通知（§10.4）。
自然语言建任务（§10.3）由 LLM 把话转成 trigger 参数，本文件只收结构化参数。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class JobSpec:
    job_id: str
    kind: str                        # once | interval | cron
    brief: str                       # 任务书（Agent run 的输入）
    trigger_args: dict               # {"run_date": ...} | {"seconds": ...} | {"hour":.., "minute":..}
    coalesce: bool = True            # 错过任务合并（spec §10）


# 模块级注册表：APScheduler 持久化 jobstore 要求 job 函数可序列化，
# 不能用 bound method（会报 Schedulers cannot be serialized）。
_FIRE_REGISTRY: dict[str, "Scheduler"] = {}


def _fire_job(sched_id: str, job_id: str):
    sched = _FIRE_REGISTRY.get(sched_id)
    if sched is not None:
        sched._fire(job_id)


class Scheduler:
    _seq = 0

    def __init__(self, db_url: str, on_fire=None):
        """on_fire(job_id, brief)：触发回调（主 Agent 的 run 入口注入）。"""
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        self._sched = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=db_url)})
        self.on_fire = on_fire
        self._specs: dict[str, JobSpec] = {}
        Scheduler._seq += 1
        self._sched_id = f"sched-{Scheduler._seq}"
        _FIRE_REGISTRY[self._sched_id] = self

    def start(self):
        self._sched.start()

    def shutdown(self):
        self._sched.shutdown(wait=False)

    def add(self, spec: JobSpec):
        from apscheduler.triggers.date import DateTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        from apscheduler.triggers.cron import CronTrigger
        from datetime import datetime, timedelta
        if spec.kind == "once":
            args = dict(spec.trigger_args)
            # 支持 seconds/minutes 快捷写法 → 换算成 run_date
            if "seconds" in args or "minutes" in args:
                delta = timedelta(seconds=args.pop("seconds", 0),
                                  minutes=args.pop("minutes", 0))
                args["run_date"] = datetime.now() + delta
            trigger = DateTrigger(**args)
        elif spec.kind == "interval":
            trigger = IntervalTrigger(**spec.trigger_args)
        elif spec.kind == "cron":
            trigger = CronTrigger(**spec.trigger_args)
        else:
            raise ValueError(f"未知任务类型: {spec.kind}")
        self._specs[spec.job_id] = spec
        self._sched.add_job(_fire_job, trigger=trigger, id=spec.job_id,
                            args=(self._sched_id, spec.job_id),
                            coalesce=spec.coalesce,
                            replace_existing=True,
                            misfire_grace_time=3600)

    def _fire(self, job_id: str):
        spec = self._specs.get(job_id)
        if spec and self.on_fire:
            try:
                self.on_fire(job_id, spec.brief)
            except Exception as e:  # noqa: BLE001 —— Job 出错必通知（§10.4）
                self._notify(f"[定时任务出错] {job_id}: {e}", urgent=True)

    def _notify(self, text: str, urgent: bool = False):
        # [M3 完成] 接 Windows 通知（spec §31.4）/ UI 推送。
        from .notify import notify
        notify(title="AlterEgo 定时任务", message=text, urgent=urgent)
        print(text)

    def remove(self, job_id: str):
        self._sched.remove_job(job_id)
        self._specs.pop(job_id, None)

    def list(self) -> list[dict]:
        return [{"job_id": j.id, "next_run": str(j.next_run_time)}
                for j in self._sched.get_jobs()]

    def backoff_reschedule(self, job_id: str, delay_minutes: int = 30,
                           max_retries: int = 3) -> dict:
        """§10.1 退避重排：在原任务上改 next_run，而非新建"·第二次"垃圾任务。

        背景：撞墙后 Agent 习惯新建《补发·第二次》《补发·第三次》一次性任务，
        与正班 Cron 撞车，半小时内连捅平台两次，触发更严反爬。

        逻辑：
        1. 原任务 retry_count +1，next_run 推迟 delay_minutes
        2. 若 retry_count > max_retries：取消任务，返回需人工介入
        3. 若推迟后的 next_run 距下一个同平台正班 Cron 不足 45 分钟：
           取消本次补跑，将 payload 并入正班任务（避免连击）

        返回：{"action": "rescheduled"|"merged"|"cancelled", "detail": str}
        """
        from datetime import datetime, timedelta
        spec = self._specs.get(job_id)
        if not spec:
            return {"action": "cancelled", "detail": f"任务不存在: {job_id}"}

        retry = getattr(spec, "retry_count", 0) + 1
        spec.retry_count = retry

        if retry > max_retries:
            self.remove(job_id)
            return {"action": "cancelled",
                    "detail": f"重试 {max_retries} 次仍失败，已取消，需人工介入"}

        new_run = datetime.now() + timedelta(minutes=delay_minutes)

        # 检查是否与同平台正班任务撞车（45 分钟内）
        platform = getattr(spec, "platform", "")
        for j in self._sched.get_jobs():
            jspec = self._specs.get(j.id)
            if not jspec or j.id == job_id:
                continue
            if getattr(jspec, "platform", "") != platform:
                continue
            # 只看周期性正班任务
            if getattr(jspec, "kind", "") != "cron":
                continue
            nxt = j.next_run_time
            if nxt and abs((nxt.replace(tzinfo=None) - new_run).total_seconds()) < 45 * 60:
                # 合并：将 payload 并入正班任务，取消本次补跑
                if hasattr(jspec, "merged_payloads"):
                    jspec.merged_payloads.append(getattr(spec, "payload", {}))
                self.remove(job_id)
                return {"action": "merged",
                        "detail": f"与正班任务 {j.id} 不足 45 分钟，已合并"}

        # 正常推迟
        self._sched.reschedule_job(
            job_id, trigger="date", run_date=new_run)
        return {"action": "rescheduled",
                "detail": f"已推迟 {delay_minutes} 分钟（第 {retry}/{max_retries} 次重试）"}
