"""Durable SQLite rollback and incremental checkpoint regressions."""
import sqlite3

import pytest

from agent.models import ImagePart, Message, ToolCall
from agent.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    instance = SessionStore(str(tmp_path / "conversations.db"))
    yield instance
    instance.db.close()


def durable_rows(store, sid):
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as reader:
        return reader.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY seq", (sid,)).fetchall()


def protocol_history():
    image = ImagePart(data=b"offline image bytes" * 8192, mime="image/png", mime_type="image/png")
    return [
        Message(role="user", content="original task", id="origin", created_at=1700000001,
                images=[image], metadata={"task_status": "running", "artifact_baseline": {
                    "workspace": "C:/offline", "files": {"result.png": [123, 456]}}}),
        Message(role="assistant", id="request", created_at=1700000002,
                tool_calls=[ToolCall(id="call-1", name="offline_tool", arguments={"count": 1})]),
        Message(role="tool", content="exit=0\noffline", id="result", created_at=1700000003,
                tool_call_id="call-1", name="offline_tool", images=[image],
                metadata={"snapshot_id": "offline-evidence"}),
        Message(role="user", content="visual observation", name="tool_observation",
                id="observation", created_at=1700000004, images=[image]),
        Message(role="assistant", content="saved answer", id="answer", created_at=1700000005),
    ]


@pytest.mark.parametrize("bad_field", ["metadata", "tool_arguments", "image"])
def test_serialization_failure_preserves_committed_history_after_unrelated_commit(store, bad_field):
    sid = store.new_session("original")
    history = protocol_history()
    store.save_history(sid, history)
    before = durable_rows(store, sid)
    changed = Message(role="user", content="changed before failure")
    bad = Message(role="assistant", content="invalid later row")
    if bad_field == "metadata":
        bad.metadata = {"not_json": object()}
    elif bad_field == "tool_arguments":
        bad.tool_calls = [ToolCall(id="bad", name="offline_tool", arguments={"not_json": object()})]
    else:
        bad.images = [ImagePart()]
        bad.images[0].data_base64 = object()
    changes = store.db.total_changes
    with pytest.raises(TypeError):
        store.save_history(sid, [changed, bad])
    assert store.db.in_transaction is False
    assert store.db.total_changes == changes, "serialization must finish before SQLite mutations"
    store.new_session("unrelated commit")
    assert durable_rows(store, sid) == before
    assert store.load_history(sid)[1].tool_calls[0].id == "call-1"


def test_sql_insert_failure_rolls_back_earlier_updates_and_unrelated_session_commit(store):
    sid = store.new_session("original")
    other = store.new_session("other")
    history = protocol_history()
    store.save_history(sid, history)
    store.save_history(other, [Message(role="user", content="other session")])
    before, other_before = durable_rows(store, sid), durable_rows(store, other)
    store.db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON messages "
                     "WHEN NEW.content='blocked' BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
    store.db.commit()
    history[0].metadata["task_status"] = "done"
    history.append(Message(role="assistant", content="blocked"))
    with pytest.raises(sqlite3.IntegrityError, match="offline failure"):
        store.save_history(sid, history)
    assert store.db.in_transaction is False
    store.update_title(other, "unrelated commit")
    assert durable_rows(store, sid) == before
    assert durable_rows(store, other) == other_before


def test_sql_tail_delete_failure_rolls_back_changed_prefix(store):
    sid = store.new_session()
    history = protocol_history()
    store.save_history(sid, history)
    before = durable_rows(store, sid)
    store.db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON messages "
                     "WHEN OLD.seq=3 BEGIN SELECT RAISE(ABORT, 'offline delete failure'); END")
    store.db.commit()
    history[0].metadata["task_status"] = "incomplete"
    with pytest.raises(sqlite3.IntegrityError, match="offline delete failure"):
        store.save_history(sid, history[:2])
    assert store.db.in_transaction is False
    store.new_session("unrelated commit")
    assert durable_rows(store, sid) == before


def test_unchanged_snapshot_and_reload_issue_no_writes(store):
    sid = store.new_session()
    history = protocol_history()
    store.save_history(sid, history)
    before = durable_rows(store, sid)
    changes = store.db.total_changes
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        store.save_history(sid, history)
        store.save_history(sid, store.load_history(sid))
    finally:
        store.db.set_trace_callback(None)
    assert store.db.total_changes == changes
    assert not any(query.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                   for query in statements)
    assert durable_rows(store, sid) == before
    assert store.db.in_transaction is False


def test_append_writes_only_new_row_and_preserves_image_protocol_prefix(store):
    sid = store.new_session()
    history = protocol_history()
    store.save_history(sid, history)
    before = durable_rows(store, sid)
    history.append(Message(role="user", content="continue", id="continuation", created_at=1700000006))
    changes = store.db.total_changes
    store.save_history(sid, history)
    assert store.db.total_changes - changes == 1
    assert durable_rows(store, sid)[:-1] == before
    loaded = store.load_history(sid)
    assert loaded[1].tool_calls[0].id == loaded[2].tool_call_id == "call-1"
    assert loaded[0].images[0].data == history[0].images[0].data
    assert loaded[2].images[0].mime_type == "image/png"
    assert loaded[3].name == "tool_observation"
    assert [message.id for message in loaded] == [message.id for message in history]
    assert [message.created_at for message in loaded] == [message.created_at for message in history]


