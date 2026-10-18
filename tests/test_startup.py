"""Startup regressions: temporary logs, loopback HTTP and fake native windows."""
import io
import json
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bottle import Bottle

from ui import launcher
from ui.server import ThreadedWSGIServer
from ui.startup import StartupDiagnostics


@pytest.fixture
def timer(monkeypatch):
    start, cancel = MagicMock(), MagicMock()
    monkeypatch.setattr("ui.startup.faulthandler.dump_traceback_later", start)
    monkeypatch.setattr("ui.startup.faulthandler.cancel_dump_traceback_later", cancel)
    return start, cancel


def test_startup_phases_and_error_locations_never_log_credentials(tmp_path, timer):
    stream = io.StringIO()
    diagnostics = StartupDiagnostics(tmp_path, stream=stream)
    path = diagnostics.path
    diagnostics.phase("open_vault")
    try:
        raise RuntimeError("private-token-in-exception https://localhost/?token=private")
    except RuntimeError as exc:
        diagnostics.failure(exc)
    diagnostics.close()
    body = path.read_text(encoding="utf-8")
    assert "phase: open_vault" in body
    assert "FAILED phase=open_vault exception=RuntimeError" in body
    assert "test_startup_phases_and_error_locations_never_log_credentials" in body
    assert "private" not in body and "private" not in stream.getvalue()
    assert timer[0].call_count == timer[1].call_count == 1


def test_slow_start_timer_stops_when_page_loads(tmp_path, timer):
    diagnostics = StartupDiagnostics(tmp_path, stream=io.StringIO())
    timer[0].assert_called_once_with(30.0, repeat=True, file=diagnostics.file)
    diagnostics.ready()
    diagnostics.close()
    timer[1].assert_called_once()
    assert "desktop_page_loaded" in diagnostics.path.read_text(encoding="utf-8")


def test_no_writable_log_directory_still_reports_phases(tmp_path, timer, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "mkdir", MagicMock(side_effect=PermissionError("private path")))
    stream = io.StringIO()
    diagnostics = StartupDiagnostics(tmp_path, stream=stream)
    diagnostics.phase("import_backend")
    diagnostics.close()
    assert diagnostics.file is None and "phase: import_backend" in stream.getvalue()
    assert "private path" not in stream.getvalue()
    timer[0].assert_not_called()


def test_slow_start_records_real_stack_without_stopping_process(tmp_path):
    diagnostics = StartupDiagnostics(tmp_path, slow_seconds=0.05, stream=io.StringIO())
    try:
        diagnostics.phase("slow_dependency")
        time.sleep(0.12)
    finally:
        diagnostics.close()
    body = diagnostics.path.read_text(encoding="utf-8")
    assert "Timeout" in body
    assert "test_slow_start_records_real_stack_without_stopping_process" in body
    assert "phase: slow_dependency" in body


def health_server(body=None):
    app = Bottle()

    @app.get("/api/health")
    def health():
        return {"status": "ok"} if body is None else json.dumps(body)

    httpd = ThreadedWSGIServer(host="127.0.0.1", port=0).create_server(app)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    return httpd, thread


def test_actual_loopback_backend_is_verified_before_window():
    httpd, thread = health_server()
    try:
        launcher._wait_backend("127.0.0.1", httpd.server_port, thread, timeout=1.0)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1.0)
    assert not thread.is_alive()


@pytest.mark.parametrize("body", [[], {"status": "starting"}, {"other": "ok"}])
def test_invalid_health_does_not_masquerade_as_ready(body):
    httpd, thread = health_server(body)
    try:
        with pytest.raises(TimeoutError):
            launcher._wait_backend("127.0.0.1", httpd.server_port, thread, timeout=0.08)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1.0)


def test_dead_http_thread_fails_immediately():
    with pytest.raises(RuntimeError):
        launcher._wait_backend("127.0.0.1", 1, SimpleNamespace(is_alive=lambda: False))


def test_existing_bound_backend_cannot_be_silently_replaced():
    httpd, thread = health_server()
    try:
        with pytest.raises(OSError):
            ThreadedWSGIServer(host="127.0.0.1", port=httpd.server_port).create_server(Bottle())
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1.0)


class LoadedEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


