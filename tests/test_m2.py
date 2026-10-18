"""M2 测试：会话/记忆/设置/验收/子 Agent。"""
import time

from agent.experience import ExperienceStore
from agent.failure_memory import FailureMemory
from agent.memory_manager import MemoryManager
from agent.session_store import SessionStore
from agent.settings import Settings
from agent.subagent import SubagentResult, SubagentTask, accept_result
from agent.verifier import verify_outcome
from agent.models import Message


def test_session_roundtrip(tmp_path):
    s = SessionStore(str(tmp_path / "c.db"))
    sid = s.new_session("t")
    msgs = [Message(role="user", content="hi"),
            Message(role="assistant", content="hello")]
    s.save_history(sid, msgs)
    back = s.load_history(sid)
    assert [m.role for m in back] == ["user", "assistant"]
    assert back[0].content == "hi"


def test_memory_remember(tmp_path):
    m = MemoryManager(tmp_path)
    assert m.maybe_remember("记住我的邮箱是 a@b.com") == "我的邮箱是 a@b.com"
    assert m.maybe_remember("我喜欢小红书风") == "用户喜欢：小红书风"
    assert m.maybe_remember("今天天气不错") is None
    # 去重
    assert m.maybe_remember("记住我的邮箱是 a@b.com") is None


def test_settings_policy(tmp_path):
    st = Settings(tmp_path / "s.json")
    assert st.get("file_read") == "allow"
    assert st.get("publish") == "deny"
    st.set("file_write", "deny")
    assert st.get("file_write") == "deny"
    st.set("desktop_click", "deny", target="app:Recordly")
    assert st.get("desktop_click", "app:Recordly") == "deny"
    assert st.get("desktop_click") == "ask"


def test_verifier_no_criteria():
    ok, failed = verify_outcome("随便聊聊")
    assert ok and failed == []


def test_subagent_accept():
    task = SubagentTask(brief="x", critical=False)
    r = SubagentResult(task_id="1", status="done", summary="ok",
                       evidence=[{"type": "file", "ref": "/a.txt"}])
    ok, _ = accept_result(r, task)
    assert ok
    r2 = SubagentResult(task_id="2", status="done", summary="ok", evidence=[])
    ok2, reason = accept_result(r2, task)
    assert not ok2 and "证据" in reason


def test_subagent_critical_spotcheck():
    task = SubagentTask(brief="x", critical=True)
    r = SubagentResult(task_id="3", status="done", summary="ok",
                       evidence=[{"type": "file", "ref": "/a.txt"}],
                       confidence="medium")
    ok, _ = accept_result(r, task, spot_check=lambda ev: True)
    assert ok
    ok2, _ = accept_result(r, task, spot_check=lambda ev: False)
    assert not ok2


def test_failure_memory(tmp_path):
    fm = FailureMemory(str(tmp_path / "f.db"))
    sig = fm.error_sig("ToolError", "file_read", {"path": "x"})
    assert fm.suggest(sig) is None
    assert not fm.already_tried("t1", "换方法A")
    fm.record_tried("t1", "换方法A", ok=False)
    assert fm.already_tried("t1", "换方法A")
    fm.record_tried("t1", "换方法B", ok=True, error_sig=sig,
                    fix_desc="改用绝对路径")
    assert fm.suggest(sig) == "改用绝对路径"
    fm.clear_task("t1")
    assert not fm.already_tried("t1", "换方法A")


def test_experience_decay(tmp_path):
    es = ExperienceStore(str(tmp_path / "e.db"))
    es.add("k1", "经验1")
    assert es.should_trust("k1")  # 0.5 >= 0.4
    es.feedback("k1", worked=False)
    assert not es.should_trust("k1")  # 0.5-0.3=0.2
    es.feedback("k1", worked=True)
    es.feedback("k1", worked=True)
    assert es.should_trust("k1")  # 回到 0.4


