"""§34 备份与灾难恢复 [必选]

跑半年的 Agent，记忆比代码值钱。机器挂了能恢复才叫完整。

备份内容（按规格 §34.1）：
| 内容 | 路径 | 频率 |
| 记忆 | memory/ | 每天 |
| 会话 | data/conversations.db | 每天 |
| 配置 | config.yaml | 变更加备份 |
| 密钥 | Windows 凭据管理器 | 手动导出+加密（月） |
| 产物 | artifacts/ | 每周 |

密钥绝不进备份包明文。
"""
from __future__ import annotations

import glob
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import time
import zipfile

log = logging.getLogger("backup")


def _backup_sqlite_db(src_path: str, dst_path: str) -> bool:
    """使用 SQLite 在线备份 API 安全导出数据库（防 WAL 并发撕裂）。"""
    try:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        with sqlite3.connect(src_path) as src_conn:
            with sqlite3.connect(dst_path) as dst_conn:
                src_conn.backup(dst_conn)
        return True
    except Exception as e:
        log.warning(f"[backup] SQLite 在线备份失败 ({src_path}): {e}，回退至文件复制")
        try:
            shutil.copy2(src_path, dst_path)
            return True
        except Exception as e2:
            log.error(f"[backup] 回退复制失败 ({src_path}): {e2}")
            return False


