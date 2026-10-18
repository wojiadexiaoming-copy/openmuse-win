"""§17.4 全链路 Trace：trace_id 贯穿主/子 Agent/工具/审批。

用 contextvars 透传，日志 Formatter 里加 trace_id。
Span 与结构化事件落 SQLite；export_trace() 一键导出故障包。
"""
from __future__ import annotations

import contextvars
import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="")


def new_trace() -> str:
    tid = f"t-{uuid.uuid4().hex[:12]}"
    trace_id_var.set(tid)
    return tid


def child_span(name: str) -> str:
    return f"{trace_id_var.get()}/s-{name}-{uuid.uuid4().hex[:6]}"


class TraceFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = trace_id_var.get() or "-"
        return True


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    kind: str
    name: str
    started_at: float
    ended_at: float | None = None
    status: str = "running"
    tokens_in: int | None = None
    tokens_out: int | None = None
    usage_available: bool = False


class Tracer:
    def __init__(self, db_path: str):
        from agent.db import get_db
        self._lock = threading.RLock()
        self.db = get_db(db_path)
        with self._lock:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS spans"
                "(trace_id TEXT, span_id TEXT PRIMARY KEY, parent_id TEXT,"
                " kind TEXT, name TEXT, started_at REAL, ended_at REAL,"
                " status TEXT, tokens_in INTEGER, tokens_out INTEGER,"
                " usage_available INTEGER DEFAULT 0)")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(spans)")}
            if "usage_available" not in columns:
                self.db.execute("ALTER TABLE spans ADD COLUMN usage_available INTEGER DEFAULT 0")
            # Legacy starts used status='ok' before any completion was recorded.
            self.db.execute("UPDATE spans SET status='running'"
                            " WHERE ended_at IS NULL AND status='ok'")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events"
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT,"
                " event TEXT, created_at REAL, data TEXT)")
            self.db.commit()

    def start(self, kind: str, name: str, parent_id: str | None = None) -> Span:
        span = Span(trace_id=trace_id_var.get(), span_id=child_span(name),
                    parent_id=parent_id, kind=kind, name=name,
                    started_at=time.time())
        with self._lock:
            self.db.execute(
                "INSERT INTO spans (trace_id, span_id, parent_id, kind, name,"
                " started_at, ended_at, status, tokens_in, tokens_out, usage_available)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (span.trace_id, span.span_id, span.parent_id, span.kind,
                 span.name, span.started_at, None, "running", None, None, 0))
            self.db.commit()
        return span

    def finish(self, span: Span, status: str = "ok",
               tokens_in: int | None = None, tokens_out: int | None = None):
        available = _valid_token_count(tokens_in) and _valid_token_count(tokens_out)
        with self._lock:
            span.ended_at = time.time()
            span.status = status
            span.usage_available = available
            span.tokens_in = tokens_in if available else None
            span.tokens_out = tokens_out if available else None
            self.db.execute(
                "UPDATE spans SET name=?, ended_at=?, status=?, tokens_in=?, tokens_out=?,"
                " usage_available=? WHERE span_id=?",
                (span.name, span.ended_at, status, span.tokens_in, span.tokens_out,
                 int(available), span.span_id))
            self.db.commit()

    @staticmethod
    def _event_metadata(data: dict | None, event: str = "") -> dict:
        """只持久化诊断元数据，不保存提示词、工具参数或异常原文。"""
        text_fields = {"scope", "status", "channel", "session_id", "error_type",
                       "tool", "model", "phase", "request_id", "requested_model",
                       "actual_model", "base_url_host", "provider", "role", "error_category", "failure_category"}
        number_fields = {"original_len", "input_chars", "extracted_count",
                         "corrections", "duration_ms", "tokens_in", "tokens_out",
                         "iterations", "retry", "input", "output", "image_count",
                         "tool_count", "elapsed_ms", "fallback_depth", "status_code", "dimensions", "vector_count", "input_count"}
        clean = {}
        for key, value in (data or {}).items():
            if key in {"prompt_tokens", "completion_tokens", "total_tokens",
                       "message_count", "text_characters"}:
                if _valid_token_count(value):
                    clean[key] = value
            elif (key in number_fields and isinstance(value, (int, float))
                    and not isinstance(value, bool) and math.isfinite(value)):
                clean[key] = value
            elif key in {"success", "is_fallback", "request_attempted", "usage_available"} and isinstance(value, bool):
                clean[key] = value
            elif (key in text_fields and isinstance(value, str)
                  and re.fullmatch(r"[A-Za-z0-9_.:/@-]{1,160}", value)
                  and not re.search(r"(?i)(sk-|bearer|api[_-]?key|secret)", value)):
                clean[key] = value
        if event == "llm_request" and isinstance((data or {}).get("images"), list):
            clean["images"] = [_safe_image_metadata(item) for item in data["images"]
                               if isinstance(item, dict)]
        return clean

    def log_event(self, event: str, data: dict | None = None):
        """供后台 Worker 和运行异常调用的兼容事件接口。"""
        event = event if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", event) else "runtime_event"
        payload = json.dumps(self._event_metadata(data, event), ensure_ascii=False)
        with self._lock:
            self.db.execute(
                "INSERT INTO events(trace_id, event, created_at, data) VALUES (?,?,?,?)",
                (trace_id_var.get(), event, time.time(), payload))
            self.db.commit()

    def log(self, event: str, data: dict | None = None):
        self.log_event(event, data)

    _COLS = ("trace_id", "span_id", "parent_id", "kind", "name",
             "started_at", "ended_at", "status", "tokens_in", "tokens_out", "usage_available")

    def read_spans(self, trace_id: str) -> list[dict]:
        """Normalize legacy incomplete statuses and unknown token counts for readers."""
        with self._lock:
            rows = self.db.execute(
                "SELECT " + ", ".join(self._COLS) + " FROM spans"
                " WHERE trace_id=? ORDER BY started_at", (trace_id,)).fetchall()
        result = []
        for row in rows:
            span = dict(zip(self._COLS, row))
            if span["ended_at"] is None and span["status"] == "ok":
                span["status"] = "running"
            available = (span["usage_available"] == 1
                         and _valid_token_count(span["tokens_in"])
                         and _valid_token_count(span["tokens_out"]))
            span["usage_available"] = available
            if not available:
                span["tokens_in"] = span["tokens_out"] = None
            result.append(span)
        return result

    def export_trace(self, trace_id: str, out_path: str):
        """导出该 trace 的 spans 与结构化 events，打包 zip（故障包）。"""
        with self._lock:
            spans = self.read_spans(trace_id)
            events = self.db.execute(
                "SELECT trace_id, event, created_at, data FROM events"
                " WHERE trace_id=? ORDER BY id", (trace_id,)).fetchall()
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("spans.json", json.dumps(
                spans,
                ensure_ascii=False, indent=2))
            z.writestr("events.json", json.dumps([
                {"trace_id": tid, "event": event, "created_at": created,
                 "data": json.loads(data)}
                for tid, event, created, data in events
            ], ensure_ascii=False, indent=2))
        return str(out)

    def close(self):
        with self._lock:
            self.db.close()


