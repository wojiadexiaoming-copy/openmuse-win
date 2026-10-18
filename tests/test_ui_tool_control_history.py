"""HTTP history reload preserves tool control metadata; no production services start."""
import io
import json
from types import SimpleNamespace

from bottle import Bottle
import pytest

from agent.models import Message, ToolCall
from agent.session_store import SessionStore
from ui.server import AgentServer


@pytest.fixture
def history_server(tmp_path):
    server = AgentServer.__new__(AgentServer)
    server.app = Bottle()
    server.api_token = "synthetic-history-token"
    server.orch = SimpleNamespace()
    server.vault = SimpleNamespace(scrub=lambda value: value)
    server.store = SessionStore(str(tmp_path / "conversations.db"))
    server._setup_routes()
    yield server
    server.store.db.close()


def reload_history(server, tools):
    session_id = server.store.new_session("offline history")
    server.store.save_history(session_id, [
        Message(role="assistant", tool_calls=[
            ToolCall(id=tool.tool_call_id, name=tool.name) for tool in tools
        ]),
        *tools,
    ])
    status = []
    environ = {
        "REQUEST_METHOD": "GET", "PATH_INFO": f"/api/sessions/{session_id}/messages",
        "wsgi.input": io.BytesIO(), "wsgi.errors": io.StringIO(), "CONTENT_LENGTH": "0",
        "HTTP_AUTHORIZATION": "Bearer synthetic-history-token",
    }
    raw = b"".join(server.app(environ, lambda value, headers, exc_info=None: status.append(value)))
    assert status[0].startswith("200"), raw[:200]
    messages = json.loads(raw)
    assert len(messages) == 1
    return messages[0]["tool_calls"]


def test_reload_stop_cancel_and_normal_results_never_count_stopped_tools_as_ok(history_server):
    control = {"retryable": False, "requires_user": True, "reason": "desktop_failsafe"}
    stopped = Message(role="tool", name="desktop_click", tool_call_id="stop",
                      content="[\u684c\u9762\u5b89\u5168\u505c\u6b62] synthetic stop",
                      metadata={"tool_control": control})
    cancelled = Message(role="tool", name="desktop_type", tool_call_id="cancel",
                        content="[\u5de5\u5177\u6267\u884c\u4e2d\u6b62] synthetic cancellation",
                        metadata={"tool_control": control, "tool_cancelled": True})
    normal = Message(role="tool", name="file_read", tool_call_id="normal", content="ordinary tool body")
    records = reload_history(history_server, [stopped, cancelled, normal])
    assert [record["status"] for record in records] == ["error", "cancelled", "ok"]
    assert "\u7b49\u5f85\u7528\u6237" in records[0]["result_summary"]
    assert "\u672a\u6267\u884c" in records[1]["result_summary"]
    assert [record["result"] for record in records] == [stopped.content, cancelled.content, normal.content]
    assert all(isinstance(record["result"], str) for record in records)
    assert all("metadata" not in record for record in records)


def test_reload_cancellation_flag_alone_remains_cancelled_with_cancel_summary(history_server):
    cancelled = Message(role="tool", name="desktop_type", tool_call_id="cancel",
                        content="neutral text without failure keywords", metadata={"tool_cancelled": True})
    record = reload_history(history_server, [cancelled])[0]
    assert record["status"] == "cancelled"
    assert "\u672a\u6267\u884c" in record["result_summary"]
    assert record["result"] == cancelled.content


@pytest.mark.parametrize("verified,expected", [(False, "error"), (True, "ok")])
def test_reload_focus_uses_verification_metadata_over_neutral_content(history_server, verified, expected):
    focus = Message(role="tool", name="desktop_inspect_focus", tool_call_id="focus",
                    content="neutral text without failure keywords",
                    metadata={"desktop_verification": {"focus_verified": verified,
                              "focus": {"editable": True, "has_keyboard_focus": True}}})
    record = reload_history(history_server, [focus])[0]
    assert record["status"] == expected
    assert record["result"] == focus.content
    if not verified:
        assert "\u672a\u9a8c\u8bc1" in record["result_summary"]
