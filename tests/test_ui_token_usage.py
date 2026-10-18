"""Observer usage accounting uses fake clients, no model or Vault access."""
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from bottle import Bottle

from ui.server import AgentServer, EventBus


class FakeClient:
    def set_request_observer(self, observer):
        self.observer = observer


class NoTracer:
    def log_event(self, *args, **kwargs):
        pass


@pytest.fixture
def usage_server():
    server = AgentServer.__new__(AgentServer)
    server.state_snapshot = {"agent_state": {"state": "idle"}, "status": {}}
    server.event_bus = EventBus()
    server.tracer = NoTracer()
    server.turn_lock = threading.RLock()
    server._observed_model = ""
    server._init_token_usage()
    client = FakeClient()
    server._bind_model_observer(client, "ui")
    return server, client


def event(identifier="r1", **changes):
    result = {"request_id": identifier, "actual_model": "primary", "provider": "nvidia",
              "fallback_depth": 0, "request_attempted": True, "success": True,
              "usage_available": True, "prompt_tokens": 10, "completion_tokens": 3}
    result.update(changes)
    return result


def usage(server):
    return server.state_snapshot["status"]["token_usage"]


def token_events(queue):
    result = []
    while not queue.empty():
        item = queue.get_nowait()
        if item.startswith("event: token_usage\n"):
            result.append(json.loads(item.split("data: ", 1)[1]))
    return result


def test_observer_counts_real_primary_and_fallback_once_and_emits_cumulative_snapshots(usage_server):
    server, client = usage_server
    queue = server.event_bus.subscribe()
    assert usage(server)["input"] is None and usage(server)["output"] is None
    primary = event()
    fallback = event(actual_model="fallback", provider="modelscope", fallback_depth=1,
                     prompt_tokens=7, completion_tokens=2)
    client.observer(primary)
    client.observer(fallback)
    client.observer(dict(primary))
    client.observer(event("skipped", request_attempted=False))
    client.observer(event("missing-attempt", request_attempted=None))
    recorded = token_events(queue)
    assert [item["revision"] for item in recorded] == [1, 2]
    assert [(item["input"], item["output"]) for item in recorded] == [(10, 3), (17, 5)]
    assert usage(server)["known_requests"] == 2 and usage(server)["unknown_requests"] == 0
    assert usage(server)["scope"] == "application_recorded_requests"
    assert recorded[0]["source_id"] == recorded[1]["source_id"]


def test_attempt_identity_includes_actual_provider_model_request_and_attempt(usage_server):
    server, client = usage_server
    records = [event(), event(provider="agnes"), event(actual_model="different-model"),
               event(fallback_depth=1), event("r2")]
    for record in records * 2:
        client.observer(record)
    assert usage(server)["known_requests"] == 5
    assert usage(server)["input"] == 50 and usage(server)["output"] == 15
    assert usage(server)["revision"] == 5


@pytest.mark.parametrize("changes", [
    {"usage_available": False}, {"usage_available": None}, {"prompt_tokens": None},
    {"prompt_tokens": True}, {"completion_tokens": -1}, {"completion_tokens": "3"},
    {"prompt_tokens": 2**63},
])
def test_unknown_or_invalid_usage_is_unknown_not_zero_consumption(usage_server, changes):
    server, client = usage_server
    record = event(**changes)
    client.observer(record)
    client.observer(dict(record))
    assert usage(server)["unknown_requests"] == 1
    assert usage(server)["known_requests"] == 0
    assert usage(server)["input"] is None and usage(server)["output"] is None
    assert usage(server)["revision"] == 1


def test_known_zero_is_distinct_from_missing_usage_and_unknowns_preserve_known_totals(usage_server):
    server, client = usage_server
    client.observer(event("known-zero", prompt_tokens=0, completion_tokens=0))
    client.observer(event("unknown", usage_available=False, success=False))
    client.observer(event("known", prompt_tokens=5, completion_tokens=1))
    assert usage(server)["known_requests"] == 2 and usage(server)["unknown_requests"] == 1
    assert usage(server)["input"] == 5 and usage(server)["output"] == 1


def test_concurrent_observers_are_atomic_and_duplicate_safe_without_turn_lock(usage_server):
    server, client = usage_server
    records = [event(str(index), usage_available=index % 3 == 0,
                     prompt_tokens=index, completion_tokens=index + 1) for index in range(90)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        # A parent turn may wait for subagents while owning this lock.
        with server.turn_lock:
            first = pool.submit(client.observer, event("independent", prompt_tokens=1, completion_tokens=2))
            first.result(timeout=2)
        list(pool.map(client.observer, records * 3))
    known = [index for index in range(90) if index % 3 == 0]
    assert usage(server)["known_requests"] == len(known) + 1
    assert usage(server)["unknown_requests"] == 90 - len(known)
    assert usage(server)["input"] == sum(known) + 1
    assert usage(server)["output"] == sum(index + 1 for index in known) + 2
    assert usage(server)["revision"] == 91


def test_delta_events_without_request_identity_cannot_fabricate_consumption(usage_server):
    server, client = usage_server
    server.emit_event("token_usage", {"input": 999, "output": 999})
    assert usage(server)["revision"] == 0
    assert usage(server)["input"] is None
    record = event()
    client.observer(record)
    server.emit_event("token_usage", record)
    assert usage(server)["known_requests"] == 1 and usage(server)["input"] == 10


def test_state_api_returns_known_partial_totals_and_unknown_count(usage_server):
    server, client = usage_server
    server.api_token = "offline-test-token"
    server.orch = SimpleNamespace(turn_lock=server.turn_lock)
    server.approval = SimpleNamespace(list_pending=lambda: [])
    server.app = Bottle()
    server._setup_routes()
    client.observer(event())
    client.observer(event("unknown", usage_available=False))
    statuses = []
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/state/snapshot",
               "wsgi.input": io.BytesIO(b""), "wsgi.errors": io.StringIO(),
               "HTTP_AUTHORIZATION": "Bearer offline-test-token"}
    body = b"".join(server.app(environ, lambda status, headers, exc_info=None: statuses.append(status)))
    assert statuses[0].startswith("200")
    snapshot = json.loads(body)["status"]["token_usage"]
    assert snapshot["input"] == 10 and snapshot["output"] == 3
    assert snapshot["known_requests"] == 1 and snapshot["unknown_requests"] == 1
    assert snapshot["revision"] == 2