def _valid_token_count(value) -> bool:
    # SQLite INTEGER is signed 64-bit; bool and coerced floats/strings are not counts.
    return type(value) is int and 0 <= value <= 2**63 - 1


def _safe_image_metadata(image: dict) -> dict:
    """Whitelist the collector schema; encoded pixels and URLs are never copied."""
    clean = {}
    digest = image.get("sha256")
    if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        clean["sha256"] = digest.lower()
    dimensions = image.get("dimensions")
    if (isinstance(dimensions, (list, tuple)) and len(dimensions) == 2
            and all(isinstance(value, int) and not isinstance(value, bool)
                    and value > 0 for value in dimensions)):
        clean["dimensions"] = list(dimensions)
    else:
        clean["dimensions"] = None
    if image.get("source") in {"inline", "external"}:
        clean["source"] = image["source"]
    size = image.get("byte_count")
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        clean["byte_count"] = size
    for field in ("format", "metadata_error"):
        value = image.get(field)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,60}", value):
            clean[field] = value
    return clean


def make_llm_request_observer(tracer: Tracer, channel: str | None = None):
    """Attach with LLMClient.set_request_observer; no request body is persisted."""
    def observe(metadata: dict):
        data = dict(metadata)
        if channel:
            data["channel"] = channel
        tracer.log_event("llm_request", data)
    return observe
