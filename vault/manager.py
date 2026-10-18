"""§11 Vault：密钥真强制。

三件套（缺一不可）：
1. resolve() 白名单 —— 只有白名单里的 key 能被取用，其余拒绝
2. 审计日志 —— 每次取用记：时间、key、调用方（防偷钥）
3. scrub() —— 所有进 LLM / 日志 / UI 的文本先过消毒，密钥明文不出境

Windows：DPAPI（CryptProtectData）绑当前用户，存文件。
Linux/开发机：同接口的加密文件 fallback（明确标记非生产强度）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
import time
import threading
from functools import wraps
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]


def _machine_tag() -> str:
    """当前机器标识（COMPUTERNAME 优先），用作 vault 分目录后缀。"""
    raw = os.environ.get("COMPUTERNAME") or platform.node() or "local"
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", raw).strip("._")
    return tag or "local"


def _dpapi_decryptable(blob: bytes) -> bool:
    """本机能否解开这段 DPAPI 密文（用来判断老 vault/ 属不属于本机）。"""
    if sys.platform != "win32" or not blob:
        return False
    in_blob = _DATA_BLOB(
        len(blob),
        ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_byte)),
    )
    out_blob = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        return False
    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return True


def _migrate_legacy_vault(legacy: Path, scoped: Path) -> None:
    """老式共享 vault/ → 机器专属目录的迁移。

    只有「其中所有 .bin 都能在本机解密」才搬（说明是本机写的老目录）；
    否则说明老目录属于另一台机器，原样保留，不碰。
    """
    if not legacy.is_dir():
        return
    bins = sorted(legacy.glob("*.bin"))
    if not bins:
        return
    if not all(_dpapi_decryptable(p.read_bytes()) for p in bins):
        return
    scoped.mkdir(parents=True, exist_ok=True)
    for src in bins:
        try:
            src.replace(scoped / src.name)
        except OSError:
            pass
    audit = legacy / "audit.db"
    if audit.is_file():
        try:
            audit.replace(scoped / "audit.db")
        except OSError:
            pass  # 被旧连接占用时留原地，无伤大雅


def _vault_locked(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded

class Vault:
    # 白名单：只有这些 key 允许被 resolve()。加 key 必须改这里 + 写理由。
    ALLOWLIST = {
        "llm_api_key",        # 主 LLM 网关密钥 (如 Cloudflare Workers AI / DeepSeek)
        "backup_llm_api_key", # 备选 LLM 网关密钥 (如 Agnes-3.0-flash)
        "gemini_api_key",     # Gemini provider only
        "planner_api_key",    # Planner 规划器专属密钥
        "subagent_api_key",   # 子 Agent 专属密钥
        "worker_api_key",     # 后台 Worker 专属密钥
        "media_api_key",      # 多模态媒体模型密钥 (Agnes Image / Video)
        "search_api_key",     # 搜索 API
        "nvidia_api_key",     # NVIDIA provider only; never reused across hosts
        "modelscope_api_key", # ModelScope API-Inference only
        "zhipu_api_key",      # BigModel provider only
        "agnes_api_key",      # Agnes provider only
        "dashscope_api_key",  # 阿里云百炼 / DashScope API (kimi-k3 / qwen3.8-*)
        "bailian_api_key",    # 阿里云百炼 API 别名
        "deepseek_api_key",   # DeepSeek 官方 API 别名
        "openai_api_key",     # OpenAI 兼容 API 别名
    }

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._audit = sqlite3.connect(str(self.dir / "audit.db"), check_same_thread=False)
        self._audit.execute(
            "CREATE TABLE IF NOT EXISTS access_log"
            "(ts REAL, key TEXT, caller TEXT, ok INTEGER)")
        # 内存缓存：明文只活在内存，进程退出即丢
        self._mem: dict[str, str] = {}
        self._is_windows = sys.platform == "win32"

    # ---------- 机器级目录（共享文件夹 + DPAPI） ----------

    @classmethod
    def for_machine(cls, data_dir: str | Path) -> "Vault":
        """返回当前机器专属的 Vault。

        背景（2026-10-17 双机实测）：项目目录在宿主机和 Hyper-V 虚拟机之间
        共享，而 Windows DPAPI 密文绑定「当前用户 + 当前机器」——另一台机器
        写出的 .bin 在本机永远解不开（CryptUnprotectData 直接失败，未捕获时
        会崩启动）。因此密钥文件落在 <data_dir>/vault-<COMPUTERNAME>/，每台
        机器一份；老式共享 vault/ 能在本机解密时自动迁移（见
        _migrate_legacy_vault），用户无感。

        Linux 的 fallback 加密与机器无关（派生自 ALTEREGO_VAULT_PW），不拆分。
        """
        base = Path(data_dir)
        if sys.platform != "win32":
            return cls(base / "vault")
        scoped = base / f"vault-{_machine_tag()}"
        if not scoped.exists():
            _migrate_legacy_vault(base / "vault", scoped)
        return cls(scoped)

    # ---------- 存取 ----------

    @_vault_locked
    def set(self, key: str, secret: str):
        """用户填表/安全通道录入。直接写密文文件，不记明文日志。"""
        if key not in self.ALLOWLIST:
            raise ValueError(f"key 不在白名单，拒绝录入: {key}")
        blob = self._protect(secret.encode("utf-8"))
        (self.dir / f"{key}.bin").write_bytes(blob)
        self._mem.pop(key, None)

    @_vault_locked
    def has(self, key: str) -> bool:
        """检查指定 key 是否已在 vault 中持久化存在。"""
        return (self.dir / f"{key}.bin").exists() or key in self._mem

    @_vault_locked
    def resolve(self, key: str, caller: str = "?") -> str:
        """取密钥。每次调用都审计；不在白名单直接拒绝。"""
        ok = key in self.ALLOWLIST
        self._audit.execute(
            "INSERT INTO access_log VALUES (?,?,?,?)",
            (time.time(), key, caller, int(ok)))
        self._audit.commit()
        if not ok:
            raise PermissionError(f"vault.resolve 拒绝：{key} 不在白名单")
        if key not in self._mem:
            p = self.dir / f"{key}.bin"
            if not p.exists():
                raise KeyError(f"vault 无此密钥（需用户录入）: {key}")
            try:
                self._mem[key] = self._unprotect(p.read_bytes()).decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # DPAPI 密文绑定「当前用户 + 当前机器」，跨机器/换用户后解不开。
                # 当成“本机没有这个 key”处理，让调用方走环境变量/重新录入的兜底路径，
                # 而不是把 WinError 抛上去崩启动（2026-10-17 双机实测）。
                raise KeyError(
                    f"vault 密文无法在本机解密（可能来自另一台机器或另一个 Windows 用户）: {key}"
                ) from exc
        return self._mem[key]

    @_vault_locked
    def ref(self, key: str) -> str:
        """给 LLM 的引用（不暴露明文）：vault://<key>。"""
        if key not in self.ALLOWLIST:
            raise PermissionError(f"vault.ref 拒绝：{key} 不在白名单")
        return f"vault://{key}"

    # ---------- 消毒 ----------

    @_vault_locked
    def scrub(self, text: str) -> str:
        """所有进 LLM / 日志 / UI 的文本先过这里。

        策略：已知的密钥明文（内存里的）直接替换；再用正则扫常见密钥形状。
        """
        if not text:
            return text
        for secret in self._mem.values():
            if secret and len(secret) >= 8 and secret in text:
                text = text.replace(secret, "[REDACTED]")
        # 常见形状兜底
        text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)['\"]?[\w\-]{16,}['\"]?",
                      r"\1[REDACTED]", text)
        text = re.sub(r"github_pat_[\w]+", "[REDACTED]", text)
        text = re.sub(r"sk-[\w\-]{16,}", "[REDACTED]", text)
        return text

    # ---------- 平台相关 ----------

    def _protect(self, data: bytes) -> bytes:
        if self._is_windows:
            return self._dpapi_protect(data)
        # [M2 完成] Windows 真机采用 DPAPI (CryptProtectData/CryptUnprotectData) 保护；Linux 环境采用密钥派生回退
        pw = os.environ.get("ALTEREGO_VAULT_PW", "dev-only").encode()
        pad = hashlib.sha256(pw).digest()
        return bytes(b ^ pad[i % len(pad)] for i, b in enumerate(data))


    def _unprotect(self, blob: bytes) -> bytes:
        if self._is_windows:
            return self._dpapi_unprotect(blob)
        return self._protect(blob)  # XOR 对称

    def _dpapi_protect(self, data: bytes) -> bytes:
        in_blob = _DATA_BLOB(
            len(data),
            ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_byte)),
        )
        out_blob = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob), "AlterEgo Secret", None, None, None, 0, ctypes.byref(out_blob)
        ):
            raise ctypes.WinError()
        buf = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        return buf

    def _dpapi_unprotect(self, blob: bytes) -> bytes:
        in_blob = _DATA_BLOB(
            len(blob),
            ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_byte)),
        )
        out_blob = _DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
        ):
            raise ctypes.WinError()
        buf = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        return buf

    # ---------- 审计查询 ----------

    @_vault_locked
    def recent_accesses(self, limit: int = 50) -> list[dict]:
        rows = self._audit.execute(
            "SELECT ts, key, caller, ok FROM access_log ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"ts": ts, "key": k, "caller": c, "ok": bool(ok)}
                for ts, k, c, ok in rows]
