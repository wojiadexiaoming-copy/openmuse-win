"""v3.1 回归：一次性任务的任务书必须随 jobstore 持久化，重启后不能静默丢失。"""
from datetime import datetime, timedelta

from agent.scheduler import JobSpec, Scheduler


def test_brief_persists_in_job_args_across_restart(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'scheduler.db'}"
    fired = []
    first = Scheduler(db_url, on_fire=lambda jid, brief: fired.append((jid, brief)))
    first.start()
    try:
        first.add(JobSpec(
            job_id="once-test", kind="once", brief="提醒我喝水",
            trigger_args={"run_date": datetime.now() + timedelta(hours=1)}))
        sched_id = first._sched_id
    finally:
        first.shutdown()

    # 模拟进程重启：新实例的 _specs 为空，只能靠 jobstore 里持久化的 args
    second = Scheduler(db_url, on_fire=lambda jid, brief: fired.append((jid, brief)))
    second.start()
    try:
        job = second._sched.get_job("once-test")
        assert job is not None
        assert job.args == (sched_id, "once-test", "提醒我喝水")
        second._fire(job.id, job.args[2])
        assert fired == [("once-test", "提醒我喝水")]
    finally:
        second.shutdown()


def test_fire_without_brief_notifies_instead_of_silent_noop(tmp_path):
    sched = Scheduler(f"sqlite:///{tmp_path / 'jobs.db'}", on_fire=lambda *a: None)
    notes = []
    sched._notify = lambda text, urgent=False: notes.append((text, urgent))
    sched._fire("ghost", "")
    assert notes and "任务书丢失" in notes[0][0]
    assert notes[0][1] is True, "丢任务必须走紧急通知"
