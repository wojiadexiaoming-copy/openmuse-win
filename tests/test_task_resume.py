"""Task metadata survives SQLite reopen and stays internal."""
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.llm import LLMClient
from agent.models import Message
from agent.session_store import SessionStore


def test_task_baseline_metadata_survives_sqlite_reopen(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    metadata = {
        "artifact_baseline": {
            "workspace": str(tmp_path.resolve()),
            "files": {"existing.png": [100, 200, 300]},
        },
        "task": {"goal": "生成3张图片", "status": "incomplete"},
    }
    origin = Message(role="user", content="生成3张图片", metadata=metadata)
    store = SessionStore(db_path)
    sid = store.new_session("resume")
    store.save_history(sid, [origin, Message(role="assistant", content="已生成2张")])
    store.db.close()

    reopened = SessionStore(db_path)
    try:
        saved = reopened.load_history(sid)
        assert saved[0].metadata == metadata
        assert saved[0].id == origin.id
        assert saved[1].metadata == {}
        assert saved[0].metadata["artifact_baseline"]["workspace"] == str(tmp_path.resolve())
        model_payload = LLMClient._to_openai(saved[0])
        assert "metadata" not in model_payload
        assert "artifact_baseline" not in json.dumps(model_payload)
    finally:
        reopened.db.close()


@pytest.mark.parametrize("has_images", [False, True])
def test_metadata_migrates_old_schema_and_preserves_history(tmp_path, has_images):
    db_path = str(tmp_path / "legacy.db")
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, started_ts REAL, title TEXT)")
    db.execute(
        "CREATE TABLE messages (session_id TEXT, seq INTEGER, role TEXT, content TEXT,"
        " tool_calls TEXT, tool_call_id TEXT, name TEXT, created_at REAL, id TEXT" +
        (", images TEXT DEFAULT '[]'" if has_images else "") +
        ", PRIMARY KEY (session_id, seq))")
    db.execute("INSERT INTO sessions VALUES ('s-old', 1, 'legacy')")
    db.execute(
        "INSERT INTO messages(session_id,seq,role,content,tool_calls,tool_call_id,name,created_at,id)"
        " VALUES ('s-old',0,'user','旧会话','[]','','',2,'msg-old')")
    db.commit()
    db.close()

    store = SessionStore(db_path)
    try:
        history = store.load_history("s-old")
        assert history[0].content == "旧会话" and history[0].id == "msg-old"
        assert history[0].metadata == {} and history[0].images == []
        history[0].metadata = {"artifact_baseline": {"workspace": str(tmp_path), "files": {}}}
        store.save_history("s-old", history)
        assert store.load_history("s-old")[0].metadata == history[0].metadata
        assert "metadata" in {row[1] for row in store.db.execute("PRAGMA table_info(messages)")}
    finally:
        store.db.close()


def test_error_message_delivery_uses_one_persisted_id():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the minimal DOM delivery regression")
    script = Path(__file__).with_name("test_ui_message_delivery.js")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
