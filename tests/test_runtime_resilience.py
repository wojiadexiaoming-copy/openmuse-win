"""运行时韧性：工具超时、子任务卡死熔断、租约释放、续期基线。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent.models import Message
from agent.orchestrator import Orchestrator
from agent.subagent import SubagentManager, SubagentResult, SubagentTask
from agent.tools_registry import ToolDefinition, ToolRegistry
from tools.browser.worker import BrowserCallTimeout, BrowserWorker


def _bare_worker():
    worker = BrowserWorker.__new__(BrowserWorker)
    worker.profile = "default"
    worker._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw_default")
    worker._lease_owner = None
    worker._lease_lock = threading.Lock()
    worker._wedged = None
    worker._wedged_lock = threading.Lock()
    worker._recover_lock = threading.Lock()
    worker._recover_history = []
    return worker


def test_browser_call_timeout_marks_wedged_with_honest_message():
    worker = _bare_worker()
    release = threading.Event()
    try:
        with pytest.raises(BrowserCallTimeout) as err:
            worker._run(lambda: release.wait(5), _timeout=0.05)
        assert "[工具超时]" in str(err.value)
        assert "自动重启" in str(err.value)
        assert worker._wedged and worker._wedged["tool"] == "<lambda>"
    finally:
        release.set()
        worker._executor.shutdown(wait=True)


def test_browser_wedged_fails_fast_after_recovery_cap():
    worker = _bare_worker()
    worker._wedged = {"since": time.time(), "tool": "click_at", "timeout": 120}
    worker._recover_history = [time.time(), time.time()]
    try:
        with pytest.raises(BrowserCallTimeout) as err:
            worker._run(lambda: "ok", _timeout=1)
        assert "反复卡死" in str(err.value)
    finally:
        worker._executor.shutdown(wait=True)


def test_registry_surfaces_browser_timeout_as_tool_timeout():
    def _boom(_args):
        raise BrowserCallTimeout("[工具超时] browser_click_at 超过 120 秒未返回，已停止等待。")

    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="browser_navigate", description="test", parameters={
            "type": "object", "properties": {}}, func=_boom))
    msg = reg.execute("browser_navigate", {})
    assert isinstance(msg, Message)
    assert msg.role == "tool"
    assert msg.content.startswith("[工具超时]")
    assert "[工具执行失败]" not in msg.content


def _manager_skeleton():
    mgr = SubagentManager.__new__(SubagentManager)
    mgr._lock = threading.RLock()
    mgr._queue_lock = threading.Lock()
    mgr._dispatcher_cv = threading.Condition(mgr._queue_lock)
    mgr._semaphore = threading.Semaphore(1)
    mgr._queue = []
    mgr.on_queue_update = None
    mgr._results = {}
    mgr._watchdog = {}
    mgr._store = None
    mgr.default_max_memory_mb = 1024
    mgr.stall_timeout_sec = 0.05
    return mgr


def _stalled_info(current_tool="browser_click_at", slot_released=True):
    orch = SimpleNamespace(
        last_turn_tool_records=[],
        _current_tool=current_tool,
        cancel=lambda: None,
        cleanup=lambda: None,
    )
    return {
        "thread": None,
        "process": None,
        "queue": None,
        "deadline": time.time() + 600,
        "task": SubagentTask(brief="注册临时邮箱", use_process=False),
        "orch": orch,
        "max_memory_mb": 512,
        "slot_released": slot_released,
        "started_ts": time.time() - 600,
        "last_progress_ts": time.time() - 600,
        "progress_digest": (),
        "idle_sec": 600.0,
        "current_tool": current_tool,
    }


def test_stall_scan_reclaims_subagent_without_progress():
    mgr = _manager_skeleton()
    mgr._results["sub-x"] = None
    mgr._watchdog["sub-x"] = _stalled_info()
    mgr._scan_stalled(time.time())

    result = mgr._results["sub-x"]
    assert result is not None
    assert result.status == "timeout"
    assert "判定卡死" in result.summary
    assert "browser_click_at" in result.summary
    assert "sub-x" not in mgr._watchdog


def test_stall_scan_exempts_human_takeover_wait():
    mgr = _manager_skeleton()
    mgr._results["sub-y"] = None
    mgr._watchdog["sub-y"] = _stalled_info(current_tool="browser_request_takeover")
    mgr._scan_stalled(time.time())
    assert mgr._results["sub-y"] is None
    assert "sub-y" in mgr._watchdog


def test_stall_scan_tolerates_slow_llm_between_tools():
    mgr = _manager_skeleton()
    mgr.stall_timeout_sec = 10.0
    mgr._results["sub-z"] = None
    info = _stalled_info(current_tool="")
    now = time.time()
    info["last_progress_ts"] = now - 12.0  # 超过工具阈值，但当前没有在跑工具
    mgr._watchdog["sub-z"] = info

    mgr._scan_stalled(now)
    assert mgr._results["sub-z"] is None

    info["last_progress_ts"] = now - 25.0  # 超过双倍阈值才回收
    mgr._scan_stalled(now)
    result = mgr._results["sub-z"]
    assert result is not None
    assert "未知工具" in result.summary


class _LeaseWorker:
    def __init__(self):
        self.released = []

    def release_lease(self, owner_id):
        self.released.append(owner_id)
        return True


def test_cancel_releases_browser_lease_immediately():
    mgr = _manager_skeleton()
    lease_worker = _LeaseWorker()
    info = _stalled_info()
    info["orch"] = SimpleNamespace(
        tools=SimpleNamespace(browser_worker=lease_worker),
        cancel=lambda: None,
        cleanup=lambda: None,
    )
    mgr._results["sub-z"] = None
    mgr._watchdog["sub-z"] = info

    mgr.cancel("sub-z", reason="用户点击停止")

    assert lease_worker.released == ["sub:sub-z"]
    assert mgr.poll("sub-z").status == "failed"
    assert "用户点击停止" in mgr.poll("sub-z").summary


class _SnapshotMgr:
    def __init__(self, records):
        self.records = records

    def progress_snapshot(self):
        return [{
            "task_id": "sub-stale",
            "brief": "看文章",
            "kind": "browser",
            "step": 3,
            "records": list(self.records),
            "idle_sec": 600.0,
            "current_tool": "browser_click_at",
        }]


def _bare_orchestrator(records):
    orch = Orchestrator.__new__(Orchestrator)
    now = time.time()
    orch.sub_mgr = _SnapshotMgr(records)
    orch._subagent_progress_seen = {}
    orch._subagent_progress_checked_at = now - 600
    orch._turn_started_at = now - 900
    orch._turn_extensions_used = 0
    orch.turn_max_extensions = 3
    orch.turn_extension_sec = 60.0
    orch.tracer = None
    orch.llm = SimpleNamespace(model="offline")
    orch.emit = lambda *a, **k: None
    return orch


def test_extension_ignores_stale_child_progress():
    """十分钟前的旧动作不能触发墙钟续期。"""
    old = {"tool": "browser_click", "status": "ok",
           "result": "[已点击] 提交按钮", "ts": time.time() - 600}
    orch = _bare_orchestrator([old])
    assert orch._maybe_extend_deadline([], [], [], 0, []) is None
    assert orch._turn_extensions_used == 0


def test_extension_accepts_recent_child_progress():
    fresh = {"tool": "browser_click", "status": "ok",
             "result": "[已点击] 提交按钮", "ts": time.time() - 5}
    orch = _bare_orchestrator([fresh])
    deadline = orch._maybe_extend_deadline([], [], [], 0, [])
    assert deadline is not None
    assert orch._turn_extensions_used == 1


class _TerminalMgr:
    def __init__(self):
        self.cancel_reasons = []

    def cancel(self, task_id, reason=""):
        self.cancel_reasons.append((task_id, reason))

    def poll(self, task_id):
        return SubagentResult(task_id=task_id, status="timeout",
                              summary="硬超时 1200s，看门狗强制回收")


def test_cancel_pending_subagents_reports_real_terminal_status():
    orch = Orchestrator.__new__(Orchestrator)
    orch.sub_mgr = _TerminalMgr()
    orch._pending_subagents = {"sub-t": {"brief": "注册"}}
    orch.emit = lambda *a, **k: None

    notes = orch._cancel_pending_subagents("timeout")

    assert orch.sub_mgr.cancel_reasons == [("sub-t", "回合时间上限，系统清退")]
    assert len(notes) == 1
    assert "状态=timeout" in notes[0]
    assert "不得写成“仍在执行”" in notes[0]
    assert not orch._pending_subagents
