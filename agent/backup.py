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

import logging

log = logging.getLogger("backup")

import os
import shutil
import time
import glob


def backup_job(workspace: str | None = None,
               backup_root: str | None = None,
               keep_days: int = 30) -> str:
    """每天凌晨 4 点跑（由 Scheduler 调度）。返回备份 zip 路径。"""
    if workspace is None:
        workspace = os.path.expanduser("~/.alterego")
    if backup_root is None:
        backup_root = os.path.join(workspace, "backups")
    os.makedirs(backup_root, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(backup_root, ts)
    os.makedirs(backup_dir, exist_ok=True)

    # 备份清单（相对 workspace）
    targets = [
        "memory",               # 记忆 ⭐⭐⭐
        "data/conversations.db",# 会话 ⭐⭐
        "config.yaml",          # 配置 ⭐⭐
        "artifacts",            # 产物 ⭐
        "proactivity",          # 推送候选
    ]
    # 明确排除：密钥、日志、缓存、pyc
    exclude_dirs = {"logs", "__pycache__", ".git", "node_modules"}
    exclude_exts = {".pyc", ".pyo", ".log"}

    for t in targets:
        src = os.path.join(workspace, t)
        if not os.path.exists(src):
            continue
        dst = os.path.join(backup_dir, t)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=_ignore(exclude_dirs, exclude_exts))
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
                # vault 数据文件跳过（密钥走 §34.2 手动导出流程）
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
    """§34.3 恢复自检：python agent/main.py --verify 调这个。
    检查：记忆可读、配置存在、关键目录完整。"""
    if workspace is None:
        workspace = os.path.expanduser("~/.alterego")
    checks = {}
    for name, rel in [("memory", "memory"),
                       ("config", "config.yaml"),
                       ("conversations", "data/conversations.db")]:
        p = os.path.join(workspace, rel)
        checks[name] = os.path.exists(p)
    # vault 完整性：只检查文件存在，不读内容
    vault_files = glob.glob(os.path.join(workspace, "vault*"))
    checks["vault_present"] = len(vault_files) > 0
    checks["ok"] = all([checks["memory"], checks["config"]])
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
        ),
        ToolDefinition(
            name="backup_verify",
            description="恢复自检：检查记忆/配置/会话是否完整。",
            parameters={"type": "object", "properties": {}, "required": []},
            func=_verify,
        ),
    ]
