from copy import deepcopy
import pytest
import yaml
from agent.provider_config import (
    PROVIDER_ENV, PROVIDER_HOST, import_provider_secrets, load_provider_config,
    parse_provider_secrets, validate_provider_plan,
)
def sample():
    return {"providers": {p: {"base_url": "https://" + PROVIDER_HOST[p] + "/v1", "api_key_env": env}
                          for p, env in PROVIDER_ENV.items()},
            "roles": {role: {"chain": ["sample@nvidia"]} for role in
                      ("main_brain", "sub_agent", "code", "vision", "memory_embedding")}}

def test_literal_import_to_provider_owned_vault_names_only():
    saved = {}
    vault = type("FakeVault", (), {"set": lambda _, key, value: saved.update({key: value})})()
    text = "\n".join("export " + env + "='test-key-" + p + "'" for p, env in PROVIDER_ENV.items())
    assert import_provider_secrets(text, vault) == list(PROVIDER_ENV)
    assert saved == {p + "_api_key": "test-key-" + p for p in PROVIDER_ENV}

@pytest.mark.parametrize("value", ["$(whoami)", "abc;rm-x", "abc && echo", "$"+"{OTHER}", "abc"+chr(96)+"cmd"+chr(96), "'unclosed"])
def test_secret_parser_rejects_shell_syntax_without_echoing_value(value):
    with pytest.raises(ValueError) as exc:
        parse_provider_secrets("NVIDIA_API_KEY=" + value)
    assert value not in str(exc.value)

def test_duplicate_or_unknown_secret_and_missing_values_fail_before_writes():
    for text in ("NVIDIA_API_KEY=one\nNVIDIA_API_KEY=two", "OTHER_KEY=secret"):
        with pytest.raises(ValueError):
            parse_provider_secrets(text)
    saved = []
    vault = type("FakeVault", (), {"set": lambda *args: saved.append(args)})()
    with pytest.raises(ValueError):
        import_provider_secrets("NVIDIA_API_KEY=only", vault)
    assert saved == []

def test_load_file_relative_to_config_path_and_preserve_source(tmp_path, monkeypatch):
    plan = sample()
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "providers.yaml").write_text(yaml.safe_dump(plan), encoding="utf-8")
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = {"llm": {}, "llm_router": {"enabled": True, "providers_file": "providers.yaml",
                                    "options": {"timeout_seconds": 12}}}
    before = deepcopy(cfg)
    loaded = load_provider_config(cfg, folder / "config.yaml")
    assert loaded["llm_router"]["providers"] == plan["providers"]
    assert loaded["llm_router"]["options"]["timeout_seconds"] == 12
    assert cfg == before

@pytest.mark.parametrize("url", ["http://integrate.api.nvidia.com/v1", "https://integrate.api.nvidia.com.evil/v1",
                                 "https://user:pw@integrate.api.nvidia.com/v1", "https://integrate.api.nvidia.com/v1?token=x"])
def test_provider_endpoint_cannot_redirect_owned_credentials(url):
    plan = sample()
    plan["providers"]["nvidia"]["base_url"] = url
    with pytest.raises(ValueError):
        validate_provider_plan(plan)

def test_inline_keys_and_unknown_role_provider_rejected():
    plan = sample()
    plan["providers"]["nvidia"]["api_key"] = "never-show-me"
    with pytest.raises(ValueError) as exc:
        validate_provider_plan(plan)
    assert "never-show-me" not in str(exc.value)
    plan = sample()
    plan["roles"]["code"]["chain"] = ["model@unknown"]
    with pytest.raises(ValueError):
        validate_provider_plan(plan)

def test_legacy_disabled_config_not_read_or_changed(tmp_path):
    cfg = {"llm_router": {"enabled": False, "providers_file": "absent.yaml"}}
    assert load_provider_config(cfg, tmp_path / "config.yaml") is cfg

def test_all_provider_keys_are_allowlisted_and_scrubbed(tmp_path):
    from vault import Vault
    vault = Vault(tmp_path / "vault")
    for p in PROVIDER_ENV:
        key = p + "_api_key"
        secret = "synthetic-credential-" + p
        vault.set(key, secret)
        assert vault.resolve(key, caller="offline-test") == secret
        assert secret not in vault.scrub("content " + secret)

def test_vault_provider_resolution_from_worker_threads_is_audited(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from vault import Vault
    vault = Vault(tmp_path / "vault")
    for provider in PROVIDER_ENV:
        vault.set(provider + "_api_key", "synthetic-threaded-" + provider)
    def read(index):
        provider = list(PROVIDER_ENV)[index % 4]
        secret = vault.resolve(provider + "_api_key", caller="worker-thread")
        assert secret not in vault.scrub(secret)
        return secret == "synthetic-threaded-" + provider
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(read, range(32)))
    assert len(vault.recent_accesses(100)) == 32
