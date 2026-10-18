"""M3 测试：scheduler + hooks。"""
import time

import pytest

from agent.hooks import Hook, HookPoller, extract_fields, hash_state
from agent.scheduler import JobSpec, Scheduler


def test_hash_state_stable():
    assert hash_state({"a": 1}) == hash_state({"a": 1})
    assert hash_state({"a": 1}) != hash_state({"a": 2})


def test_extract_fields():
    assert extract_fields({"p": 1, "x": 9}, ["p"]) == {"p": 1}


def test_hook_semantic_dedup_no_wake():
    """关心的字段没变 → 不唤醒（连 judge 都不调）。"""
    woken = []
    calls = {"n": 0}
    state = {"price": 100, "ad": "x"}

    def check_fn():
        calls["n"] += 1
        if calls["n"] == 2:
            state["ad"] = "y"  # 只变了不关心的字段
        return dict(state)

    poller = HookPoller(on_wake=lambda h, e: woken.append(h.hook_id),
                        semantic_judge=lambda p: "YES")
    hook = Hook(hook_id="h1", name="t", check_fn=check_fn,
                check_interval_sec=1, cooldown_sec=0,
                semantic_fields=["price"])
    poller.add(hook)
    time.sleep(2.5)
    poller.stop_all()
    # 第一次 hash 从 "" 变 → 唤醒 1 次；第二次只变了 ad → 不唤醒
    assert woken == ["h1"]


def test_hook_wake_on_semantic_change():
    woken = []
    state = {"price": 100}

    def check_fn():
        state["price"] += 1
        return dict(state)

    poller = HookPoller(on_wake=lambda h, e: woken.append(h.hook_id),
                        semantic_judge=lambda p: "YES")
    hook = Hook(hook_id="h2", name="t", check_fn=check_fn,
                check_interval_sec=1, cooldown_sec=0,
                semantic_fields=["price"])
    poller.add(hook)
    time.sleep(1.5)
    poller.stop_all()
    assert woken  # 价格变了 → 唤醒


def test_scheduler_once(tmp_path):
    fired = []
    s = Scheduler(f"sqlite:///{tmp_path}/jobs.db",
                  on_fire=lambda jid, brief: fired.append((jid, brief)))
    s.start()
    try:
        import datetime
        run_at = datetime.datetime.now() + datetime.timedelta(seconds=1)
        s.add(JobSpec(job_id="j1", kind="once", brief="hello",
                      trigger_args={"run_date": run_at}))
        time.sleep(2.5)
        assert fired == [("j1", "hello")]
    finally:
        s.shutdown()
