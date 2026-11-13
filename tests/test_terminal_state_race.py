"""v1.5.16 终态竞态、收尾卡死、验收语境、恢复文案与孤儿 span。"""
import sqlite3
import threading
import time
from types import SimpleNamespace

from agent.models import Message
from agent.orchestrator import Orchestrator
from agent.subagent import (
    SubagentManager,
    SubagentResult,
    SubagentStore,
    SubagentTask,
)
from agent.tracing import Tracer
from agent.verifier import extract_criteria, outcome_requirement
from tools.browser.worker import BrowserWorker


CONCLUSION = (
    "todayai.cn 登录页只支持中国大陆手机号，没有邮箱/密码输入框；"
    "因此无法用邮箱注册，需要改用手机号或更换站点。"
    "已确认页面元素：手机号输入框 + 获取验证码按钮；未发现邮箱、密码或注册链接。"
)


def _manager_skeleton():
    mgr = SubagentManager.__new__(SubagentManager)
    mgr._lock = threading.RLock()
    mgr._queue_lock = threading.Lock()
    mgr._dispatcher_cv = threading.Condition(mgr._queue_lock)
    mgr._semaphore = threading.Semaphore(1)
    mgr._queue = []
    mgr.on_queue_update = None
    mgr._results = {}
    mgr._provisional = {}
    mgr._watchdog = {}
    mgr._store = None
    mgr.default_max_memory_mb = 1024
    mgr.stall_timeout_sec = 420.0
    mgr.closeout_stall_sec = 180.0
    return mgr


def _info(phase="", phase_age=0.0, tool="browser_click_at"):
    orch = SimpleNamespace(
        last_turn_tool_records=[{"tool": "browser_navigate", "status": "ok",
                                 "args": {}, "result": "已打开登录页"}],
        _current_tool=tool,
        _turn_phase=phase,
        _turn_phase_ts=time.time() - phase_age,
        cancel=lambda: None,
        cleanup=lambda: None,
    )
    return {
        "thread": None,
        "process": None,
        "queue": None,
        "deadline": time.time() + 600,
        "task": SubagentTask(brief="注册 todayai.cn", use_process=False),
        "orch": orch,
        "max_memory_mb": 512,
        "slot_released": True,
        "started_ts": time.time() - 600,
        "last_progress_ts": time.time() - 600,
        "progress_digest": (),
        "idle_sec": 600.0,
        "current_tool": tool,
    }


def _stash(mgr, task_id, text=CONCLUSION):
    mgr._provisional[task_id] = {"content": text, "at": time.time(),
                                 "status": "waiting_user"}


def test_cancel_returns_partial_from_ready_conclusion():
    mgr = _manager_skeleton()
    mgr._results["sub-p"] = None
    mgr._watchdog["sub-p"] = _info()
    _stash(mgr, "sub-p")

    result = mgr.cancel("sub-p", reason="回合时间上限，系统清退")

    assert result is not None
    assert result.status == "partial"
    assert CONCLUSION in result.summary
    assert "回合时间上限，系统清退" in result.summary
    assert "未经最终核验" in result.summary
    assert "failed" not in result.summary
    assert mgr.poll("sub-p").status == "partial"
    assert "sub-p" not in mgr._watchdog


def test_cancel_without_conclusion_stays_failed():
    mgr = _manager_skeleton()
    mgr._results["sub-q"] = None
    mgr._watchdog["sub-q"] = _info()

    result = mgr.cancel("sub-q", reason="用户点击停止")

    assert result.status == "failed"
    assert "用户点击停止" in result.summary


def test_closeout_stall_reclaims_with_partial_and_stack_dump():
    mgr = _manager_skeleton()
    mgr.closeout_stall_sec = 5.0
    mgr._results["sub-c"] = None
    mgr._watchdog["sub-c"] = _info(phase="emit_state", phase_age=30.0)
    _stash(mgr, "sub-c")
    dumps = []
    mgr._dump_thread_stacks = lambda tid, phase: dumps.append((tid, phase)) or "dump.log"

    mgr._scan_stalled(time.time())

    result = mgr._results["sub-c"]
    assert result.status == "partial"
    assert CONCLUSION in result.summary
    assert "收尾阶段" in result.summary and "emit_state" in result.summary
    assert dumps == [("sub-c", "emit_state")]
    assert "sub-c" not in mgr._watchdog


def test_closeout_phase_does_not_misjudge_tool_stall():
    mgr = _manager_skeleton()
    mgr.closeout_stall_sec = 5.0
    mgr._results["sub-d"] = None
    mgr._watchdog["sub-d"] = _info(phase="fix_turn", phase_age=999.0)

    mgr._scan_stalled(time.time())

    assert mgr._results["sub-d"] is None
    assert "sub-d" in mgr._watchdog


def test_late_result_audited_and_done_is_sticky():
    mgr = _manager_skeleton()
    mgr._remember_result("sub-a", SubagentResult(task_id="sub-a", status="partial",
                                                 summary="部分"))
    mgr._remember_result("sub-a", SubagentResult(task_id="sub-a", status="done",
                                                 summary="完整"))
    done = mgr._results["sub-a"]
    assert done.status == "done"
    assert done.late is True
    assert done.superseded_status == "partial"

    mgr._remember_result("sub-a", SubagentResult(task_id="sub-a", status="failed",
                                                 summary="迟到失败"))
    assert mgr._results["sub-a"].status == "done"
    assert mgr._results["sub-a"].summary == "完整"


