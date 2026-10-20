import pytest
from vault.manager import Vault
from agent.config_schema import Config
from agent.session_store import SessionStore


def test_sec03_audit_log_injection_blocked(tmp_path):
    v = Vault(str(tmp_path))
    try:
        with pytest.raises(ValueError) as exc:
            v.resolve("test_key\nINSERT INTO access_log VALUES (1,2,3,4);--")
        assert "非法 key 格式" in str(exc.value)
    finally:
        v._audit.close()


def test_sec04_config_forbidden_keys_blocked():
    with pytest.raises(Exception) as exc:
        Config.model_validate({"llm": {}, "evil": {"__class__": "os.system"}})
    assert "非法魔术键名" in str(exc.value) or "__class__" in str(exc.value)


def test_edge03_session_messages_index_created(tmp_path):
    db_path = str(tmp_path / "test.db")
    store = SessionStore(db_path)
    try:
        rows = store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_messages_session_seq'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "idx_messages_session_seq"
    finally:
        store.db.close()