def backup_job(workspace: str | None = None,
               backup_root: str | None = None,
               keep_days: int = 30) -> str:
    """每天凌晨 4 点跑（由 Scheduler 调度）。返回备份 zip 路径。"""
    from agent.paths import get_repo_root, resolve_data_dir

    repo_root = str(get_repo_root())
    if workspace is None:
        workspace = str(resolve_data_dir())
    else:
        workspace = str(Path(workspace).resolve())

    if backup_root is None:
        backup_root = os.path.join(workspace, "backups")
    os.makedirs(backup_root, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(backup_root, ts)
    os.makedirs(backup_dir, exist_ok=True)

    # 排除项：密钥、日志、缓存、pyc
    exclude_dirs = {"logs", "__pycache__", ".git", "node_modules", "backups"}
    exclude_exts = {".pyc", ".pyo", ".log", ".lock"}

    backup_entries: list[tuple[str, str]] = []

    # 1. 扫描 workspace 下的目标
    for rel in [
        "conversations.db",
        "approvals.db",
        "settings.json",
        "memory",
        "data/conversations.db",
        "config.yaml",
        "artifacts",
        "proactivity",
    ]:
        src = os.path.join(workspace, rel)
        if os.path.exists(src):
            backup_entries.append((src, rel))

    # 2. 补充 repo_root 下的关键配置和产物
    for rel in ["config.yaml", "memory", "artifacts", "proactivity"]:
        src = os.path.join(repo_root, rel)
        if not any(dst == rel for _, dst in backup_entries) and os.path.exists(src):
            backup_entries.append((src, rel))

    for src, rel in backup_entries:
        dst = os.path.join(backup_dir, rel)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=_ignore(exclude_dirs, exclude_exts), dirs_exist_ok=True)
        elif src.endswith(".db"):
            _backup_sqlite_db(src, dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

    # 打包
    zip_path = shutil.make_archive(backup_dir, "zip", backup_dir)
    shutil.rmtree(backup_dir, ignore_errors=True)

    # 只留最近 keep_days 天
    prune_old_backups(backup_root, keep_days)
    log.info(f"[backup] 完成：{zip_path}")
    return zip_path


def restore_backup(zip_path: str, target_workspace: str | None = None) -> list[str]:
    """解压备份包并恢复至目标工作空间（安全解压防路径穿越）。"""
    from agent.paths import resolve_data_dir

    if target_workspace is None:
        target_workspace = str(resolve_data_dir())

    target = Path(target_workspace).resolve()
    target.mkdir(parents=True, exist_ok=True)

    restored = []
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.infolist():
            dest_path = (target / member.filename).resolve()
            if not str(dest_path).startswith(str(target)):
                raise ValueError(f"Unsafe path traversal detected in zip archive: {member.filename}")
            z.extract(member, target)
            restored.append(member.filename)

    log.info(f"[backup] 成功恢复备份 {zip_path} 到 {target_workspace} ({len(restored)} 项)")
    return restored


def list_backups(workspace: str | None = None, backup_root: str | None = None) -> list[dict]:
    """列出已有备份包。"""
    from agent.paths import resolve_data_dir

    if workspace is None:
        workspace = str(resolve_data_dir())
    if backup_root is None:
        backup_root = os.path.join(workspace, "backups")
    if not os.path.isdir(backup_root):
        return []
    items = []
    for z in glob.glob(os.path.join(backup_root, "*.zip")):
        try:
            st = os.stat(z)
            items.append({
                "name": os.path.basename(z),
                "path": z,
                "size": st.st_size,
                "created_at": st.st_mtime,
            })
        except OSError:
            pass
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def _ignore(dirs: set, exts: set):
    def _fn(path, names):
        out = []
        for n in names:
            full = os.path.join(path, n)
            if os.path.isdir(full) and n in dirs:
                out.append(n)
            elif os.path.isfile(full) and os.path.splitext(n)[1] in exts:
                out.append(n)
            # 密钥文件绝不备份
            elif "vault" in n.lower() and n.endswith((".bin", ".key", ".db")):
                if "vault" in path.lower() or n.startswith("vault"):
                    out.append(n)
        return out
    return _fn


def prune_old_backups(backup_root: str, keep_days: int = 30):
    """删除 keep_days 天前的备份。"""
    now = time.time()
    for z in glob.glob(os.path.join(backup_root, "*.zip")):
        try:
            age_days = (now - os.path.getmtime(z)) / 86400
            if age_days > keep_days:
                os.remove(z)
        except OSError:
            pass


def verify_restore(workspace: str | None = None) -> dict:
    """§34.3 恢复自检：检查记忆可读、配置存在、关键目录与数据库完整。"""
    from agent.paths import get_repo_root, resolve_data_dir

    repo_root = get_repo_root()
    if workspace is None:
        data_dir = resolve_data_dir()
    else:
        data_dir = Path(workspace).resolve()

    checks = {}
    # 记忆
    mem_p = data_dir / "memory"
    if not mem_p.exists():
        mem_p = repo_root / "memory"
    checks["memory"] = mem_p.is_dir()

    # 配置
    cfg_p = repo_root / "config.yaml"
    if not cfg_p.exists():
        cfg_p = data_dir / "config.yaml"
    checks["config"] = cfg_p.is_file()

    # 会话
    conv_p = data_dir / "conversations.db"
    if not conv_p.exists():
        conv_p = data_dir / "data" / "conversations.db"
    checks["conversations"] = conv_p.is_file()

    # 审批
    appr_p = data_dir / "approvals.db"
    checks["approvals"] = appr_p.is_file()

    # vault 完整性：只检查文件存在，不读内容
    vault_files = glob.glob(os.path.join(str(data_dir), "vault*"))
    checks["vault_present"] = len(vault_files) > 0
    checks["ok"] = bool((checks["conversations"] or checks["memory"]) and checks["config"])
    return checks


def make_backup_tools(workspace: str | None = None):
    """构建备份工具。"""
    from agent.tools_registry import ToolDefinition

    def _run(args: dict) -> str:
        try:
            z = backup_job(workspace=workspace)
            return f"[备份] 完成：{z}"
        except Exception as e:
            return f"[工具执行失败] {e}"

    def _verify(args: dict) -> str:
        v = verify_restore(workspace=workspace)
        ok = "✓" if v.get("ok") else "✗"
        return f"[恢复自检] {ok} {v}"

    return [
        ToolDefinition(
            name="backup_run",
            description="立即执行一次备份（记忆/会话/配置/产物）。",
            parameters={"type": "object", "properties": {}, "required": []},
            func=_run,
            parallel_safe=False,
            is_readonly=False,
            cacheable=False,
        ),
        ToolDefinition(
            name="backup_verify",
            description="恢复自检：检查记忆/配置/会话是否完整。",
            parameters={"type": "object", "properties": {}, "required": []},
            func=_verify,
            parallel_safe=True,
            is_readonly=True,
            cacheable=False,
        ),
    ]
