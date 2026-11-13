"""v3.0 §39.10：自我进化接线（经验置信度、记忆加固、夜间技能评审、检索）。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.evolution_tools import memory_search, skill_search
from agent.experience import ExperienceStore
from agent.memory_manager import MemoryManager
from agent.memory_pipeline import run_memory_distill
from agent.models import Message, ToolCall
from agent.session_store import SessionStore
from agent.skill_distiller import SkillManager
from agent.skill_review import run_skill_review
from agent.tools_registry import ToolDefinition, ToolRegistry


def _make_registry():
    reg = ToolRegistry()
    for name in ("browser_navigate", "browser_fill_combo", "browser_click",
                 "browser_evaluate", "browser_select", "send_email"):
        reg.register(ToolDefinition(
            name=name, description="t",
            parameters={"type": "object", "properties": {}},
            func=lambda args, _name=name: f"{_name}:ok"))
    reg.set_approval(SimpleNamespace(check=lambda *a, **k: True))
    return reg


def _trajectory_history(task: str, index: int, final: str = "已查到 G1025，11:05 出发"):
    calls = [
        ToolCall(id="c1", name="browser_navigate",
                 arguments={"url": f"https://example.com/{index}"}),
        ToolCall(id="c2", name="browser_fill_combo",
                 arguments={"ref": "ae1", "text": f"天津-深圳-{index}"}),
        ToolCall(id="c3", name="browser_click", arguments={"ref": "ae2"}),
        ToolCall(id="c4", name="browser_select", arguments={"ref": "ae3", "value": str(index)}),
        ToolCall(id="c5", name="browser_evaluate",
                 arguments={"script": f"return {index}"}),
        ToolCall(id="c6", name="browser_screenshot", arguments={}),
    ]
    history = [
        Message(role="user", content=task),
        Message(role="assistant", content="", tool_calls=calls),
    ]
    for call in calls:
        history.append(Message(role="tool", name=call.name,
                               tool_call_id=call.id, content="ok"))
    history.append(Message(role="assistant", content=final))
    return history


def _seed_sessions(store, count, final="已查到 G1025，11:05 出发"):
    for index in range(count):
        sid = store.new_session("查高铁")
        store.save_history(sid, _trajectory_history("查天津到深圳的高铁", index, final))
    return store


def test_experience_archive_writes_json(tmp_path):
    store = ExperienceStore(str(tmp_path / "experience.db"))
    store.add("k1", "lesson", confidence=0.9)
    store.db.execute("UPDATE experience SET last_used_ts=?",
                     (time.time() - 181 * 86400,))
    store.db.commit()
    store.decay_job()
    assert store.get("k1") is None
    archives = list((tmp_path / "memory" / "bank" / "archive").glob("*.json"))
    assert len(archives) == 1
    record = json.loads(archives[0].read_text(encoding="utf-8"))
    assert record["reason"] == "180天未用"


def test_memory_pipeline_writes_uuid_lessons_to_experience(tmp_path):
    memory = MemoryManager(tmp_path / "memory")
    store = SessionStore(str(tmp_path / "sessions.db"))
    experience = ExperienceStore(str(tmp_path / "experience.db"))
    sid = store.new_session("测试")
    store.save_history(sid, [Message(role="user", content="查价"),
                             Message(role="assistant", content="完成")])
    reply = Message(role="assistant", content=(
        "## Lessons\n- 12306 查价优先使用页面同源只读接口（2026-10-22，会话）\n"))
    llm = MagicMock()
    llm.chat.return_value = reply
    with patch("agent.llm.LLMClient.for_role", return_value=llm):
        result = run_memory_distill(
            memory=memory, store=store, experience_store=experience,
            scope="session", session_id=sid, cfg={"models": {}})
    assert result["lessons"] == 1
    row = experience.db.execute(
        "SELECT ekey, claim FROM experience WHERE archived=0").fetchone()
    assert row and row[0].startswith("lesson_") and len(row[0]) == len("lesson_") + 32
    assert "12306" in row[1]


def test_memory_dedup_atomic_and_remove_only_one(tmp_path):
    memory = MemoryManager(tmp_path / "memory")
    first = "用户喜欢简洁代码风格，不要冗长注释"
    paraphrase = "用户偏好简洁代码风格，不要冗长的注释"
    unrelated = "用户养了一只叫豆豆的猫"
    memory._append("Preferences", first)
    memory._append("Preferences", paraphrase)
    memory._append("Preferences", unrelated)
    text = memory.main.read_text(encoding="utf-8")
    assert text.count("简洁代码风格") == 1
    assert "豆豆" in text
    assert not list((tmp_path / "memory").glob(".MEMORY.*.tmp"))
    assert memory.remove_claim("用户喜欢简洁代码风格")
    text = memory.main.read_text(encoding="utf-8")
    assert "简洁代码风格" not in text
    assert "豆豆" in text


def test_memory_concurrent_append_is_serialized(tmp_path):
    memory = MemoryManager(tmp_path / "memory")
    claims = [f"并发记忆条目编号{i:02d}" for i in range(10)]
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda claim: memory._append("Facts", claim), claims))
    text = memory.main.read_text(encoding="utf-8")
    assert all(claim in text for claim in claims)
    assert not list((tmp_path / "memory").glob(".MEMORY.*.tmp"))


def test_skill_review_creates_and_registers_skill(tmp_path):
    store = _seed_sessions(SessionStore(str(tmp_path / "s.db")), 3)
    manager = SkillManager(tmp_path / "skills.json")
    registry = _make_registry()
    result = run_skill_review(store, manager, registry)
    assert len(result["created"]) == 1
    created = result["created"][0]
    assert created["name"].startswith("auto-")
    assert created["status"] == "active"
    tool_name = f"skill_{created['name'].replace('-', '_')}"
    assert tool_name in registry
    skill = manager.skills[created["name"]]
    assert len(skill["steps"]) >= 5
    assert skill["params"]
    message = registry.execute(tool_name, {"url": "https://new.example.com"})
    assert "browser_navigate" in message.content


def test_skill_review_requires_three_complete_traces(tmp_path):
    store = _seed_sessions(SessionStore(str(tmp_path / "s.db")), 2)
    manager = SkillManager(tmp_path / "skills.json")
    result = run_skill_review(store, manager, _make_registry())
    assert result["created"] == []

    partial = _seed_sessions(SessionStore(str(tmp_path / "p.db")), 3,
                             final="[已超时] 只查到部分车次")
    manager2 = SkillManager(tmp_path / "skills2.json")
    result2 = run_skill_review(partial, manager2, _make_registry())
    assert result2["created"] == []


def test_skill_review_l3_becomes_draft(tmp_path):
    store = SessionStore(str(tmp_path / "s.db"))
    for index in range(3):
        history = _trajectory_history("发送结果邮件", index)
        history[1].tool_calls[0] = ToolCall(
            id="m1", name="send_email", arguments={"to": f"u{index}@example.com"})
        store.save_history(store.new_session("发邮件"), history)
    manager = SkillManager(tmp_path / "skills.json")
    registry = _make_registry()
    result = run_skill_review(store, manager, registry)
    assert len(result["created"]) == 1
    skill = manager.skills[result["created"][0]["name"]]
    assert skill["status"] == "draft"
    tool_name = f"skill_{skill['name'].replace('-', '_')}"
    assert tool_name not in registry


def test_skill_manager_usage_stats(tmp_path):
    manager = SkillManager(tmp_path / "skills.json")
    manager.add_skill({"name": "demo", "description": "d", "steps": [],
                       "params": {}, "status": "active", "created_at": time.time()})
    for _ in range(5):
        manager.record_use("demo", True)
    assert manager.skills["demo"]["confidence"] == "high"
    for _ in range(6):
        manager.record_use("demo", False)
    assert manager.skills["demo"]["status"] == "rewrite"

    manager.add_skill({"name": "old", "description": "d", "steps": [],
                       "params": {}, "status": "active",
                       "created_at": time.time() - 91 * 86400})
    audit = manager.audit_job()
    assert "old" in audit["retired"]


def test_evolution_search_tools(tmp_path):
    memory = MemoryManager(tmp_path / "memory")
    memory._append("Preferences", "用户偏好夜间运行技能评审")
    manager = SkillManager(tmp_path / "skills.json")
    manager.add_skill({"name": "auto-demo", "description": "高铁查价流程",
                       "steps": [], "params": {}, "status": "active"})
    memory_result = memory_search(memory, {"query": "技能评审"})
    assert "MEMORY.md" in memory_result and "夜间" in memory_result
    skill_result = skill_search(manager, {"query": "高铁"})
    assert "skill_auto_demo" in skill_result
    assert "query 不能为空" in memory_search(memory, {"query": ""})
