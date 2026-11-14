import io
import json
import pytest
from pathlib import Path
from ui.server import AgentServer

@pytest.fixture(scope="module")
def app_server():
    server = AgentServer()
    return server

def request_json(server, path, method="GET", body=None):
    errors = io.StringIO()
    input_stream = io.BytesIO(json.dumps(body).encode("utf-8") if body else b"")
    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": method,
        "wsgi.input": input_stream,
        "wsgi.errors": errors,
        "QUERY_STRING": f"token={server.api_token}",
        "HTTP_X_ALTEREGO_TOKEN": server.api_token,
    }
    status_code = []
    def start_response(status, headers, exc_info=None):
        status_code.append(status)
    raw = b"".join(server.app(environ, start_response))
    assert status_code and status_code[0].startswith("200"), f"Error {status_code}: {errors.getvalue()}"
    return json.loads(raw.decode("utf-8"))

def test_api_ideas_returns_real_cards_and_rows(app_server):
    data = request_json(app_server, "/api/ideas")
    assert data.get("status") == "ok"
    assert "cards" in data and len(data["cards"]) >= 2
    assert "rows" in data and len(data["rows"]) >= 2
    for c in data["cards"]:
        assert "title" in c and "desc" in c and "prompt" in c

def test_api_goals_returns_scheduled_and_subagent_tasks(app_server):
    data = request_json(app_server, "/api/goals")
    assert data.get("status") == "ok"
    assert "cards" in data and len(data["cards"]) >= 1
    assert "rows" in data
    assert "scheduled_count" in data

def test_api_library_returns_all_registered_tools_and_stats(app_server):
    data = request_json(app_server, "/api/library")
    assert data.get("status") == "ok"
    assert data.get("total_tools", 0) >= 30
    assert "tools_by_cat" in data
    assert "浏览器自动化" in data["tools_by_cat"]
    assert "Windows 桌面交互" in data["tools_by_cat"]
    assert "memory_stats" in data
