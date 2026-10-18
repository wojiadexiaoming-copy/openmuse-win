"""§18.6 看门狗机制（spec §18.6）：心跳守护与故障自愈。

主进程每 5 秒更新心跳时间；看门狗检测到超过 30 秒无心跳即认定主进程假死/崩溃。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class HeartbeatSender:
    """运行在 Agent 主进程内的心跳发射器。"""

    def __init__(self, heartbeat_file: str | Path | None = None, interval_sec: float = 5.0):
        if heartbeat_file is None:
            local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / ".alterego"))
            self.file_path = Path(local_appdata) / "AlterEgo" / "heartbeat.txt"
        else:
            self.file_path = Path(heartbeat_file)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.interval_sec = interval_sec
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self):
        self._running = True
        self.ping()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def ping(self):
        try:
            self.file_path.write_text(f"{time.time()}:{os.getpid()}", encoding="utf-8")
        except Exception:
            pass

    def _loop(self):
        while self._running:
            time.sleep(self.interval_sec)
            self.ping()

    def stop(self):
        self._running = False
        try:
            if self.file_path.exists():
                self.file_path.unlink()
        except Exception:
            pass


class WatchdogChecker:
    """看门狗检测逻辑：用于独立看门狗进程或健康状态探测。"""

    def __init__(self, heartbeat_file: str | Path, timeout_sec: float = 30.0):
        self.file_path = Path(heartbeat_file)
        self.timeout_sec = timeout_sec

    def is_alive(self) -> bool:
        """检查心跳文件是否存在且未超时。"""
        if not self.file_path.exists():
            return False
        try:
            content = self.file_path.read_text(encoding="utf-8").strip()
            ts_str, _ = content.split(":", 1)
            ts = float(ts_str)
            return (time.time() - ts) < self.timeout_sec
        except Exception:
            return False
