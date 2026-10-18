"""Screenshot evidence survives tool execution, caching, and session restoration."""
import base64
import io
import sqlite3

import pytest
from PIL import Image

from agent.models import ImagePart, Message, ToolCall
from agent.session_store import SessionStore
from agent.tools_registry import ToolDefinition, ToolRegistry
from tools.cache import ToolResultCache


def make_image():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), color=(10, 50, 100)).save(buffer, "PNG")
    return ImagePart(data=buffer.getvalue(), mime="image/png", mime_type="image/png")


def register_result(registry, result):
    registry.register(ToolDefinition(
        name="file_read",
        description="Read a visual result",
        parameters={"type": "object", "properties": {}},
        func=lambda args: result,
    ))


def test_registry_normalizes_message_and_preserves_visual_evidence():
    image = make_image()
    original = Message(role="assistant", name="other", content="visual evidence", images=[image])
    registry = ToolRegistry()
    register_result(registry, original)

    result = registry.execute("file_read", {})

    assert result.role == "tool"
    assert result.name == "file_read"
    assert result.content == "visual evidence"
    assert result.images == [image]
    assert result.images[0].data == image.data
    assert original.role == "assistant"
    assert original.name == "other"


def test_visual_tool_result_never_enters_text_cache(tmp_path):
    image = make_image()
    result = Message(role="tool", content="fresh screenshot", images=[image])
    registry = ToolRegistry()
    register_result(registry, result)
    cache = ToolResultCache(str(tmp_path / "cache.db"))
    registry.set_cache(cache)
    calls = []
    registry.get("file_read").func = lambda args: (calls.append(True) or result)
    try:
        first = registry.execute("file_read", {})
        second = registry.execute("file_read", {})
        assert len(calls) == 2
        assert first.images == second.images == [image]
        assert cache.get("file_read", {}) is None
    finally:
        cache.db.close()


def test_text_tool_result_still_uses_cache(tmp_path):
    registry = ToolRegistry()
    register_result(registry, "plain text")
    cache = ToolResultCache(str(tmp_path / "cache.db"))
    registry.set_cache(cache)
    calls = []
    registry.get("file_read").func = lambda args: (calls.append(True) or "plain text")
    try:
        first = registry.execute("file_read", {})
        second = registry.execute("file_read", {})
        assert first.content == "plain text"
        assert second.content == "plain text\n[缓存命中]"
        assert second.images == []
        assert len(calls) == 1
    finally:
        cache.db.close()


def test_session_restores_images_and_tool_links_after_reopen(tmp_path):
    image = make_image()
    filename = tmp_path / "capture.png"
    filename.write_bytes(image.data)
    image = ImagePart(data=filename.read_bytes(), mime="image/png", mime_type="image/png")
    messages = [
        Message(role="user", content="inspect this", images=[image]),
        Message(role="assistant", tool_calls=[
            ToolCall(id="capture-1", name="desktop_screenshot", arguments={})
        ]),
        Message(role="tool", name="desktop_screenshot", tool_call_id="capture-1",
                content="saved capture", images=[image]),
        Message(role="user", content="visual observation", images=[image]),
    ]
    db_path = str(tmp_path / "sessions.db")
    store = SessionStore(db_path)
    sid = store.new_session("vision")
    try:
        store.save_history(sid, messages)
        serialized = store.db.execute(
            "SELECT images FROM messages WHERE session_id=? AND seq=2", (sid,)
        ).fetchone()[0]
        assert base64.b64encode(image.data).decode("ascii") in serialized
    finally:
        store.db.close()
    filename.unlink()

    restored_store = SessionStore(db_path)
    try:
        restored = restored_store.load_history(sid)
        assert restored == messages
        assert restored[2].images[0].mime_type == "image/png"
        assert restored[2].images[0].data == image.data
        assert restored[1].tool_calls[0].id == restored[2].tool_call_id
    finally:
        restored_store.db.close()


@pytest.mark.parametrize("has_metadata", [False, True])
def test_legacy_session_schema_migrates_without_losing_messages(tmp_path, has_metadata):
    db_path = str(tmp_path / "legacy.db")
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, started_ts REAL, title TEXT)")
    metadata_columns = ", created_at REAL, id TEXT" if has_metadata else ""
    db.execute(
        "CREATE TABLE messages (session_id TEXT, seq INTEGER, role TEXT, content TEXT, "
        "tool_calls TEXT, tool_call_id TEXT, name TEXT" + metadata_columns +
        ", PRIMARY KEY (session_id, seq))"
    )
    db.execute("INSERT INTO sessions VALUES ('s-old', 1.0, 'old')")
    fields = "session_id, seq, role, content, tool_calls, tool_call_id, name"
    values = ["s-old", 0, "user", "legacy text", "[]", "", ""]
    if has_metadata:
        fields += ", created_at, id"
        values.extend([2.0, "old-message"])
    db.execute(
        "INSERT INTO messages (" + fields + ") VALUES (" +
        ",".join("?" for _ in values) + ")", values
    )
    db.commit()
    db.close()

    store = SessionStore(db_path)
    try:
        old_history = store.load_history("s-old")
        assert len(old_history) == 1
        assert old_history[0].content == "legacy text"
        assert old_history[0].images == []
        if has_metadata:
            assert old_history[0].created_at == 2.0
            assert old_history[0].id == "old-message"
        assert "images" in {row[1] for row in store.db.execute("PRAGMA table_info(messages)")}
        image_message = Message(role="user", content="new image", images=[make_image()])
        store.save_history("s-old", old_history + [image_message])
        assert store.load_history("s-old") == old_history + [image_message]
    finally:
        store.db.close()


def test_desktop_dsl_screenshot_detail_excludes_image_payload(monkeypatch):
    from jobs import desktop_test

    image = make_image()
    result = Message(role="tool", content="capture saved", images=[image])
    monkeypatch.setattr(desktop_test, "desktop_screenshot", lambda args: result)

    step = desktop_test.execute_dsl_step({"action": "screenshot", "label": "frame"})
    assert step["success"]
    assert "capture saved" in step["detail"]
    assert image.data_base64 not in step["detail"]
    assert "ImagePart" not in step["detail"]
