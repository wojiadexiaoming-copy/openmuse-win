import time
import pytest
from vault.manager import Vault, _MigrationFileLock
from agent.config_schema import Config
from agent.session_store import SessionStore
from agent.approval import ApprovalManager
from agent.models import ImagePart, _B64_CACHE


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


def test_edge05_recursive_key_sorting():
    m = ApprovalManager(":memory:")
    args1 = {"b": 2, "a": {"nested_y": 20, "nested_x": 10}}
    args2 = {"a": {"nested_x": 10, "nested_y": 20}, "b": 2}
    k1 = m._wkey("file_write", args1)
    k2 = m._wkey("file_write", args2)
    assert k1 == k2


def test_perf01_image_base64_caching():
    raw = b"GIF89a" + b"\x00" * 30000
    p1 = ImagePart(data=raw)
    t0 = time.time()
    p2 = ImagePart(data=raw)
    dt = (time.time() - t0) * 1000
    assert dt < 5.0
    assert p1.data_base64 == p2.data_base64


def test_edge06_migration_file_lock(tmp_path):
    lock_file = tmp_path / ".migration.lock"
    with _MigrationFileLock(lock_file):
        assert lock_file.exists()
