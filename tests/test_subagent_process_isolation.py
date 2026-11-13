import time
import pytest
from unittest.mock import MagicMock
from agent.subagent import (
    SubagentManager, SubagentTask, SubagentResult, _get_process_memory_bytes
)


class DummyTopLevelOrch:
    """顶级类，支持 pickle 跨进程传输。"""
    def __init__(self, task):
        self.task = task

    def handle(self, prompt):
        return "top-level process executed successfully"


class OOMTriggerOrch:
    """模拟内存超标的子任务。"""
    def __init__(self, task):
        self.task = task

    def handle(self, prompt):
        # 持续申请大量内存直到被监控看门狗干掉
        chunks = []
        for _ in range(50):
            chunks.append(b"x" * (10 * 1024 * 1024))  # 每次申请 10MB
            time.sleep(0.1)
        return "done without oom"


def test_process_memory_reading():
    import os
    mem = _get_process_memory_bytes(os.getpid())
    assert isinstance(mem, int)
    assert mem > 0


def test_subagent_multiprocessing_execution():
    mgr = SubagentManager(make_orchestrator=DummyTopLevelOrch, max_memory_mb=1024)
    task = SubagentTask(brief="process task", depth=0, use_process=True)
    tid = mgr.spawn(task)
    assert tid.startswith("sub-")

    start = time.time()
    res = None
    while time.time() - start < 10:
        res = mgr.poll(tid)
        if res is not None:
            break
        time.sleep(0.05)

    assert res is not None
    assert res.status == "done"
    assert "top-level process executed successfully" in res.summary


def test_subagent_multiprocessing_hard_cancellation():
    mgr = SubagentManager(make_orchestrator=OOMTriggerOrch, max_memory_mb=2048)
    task = SubagentTask(brief="cancel process task", depth=0, use_process=True)
    tid = mgr.spawn(task)
    assert tid.startswith("sub-")

    time.sleep(0.3)
    info = mgr._watchdog.get(tid)
    proc = info.get("process") if info else None
    assert proc is not None
    assert proc.is_alive()

    # 主动强杀取消
    mgr.cancel(tid)
    time.sleep(0.5)

    assert not proc.is_alive()
    res = mgr.poll(tid)
    assert res is not None
    assert res.status == "failed"
    assert "已主动取消" in res.summary


def test_subagent_oom_watchdog_termination():
    """验证当子进程内存超过配额限制时，看门狗自动执行 OOM 熔断强杀，主进程存活。"""
    # 设置一个较小的配额上限：20 MB
    mgr = SubagentManager(make_orchestrator=OOMTriggerOrch, max_memory_mb=20, enforce_min_memory=False)
    task = SubagentTask(brief="oom task", depth=0, use_process=True, max_memory_mb=20, enforce_min_memory=False)
    tid = mgr.spawn(task)

    start = time.time()
    res = None
    while time.time() - start < 15:
        res = mgr.poll(tid)
        if res is not None:
            break
        time.sleep(0.1)

    assert res is not None
    assert res.status == "failed"
    assert "SubAgent OOM terminated" in res.summary
