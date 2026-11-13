"""§34 数据保留期管理（B4-04）。

对 spans.db、uploads/、archive/、logs/ 等各模块数据实行统一过期清理策略。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("alterego.retention")


def prune_directory_by_mtime(directory: Path | str, keep_days: int, pattern: str = "*") -> list[str]:
    """按修改时间删除目录内超过 keep_days 天的文件。"""
    d = Path(directory)
    if not d.is_dir():
        return []
    cutoff = time.time() - max(1, keep_days) * 86400
    deleted = []
    for f in d.glob(pattern):
        if f.is_file():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    deleted.append(f.name)
            except OSError:
                pass
    return deleted


def run_retention_cleanup(data_dir: Path | str,
                          archive_days: int = 30,
                          upload_days: int = 30,
                          log_days: int = 14,
                          span_days: int = 14,
                          tracer=None) -> dict[str, int]:
    """执行统一保留期清理策略并返回清理统计。"""
    d = Path(data_dir)
    stats: dict[str, int] = {}

    # 1. 归档会话库清理 (archive/)
    arch_deleted = prune_directory_by_mtime(d / "archive", archive_days, pattern="*.db")
    stats["archived_sessions"] = len(arch_deleted)

    # 2. 上传临时文件清理 (uploads/)
    up_deleted = prune_directory_by_mtime(d / "uploads", upload_days)
    stats["uploads"] = len(up_deleted)

    # 3. 日志文件清理 (logs/)
    logs_deleted = prune_directory_by_mtime(d / "logs", log_days, pattern="*.log")
    # 若根目录下存在旧 logs 目录，同步清理
    try:
        repo_logs = d.parent / "logs"
        if repo_logs.is_dir() and repo_logs.resolve() != (d / "logs").resolve():
            logs_deleted += prune_directory_by_mtime(repo_logs, log_days, pattern="*.log")
    except Exception:
        pass
    stats["logs"] = len(logs_deleted)

    # 4. 链路追踪数据清理 (spans.db)
    if tracer and hasattr(tracer, "cleanup"):
        span_res = tracer.cleanup(days=span_days)
        stats["spans"] = span_res.get("deleted_spans", 0)
        stats["events"] = span_res.get("deleted_events", 0)
    else:
        stats["spans"] = 0
        stats["events"] = 0

    logger.info("Retention cleanup finished: %s", stats)
    return stats
