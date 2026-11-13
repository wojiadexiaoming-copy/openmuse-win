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
import hmac
import logging
import secrets
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

logger = logging.getLogger(__name__)

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
    if blob.startswith(b"AEV1") and len(blob) >= 36:
        blob = blob[36:]
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


class _MigrationFileLock:
    """EDGE-06: 针对并发多实例启动时的跨进程文件互斥锁，杜绝迁移竞态。"""
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fd = None

    def __enter__(self):
        # SEC-11：获取失败必须重试后放弃，严禁无锁继续执行迁移。
        # 旧实现失败仅 sleep 0.3s 后照常返回 self，EDGE-06 想防的并发竞态防不住。
        last_exc = None
        for _ in range(10):  # 共 ~3s
            try:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except Exception as exc:
                last_exc = exc
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except Exception:
                        pass
                    self._fd = None
                time.sleep(0.3)
        raise TimeoutError(f"Vault 迁移锁获取超时（其他实例持有中）: {self.lock_path}") from last_exc

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None


def _migrate_legacy_vault(legacy: Path, scoped: Path, move: bool = False) -> None:
    """旧目录 / 上级机器目录 → 机器专属目录的合并同步。

    逐一探测每个 .bin 文件，若本机 DPAPI 可解密且目标目录缺失，则同步拷贝/搬迁过去。
    """
    if not legacy.is_dir():
        return
    try:
        with _MigrationFileLock(scoped / ".migration.lock"):
            _do_migrate_legacy_vault(legacy, scoped, move=move)
    except TimeoutError as exc:
        # 迁移是锦上添花：锁被其他实例持有时跳过本轮，缺失的密钥仍可由
        # _try_import_key_from_candidates 动态补全，不阻塞启动。
        logger.warning("[Vault] 跳过本轮密钥迁移（迁移锁被其他实例占用）: %s", exc)


