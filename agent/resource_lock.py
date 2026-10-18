"""§5.9 并发任务资源隔离：任务级锁。

只读工具不加锁；写/操作类按资源 ID 加锁；超时改道；任务结束必须释放。
"""
from __future__ import annotations

import threading
import time


class ResourceLock:
    def __init__(self):
        self._locks: dict[str, str] = {}   # resource_id -> task_id
        self._mtx = threading.Lock()

    def acquire(self, resource_id: str, task_id: str, timeout_sec: int = 300) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._mtx:
                if resource_id not in self._locks:
                    self._locks[resource_id] = task_id
                    return True
            time.sleep(0.5)
        return False

    def release(self, resource_id: str, task_id: str):
        with self._mtx:
            if self._locks.get(resource_id) == task_id:
                del self._locks[resource_id]

    def release_all(self, task_id: str):
        """任务结束时调用：释放该任务持有的所有锁（防死锁）。"""
        with self._mtx:
            for r in [r for r, t in self._locks.items() if t == task_id]:
                del self._locks[r]


def resource_ids_for(tool: str, args: dict) -> list[str]:
    """工具调用映射到资源 ID。只读工具返回空（不加锁）。"""
    if tool in ("file_read", "web_search", "memory_search"):
        return []
    if tool in ("file_write", "file_edit"):
        return [f"file:{args.get('path', '?')}"]
    if tool == "shell_exec":
        return ["shell"]                     # shell 全局串行
    if tool.startswith("browser_"):
        return [f"browser:{args.get('profile', 'default')}"]
    if tool.startswith("desktop_"):
        return [f"desktop:{args.get('app', 'global')}"]
    return [f"tool:{tool}"]                  # 未知工具按工具名串行（保守）