def test_context_builder_dynamic_memory(tmp_path):
    from agent.context import ContextBuilder
    mem = MemoryManager(tmp_path)
    ctx = ContextBuilder(extra_system=mem.read_main)
    # 第一轮：无个性化记忆
    msgs_1 = ctx.build([])
    assert "默认生图采用中国风" not in msgs_1[0].content
    # 动态记住偏好
    mem.maybe_remember("以后默认生图采用中国风")
    # 第二轮：动态生效
    msgs_2 = ctx.build([])
    assert "默认生图采用中国风" in msgs_2[0].content


def test_subagent_run_with_retry_success():
    from agent.subagent import SubagentManager, SubagentTask

    attempts = 0

    class MockOrch:
        def __init__(self, task):
            self.task = task
            self.last_turn_tool_records = []

        def handle(self, prompt):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # 第一轮：无有效证据，打回
                self.last_turn_tool_records = []
                return "初步尝试，未产生证据"
            # 第二轮：产出有效证据
            self.last_turn_tool_records = [{"tool": "file_write", "status": "ok", "args": {"path": "out.txt"}}]
            return "成功生成 out.txt"

    mgr = SubagentManager(make_orchestrator=MockOrch)
    task = SubagentTask(brief="写入重要文件", critical=False)
    res, takeover = mgr.run_with_retry(task, max_retries=2, timeout_sec=5.0)

    assert attempts == 2
    assert not takeover
    assert res.status == "done"
    assert len(res.evidence) == 1


def test_subagent_run_with_retry_takeover():
    from agent.subagent import SubagentManager, SubagentTask

    class AlwaysFailOrch:
        def __init__(self, task):
            self.last_turn_tool_records = []

        def handle(self, prompt):
            return "无法获取结果"

    mgr = SubagentManager(make_orchestrator=AlwaysFailOrch)
    task = SubagentTask(brief="总是失败的任务", critical=False)
    res, takeover = mgr.run_with_retry(task, max_retries=2, timeout_sec=5.0)

    # 超过重试上限，主 Agent 接管
    assert takeover is True
    assert res.status == "done"  # 但无证据未通过验收


def test_verifier_duration_and_counts(tmp_path):
    # 1. 数量检查
    ok, failed = verify_outcome("生成 2 个文件", workspace=str(tmp_path))
    assert not ok and "数量检查：2个" in failed

    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    ok, failed = verify_outcome("生成 2 个文件", workspace=str(tmp_path))
    assert ok and failed == []

    # 2. 时长检查（无视频文件时失败，生成视频文件后通过）
    ok_dur, failed_dur = verify_outcome("录制时长 10 秒", workspace=str(tmp_path))
    assert not ok_dur and "时长检查：10秒" in failed_dur

    (tmp_path / "rec.mp4").write_bytes(b"dummy video")
    ok_dur2, failed_dur2 = verify_outcome("录制时长 10 秒", workspace=str(tmp_path))
    assert ok_dur2 and failed_dur2 == []


def test_notification_dispatcher():
    from agent.notify import notify, register_notification_listener, unregister_notification_listener

    received = []

    def on_notif(title, msg, urgent):
        received.append((title, msg, urgent))

    register_notification_listener(on_notif)
    try:
        notify("测试标题", "这是一条测试内容", urgent=True)
        assert len(received) == 1
        assert received[0] == ("测试标题", "这是一条测试内容", True)
    finally:
        unregister_notification_listener(on_notif)


