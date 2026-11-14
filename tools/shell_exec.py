"""shell_exec：白名单命令执行。"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys

from agent.tools_registry import ToolDefinition

# 白名单：只允许这些命令的 argv[0]。Windows 下同样适用（where/python/pip 等）。
ALLOWED = {
    "python", "python3", "pip", "pip3", "pytest", "uv", "uvx",
    "git", "node", "npm",
    "dir", "echo", "type", "where", "set", "ver",
    "ls", "cat", "pwd", "which", "env",
    "tasklist", "taskkill", "findstr", "start", "whoami", "curl", "hostname",
    "ping", "ipconfig", "netstat", "systeminfo", "explorer",
}

# 危险模式：参数里出现直接拒绝（防 rm -rf /、格式化磁盘、探测本地私有调试端口等）
BLOCKED_SUBSTRINGS = (
    "rm -rf /", "rm -rf ~", ":(){:|:&};:",
    "format ", "mkfs", "dd if=",
    "powershell -enc", "powershell -EncodedCommand",
    "127.0.0.1:9222", "localhost:9222", ":9222",
)

# 命令串联/重定向元字符：shell=False 下不会被解释，但仍按纵深防御拒绝
_CMD_META = ("&", "|", "<", ">", "^", ";", "`", "\n", "\r")
_URL_TOKEN_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://\S+$")


def _is_url_token(token: str) -> bool:
    """token 是否为完整 URL（shlex 非 posix 模式会保留外层引号）。"""
    text = token.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    return bool(_URL_TOKEN_RE.match(text))


def validate_command(command: str) -> str | None:
    """纯校验：返回拒绝原因，None 表示可执行。

    与执行解耦，供 ToolRegistry 在人工审批前调用：无效命令不再让用户白点授权。
    """
    for bad in BLOCKED_SUBSTRINGS:
        if bad in command:
            return f"[拒绝] 命令含危险模式: {bad}"
    posix_mode = sys.platform != "win32"
    try:
        argv = shlex.split(command, posix=posix_mode)
    except ValueError as exc:
        return f"[拒绝] 命令解析失败: {exc}"
    if not argv or argv[0].lower() not in ALLOWED:
        # v2.6：拒绝时回带白名单清单，杜绝模型对同一不可用命令反复盲试
        return (f"[拒绝] 不在白名单: {argv[0] if argv else '(空)'}"
                f"（可用命令: {', '.join(sorted(ALLOWED))}。"
                "重复提交白名单外的命令只会浪费步数，请改用白名单内等价命令）")

    # 严格校验所有 token，严防通过 &、|、^、; 等元字符绕过白名单进行命令链式注入。
    # curl 的 URL 查询参数允许 &（shell=False，不存在命令串联），其余元字符仍拒绝。
    allow_url_amp = argv[0].lower() == "curl"
    for token in argv:
        bad = [m for m in _CMD_META if m in token]
        if not bad:
            continue
        if allow_url_amp and set(bad) <= {"&"} and _is_url_token(token):
            continue
        return (f"[拒绝] 命令包含禁止的元字符或命令串联符: {token}。"
                "不支持 cd、管道 |、重定向 >、&&、;、`。"
                "多步骤任务请用 file_write 写 Python 脚本，再执行 python <脚本路径>；"
                "curl 的 URL 查询参数请整体加引号（URL 中的 & 允许保留）。")
    return None


def _precheck(args: dict) -> str | None:
    return validate_command(str(args.get("command", "")))


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
    from agent.paths import resolve_data_dir
    from vault.manager import Vault
    data_dir = resolve_data_dir()
    return Vault.for_machine(data_dir)


def _run(args: dict) -> str:
    command: str = args.get("command", "")
    timeout: int = int(args.get("timeout", 30))
    extra_env: dict[str, str] = {}
    vault = None
    if "vault://" in command:
        try:
            vault = _current_vault()
            def _sub_vault_env(m):
                k = m.group(1)
                secret = vault.resolve(k, caller="shell_exec")
                env_key = f"ALTEREGO_VAULT_{re.sub(r'[^A-Za-z0-9_]', '_', k.upper())}"
                extra_env[env_key] = secret
                if sys.platform == "win32":
                    return f"%{env_key}%"
                return f"${env_key}"
            command = re.sub(r"vault://([\w\.\-]+)", _sub_vault_env, command)
        except Exception as e:
            return f"[拒绝] Vault 解析密钥失败: {e}"

    pre_err = validate_command(command)
    if pre_err:
        return pre_err
    posix_mode = sys.platform != "win32"
    argv = shlex.split(command, posix=posix_mode)

    is_win = sys.platform == "win32"
    WIN_CMD_BUILTINS = {"dir", "echo", "type", "set", "start", "ver"}
    cmd_to_run = argv
    if is_win and argv[0].lower() in WIN_CMD_BUILTINS:
        cmd_to_run = ["cmd", "/d", "/s", "/c", *argv]

    import os
    sub_env = os.environ.copy()
    if extra_env:
        sub_env.update(extra_env)

    try:
        p = subprocess.run(cmd_to_run, capture_output=True,
                           shell=False,
                           env=sub_env,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"[超时] {timeout}s 未结束，已终止"
    except FileNotFoundError:
        return f"[启动失败] 系统找不到命令或可执行文件: {argv[0]}"
    out = _decode_stream(p.stdout)[-4000:]
    err = _decode_stream(p.stderr)[-2000:]
    result_text = f"exit={p.returncode}\n{out}\n{err}".strip()
    from vault.manager import Vault
    if vault is not None:
        return vault.scrub(result_text)
    return Vault.scrub_text(result_text)


SHELL_TOOL = ToolDefinition(
    name="shell_exec",
    description=("执行白名单内的 shell 命令，返回 stdout+stderr。"
                 "不支持 cd、管道 |、重定向 >、&&、;；多步任务先用 file_write 写 Python 脚本，"
                 "再执行 python <脚本路径>。curl 的 URL 查询参数请整体加引号。"),
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
    precheck=_precheck,
)
# [M2/M4 完成] shell_exec vault:// 密钥解析（§11）与 parallel_safe=False（§5.5）均已闭环实现。
