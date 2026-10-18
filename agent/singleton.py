"""§18.6 单实例防护（spec §18.6）：防双击启动两个 Agent 打架。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import msvcrt
except ImportError:
    msvcrt = None


class SingleInstanceLock:
    def __init__(self, lock_file: str | Path | None = None):
        if lock_file is None:
            local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / ".alterego"))
            self.lock_path = Path(local_appdata) / "AlterEgo" / "app.lock"
        else:
            self.lock_path = Path(lock_file)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = None

    def acquire(self) -> bool:
        """尝试获取单实例独占锁。返回 True 表示成功获取，False 表示已有运行中实例。"""
        try:
            self.fp = open(self.lock_path, "w")
            if msvcrt:
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            self.fp.write(str(os.getpid()))
            self.fp.flush()
            return True
        except (OSError, IOError):
            if self.fp:
                try:
                    self.fp.close()
                except Exception:
                    pass
                self.fp = None
            return False

    def release(self):
        if self.fp and msvcrt:
            try:
                self.fp.seek(0)
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        if self.fp:
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
        except Exception:
            pass
