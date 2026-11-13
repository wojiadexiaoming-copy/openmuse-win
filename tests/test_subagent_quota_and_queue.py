import time
import pytest
from agent.subagent import (
    SubagentManager,
    SubagentTask,
    compute_dynamic_resource_quota,
    ResourceQuota,
)


class SimpleSleepOrch:
    def __init__(self, task):
        self.task = task

    def handle(self, prompt):
        time.sleep(0.3)
        return "completed"


def test_dynamic_quota_calculation():
    quota = compute_dynamic_resource_quota()
    assert isinstance(quota, ResourceQuota)
    # 必须满足硬下限 512MB
    assert quota.max_memory_bytes >= 512 * 1024 * 1024
    # 并发槽位在 [1, 4] 之间
    assert 1 <= quota.max_concurrent <= 4


def test_quota_min_memory_rejection():
    # 低于 512MB 必须抛出 ValueError
    with pytest.raises(ValueError, match="低于最低保证 512MB"):
        compute_dynamic_resource_quota(override_mb=256, enforce_min_memory=True)

    with pytest.raises(ValueError, match="不能低于 512MB"):
        SubagentTask(brief="too small mem", max_memory_mb=256, enforce_min_memory=True)


def test_subagent_queue_and_concurrency_slots():
    events = []

    def on_queue(qsize):
        events.append(qsize)

    # 限制最大并发槽位为 2
    mgr = SubagentManager(
        make_orchestrator=SimpleSleepOrch,
        on_queue_update=on_queue,
        max_concurrent=2,
    )

    tids = []
    # 提交 4 个任务
    for i in range(4):
        t = SubagentTask(brief=f"task-{i}", use_process=False)
        tids.append(mgr.spawn(t))

    # 检查是否有排队事件
    assert any(q > 0 for q in events)

    # 等待全部跑完
    start = time.time()
    while time.time() - start < 10:
        if all(mgr.poll(tid) is not None for tid in tids):
            break
        time.sleep(0.1)

    for tid in tids:
        res = mgr.poll(tid)
        assert res is not None
        assert res.status == "done"

    # 跑完后队列数应清零
    assert mgr.queue_waiting_count == 0
    mgr.shutdown()


def test_subagent_cancel_in_queue():
    events = []
    mgr = SubagentManager(
        make_orchestrator=SimpleSleepOrch,
        on_queue_update=lambda q: events.append(q),
        max_concurrent=1,
    )

    t1 = SubagentTask(brief="task-running", use_process=False)
    t2 = SubagentTask(brief="task-queued", use_process=False)

    tid1 = mgr.spawn(t1)
    tid2 = mgr.spawn(t2)

    # 取消排队中的任务 t2
    mgr.cancel(tid2)

    res2 = mgr.poll(tid2)
    assert res2 is not None
    assert res2.status == "failed"
    assert "已主动取消" in res2.summary

    # 等待 t1 完成
    start = time.time()
    while time.time() - start < 5:
        if mgr.poll(tid1) is not None:
            break
        time.sleep(0.1)

    assert mgr.poll(tid1).status == "done"
    mgr.shutdown()