def test_in_place_task_metadata_update_writes_one_column_in_one_session(store):
    sid = store.new_session()
    other = store.new_session()
    history = protocol_history()
    store.save_history(sid, history)
    store.save_history(other, protocol_history())
    other_before = durable_rows(store, other)
    before = durable_rows(store, sid)
    history[0].metadata["task_status"] = "waiting_user"
    history[0].metadata["artifact_baseline"]["files"]["new.png"] = [12, 34]
    changes, statements = store.db.total_changes, []
    store.db.set_trace_callback(statements.append)
    try:
        store.save_history(sid, history)
    finally:
        store.db.set_trace_callback(None)
    assert store.db.total_changes - changes == 1
    updates = [query for query in statements if query.startswith("UPDATE messages")]
    assert len(updates) == 1 and updates[0].startswith("UPDATE messages SET metadata=")
    after = durable_rows(store, sid)
    assert after[0][:-1] == before[0][:-1], "image/tool/identity columns remain byte-for-byte unchanged"
    assert after[1:] == before[1:]
    assert durable_rows(store, other) == other_before
    assert store.load_history(sid)[0].metadata == history[0].metadata


def test_changed_tool_arguments_and_image_update_only_affected_rows(store):
    sid = store.new_session()
    history = protocol_history()
    store.save_history(sid, history)
    before = durable_rows(store, sid)
    history[1].tool_calls[0].arguments["count"] = 2
    history[2].images = [ImagePart(data=b"new offline bytes", mime="image/jpeg", mime_type="image/jpeg")]
    changes = store.db.total_changes
    store.save_history(sid, history)
    assert store.db.total_changes - changes == 2
    loaded = store.load_history(sid)
    assert loaded[1].tool_calls[0].arguments == {"count": 2}
    assert loaded[1].tool_calls[0].id == loaded[2].tool_call_id
    assert loaded[2].images[0].data == b"new offline bytes"
    assert loaded[2].id == "result" and loaded[2].created_at == 1700000003
    after = durable_rows(store, sid)
    assert after[0] == before[0] and after[3:] == before[3:]


def test_tail_truncation_and_clear_never_touch_other_session(store):
    sid, other = store.new_session(), store.new_session()
    history = protocol_history()
    store.save_history(sid, history)
    store.save_history(other, protocol_history())
    other_before = durable_rows(store, other)
    before = durable_rows(store, sid)
    changes = store.db.total_changes
    store.save_history(sid, history[:3])
    assert store.db.total_changes - changes == 2
    assert durable_rows(store, sid) == before[:3]
    assert durable_rows(store, other) == other_before
    changes = store.db.total_changes
    store.save_history(sid, [])
    assert store.db.total_changes - changes == 3
    assert store.load_history(sid) == []
    assert durable_rows(store, other) == other_before


def test_legacy_schema_migration_persists_stable_fallback_identity(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages (session_id TEXT, seq INTEGER, role TEXT, content TEXT, "
                   "tool_calls TEXT, tool_call_id TEXT, name TEXT, PRIMARY KEY(session_id,seq))")
        db.execute("INSERT INTO messages VALUES ('old',0,'user','legacy',NULL,NULL,NULL)")
    store = SessionStore(path)
    try:
        history = store.load_history("old")
        assert history[0].images == [] and history[0].metadata == {}
        original_id, original_ts = history[0].id, history[0].created_at
        history[0].metadata["task_status"] = "waiting_user"
        store.save_history("old", history)
    finally:
        store.db.close()
    reopened = SessionStore(path)
    try:
        restored = reopened.load_history("old")
        assert restored[0].id == original_id and restored[0].created_at == original_ts
        assert restored[0].metadata == {"task_status": "waiting_user"}
        changes = reopened.db.total_changes
        reopened.save_history("old", restored)
        assert reopened.db.total_changes == changes
    finally:
        reopened.db.close()


def test_missing_message_identity_reuses_existing_checkpoint_values(store):
    sid = store.new_session()
    message = Message(role="user", content="legacy adapter")
    message.id = ""
    message.created_at = 0
    store.save_history(sid, [message])
    before = durable_rows(store, sid)
    changes = store.db.total_changes
    store.save_history(sid, [message])
    assert store.db.total_changes == changes
    assert durable_rows(store, sid) == before
    loaded = store.load_history(sid)
    assert loaded[0].id == "msg_" + sid + "_0" and loaded[0].created_at > 0


def test_repeated_append_checkpoints_have_linear_row_write_count(store):
    sid = store.new_session()
    history = protocol_history()
    store.save_history(sid, history)
    prefix = durable_rows(store, sid)
    changes = store.db.total_changes
    for index in range(20):
        history.append(Message(role="assistant", content="checkpoint " + str(index)))
        store.save_history(sid, history)
    assert store.db.total_changes - changes == 20
    assert durable_rows(store, sid)[:len(prefix)] == prefix