def desktop_harness(tmp_path, monkeypatch, timer):
    monkeypatch.setenv("ALTEREGO_DATA", str(tmp_path))
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    order = []
    loaded = LoadedEvent()
    webview = SimpleNamespace(
        create_window=MagicMock(return_value=SimpleNamespace(events=SimpleNamespace(loaded=loaded))),
        start=MagicMock(side_effect=lambda **kwargs: loaded.fire()),
    )
    monkeypatch.setitem(sys.modules, "webview", webview)
    server = SimpleNamespace(
        app=object(), api_token="private-api-token",
        weixin_channel=SimpleNamespace(stop_message_loop=MagicMock()),
        scheduler=SimpleNamespace(shutdown=MagicMock()),
    )

    def build(*, startup_progress):
        order.append("initialize")
        startup_progress("test_initialize")
        return server

    httpd = SimpleNamespace(server_port=5000, serve_forever=MagicMock(), set_app=MagicMock(),
                            shutdown=MagicMock(), server_close=MagicMock())
    create = MagicMock(side_effect=lambda _: order.append("bind") or httpd)
    adapter = MagicMock(return_value=SimpleNamespace(create_server=create))
    monkeypatch.setattr("ui.server.AgentServer", build)
    monkeypatch.setattr("ui.server.ThreadedWSGIServer", adapter)
    monkeypatch.setattr(launcher, "_wait_backend", lambda *a, **k: order.append("health"))
    monkeypatch.setattr(launcher.threading, "Thread", lambda **kwargs: SimpleNamespace(
        start=lambda: order.append("thread"), is_alive=lambda: True))
    return SimpleNamespace(order=order, webview=webview, server=server,
                           httpd=httpd, create=create)


def test_native_window_waits_for_backend_and_cancels_watchdog(tmp_path, monkeypatch, timer, capsys):
    h = desktop_harness(tmp_path, monkeypatch, timer)
    launcher.main()
    assert h.order == ["bind", "initialize", "thread", "health"]
    h.httpd.set_app.assert_called_once_with(h.server.app)
    h.webview.create_window.assert_called_once()
    assert "private-api-token" in h.webview.create_window.call_args.args[1]
    h.httpd.shutdown.assert_called_once()
    h.httpd.server_close.assert_called_once()
    h.server.weixin_channel.stop_message_loop.assert_called_once()
    h.server.scheduler.shutdown.assert_called_once()
    timer[1].assert_called_once()
    logs = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "logs").iterdir())
    assert "desktop_page_loaded" in logs
    assert "private-api-token" not in logs + capsys.readouterr().out


@pytest.mark.parametrize("failure_phase", ["bind", "health", "native"])
def test_startup_failures_are_visible_and_clean_up(tmp_path, monkeypatch, timer, capsys, failure_phase):
    h = desktop_harness(tmp_path, monkeypatch, timer)
    if failure_phase == "bind":
        h.create.side_effect = OSError("private-port-detail")
    elif failure_phase == "health":
        monkeypatch.setattr(launcher, "_wait_backend", MagicMock(side_effect=TimeoutError("private-health-detail")))
    else:
        h.webview.start.side_effect = RuntimeError("private-native-detail")
    with pytest.raises(SystemExit) as exc:
        launcher.main()
    assert exc.value.code == 1
    if failure_phase != "native":
        h.webview.create_window.assert_not_called()
    if failure_phase == "bind":
        assert h.order == []
        h.server.weixin_channel.stop_message_loop.assert_not_called()
    else:
        h.server.weixin_channel.stop_message_loop.assert_called_once()
        h.server.scheduler.shutdown.assert_called_once()
    logs = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "logs").iterdir())
    expected = {"bind": "bind_local_port", "health": "wait_backend_health", "native": "start_desktop_runtime"}
    assert "FAILED phase=" + expected[failure_phase] in logs
    assert "private" not in logs + capsys.readouterr().out


def test_optional_server_progress_callback_does_not_change_initialization_errors(caplog):
    from ui.server import AgentServer
    server = AgentServer.__new__(AgentServer)
    server._startup_progress = MagicMock(side_effect=RuntimeError("private detail"))
    server._startup_phase("open_vault")
    assert "RuntimeError" in caplog.text and "private detail" not in caplog.text
