"""Batch 4: 数据生命周期与产品化自动化测试 (B4-01 ~ B4-06).

覆盖:
- B4-01: 启动恢复未完成任务与历史会话归档协同 (keep_session_id 保护最近会话)
- B4-02: 单实例进程互斥锁 (SingleInstanceLock) 并发互斥与安全释放
- B4-03: 数据目录隔离与旧数据平滑迁移 (resolve_data_dir, migrate_legacy_data)
- B4-04: 数据保留期清理 (Tracer.cleanup, prune_directory_by_mtime, run_retention_cleanup)
- B4-05: SQLite 在线备份 API, 安全恢复 (防 Zip Slip), 备份列表与自检
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import zipfile
import pytest

from agent.singleton import SingleInstanceLock
from agent.paths import resolve_data_dir, migrate_legacy_data
from agent.retention import prune_directory_by_mtime, run_retention_cleanup
from agent.tracing import Tracer
from agent.backup import (
    _backup_sqlite_db,
    backup_job,
    restore_backup,
    list_backups,
    verify_restore,
)
from agent.session_store import SessionStore
from agent.models import Message


class TestBatch4Lifecycle:
    def test_b4_01_startup_resume_keep_sid(self, tmp_path):
        """测试历史归档能够跳过当前/最近的 session_id，避免启动时清空正在运行或待恢复会话。"""
        db_path = str(tmp_path / "conversations.db")
        store = SessionStore(db_path)

        # 写入两个历史 session
        store.save_history("sess_old", [Message(role="user", content="old hello")])
        time.sleep(0.01)
        store.save_history("sess_new", [
            Message(role="user", content="download file"),
            Message(role="assistant", content="working...", metadata={"task_status": "incomplete"})
        ])

        archive_dir = str(tmp_path / "archive")
        archived = store.archive_old_sessions(archive_dir, keep_session_id="sess_new")

        # sess_old 被归档移出，sess_new 必须保留在主库
        assert "sess_old" in archived
        assert "sess_new" not in archived
        sessions = [s.get("session_id") or s.get("id") for s in store.list_sessions()]
        assert "sess_new" in sessions
        assert "sess_old" not in sessions
        assert store.get_incomplete_task("sess_new") is not None

    def test_b4_02_single_instance_lock(self, tmp_path):
        """测试 SingleInstanceLock 跨进程/实例互斥机制。"""
        lock_file = tmp_path / "app.lock"

        lock1 = SingleInstanceLock(lock_file)
        assert lock1.acquire() is True
        assert lock_file.exists()

        # 第二个实例尝试获取同一锁文件必须失败
        lock2 = SingleInstanceLock(lock_file)
        assert lock2.acquire() is False

        # lock1 释放后，lock2 应该可以获取
        lock1.release()
        assert lock2.acquire() is True
        lock2.release()

        # 上下文管理器用法测试
        with SingleInstanceLock(lock_file) as ctx_lock:
            assert ctx_lock is not None
            with pytest.raises(RuntimeError):
                with SingleInstanceLock(lock_file):
                    pass

    def test_b4_03_paths_and_migration(self, tmp_path, monkeypatch):
        """测试统一数据路径解析及旧版根目录数据自动迁移。"""
        custom_dir = tmp_path / "custom_data"
        resolved = resolve_data_dir(custom_dir)
        assert resolved == custom_dir.resolve()
        assert resolved.is_dir()

        # 测试环境变量覆盖
        env_dir = tmp_path / "env_data"
        monkeypatch.setenv("ALTEREGO_DATA", str(env_dir))
        assert resolve_data_dir(None) == env_dir.resolve()
        monkeypatch.delenv("ALTEREGO_DATA", raising=False)

        # 测试旧文件平滑迁移
        legacy_root = tmp_path / "legacy_root"
        legacy_root.mkdir()
        (legacy_root / "conversations.db").write_text("dummy db", encoding="utf-8")
        (legacy_root / "memory").mkdir()
        (legacy_root / "memory" / "core.json").write_text("{}", encoding="utf-8")

        target_dir = tmp_path / "migrated_data"
        migrate_legacy_data(legacy_root, target_dir)

        assert (target_dir / "conversations.db").exists()
        assert (target_dir / "memory" / "core.json").exists()

    def test_b4_04_retention_cleanup(self, tmp_path):
        """测试保留期过期清理机制 (文件清理与 Tracer 表行级清理)。"""
        # 1. 模拟旧文件与新文件
        test_dir = tmp_path / "logs"
        test_dir.mkdir()
        old_file = test_dir / "old.log"
        new_file = test_dir / "new.log"
        old_file.write_text("old", encoding="utf-8")
        new_file.write_text("new", encoding="utf-8")

        # 将 old_file 修改时间伪造成 20 天前
        past_time = time.time() - (20 * 86400)
        os.utime(str(old_file), (past_time, past_time))

        deleted = prune_directory_by_mtime(test_dir, keep_days=14, pattern="*.log")
        assert "old.log" in deleted
        assert not old_file.exists()
        assert new_file.exists()

        # 2. 测试 Tracer 表保留清理
        tracer_db = tmp_path / "spans.db"
        tracer = Tracer(str(tracer_db))
        # 插入过期与当前记录
        with tracer._lock:
            tracer.db.execute(
                "INSERT INTO spans (trace_id, span_id, name, started_at, status) VALUES (?,?,?,?,?)",
                ("t_old", "s_old", "old_span", past_time, "ok")
            )
            tracer.db.execute(
                "INSERT INTO spans (trace_id, span_id, name, started_at, status) VALUES (?,?,?,?,?)",
                ("t_new", "s_new", "new_span", time.time(), "ok")
            )
            tracer.db.execute(
                "INSERT INTO events (trace_id, event, created_at, data) VALUES (?,?,?,?)",
                ("t_old", "old_evt", past_time, "{}")
            )
            tracer.db.commit()

        clean_res = tracer.cleanup(days=14)
        assert clean_res["deleted_spans"] == 1
        assert clean_res["deleted_events"] == 1

        remaining_spans = tracer.db.execute("SELECT span_id FROM spans").fetchall()
        assert len(remaining_spans) == 1
        assert remaining_spans[0][0] == "s_new"
        tracer.close()

    def test_b4_05_backup_online_sqlite_and_safe_restore(self, tmp_path):
        """测试 SQLite 在线热备份 API 及防 Zip Slip 安全恢复。"""
        ws = tmp_path / "workspace"
        ws.mkdir()
        db_file = ws / "conversations.db"

        # 创建一个活跃的 SQLite 数据库并开启 WAL 模式
        conn = sqlite3.connect(str(db_file))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE test (id INT, val TEXT)")
        conn.execute("INSERT INTO test VALUES (1, 'hello'), (2, 'world')")
        conn.commit()

        # 1. 验证 _backup_sqlite_db
        dst_db = tmp_path / "backup.db"
        ok = _backup_sqlite_db(str(db_file), str(dst_db))
        assert ok is True
        assert dst_db.exists()

        # 验证备份内容一致性
        b_conn = sqlite3.connect(str(dst_db))
        rows = b_conn.execute("SELECT val FROM test ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["hello", "world"]
        b_conn.close()
        conn.close()

        # 2. 验证全量 backup_job
        (ws / "config.yaml").write_text("version: 1", encoding="utf-8")
        (ws / "memory").mkdir()
        (ws / "memory" / "facts.json").write_text('["test"]', encoding="utf-8")

        zip_p = backup_job(workspace=str(ws), backup_root=str(ws / "backups"))
        assert os.path.exists(zip_p)

        # 验证 list_backups
        b_list = list_backups(workspace=str(ws))
        assert len(b_list) >= 1
        assert b_list[0]["path"] == zip_p

        # 3. 验证安全恢复 restore_backup
        restore_dir = tmp_path / "restored"
        restored_files = restore_backup(zip_p, target_workspace=str(restore_dir))
        assert "conversations.db" in restored_files
        assert (restore_dir / "conversations.db").exists()
        assert (restore_dir / "memory" / "facts.json").exists()

        # 验证恢复自检 verify_restore
        v = verify_restore(workspace=str(restore_dir))
        assert v["conversations"] is True
        assert v["memory"] is True
        assert v["ok"] is True

        # 4. 验证防 Zip Slip 路径穿越防御
        malicious_zip = tmp_path / "evil.zip"
        with zipfile.ZipFile(malicious_zip, "w") as z:
            z.writestr("../../evil.txt", "payload")

        with pytest.raises(ValueError, match="Unsafe path traversal"):
            restore_backup(str(malicious_zip), target_workspace=str(restore_dir))

    def test_b4_06_server_backup_api(self, tmp_path, monkeypatch):
        """测试 ui/server.py 暴露的 /api/backup 接口 (GET/POST) 与 Token 鉴权。"""
        import json
        from ui.server import AgentServer

        monkeypatch.setenv("ALTEREGO_LLM_API_KEY", "sk-mock-auth-key")
        server = AgentServer(data_dir=tmp_path)

        # 1. 无 token 请求应被 401 拦截
        status_box = []
        def start_response(status, response_headers, exc_info=None):
            status_box.append(status)

        env_unauth = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/api/backup",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": io.StringIO(),
            "CONTENT_LENGTH": "0",
        }
        b"".join(server.app(env_unauth, start_response))
        assert status_box[0].startswith("401")

        # 2. 携带有效 token 请求 GET /api/backup
        status_box.clear()
        env_auth = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/api/backup",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": io.StringIO(),
            "CONTENT_LENGTH": "0",
            "HTTP_AUTHORIZATION": f"Bearer {server.api_token}",
        }
        resp_data = b"".join(server.app(env_auth, start_response))
        assert status_box[0].startswith("200")
        res_json = json.loads(resp_data.decode("utf-8"))
        assert "backups" in res_json
        assert isinstance(res_json["backups"], list)

        # 3. 携带 token 请求 POST /api/backup 触发备份
        status_box.clear()
        post_body = json.dumps({"action": "backup"}).encode("utf-8")
        env_post = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/backup",
            "wsgi.input": io.BytesIO(post_body),
            "wsgi.errors": io.StringIO(),
            "CONTENT_LENGTH": str(len(post_body)),
            "CONTENT_TYPE": "application/json",
            "HTTP_AUTHORIZATION": f"Bearer {server.api_token}",
        }
        resp_post = b"".join(server.app(env_post, start_response))
        assert status_box[0].startswith("200")
        post_json = json.loads(resp_post.decode("utf-8"))
        assert post_json.get("success") is True
        assert "backup_file" in post_json

