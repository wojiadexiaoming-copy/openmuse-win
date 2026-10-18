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