def _do_migrate_legacy_vault(legacy: Path, scoped: Path, move: bool = False) -> None:
    bins = sorted(legacy.glob("*.bin"))
    if not bins:
        return
    scoped.mkdir(parents=True, exist_ok=True)
    import shutil
    for src in bins:
        target = scoped / src.name
        if target.exists():
            continue
        try:
            raw = src.read_bytes()
            if _dpapi_decryptable(raw):
                if move:
                    src.replace(target)
                else:
                    shutil.copy2(src, target)
        except Exception:
            pass
    integrity = legacy / ".integrity_key"
    if integrity.is_file() and not (scoped / ".integrity_key").exists():
        try:
            if move:
                integrity.replace(scoped / ".integrity_key")
            else:
                shutil.copy2(integrity, scoped / ".integrity_key")
        except Exception:
            pass
    audit = legacy / "audit.db"
    if audit.is_file() and not (scoped / "audit.db").exists():
        try:
            if move:
                audit.replace(scoped / "audit.db")
            else:
                shutil.copy2(audit, scoped / "audit.db")
        except Exception:
            pass


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
        "echobird_api_key",   # 本地代理 (EchoBird / Codex-LB / 127.0.0.1:53683) 专属密钥
        "anthropic_api_key",  # Anthropic 官方 API 别名
        "claude_api_key",     # Claude API 别名
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
        tag = _machine_tag()
        scoped = base / f"vault-{tag}"
        scoped.mkdir(parents=True, exist_ok=True)
        # 扫描并同步本机可能存在的各旧目录 / 上级共享目录中的密钥
        # 1. 对 base / "vault" 执行标准搬迁 (move=True)
        if (base / "vault").is_dir() and (base / "vault").resolve() != scoped.resolve():
            _migrate_legacy_vault(base / "vault", scoped, move=True)
        # 2. 对其他机器/上级共享目录执行安全复制同步 (move=False)
        for c in [base.parent / f"vault-{tag}", base.parent / "vault"]:
            try:
                if c.is_dir() and c.resolve() != scoped.resolve():
                    _migrate_legacy_vault(c, scoped, move=False)
            except Exception:
                pass
        return cls(scoped)

    def _try_import_key_from_candidates(self, key: str) -> bool:
        """尝试从上级/同级机器目录中动态补全缺失的密钥。"""
        tag = _machine_tag()
        candidates = [
            self.dir.parent / "vault",
            self.dir.parent.parent / f"vault-{tag}",
            self.dir.parent.parent / "vault",
        ]
        import shutil
        for c in candidates:
            try:
                src = c / f"{key}.bin"
                if src.is_file():
                    raw = src.read_bytes()
                    if _dpapi_decryptable(raw):
                        cand_integrity = c / ".integrity_key"
                        if cand_integrity.is_file() and not (self.dir / ".integrity_key").exists():
                            try:
                                shutil.copy2(cand_integrity, self.dir / ".integrity_key")
                            except Exception:
                                pass
                        target = self.dir / f"{key}.bin"
                        shutil.copy2(src, target)
                        return True
            except Exception:
                pass
        return False

    # ---------- 存取 ----------


    def _get_integrity_key(self) -> bytes:
        """获取或生成用于 Vault HMAC-SHA256 完整性签名的对称密钥。"""
        if getattr(self, "_integrity_key", None):
            return self._integrity_key
        if not self._is_windows:
            pw = os.environ.get("ALTEREGO_VAULT_PW", "dev-only").encode("utf-8")
            self._integrity_key = hashlib.sha256(pw + b":integrity-salt").digest()
            return self._integrity_key

        key_file = self.dir / ".integrity_key"
        if key_file.exists():
            try:
                raw = key_file.read_bytes()
                decrypted = self._dpapi_unprotect(raw)
                if len(decrypted) == 32:
                    self._integrity_key = decrypted
                    return self._integrity_key
            except Exception as exc:
                logger.warning("[Vault] 读取现有 integrity key 异常: %s", exc)

        new_key = secrets.token_bytes(32)
        try:
            encrypted = self._dpapi_protect(new_key)
            key_file.write_bytes(encrypted)
        except Exception as exc:
            logger.warning("[Vault] 持久化 integrity key 异常: %s", exc)
        self._integrity_key = new_key
        return self._integrity_key

    def _sign_blob(self, ciphertext: bytes) -> bytes:
        """为密文数据计算并附加 HMAC-SHA256 签名（前缀 b'AEV1' + 32字节摘要 + 密文）。"""
        key = self._get_integrity_key()
        mac = hmac.new(key, ciphertext, hashlib.sha256).digest()
        return b"AEV1" + mac + ciphertext

    def _verify_and_strip_blob(self, key_name: str, blob: bytes) -> bytes:
        """校验密文 HMAC 完整性；若被篡改则报错，通过后剥离签名返回纯密文。"""
        if blob.startswith(b"AEV1") and len(blob) >= 36:
            mac = blob[4:36]
            ciphertext = blob[36:]
            key = self._get_integrity_key()
            expected_mac = hmac.new(key, ciphertext, hashlib.sha256).digest()
            if not hmac.compare_digest(mac, expected_mac):
                logger.error("[Vault] 密钥 [%s] 完整性校验失败：文件内容已被篡改或损坏", key_name)
                raise ValueError(f"Vault 完整性校验失败（数据被篡改或损坏）: {key_name}")
            return ciphertext
        # 兼容旧版本未带 AEV1 签名的裸密文
        return blob

    @_vault_locked
    def set(self, key: str, secret: str):
        """用户填表/安全通道录入。直接写密文文件，不记明文日志。"""
        if key not in self.ALLOWLIST:
            raise ValueError(f"key 不在白名单，拒绝录入: {key}")
        ciphertext = self._protect(secret.encode("utf-8"))
        signed_blob = self._sign_blob(ciphertext)
        (self.dir / f"{key}.bin").write_bytes(signed_blob)
        self._mem.pop(key, None)

    @_vault_locked
    def has(self, key: str) -> bool:
        """检查指定 key 是否已在 vault 中持久化存在。"""
        if (self.dir / f"{key}.bin").exists() or key in self._mem:
            return True
        return self._try_import_key_from_candidates(key)

    @_vault_locked
    def resolve(self, key: str, caller: str = "?") -> str:
        """取密钥。每次调用都审计；不在白名单直接拒绝。"""
        import re
        if not isinstance(key, str) or not re.match(r"^[a-zA-Z0-9_.-]+$", key):
            raise ValueError(f"vault.resolve 拒绝：非法 key 格式: {key!r}")
        safe_caller = caller if (isinstance(caller, str) and re.match(r"^[a-zA-Z0-9_.:?-]+$", caller)) else "unknown"
        ok = key in self.ALLOWLIST
        self._audit.execute(
            "INSERT INTO access_log VALUES (?,?,?,?)",
            (time.time(), key, safe_caller, int(ok)))
        self._audit.commit()
        if not ok:
            raise PermissionError(f"vault.resolve 拒绝：{key} 不在白名单")
        if key not in self._mem:
            p = self.dir / f"{key}.bin"
            if not p.exists():
                if not self._try_import_key_from_candidates(key):
                    raise KeyError(f"vault 无此密钥（需用户录入）: {key}")
            try:
                raw_bytes = p.read_bytes()
                ciphertext = self._verify_and_strip_blob(key, raw_bytes)
                self._mem[key] = self._unprotect(ciphertext).decode("utf-8")
            except ValueError as exc:
                if "完整性校验失败" in str(exc):
                    raise
                raise KeyError(f"vault 密文解析失败: {key}") from exc
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

    @classmethod
    def scrub_text(cls, text: str) -> str:
        """无状态的文本通用脱敏，覆盖常见云服务与模型凭据特征（B3-07）。"""
        if not text:
            return text
        # 常见键值对形：api_key=xxx, secret: xxx, token=xxx
        text = re.sub(r"(?i)(api[_-]?key|secret|token|password|auth|authorization)\s*[:=]\s*['\"]?[\w\-\.]{12,}['\"]?",
                      r"\1=[REDACTED]", text)
        # 常见前缀凭证
        text = re.sub(r"(?i)\bbearer\s+['\"]?[A-Za-z0-9_\-\.]{16,}['\"]?", "Bearer [REDACTED]", text)
        text = re.sub(r"\bgithub_pat_[\w]+", "[REDACTED]", text)
        text = re.sub(r"\bgh[pousr]_[\w]{20,}", "[REDACTED]", text)
        text = re.sub(r"\bsk-[\w\-]{16,}", "[REDACTED]", text)
        text = re.sub(r"\bnvapi-[\w\-]{16,}", "[REDACTED]", text)
        text = re.sub(r"\bms-[a-zA-Z0-9\-]{20,}", "[REDACTED]", text)
        text = re.sub(r"\bAIza[0-9A-Za-z\-_]{20,}", "[REDACTED]", text)
        text = re.sub(r"\bAQ\.[A-Za-z0-9\-_]{20,}", "[REDACTED]", text)
        text = re.sub(r"\bcfat_[\w\-]{16,}", "[REDACTED]", text)
        # 智谱 BigModel：{32位hex id}.{16位secret} 形状（SEC-12，此前发布脚本
        # 被迫硬编码真实 key 前缀堵洞，正是本缺口的反证）
        text = re.sub(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b", "[REDACTED]", text)
        return text

    @_vault_locked
    def scrub(self, text: str) -> str:
        """所有进 LLM / 日志 / UI 的文本先过这里。

        策略：已知的密钥明文（内存里的）直接替换；再用通用正则扫常见密钥形状。
        """
        if not text:
            return text
        for secret in self._mem.values():
            if secret and len(secret) >= 8 and secret in text:
                text = text.replace(secret, "[REDACTED]")
        return self.scrub_text(text)

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
