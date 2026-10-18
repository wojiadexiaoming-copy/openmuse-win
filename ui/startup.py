"""Startup diagnostics that work before app dependencies and databases load."""
from __future__ import annotations

import faulthandler
import os
import sys
import tempfile
import threading
import time
from pathlib import Path


class StartupDiagnostics:
    """Log fixed phases and stack locations, without credentials or exception bodies."""

    def __init__(self, data_dir: Path, *, slow_seconds: float = 30.0, stream=None):
        self.stream = stream or sys.stdout
        self.started = time.perf_counter()
        self.phase_name = "bootstrap"
        self._lock = threading.RLock()
        self._timer_active = False
        self._closed = False
        self.file = None
        self.path = None
        self._write("bootstrap: Python entered launcher")
        name = time.strftime("startup-%Y%m%d-%H%M%S-") + str(os.getpid()) + ".log"
        roots = [Path(data_dir) / "logs", Path(tempfile.gettempdir()) / "AlterEgo" / "logs"]
        for root in roots:
            try:
                root.mkdir(parents=True, exist_ok=True)
                self.file = (root / name).open("a", encoding="utf-8", buffering=1)
                self.path = root / name
                break
            except OSError:
                continue
        if self.path:
            self._write("diagnostic log: " + str(self.path))
            try:
                faulthandler.dump_traceback_later(slow_seconds, repeat=True, file=self.file)
                self._timer_active = True
            except (RuntimeError, ValueError, OSError):
                self._write("automatic stack capture unavailable")
        else:
            self._write("diagnostic file unavailable; console phases remain enabled")
        self._write(f"slow startup: stack locations recorded every {slow_seconds:g}s until page loads")

    def _write(self, text):
        line = f"[AlterEgo startup +{time.perf_counter() - self.started:.2f}s] {text}"
        with self._lock:
            try:
                print(line, file=self.stream, flush=True)
            except (OSError, UnicodeError):
                pass
            if self.file is not None and not self._closed:
                try:
                    self.file.write(line + "\n")
                    self.file.flush()
                except OSError:
                    pass

    def phase(self, name: str):
        self.phase_name = name
        self._write("phase: " + name)

    def ready(self, *args):
        self.phase("desktop_page_loaded")
        self._cancel_timer()

    def _cancel_timer(self):
        with self._lock:
            if self._timer_active:
                faulthandler.cancel_dump_traceback_later()
                self._timer_active = False

    def failure(self, error: BaseException):
        self._write(f"FAILED phase={self.phase_name} exception={type(error).__name__}")
        # Frame positions are useful; exception text, locals, source lines and
        # token-bearing URLs are deliberately omitted before Vault is ready.
        trace = error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            self._write(f"frame: {code.co_filename}:{trace.tb_lineno} {code.co_name}")
            trace = trace.tb_next

    def close(self):
        self._cancel_timer()
        with self._lock:
            self._closed = True
            if self.file is not None:
                self.file.close()
                self.file = None
