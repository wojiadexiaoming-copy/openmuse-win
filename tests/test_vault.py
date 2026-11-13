"""M2/M4 测试：Vault。"""
import sys

import pytest

from vault import Vault


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault")


def test_allowlist_blocks_unknown_key(vault):
    with pytest.raises(ValueError):
        vault.set("evil_key", "x")


def test_resolve_audit_and_ref(vault):
    vault.set("llm_api_key", "secret-12345678")
    assert vault.resolve("llm_api_key", caller="test") == "secret-12345678"
    assert vault.ref("llm_api_key") == "vault://llm_api_key"
    logs = vault.recent_accesses()
    assert logs[0]["key"] == "llm_api_key" and logs[0]["ok"] is True


def test_allowlist_permits_backup_and_media_keys(vault):
    vault.set("backup_llm_api_key", "backup-secret-123")
    vault.set("media_api_key", "media-secret-123")
    assert vault.resolve("backup_llm_api_key", caller="test") == "backup-secret-123"
    assert vault.resolve("media_api_key", caller="test") == "media-secret-123"


def test_resolve_unknown_key_rejected(vault):
    with pytest.raises(PermissionError):
        vault.resolve("nope", caller="test")
    logs = vault.recent_accesses()
    assert logs[0]["ok"] is False  # 拒绝也审计


def test_scrub_redacts_secret(vault):
    vault.set("llm_api_key", "secret-12345678")
    vault.resolve("llm_api_key", caller="t")
    text = vault.scrub("key=secret-12345678 and github_pat_abc123 and sk-xyz987654321abcdefgh")
    assert "secret-12345678" not in text
    assert "github_pat_abc123" not in text
    assert "sk-xyz987654321abcdefgh" not in text
    assert "[REDACTED]" in text


def test_scrub_redacts_modelscope_agnes_and_zhipu_shapes(vault):
    """SEC-12：scrub_text 必须覆盖 ModelScope ms- 前缀、Agnes AQ. 前缀与智谱 id.secret 形状。"""
    raw = ("nvidia nvapi-KqXx25charTokenAbcdefg123 ms-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee "
           "agnes AQ.aaaaBBBBccccddddEEEE1111AAAA2222 "
           "zhipu 0123456789abcdef0123456789abcdef.Abcdef1234567890")
    scrubbed = vault.scrub(raw)
    assert "ms-aaaaaaaa" not in scrubbed
    assert "AQ.aaaaBBBBccccdddd" not in scrubbed
    assert "0123456789abcdef0123456789abcdef.Abcdef1234567890" not in scrubbed
    assert scrubbed.count("[REDACTED]") >= 3


# ---- 机器级目录（2026-10-17 共享文件夹双机方案）----

def test_for_machine_scopes_vault_dir(tmp_path):
    v = Vault.for_machine(tmp_path)
    if sys.platform == "win32":
        assert v.dir.parent == tmp_path
        assert v.dir.name.startswith("vault-")
    else:
        assert v.dir == tmp_path / "vault"


def test_for_machine_adopts_legacy_vault_owned_by_this_machine(tmp_path):
    legacy = Vault(tmp_path / "vault")
    legacy.set("llm_api_key", "secret-12345678")
    v = Vault.for_machine(tmp_path)
    # 能在本机解密的老 vault/ 会被搬进机器专属目录，密钥照常可用、旧文件不残留
    assert v.resolve("llm_api_key", caller="test") == "secret-12345678"
    if sys.platform == "win32":
        assert v.dir != tmp_path / "vault"
        assert not (tmp_path / "vault" / "llm_api_key.bin").exists()


def test_resolve_foreign_blob_reports_as_missing(tmp_path):
    """别的机器写的 DPAPI 密文（或损坏的密文）应报 KeyError，而不是把 WinError 抛上去。"""
    if sys.platform != "win32":
        pytest.skip("DPAPI 专用行为")
    v = Vault(tmp_path / "vault")
    (v.dir / "llm_api_key.bin").write_bytes(b"\x01\x02garbage-not-a-real-dpapi-blob")
    with pytest.raises(KeyError):
        v.resolve("llm_api_key", caller="test")


def test_vault_hmac_integrity_and_tampering_detection(vault):
    """P0-7: 验证 HMAC-SHA256 签名，并在密文被外部篡改时拒绝解密并报错。"""
    vault.set("llm_api_key", "secret-integrity-test")
    # 正常读取应通过 HMAC 校验
    assert vault.resolve("llm_api_key", caller="test") == "secret-integrity-test"

    # 模拟外部离线篡改：修改 .bin 文件的密文部分（最后 1 个字节）
    p = vault.dir / "llm_api_key.bin"
    raw = bytearray(p.read_bytes())
    assert raw.startswith(b"AEV1")
    raw[-1] ^= 0xFF
    p.write_bytes(bytes(raw))
    vault._mem.clear()

    # 篡改后 resolve 必须直接抛出 ValueError 且包含完整性校验失败提示
    with pytest.raises(ValueError, match="完整性校验失败"):
        vault.resolve("llm_api_key", caller="test")
