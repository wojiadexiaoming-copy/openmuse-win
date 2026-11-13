"""§18.6 单实例防护（spec §18.6）：防双击启动两个 Agent 打架。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
except ImportError:
    fcntl = None


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
            # 打开或创建锁文件；在 Windows/Linux 平台下均进行非阻塞排他锁保护
            self.fp = open(self.lock_path, "a+")
            if msvcrt:
                self.fp.seek(0)
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            elif fcntl:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fp.seek(0)
            self.fp.truncate()
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
        """释放锁并尝试清理锁文件。"""
        if self.fp:
            if msvcrt:
                try:
                    self.fp.seek(0)
                    msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            elif fcntl:
                try:
                    fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
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

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"已有正在运行的实例占有单实例锁: {self.lock_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
