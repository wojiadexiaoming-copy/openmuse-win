"""v1.5.18 重启后未完成任务提醒：识别、抑制、一次性注入与丢弃持久化。"""
import pytest

from agent.context import ContextBuilder
from agent.models import Message
from agent.orchestrator import Orchestrator
from agent.session_store import SessionStore
from ui.server import AgentServer


@pytest.fixture()
def store_path(tmp_path):
    return str(tmp_path / "conversations.db")


@pytest.fixture()
def store(store_path):
    return SessionStore(store_path)


def _task(origin_text, status, origin_id=None, with_reply=False):
    origin = Message(role="user", content=origin_text)
    origin.metadata["task_origin_id"] = origin_id or origin.id
    origin.metadata["task_goal"] = origin_text
    origin.metadata["task_status"] = status
    chain = [origin]
    if with_reply:
        reply = Message(
            role="assistant", content="做到一半",
            metadata={"task_origin_id": origin.metadata["task_origin_id"],
                      "task_goal": origin_text,
                      "task_status": status})
        chain.append(reply)
    return chain


def _server(store):
    server = AgentServer.__new__(AgentServer)
    server.store = store
    server.orch = Orchestrator.__new__(Orchestrator)
    server.orch.history = []
    server._pending_resume_prompt = None
    server._resume_reminded = set()
    return server


def _pending(store, sid):
    return {"session_id": sid, **store.get_incomplete_task(sid)}


# ---------- SessionStore 识别 ----------

def test_incomplete_task_uses_origin_text_not_latest_user_message(store):
    sid = store.new_session("t")
    chain = _task("去那曲市做旅游攻略", "incomplete")
    follow_up = Message(role="user", content="今天天气怎么样",
                        metadata={"task_origin_id": "u-other",
                                  "task_status": "done"})
    store.save_history(sid, chain + [follow_up])
    info = store.get_incomplete_task(sid)
    assert info["task_desc"] == "去那曲市做旅游攻略"
    assert info["task_status"] == "incomplete"


def test_done_chain_is_not_reported_despite_earlier_incomplete(store):
    """同一任务链早期有 incomplete 记录，但最新状态是 done → 不该提醒。"""
    sid = store.new_session("t")
    chain = _task("注册账号", "incomplete", with_reply=True)
    done_reply = Message(
        role="assistant", content="已完成",
        metadata={"task_origin_id": chain[0].metadata["task_origin_id"],
                  "task_status": "done"})
    store.save_history(sid, chain + [done_reply])
    assert store.get_incomplete_task(sid) is None


def test_dismissed_chain_is_skipped_and_persisted(store, store_path):
    sid = store.new_session("t")
    chain = _task("注册 Today AI", "waiting_user", with_reply=True)
    store.save_history(sid, chain)
    assert store.get_incomplete_task(sid)["task_origin_id"] == chain[0].id

    assert store.mark_task_dismissed(sid, chain[0].id) is True
    assert store.get_incomplete_task(sid) is None

    reopened = SessionStore(store_path)
    assert reopened.get_incomplete_task(sid) is None


def test_dismiss_falls_back_to_latest_incomplete_when_origin_unknown(store):
    sid = store.new_session("t")
    chain = _task("旧任务", "incomplete", with_reply=True)
    store.save_history(sid, chain)
    assert store.mark_task_dismissed(sid, "no-such-origin") is True
    assert store.get_incomplete_task(sid) is None


def test_older_incomplete_chain_reported_after_newer_one_is_dismissed(store):
    sid = store.new_session("t")
    old = _task("旧任务A", "incomplete")
    new = _task("新任务B", "waiting_user")
    store.save_history(sid, old + new)
    assert store.get_incomplete_task(sid)["task_origin_id"] == new[0].id
    store.mark_task_dismissed(sid, new[0].id)
    assert store.get_incomplete_task(sid)["task_origin_id"] == old[0].id


# ---------- 提醒文本生成 ----------

def test_resume_notice_generated_for_new_topic(store):
    sid = store.new_session("t")
    store.save_history(sid, _task("注册 Today AI", "waiting_user"))
    server = _server(store)
    server._pending_resume_prompt = _pending(store, sid)

    notice = server._resume_notice_text(sid, "帮我查一下明天的天气")
    assert "注册 Today AI" in notice
    assert "等待你补充信息" in notice
    assert "新话题" in notice


def test_resume_notice_skipped_when_user_is_continuing(store):
    sid = store.new_session("t")
    store.save_history(sid, _task("注册 Today AI", "waiting_user"))
    server = _server(store)
    server._pending_resume_prompt = _pending(store, sid)

    assert server._resume_notice_text(sid, "继续") == ""
    assert server._resume_notice_text(sid, "再重试下") == ""
    assert server._resume_notice_text(sid, "13800138000") == ""
    # 续接不消耗「已提醒」，之后聊新话题仍能提醒
    assert "注册 Today AI" in server._resume_notice_text(sid, "帮我查天气")


def test_resume_notice_only_once_per_process(store):
    sid = store.new_session("t")
    store.save_history(sid, _task("注册 Today AI", "incomplete"))
    server = _server(store)
    server._pending_resume_prompt = _pending(store, sid)

    assert server._resume_notice_text(sid, "查天气") != ""
    assert server._resume_notice_text(sid, "再查一下新闻") == ""


def test_resume_candidate_invalidated_after_task_completes(store):
    sid = store.new_session("t")
    store.save_history(sid, _task("注册 Today AI", "running"))
    server = _server(store)
    server._pending_resume_prompt = _pending(store, sid)
    assert server._current_resume_candidate() is not None

    # 任务续接完成：快照必须失效，不能再拿旧状态提醒
    history = store.load_history(sid)
    for msg in history:
        msg.metadata["task_status"] = "done"
    store.save_history(sid, history)

    assert server._current_resume_candidate() is None
    assert server._pending_resume_prompt is None
    assert server._resume_notice_text(sid, "查天气") == ""


def test_resume_notice_missing_when_no_pending_snapshot(store):
    sid = store.new_session("t")
    store.save_history(sid, _task("注册 Today AI", "incomplete"))
    server = _server(store)
    assert server._resume_notice_text(sid, "查天气") == ""


# ---------- 新话题判定与 system prompt 注入 ----------

def test_is_new_topic_excludes_continuation_phrases():
    orch = Orchestrator.__new__(Orchestrator)
    orch.history = []
    assert orch.is_new_topic("帮我查一下明天的天气") is True
    assert orch.is_new_topic("继续") is False
    assert orch.is_new_topic("再重试下") is False
    assert orch.is_new_topic("13800138000") is False
    assert orch.is_new_topic("我登录好了") is False


def test_context_builder_injects_and_clears_turn_notice():
    ctx = ContextBuilder(max_tokens=8000)
    ctx.turn_notice = "上一进程遗留了一个未完成的任务：「注册 Today AI」"
    messages = ctx.build([Message(role="user", content="今天天气怎么样")])
    assert "本轮系统提醒" in messages[0].content
    assert "注册 Today AI" in messages[0].content

    ctx.turn_notice = ""
    messages = ctx.build([Message(role="user", content="今天天气怎么样")])
    assert "本轮系统提醒" not in messages[0].content
