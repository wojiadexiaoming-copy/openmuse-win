import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
import pytest

from agent.session_store import SessionStore
from agent.models import Message


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = SessionStore(path)
    yield store, path
    store.close()
    if os.path.exists(path):
        os.remove(path)


def test_estimate_token_cost():
    # Local / Agnes model: cost 0
    cost, provider, _ = SessionStore.estimate_token_cost("agnes-3.0-flash", 1_000_000, 100_000)
    assert cost == 0.0
    assert "本地" in provider or "免费" in provider

    # DeepSeek: in 1.0, out 2.0 per 1M
    cost, provider, _ = SessionStore.estimate_token_cost("deepseek-v4.1-flash", 1_000_000, 500_000)
    # 1.0 * 1.0 + 0.5 * 2.0 = 2.0
    assert cost == 2.0
    assert "DeepSeek" in provider

    # GPT-4o-mini
    cost, _, _ = SessionStore.estimate_token_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    assert round(cost, 2) == 5.40  # 1.08 + 4.32


def test_get_token_usage_stats_empty(temp_db):
    store, _ = temp_db
    stats = store.get_token_usage_stats(range_days=30)
    assert stats["summary"]["total_tokens"] == 0
    assert stats["summary"]["input_tokens"] == 0
    assert stats["summary"]["output_tokens"] == 0
    assert stats["summary"]["requests"] == 0
    assert stats["summary"]["total_cost"] == 0.0
    assert len(stats["models"]) == 0
    assert len(stats["days"]) == 35


def test_get_token_usage_stats_aggregation(temp_db):
    store, path = temp_db
    sid = store.new_session("Test Session")

    now = time.time()
    # Day 1: 10 days ago
    ts_10d_ago = now - 10 * 86400
    # Day 2: 2 days ago
    ts_2d_ago = now - 2 * 86400
    # Day 3: 40 days ago (outside 30 days)
    ts_40d_ago = now - 40 * 86400

    m1 = Message(
        role="assistant",
        content="Reply 1",
        created_at=ts_10d_ago,
        metadata={"prompt_tokens": 1000, "completion_tokens": 200, "actual_model": "deepseek-v4.1-flash"}
    )
    m2 = Message(
        role="assistant",
        content="Reply 2",
        created_at=ts_2d_ago,
        metadata={"prompt_tokens": 2000, "completion_tokens": 400, "actual_model": "auto", "echobird_model": "deepseek-v4.1-flash"}
    )
    m3 = Message(
        role="assistant",
        content="Reply 3 (old)",
        created_at=ts_40d_ago,
        metadata={"prompt_tokens": 5000, "completion_tokens": 1000, "actual_model": "gpt-4o"}
    )

    store.save_history(sid, [m1, m2, m3])

    # 1. Query with 30-day range (should include m1 and m2, exclude m3)
    stats_30 = store.get_token_usage_stats(range_days=30)
    assert stats_30["summary"]["requests"] == 2
    assert stats_30["summary"]["input_tokens"] == 3000
    assert stats_30["summary"]["output_tokens"] == 600
    assert stats_30["summary"]["total_tokens"] == 3600
    assert len(stats_30["models"]) == 1
    assert stats_30["models"][0]["model"] == "deepseek-v4.1-flash"
    assert stats_30["models"][0]["ratio"] == 1.0

    # 2. Query with 7-day range (should include only m2)
    stats_7 = store.get_token_usage_stats(range_days=7)
    assert stats_7["summary"]["requests"] == 1
    assert stats_7["summary"]["input_tokens"] == 2000
    assert stats_7["summary"]["output_tokens"] == 400
    assert stats_7["summary"]["total_tokens"] == 2400

    # 3. Query with range=0 (all time, should include m1, m2, m3)
    stats_all = store.get_token_usage_stats(range_days=0)
    assert stats_all["summary"]["requests"] == 3
    assert stats_all["summary"]["input_tokens"] == 8000
    assert stats_all["summary"]["output_tokens"] == 1600
    assert stats_all["summary"]["total_tokens"] == 9600
    assert len(stats_all["models"]) == 2


def test_server_api_usage_endpoint(temp_db):
    store, path = temp_db
    sid = store.new_session("Test")
    m = Message(
        role="assistant",
        content="Done",
        created_at=time.time(),
        metadata={"prompt_tokens": 15000, "completion_tokens": 500, "actual_model": "deepseek-v4.1-flash"}
    )
    store.save_history(sid, [m])

    # Test stats on session store
    stats = store.get_token_usage_stats(range_days=30)
    assert stats["summary"]["total_tokens"] == 15500
    assert stats["summary"]["requests"] == 1
    assert stats["models"][0]["model"] == "deepseek-v4.1-flash"


def test_server_wsgi_api_usage_route(temp_db):
    import io
    from bottle import Bottle
    from ui.server import AgentServer

    store, path = temp_db
    sid = store.new_session("Test WSGI")
    m = Message(
        role="assistant",
        content="Testing",
        created_at=time.time(),
        metadata={"prompt_tokens": 12000, "completion_tokens": 800, "actual_model": "deepseek-v4.1-flash"}
    )
    store.save_history(sid, [m])

    server = AgentServer.__new__(AgentServer)
    server.store = store
    server.api_token = "secret-token"
    server.app = Bottle()
    server.state_snapshot = {"status": {"token_usage": {}}}
    server._init_token_usage()
    server._preseed_token_usage_from_db()

    # Preseeded token_usage in status should reflect the DB!
    assert server.state_snapshot["status"]["token_usage"]["input"] == 12000
    assert server.state_snapshot["status"]["token_usage"]["output"] == 800
    assert server.state_snapshot["status"]["token_usage"]["known_requests"] == 1

    # Register the usage route
    @server.app.route("/api/usage", method="GET")
    def get_usage():
        from bottle import request, response
        raw_range = (request.query.get("range") or "30").strip().lower()
        range_days = 0 if raw_range in ("all", "0", "max") else int(raw_range)
        data = server.store.get_token_usage_stats(range_days=range_days)
        response.content_type = "application/json; charset=utf-8"
        return json.dumps(data, ensure_ascii=False)

    # Call /api/usage via WSGI
    statuses = []
    headers = []
    def start_response(status, hdrs, exc_info=None):
        statuses.append(status)
        headers.extend(hdrs)

    env = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/api/usage",
        "QUERY_STRING": "range=30",
        "wsgi.input": io.BytesIO(),
        "wsgi.errors": io.StringIO(),
    }
    body = b"".join(server.app(env, start_response)).decode("utf-8")
    assert "200" in statuses[0]
    payload = json.loads(body)
    assert payload["summary"]["total_tokens"] == 12800
    assert payload["summary"]["requests"] == 1
    assert payload["models"][0]["model"] == "deepseek-v4.1-flash"
    assert len(payload["days"]) == 35
