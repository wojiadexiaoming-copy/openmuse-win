"""§33 会话持久化：conversations.db。

M2：history 落盘 + 启动恢复。表结构简单，后续加全文检索再扩展。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid

from .models import ImagePart, Message, ToolCall


logger = logging.getLogger("alterego.session_store")


class SessionStore:
    def __init__(self, db_path: str):
        from agent.db import get_db
        self._lock = threading.Lock()
        with self._lock:
            self.db = get_db(db_path)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS sessions"
                "(session_id TEXT PRIMARY KEY, started_ts REAL, title TEXT)")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS messages"
                "(session_id TEXT, seq INTEGER, role TEXT, content TEXT,"
                " tool_calls TEXT, tool_call_id TEXT, name TEXT,"
                " created_at REAL, id TEXT, images TEXT DEFAULT '[]', metadata TEXT DEFAULT '{}',"
                " PRIMARY KEY (session_id, seq))")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq)")
            try:
                self.db.execute("ALTER TABLE messages ADD COLUMN created_at REAL")
            except sqlite3.OperationalError:
                pass
            try:
                self.db.execute("ALTER TABLE messages ADD COLUMN id TEXT")
            except sqlite3.OperationalError:
                pass
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
            if "images" not in columns:
                self.db.execute("ALTER TABLE messages ADD COLUMN images TEXT DEFAULT '[]'")
            if "metadata" not in columns:
                self.db.execute("ALTER TABLE messages ADD COLUMN metadata TEXT DEFAULT '{}'")
            self.db.commit()

    def new_session(self, title: str = "") -> str:
        sid = f"s-{uuid.uuid4().hex[:10]}"
        with self._lock:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?)",
                            (sid, time.time(), title))
            self.db.commit()
        return sid

    def save_history(self, session_id: str, history: list[Message]):
        """Atomically save a snapshot, writing only changed rows and columns."""
        try:
            self._save_history_internal(session_id, history)
        except Exception as e:
            logger.error("数据库写入失败 session_id=%s: %s", session_id, e, exc_info=True)
            raise

    def _save_history_internal(self, session_id: str, history: list[Message]):
        """Atomically save a snapshot, writing only changed rows and columns."""
        columns = ("role", "content", "tool_calls", "tool_call_id", "name",
                   "created_at", "id", "images", "metadata")
        with self._lock, self.db:
            # 确保 sessions 表中存在该 session_id 记录
            s_row = self.db.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if not s_row:
                title = history[0].content[:20] if (history and history[0].content) else ""
                self.db.execute("INSERT INTO sessions VALUES (?,?,?)", (session_id, time.time(), title))

            # Query existing lightweight metadata without loading entire image JSON blobs
            existing_summary = {
                row[0]: {
                    "role": row[1],
                    "content": row[2],
                    "tool_calls": row[3],
                    "tool_call_id": row[4],
                    "name": row[5],
                    "created_at": row[6],
                    "id": row[7],
                    "img_len": row[8],
                    "metadata": row[9],
                } for row in self.db.execute(
                    "SELECT seq, role, content, tool_calls, tool_call_id, name,"
                    " created_at, id, length(images), metadata FROM messages WHERE session_id=?",
                    (session_id,)).fetchall()
            }
            # Finish serialization before any mutation. A malformed tool/image/metadata
            # value must never leave a partly replaced checkpoint in the connection.
            serialized = []
            for i, m in enumerate(history):
                previous = existing_summary.get(i)
                prev_ts = previous["created_at"] if previous else None
                prev_id = previous["id"] if previous else None
                c_ts = getattr(m, "created_at", None) or prev_ts or time.time()
                mid = getattr(m, "id", None) or prev_id or f"msg_{session_id}_{i}"

                # Tool calls are tiny (<1KB), always dump directly to handle any in-place mutations
                tcs_json = json.dumps([{
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                    **({"extra_content": tc.extra_content} if getattr(tc, "extra_content", None) else {}),
                } for tc in m.tool_calls], ensure_ascii=False)

                # Images are large (1-2MB), cache with tuple signature to avoid O(N^2) re-serialization
                imgs_sig = tuple((id(img), len(img.data_base64), len(img.data)) for img in m.images)
                if getattr(m, "_cached_imgs_sig", None) == imgs_sig and hasattr(m, "_cached_images_json"):
                    imgs_json = m._cached_images_json
                else:
                    imgs_json = json.dumps([{"data_base64": img.data_base64,
                                             "mime_type": img.mime_type or img.mime}
                                            for img in m.images], ensure_ascii=False)
                    m._cached_images_json = imgs_json
                    m._cached_imgs_sig = imgs_sig
                    m._cached_images_saved = False

                from vault.manager import Vault
                clean_content = Vault.scrub_text(m.content) if m.content else m.content
                clean_tcs_json = Vault.scrub_text(tcs_json) if tcs_json else tcs_json

                meta_json = json.dumps(getattr(m, "metadata", {}) or {}, ensure_ascii=False)

                serialized.append((
                    m.role, clean_content, clean_tcs_json, m.tool_call_id, m.name,
                    c_ts, mid, imgs_json, meta_json
                ))

            new_rows = []
            for seq, values in enumerate(serialized):
                prev = existing_summary.get(seq)
                m = history[seq]
                if prev is None:
                    new_rows.append((session_id, seq, *values))
                    m._cached_images_saved = True
                    continue

                changed = []
                if values[0] != prev["role"]: changed.append(0)
                if values[1] != prev["content"]: changed.append(1)
                if values[2] != prev["tool_calls"]: changed.append(2)
                if values[3] != prev["tool_call_id"]: changed.append(3)
                if values[4] != prev["name"]: changed.append(4)
                if values[5] != prev["created_at"]: changed.append(5)
                if values[6] != prev["id"]: changed.append(6)

                # Check images
                if getattr(m, "_cached_images_saved", False) and getattr(m, "_cached_imgs_sig", None) == imgs_sig:
                    pass
                else:
                    prev_len = prev["img_len"] if prev["img_len"] is not None else 0
                    if len(values[7]) != prev_len:
                        changed.append(7)
                    else:
                        if len(values[7]) > 2:
                            db_img = self.db.execute(
                                "SELECT images FROM messages WHERE session_id=? AND seq=?",
                                (session_id, seq)).fetchone()[0]
                            if values[7] != db_img:
                                changed.append(7)
                    m._cached_images_saved = True

                if values[8] != prev["metadata"]: changed.append(8)

                if changed:
                    assignments = ", ".join(columns[i] + "=?" for i in changed)
                    self.db.execute(
                        "UPDATE messages SET " + assignments + " WHERE session_id=? AND seq=?",
                        (*[values[i] for i in changed], session_id, seq))

            if new_rows:
                # PERF-03: 批量写入，单事务 executemany
                self.db.executemany(
                    "INSERT INTO messages (session_id, seq, role, content, tool_calls,"
                    " tool_call_id, name, created_at, id, images, metadata)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)", new_rows)

            if any(seq >= len(serialized) for seq in existing_summary):
                self.db.execute("DELETE FROM messages WHERE session_id=? AND seq>=?",
                                (session_id, len(serialized)))

    def load_history(self, session_id: str) -> list[Message]:
        with self._lock:
            rows = self.db.execute(
                "SELECT role, content, tool_calls, tool_call_id, name, created_at, id, images, metadata"
                " FROM messages WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
        out = []
        for role, content, tcs_json, tcid, name, created_at, mid, images_json, metadata_json in rows:
            tcs = [ToolCall(id=t["id"], name=t["name"], arguments=t["arguments"],
                            extra_content=t.get("extra_content"))
                   for t in json.loads(tcs_json or "[]")]
            c_ts = created_at if created_at is not None else time.time()
            msg_id = mid or f"msg_{session_id}_{len(out)}"
            images = [ImagePart(data_base64=img["data_base64"],
                                mime=img.get("mime_type") or "image/jpeg",
                                mime_type=img.get("mime_type") or "image/jpeg")
                      for img in json.loads(images_json or "[]")]
            msg = Message(role=role, content=content, tool_calls=tcs,
                          tool_call_id=tcid or "", name=name or "",
                          created_at=c_ts, id=msg_id, images=images,
                          metadata=json.loads(metadata_json or "{}"))
            msg._cached_images_json = images_json or "[]"
            msg._cached_imgs_sig = tuple((id(img), len(img.data_base64), len(img.data)) for img in msg.images)
            msg._cached_images_saved = True
            out.append(msg)
        return out

    def get_incomplete_task(self, session_id: str) -> dict | None:
        """检查指定会话是否有未完成的任务。

        返回 None 表示无未完成任务；否则返回摘要：
        {"task_desc": str, "last_update": float, "message_count": int}
        """
        history = self.load_history(session_id)
        if not history:
            return None
        # 从后往前找 task_status 为 incomplete/waiting_user/error 的消息
        for msg in reversed(history):
            status = (msg.metadata or {}).get("task_status")
            if status in {"incomplete", "waiting_user", "error", "running"}:
                # 找任务描述（最近的 user 消息）
                task_desc = ""
                for m in reversed(history):
                    if m.role == "user" and m.name not in {
                            "tool_observation", "verification_feedback", "plan_step"}:
                        task_desc = (m.content or "")[:200]
                        break
                return {
                    "task_desc": task_desc,
                    "task_status": status,
                    "last_update": msg.created_at,
                    "message_count": len(history),
                }
        return None

    def list_sessions(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT session_id, started_ts, title FROM sessions"
                " ORDER BY started_ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"session_id": s, "started_ts": ts, "title": t}
                for s, ts, t in rows]

    def delete_session(self, session_id: str):
        with self._lock:
            self.db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            self.db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            self.db.commit()

    def update_title(self, session_id: str, title: str):
        with self._lock:
            self.db.execute("UPDATE sessions SET title=? WHERE session_id=?", (title, session_id))
            self.db.commit()

    def close(self):
        """显式关闭数据库连接（测试 / 归档后清理用）。"""
        with self._lock:
            self.db.close()


    def _vacuum_if_needed(self):
        """如果存在大量删除的碎片页（freelist > 10%），执行 VACUUM 回收磁盘空间。"""
        try:
            with self._lock:
                cur = self.db.cursor()
                page_count = cur.execute("PRAGMA page_count").fetchone()[0]
                freelist_count = cur.execute("PRAGMA freelist_count").fetchone()[0]
                if page_count > 50 and (freelist_count / max(1, page_count)) > 0.10:
                    self.db.execute("VACUUM")
        except Exception as exc:
            import logging
            logging.getLogger("alterego.session_store").warning("[SessionStore] VACUUM 回收数据库空间异常: %s", exc)

    @staticmethod
    def _prune_archive(archive_dir: str, max_keep: int = 20):
        """保持归档目录下最多 max_keep 个历史数据库文件，自动按修改时间清理最老的归档。"""
        try:
            import os
            import glob
            files = sorted(glob.glob(os.path.join(archive_dir, "conversations_*.db")), key=os.path.getmtime)
            if len(files) > max_keep:
                to_remove = files[:len(files) - max_keep]
                for f in to_remove:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        except Exception as exc:
            import logging
            logging.getLogger("alterego.session_store").warning("[SessionStore] 清理旧归档文件异常: %s", exc)

    def archive_old_sessions(self, archive_dir: str, keep_session_id: str | None = None) -> list[str]:
        """把 keep_session_id 以外的所有旧 session 归档到独立 SQLite 文件。

        每个 session 导出为 archive_dir/conversations_<sid>_<ts>.db，
        归档成功后从主库删除。返回已归档的 session_id 列表。

        Args:
            archive_dir: 归档目录路径（不存在会自动创建）。
            keep_session_id: 保留在主库的 session（通常是当前 session）。
                             传 None 则归档全部。
        """
        import os
        import math
        os.makedirs(archive_dir, exist_ok=True)

        with self._lock:
            rows = self.db.execute(
                "SELECT session_id, started_ts, title FROM sessions ORDER BY started_ts"
            ).fetchall()

        archived = []
        for sid, ts, title in rows:
            if sid == keep_session_id:
                continue

            # 归档文件名：conversations_<sid>_<unix_ts_int>.db
            ts_int = math.floor(ts) if ts else 0
            archive_path = os.path.join(archive_dir, f"conversations_{sid}_{ts_int}.db")

            # 已经归档过就跳过（幂等）
            if os.path.exists(archive_path):
                with self._lock:
                    self.db.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
                    self.db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                    self.db.commit()
                archived.append(sid)
                continue

            # 写归档库
            try:
                arc_db = sqlite3.connect(archive_path)
                arc_db.execute(
                    "CREATE TABLE IF NOT EXISTS sessions"
                    "(session_id TEXT PRIMARY KEY, started_ts REAL, title TEXT)"
                )
                arc_db.execute(
                    "CREATE TABLE IF NOT EXISTS messages"
                    "(session_id TEXT, seq INTEGER, role TEXT, content TEXT,"
                    " tool_calls TEXT, tool_call_id TEXT, name TEXT,"
                    " created_at REAL, id TEXT, images TEXT DEFAULT '[]',"
                    " metadata TEXT DEFAULT '{}',"
                    " PRIMARY KEY (session_id, seq))"
                )
                arc_db.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq)")
                arc_db.execute(
                    "INSERT OR REPLACE INTO sessions VALUES (?,?,?)", (sid, ts, title)
                )
                with self._lock:
                    msgs = self.db.execute(
                        "SELECT seq, role, content, tool_calls, tool_call_id, name,"
                        " created_at, id, images, metadata"
                        " FROM messages WHERE session_id=? ORDER BY seq", (sid,)
                    ).fetchall()
                for row in msgs:
                    arc_db.execute(
                        "INSERT OR REPLACE INTO messages"
                        " (session_id, seq, role, content, tool_calls, tool_call_id,"
                        "  name, created_at, id, images, metadata)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, *row)
                    )
                arc_db.commit()
                arc_db.close()
            except Exception as e:
                logger.error("会话归档失败 session_id=%s: %s", sid, e, exc_info=True)
                # 归档失败不影响主流程，跳过这个 session
                try:
                    os.remove(archive_path)
                except OSError:
                    pass
                continue

            # 从主库删除
            with self._lock:
                self.db.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
                self.db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                self.db.commit()
            archived.append(sid)

        if archived:
            self._vacuum_if_needed()
            self._prune_archive(archive_dir, max_keep=20)

        return archived
