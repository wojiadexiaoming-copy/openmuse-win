"""Usage and incomplete trace tests use synthetic HTTP or temporary SQLite only."""
from copy import deepcopy
import json
import sqlite3
import zipfile

import httpx
from openai import OpenAI
import pytest

from agent.llm import LLMClient
from agent.models import Message
from agent.tracing import Tracer, make_llm_request_observer, new_trace


MISSING = object()


def response(model, usage=MISSING, *, choices=None):
    body = {
        "id": "chatcmpl-offline", "object": "chat.completion", "created": 0,
        "model": model, "choices": choices if choices is not None else [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "offline reply"},
        }],
    }
    if usage is not MISSING:
        body["usage"] = usage
    return body


@pytest.fixture
def offline_client():
    clients = []
    calls = []

    def build(model="primary", outcome=None, **kwargs):
        base_url = kwargs.pop("base_url", "https://offline.invalid/v1")
        kwargs.pop("api_key", None)
        client = LLMClient(base_url=base_url, model=model, api_key="synthetic-only", **kwargs)
        client.client.close()

        def handle(request):
            calls.append((request.url.host, json.loads(request.content)["model"]))
            result = outcome() if callable(outcome) else outcome
            status, body = result if result is not None else (200, response(model))
            return httpx.Response(status, json=body, request=request)

        client.client = OpenAI(
            base_url=base_url, api_key="synthetic-only", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        clients.append(client)
        return client

    build.calls = calls
    yield build
    for client in clients:
        client.client.close()


def ask(client):
    return client.chat([Message(role="user", content="synthetic prompt")])


def test_real_sdk_usage_reaches_reply_observer_and_audit(offline_client, tmp_path):
    counts = {"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29,
              "prompt_tokens_details": {"private_field": "not retained"}}
    client = offline_client(outcome=(200, response("primary", counts)))
    tracer = Tracer(str(tmp_path / "audit.db"))
    events = []
    audit = make_llm_request_observer(tracer)
    client.set_request_observer(lambda event: (events.append(event), audit(event)))
    reply = ask(client)
    expected = {"actual_model": "primary", "usage_available": True, "prompt_tokens": 21,
                "completion_tokens": 8, "total_tokens": 29}
    assert reply.metadata == expected
    assert all(events[0][key] == value for key, value in expected.items())
    raw = tracer.db.execute("SELECT data FROM events").fetchone()[0]
    persisted = json.loads(raw)
    assert all(persisted[key] == value for key, value in expected.items())
    assert "private_field" not in raw and "synthetic prompt" not in raw
    assert len(offline_client.calls) == 1
    tracer.close()


@pytest.mark.parametrize("usage", [
    MISSING, None, {}, {"prompt_tokens": 1, "completion_tokens": 2},
    {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
    {"prompt_tokens": "1", "completion_tokens": 2, "total_tokens": 3},
    {"prompt_tokens": 1.5, "completion_tokens": 2, "total_tokens": 3},
    {"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1},
    {"prompt_tokens": 1, "completion_tokens": False, "total_tokens": 1},
    {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": "3"},
    {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 2**63},
])
def test_raw_sdk_usage_rejects_missing_and_coerced_counters(offline_client, usage):
    client = offline_client(outcome=(200, response("primary", usage)))
    events = []
    client.set_request_observer(events.append)
    assert ask(client).metadata == {"actual_model": "primary", "usage_available": False}
    assert events[0]["usage_available"] is False
    assert not {"prompt_tokens", "completion_tokens", "total_tokens"} & events[0].keys()


def test_explicit_zero_usage_is_available(offline_client):
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    client = offline_client(outcome=(200, response("primary", usage)))
    assert ask(client).metadata == {"actual_model": "primary", "usage_available": True, **usage}


def test_actual_model_comes_from_request_configuration_not_response(offline_client):
    client = offline_client("primary", outcome=(200, response("untrusted-response-model", {
        "prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4,
    })))
    assert ask(client).metadata["actual_model"] == "primary"


def test_response_parsing_failure_still_reports_actual_usage(offline_client):
    usage = {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}
    client = offline_client(outcome=(200, response("primary", usage, choices=[])))
    events = []
    client.set_request_observer(events.append)
    with pytest.raises(IndexError):
        ask(client)
    assert events[0]["success"] is False
    assert events[0]["usage_available"] is True
    assert events[0]["prompt_tokens"] == 4 and events[0]["completion_tokens"] == 1


def test_primary_failure_never_inherits_fallback_usage_or_previous_reply(offline_client):
    primary_results = iter([
        (200, response("primary", {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})),
        (503, {"error": {"message": "synthetic unavailable", "type": "service_error"}}),
    ])
    primary = offline_client(outcome=lambda: next(primary_results))
    backup = offline_client("backup", outcome=(200, response("backup", {
        "prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9,
    })))
    primary.fallback = backup
    events = []
    primary.set_request_observer(events.append)
    assert ask(primary).metadata["prompt_tokens"] == 11
    assert ask(primary).metadata["prompt_tokens"] == 7
    assert [event["actual_model"] for event in events] == ["primary", "primary", "backup"]
    assert events[1]["success"] is False and events[1]["usage_available"] is False
    assert "prompt_tokens" not in events[1]
    assert events[2]["success"] is True and events[2]["prompt_tokens"] == 7
    assert events[1]["request_id"] == events[2]["request_id"] != events[0]["request_id"]
    assert len(offline_client.calls) == 3


def test_primary_parse_failure_and_fallback_success_each_keep_own_usage(offline_client):
    primary = offline_client(outcome=(200, response("primary", {
        "prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
    }, choices=[])))
    backup = offline_client("backup", outcome=(200, response("backup", {
        "prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8,
    })))
    primary.fallback = backup
    events = []
    primary.set_request_observer(events.append)
    reply = ask(primary)
    assert reply.metadata["total_tokens"] == 8
    assert reply.metadata["actual_model"] == "backup"
    assert [event["success"] for event in events] == [False, True]
    assert [event["usage_available"] for event in events] == [True, True]
    assert [event["total_tokens"] for event in events] == [16, 8]
    assert [event["actual_model"] for event in events] == ["primary", "backup"]
    assert len(offline_client.calls) == 2


def test_routed_actual_provider_usage_remains_separate(offline_client):
    from agent.llm_router import DEFAULT_PROVIDERS, build_routed_client

    providers = deepcopy(DEFAULT_PROVIDERS)
    plan = {
        "enabled": True, "providers": providers,
        "roles": {"main_brain": ["primary@nvidia", "backup@zhipu"]},
        "capabilities": {"primary@nvidia": {"chat": True}, "backup@zhipu": {"chat": True}},
        "options": {"max_retries": 0, "timeout_seconds": 5},
    }
    def factory(**kwargs):
        model = kwargs.pop("model")
        if model == "primary":
            outcome = (503, {"error": {"message": "synthetic unavailable", "type": "service_error"}})
        else:
            outcome = (200, response(model, {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}))
        return offline_client(model, outcome=outcome, **kwargs)

    client = build_routed_client(plan, env={
        config["api_key_env"]: "synthetic-only" for config in providers.values()
    }, client_factory=factory)
    events = []
    client.set_request_observer(events.append)
    assert ask(client).metadata["total_tokens"] == 8
    assert [event["provider"] for event in events] == ["nvidia", "zhipu"]
    assert events[0]["usage_available"] is False and "total_tokens" not in events[0]
    assert events[1]["usage_available"] is True and events[1]["total_tokens"] == 8
    assert events[0]["request_id"] == events[1]["request_id"]
    assert len(offline_client.calls) == 2


def test_legacy_trace_migration_and_export_do_not_claim_completion_or_zero_usage(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE spans (trace_id TEXT, span_id TEXT PRIMARY KEY, parent_id TEXT,"
                   "kind TEXT, name TEXT, started_at REAL, ended_at REAL, status TEXT,"
                   "tokens_in INTEGER, tokens_out INTEGER)")
        db.execute("INSERT INTO spans VALUES ('legacy','open',NULL,'llm','model',1,NULL,'ok',0,0)")
        db.execute("INSERT INTO spans VALUES ('legacy','done',NULL,'llm','model',2,3,'ok',4,2)")
    tracer = Tracer(str(db_path))
    assert tracer.db.execute("SELECT status FROM spans WHERE span_id='open'").fetchone()[0] == "running"
    spans = tracer.read_spans("legacy")
    assert spans[0]["status"] == "running" and spans[0]["ended_at"] is None
    assert spans[1]["status"] == "ok" and spans[1]["ended_at"] == 3
    assert all(row["usage_available"] is False and row["tokens_in"] is None
               and row["tokens_out"] is None for row in spans)
    # Defensive reads also normalize an old writer inserting ok without ended_at.
    tracer.db.execute("UPDATE spans SET status='ok' WHERE span_id='open'")
    tracer.db.commit()
    out = tracer.export_trace("legacy", str(tmp_path / "trace.zip"))
    with zipfile.ZipFile(out) as bundle:
        exported = json.loads(bundle.read("spans.json"))
    assert exported[0]["status"] == "running"
    assert all(row["tokens_in"] is None and row["usage_available"] is False for row in exported)
    tracer.close()


def test_new_spans_running_and_finish_distinguishes_unknown_from_explicit_zero(tmp_path):
    tracer = Tracer(str(tmp_path / "trace.db"))
    trace_id = new_trace()
    unknown = tracer.start("llm", "unknown")
    assert unknown.status == "running" and unknown.usage_available is False
    row = tracer.read_spans(trace_id)[0]
    assert row["status"] == "running" and row["tokens_in"] is None
    tracer.finish(unknown)
    zero = tracer.start("llm", "zero")
    tracer.finish(zero, tokens_in=0, tokens_out=0)
    spans = tracer.read_spans(trace_id)
    assert spans[0]["status"] == "ok" and spans[0]["usage_available"] is False
    assert spans[0]["tokens_in"] is None
    assert spans[1]["usage_available"] is True and spans[1]["tokens_in"] == spans[1]["tokens_out"] == 0
    tracer.close()


def test_finish_persists_actual_model_name_after_fallback(tmp_path):
    tracer = Tracer(str(tmp_path / "trace.db"))
    trace_id = new_trace()
    span = tracer.start("llm", "primary")
    span.name = "backup"
    tracer.finish(span, tokens_in=7, tokens_out=2)
    row = tracer.read_spans(trace_id)[0]
    assert row["name"] == "backup" and row["usage_available"] is True
    assert row["tokens_in"] == 7 and row["tokens_out"] == 2
    tracer.close()


@pytest.mark.parametrize("count", [True, -1, "3", 3.0, 2**63])
def test_tracer_rejects_invalid_token_counters_and_audit_fields(tmp_path, count):
    tracer = Tracer(str(tmp_path / "trace.db"))
    trace_id = new_trace()
    span = tracer.start("llm", "model")
    tracer.finish(span, tokens_in=count, tokens_out=1)
    assert tracer.read_spans(trace_id)[0]["usage_available"] is False
    tracer.log_event("llm_request", {"usage_available": False, "prompt_tokens": count,
                                   "completion_tokens": 1, "total_tokens": 2})
    event = json.loads(tracer.db.execute("SELECT data FROM events").fetchone()[0])
    assert event["usage_available"] is False and "prompt_tokens" not in event
    assert event["completion_tokens"] == 1
    tracer.close()