def test_store_migrates_old_schema_and_persists_late_audit(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE subagent_tasks (task_id TEXT PRIMARY KEY,"
        " brief TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'generic',"
        " status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',"
        " confidence TEXT NOT NULL DEFAULT 'medium', evidence TEXT NOT NULL DEFAULT '[]',"
        " pid INTEGER NOT NULL DEFAULT 0, stale INTEGER NOT NULL DEFAULT 0,"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.commit()
    conn.close()

    store = SubagentStore(db_path)
    assert store.enabled
    store.save_result(SubagentResult(
        task_id="sub-old", status="done", summary="迟到完成",
        late=True, superseded_status="partial"))
    row = store._run("SELECT status, late, superseded_status FROM subagent_tasks "
                     "WHERE task_id=?", ("sub-old",))
    assert row == [("done", 1, "partial")]
    store.close()


def _bare_orchestrator():
    orch = Orchestrator.__new__(Orchestrator)
    orch.tracer = None
    orch.sub_mgr = None
    orch._pending_subagents = {}
    orch.on_turn_reply_ready = None
    orch._turn_reply_ready_at = 0.0
    orch._turn_phase = ""
    orch._turn_phase_ts = 0.0
    orch.last_turn_status = "waiting_user"
    return orch


def test_mark_reply_ready_invokes_subagent_callback():
    orch = _bare_orchestrator()
    seen = []
    orch.on_turn_reply_ready = lambda content, status: seen.append((content, status))

    orch._mark_reply_ready(Message(role="assistant", content=CONCLUSION))

    assert seen == [(CONCLUSION, "waiting_user")]
    assert orch._turn_reply_ready_at > 0


def test_turn_phase_breadcrumbs_are_logged():
    events = []
    orch = _bare_orchestrator()
    orch.tracer = SimpleNamespace(log_event=lambda name, data: events.append((name, data)))

    orch._set_turn_phase("start")
    orch._set_turn_phase("post_turn")

    assert len(events) == 1
    name, data = events[0]
    assert name == "turn_closeout"
    assert data["phase"] == "post_turn"
    assert data["previous"] == "start"
    assert "elapsed_ms" in data
    assert orch._turn_phase == "post_turn"


def test_exhausted_with_partial_does_not_duplicate_header():
    orch = _bare_orchestrator()
    orch._partial_delivery_used = False
    orch._partial_delivery = lambda conversation, reason="steps": "[部分交付]\n已完成的部分"

    text = orch._exhausted_with_partial("原因", [], reason="timeout")

    assert text.count("[部分交付]") == 1
    assert text.startswith("原因")


class _PartialMgr:
    def __init__(self):
        self.cancel_reasons = []

    def cancel(self, task_id, reason=""):
        self.cancel_reasons.append((task_id, reason))

    def poll(self, task_id):
        return SubagentResult(task_id=task_id, status="partial",
                              summary=CONCLUSION, confidence="medium")


def test_cancel_pending_note_marks_partial_as_unverified():
    orch = _bare_orchestrator()
    orch.sub_mgr = _PartialMgr()
    orch._pending_subagents = {"sub-t": {"brief": "注册"}}
    orch.emit = lambda *a, **k: None

    notes = orch._cancel_pending_subagents("timeout")

    assert "状态=partial" in notes[0]
    assert "已交付已产出部分" in notes[0]
    assert "未最终核验" in notes[0]
    assert not orch._pending_subagents


def test_duration_without_media_context_is_not_a_deliverable():
    goal = "请在10分钟内完成 todayai.cn 的注册，最多15分钟"
    assert extract_criteria(goal) == []
    assert outcome_requirement(goal)["mode"] == "ordinary"


def test_duration_with_media_context_is_a_deliverable():
    video = outcome_requirement("生成一个10分钟的视频并保存到本地")
    assert video["kind"] == "视频"
    assert video["mode"] == "local_artifact"
    assert extract_criteria("生成一个10分钟的视频并保存到本地") == ["时长检查：10分钟"]

    audio = outcome_requirement("录制一段30秒的音频")
    assert audio["kind"] == "音频"
    assert extract_criteria("录制一段30秒的音频") == ["时长检查：30秒"]


def _bare_worker():
    worker = BrowserWorker.__new__(BrowserWorker)
    worker.profile = "default"
    worker._lease_owner = None
    worker._lease_lock = threading.Lock()
    worker._wedged = None
    worker._wedged_lock = threading.Lock()
    worker._recover_lock = threading.Lock()
    worker._recover_history = []
    return worker


def test_browser_recovery_note_reports_real_timeout():
    worker = _bare_worker()
    worker._restart_kernel = lambda: None
    worker._wedged = {"since": time.time() - 5, "tool": "click_at", "timeout": 120}

    note = worker._recover_if_wedged()

    assert "等待上限 120 秒" in note
    assert "累计等待约 125 秒" in note
    assert worker._wedged is None


def test_tracer_sweeps_abandoned_spans_on_startup(tmp_path):
    db_path = tmp_path / "spans.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE spans (trace_id TEXT, span_id TEXT PRIMARY KEY, parent_id TEXT,"
        " kind TEXT, name TEXT, started_at REAL, ended_at REAL, status TEXT,"
        " tokens_in INTEGER, tokens_out INTEGER)")
    now = time.time()
    conn.execute("INSERT INTO spans VALUES ('t1','old',NULL,'agent','turn',?,NULL,'running',NULL,NULL)",
                 (now - 3 * 3600,))
    conn.execute("INSERT INTO spans VALUES ('t2','fresh',NULL,'agent','turn',?,NULL,'running',NULL,NULL)",
                 (now - 60,))
    conn.commit()
    conn.close()

    tracer = Tracer(str(db_path))
    rows = dict(tracer.db.execute("SELECT span_id, status FROM spans").fetchall())
    assert rows["old"] == "abandoned"
    assert rows["fresh"] == "running"
