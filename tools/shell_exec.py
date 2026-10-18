"""shell_exec：白名单命令执行。"""
from __future__ import annotations

import shlex
import subprocess
import sys

from agent.tools_registry import ToolDefinition

# 白名单：只允许这些命令的 argv[0]。Windows 下同样适用（where/python/pip 等）。
ALLOWED = {
    "python", "python3", "pip", "pip3",
    "git", "node", "npm",
    "dir", "echo", "type", "where", "set",
    "ls", "cat", "pwd", "which", "env",
    "tasklist", "findstr", "start", "whoami", "curl", "hostname",
}

# 危险模式：参数里出现直接拒绝（防 rm -rf /、格式化磁盘、探测本地私有调试端口等）
BLOCKED_SUBSTRINGS = (
    "rm -rf /", "rm -rf ~", ":(){:|:&};:",
    "format ", "mkfs", "dd if=",
    "powershell -enc", "powershell -EncodedCommand",
    "127.0.0.1:9222", "localhost:9222", ":9222",
)


def _decode_stream(raw: bytes | str | None) -> str:
    """智能解码子进程输出：优先 UTF-8，Windows 回退至 GBK/CP936，彻底杜绝菱形问号乱码。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    import locale
    for enc in ("gbk", "cp936", locale.getpreferredencoding(False), sys.getdefaultencoding()):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _current_vault():
    """与 UI/CLI 同一目录约定，按当前机器解析 vault。"""
    import os
    from pathlib import Path
    from vault.manager import Vault
    repo_root = Path(__file__).resolve().parent.parent
    data_dir = Path(os.environ.get("ALTEREGO_DATA", str(repo_root)))
    return Vault.for_machine(data_dir)


def _run(args: dict) -> str:
    command: str = args.get("command", "")
    timeout: int = int(args.get("timeout", 30))
    if "vault://" in command:
        try:
            import re
            vault = _current_vault()
            def _sub_vault(m):
                k = m.group(1)
                return vault.resolve(k, caller="shell_exec")
            command = re.sub(r"vault://([\w\.\-]+)", _sub_vault, command)
        except Exception as e:
            return f"[拒绝] Vault 解析密钥失败: {e}"

    for bad in BLOCKED_SUBSTRINGS:
        if bad in command:
            return f"[拒绝] 命令含危险模式: {bad}"
    posix_mode = sys.platform != "win32"
    argv = shlex.split(command, posix=posix_mode)
    if not argv or argv[0].lower() not in ALLOWED:
        return f"[拒绝] 不在白名单: {argv[0] if argv else '(空)'}"

    # 严格校验所有 token，严防通过 &、|、^、; 等元字符绕过白名单进行命令链式注入
    CMD_META = ("&", "|", "<", ">", "^", ";", "`", "\n", "\r")
    for token in argv:
        if any(m in token for m in CMD_META):
            return f"[拒绝] 命令包含禁止的元字符或命令串联符: {token}"

    is_win = sys.platform == "win32"
    WIN_CMD_BUILTINS = {"dir", "echo", "type", "set"}
    cmd_to_run = argv
    if is_win and argv[0].lower() in WIN_CMD_BUILTINS:
        cmd_to_run = ["cmd", "/d", "/s", "/c", *argv]

    try:
        p = subprocess.run(cmd_to_run, capture_output=True,
                           shell=False,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"[超时] {timeout}s 未结束，已终止"
    out = _decode_stream(p.stdout)[-4000:]
    err = _decode_stream(p.stderr)[-2000:]
    return f"exit={p.returncode}\n{out}\n{err}".strip()


SHELL_TOOL = ToolDefinition(
    name="shell_exec",
    description="执行白名单内的 shell 命令，返回 stdout+stderr。危险命令会被拒绝。",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "default": 30, "description": "超时秒数"},
        },
        "required": ["command"],
    },
    func=_run,
    parallel_safe=False,
    is_readonly=False,
    cacheable=False,
)
# [M2/M4 完成] shell_exec vault:// 密钥解析（§11）与 parallel_safe=False（§5.5）均已闭环实现。