def test_orchestrator_spawn_and_poll_subagent():
    from agent.context import ContextBuilder
    from agent.models import ToolCall
    from agent.orchestrator import Orchestrator
    from agent.subagent import SubagentManager
    from agent.tools_registry import ToolRegistry
    from unittest.mock import MagicMock

    class MockSubOrch:
        def __init__(self, task):
            self.task = task
            self.last_turn_tool_records = [{"tool": "test", "status": "ok", "args": {"ref": "done"}}]

        def handle(self, prompt):
            return "子任务处理完毕"

    sub_mgr = SubagentManager(make_orchestrator=MockSubOrch)
    mock_llm = MagicMock()
    orch = Orchestrator(
        llm=mock_llm,
        tools=ToolRegistry(),
        ctx=ContextBuilder(),
        sub_mgr=sub_mgr,
    )

    # 1. 真实派发 spawn_subagent
    spawn_tc = ToolCall(id="call_1", name="spawn_subagent", arguments={"brief": "测试子任务"})
    msgs = orch._exec_tools([spawn_tc], task_id=None)
    assert len(msgs) == 1
    assert "[子 Agent 已派发]" in msgs[0].content
    tid = msgs[0].content.split("task_id=")[1].split("，")[0]

    # 等待子任务线程跑完（MockSubOrch 极快）
    time.sleep(0.1)

    # 2. 真实查询 poll_subagent
    poll_tc = ToolCall(id="call_2", name="poll_subagent", arguments={"task_id": tid})
    poll_msgs = orch._exec_tools([poll_tc], task_id=None)
    assert len(poll_msgs) == 1
    assert "[子 Agent 执行完毕]" in poll_msgs[0].content
    assert "子任务处理完毕" in poll_msgs[0].content


def test_context_builder_skill_injection():
    from agent.context import ContextBuilder
    from agent.models import Message
    ctx = ContextBuilder()

    # 1. 默认无浏览器工具：不注入 Skill: browser
    msgs = ctx.build([Message(role="user", content="你好")])
    assert "Skill: browser" not in msgs[0].content

    # 2. 传入 browser 工具 schema 时：动态注入 Skill: browser
    tools = [{"type": "function", "function": {"name": "browser_navigate"}}]
    msgs_browser = ctx.build([Message(role="user", content="打开网页")], tools=tools)
    assert "# Skill: browser" in msgs_browser[0].content
    assert "复合指令与执行意图" in msgs_browser[0].content


def test_memory_pipeline_distill(tmp_path):
    from agent.memory_manager import MemoryManager
    from agent.session_store import SessionStore
    from agent.memory_pipeline import run_memory_distill
    from agent.models import Message
    from unittest.mock import MagicMock, patch

    mem = MemoryManager(tmp_path / "memory")
    store = SessionStore(str(tmp_path / "sessions.db"))
    sid = store.new_session("测试提炼")
    store.save_history(sid, [
        Message(role="user", content="我换了新邮箱，现在是 new_user@domain.com，以前的 old_user@domain.com 不用了"),
        Message(role="assistant", content="好的，已了解。"),
    ])

    # 先放一条旧邮箱在 MEMORY.md
    mem._append("Facts", "用户邮箱是 old_user@domain.com")
    assert "old_user@domain.com" in mem.read_main()

    # 模拟 Worker LLM 提炼出旧条目纠正与新事实
    mock_worker_reply = Message(
        role="assistant",
        content="""## Facts
- 用户邮箱是 old_user@domain.com → 用户邮箱是 new_user@domain.com
- 用户常驻地点是上海（2026-10-12，会话）

## Preferences
- 偏好简洁代码风格（2026-10-12，用户原话）
"""
    )
    mock_llm = MagicMock()
    mock_llm.chat.return_value = mock_worker_reply

    with patch("agent.llm.LLMClient.for_role", return_value=mock_llm):
        res = run_memory_distill(
            memory=mem,
            store=store,
            scope="session",
            session_id=sid,
            cfg={"models": {}},
        )

    assert res["status"] == "ok"
    assert res["extracted_count"] >= 2
    main_text = mem.read_main()
    # 旧邮箱被移除
    assert "old_user@domain.com" not in main_text
    # 新邮箱和新偏好已写入
    assert "new_user@domain.com" in main_text
    assert "偏好简洁代码风格" in main_text




