"""统一数据目录与旧路径平滑迁移管理（B4-03）。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def get_repo_root() -> Path:
    """返回仓库根目录。"""
    return REPO_ROOT


def resolve_data_dir(custom_path: str | Path | None = None) -> Path:
    """按优先级解析数据目录：
    1. 显式传入 custom_path
    2. 环境变量 ALTEREGO_DATA
    3. 仓库内 data/ 目录（如不存在会自动创建）
    """
    if custom_path:
        p = Path(custom_path)
        p.mkdir(parents=True, exist_ok=True)
        return p

    if "ALTEREGO_DATA" in os.environ and os.environ["ALTEREGO_DATA"].strip():
        p = Path(os.environ["ALTEREGO_DATA"].strip())
    else:
        p = REPO_ROOT / "data"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        import tempfile
        p = Path(tempfile.gettempdir()) / "alterego_data"
        p.mkdir(parents=True, exist_ok=True)
    migrate_legacy_data(REPO_ROOT, p)
    return p


def resolve_sub_dir(sub_name: str, filename: str | None = None) -> Path:
    """按优先级安全解析子数据目录/文件路径：
    1. 统一数据目录 (data/<sub_name>)
    2. 用户根目录 (~/.alterego/<sub_name>)
    3. 系统临时目录 (tempfile/<sub_name>)
    发生 PermissionError 或 OSError 时自动平滑回退，确保目录创建绝对不抛出致命异常。
    """
    candidates = []
    try:
        candidates.append(resolve_data_dir() / sub_name)
    except Exception:
        pass
    try:
        candidates.append(Path.home() / ".alterego" / sub_name)
    except Exception:
        pass
    import tempfile
    candidates.append(Path(tempfile.gettempdir()) / "alterego_data" / sub_name)

    chosen = None
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            chosen = cand
            break
        except (PermissionError, OSError):
            continue

    if chosen is None:
        chosen = Path(tempfile.gettempdir())

    return chosen / filename if filename else chosen


def migrate_legacy_data(source_root: Path, target_dir: Path) -> list[str]:
    """若目标目录尚无数据，平滑将仓库根目录下的遗留文件迁移至统一数据目录。"""
    source_root = Path(source_root)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        if source_root.resolve() == target_dir.resolve():
            return []
    except Exception:
        return []

    migrated = []
    # 单文件列表
    legacy_files = [
        "conversations.db", "conversations.db-wal", "conversations.db-shm",
        "approvals.db", "approvals.db-wal", "approvals.db-shm",
        "cache.db", "cache.db-wal", "cache.db-shm",
        "spans.db", "spans.db-wal", "spans.db-shm",
        "jobs.sqlite", "jobs.sqlite-wal", "jobs.sqlite-shm",
        "settings.json", "custom_models.json", ".api_token",
    ]
    for fname in legacy_files:
        src = source_root / fname
        dst = target_dir / fname
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
                migrated.append(fname)
            except Exception:
                pass

    # 目录列表
    legacy_dirs = ["vault", "memory", "archive", "uploads", "weixin"]
    for dname in legacy_dirs:
        src = source_root / dname
        dst = target_dir / dname
        if src.is_dir() and not dst.exists():
            try:
                shutil.copytree(src, dst)
                migrated.append(dname)
            except Exception:
                pass

    return migrated
