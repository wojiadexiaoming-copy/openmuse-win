"""§33 会话持久化：conversations.db。

M2：history 落盘 + 启动恢复。表结构简单，后续加全文检索再扩展。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

from .models import ImagePart, Message, ToolCall


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
        columns = ("role", "content", "tool_calls", "tool_call_id", "name",
                   "created_at", "id", "images", "metadata")
        with self._lock, self.db:
            existing = {
                row[0]: tuple(row[1:]) for row in self.db.execute(
                    "SELECT seq, role, content, tool_calls, tool_call_id, name,"
                    " created_at, id, images, metadata FROM messages WHERE session_id=?",
                    (session_id,)).fetchall()
            }
            # Finish serialization before any mutation. A malformed tool/image/metadata
            # value must never leave a partly replaced checkpoint in the connection.
            serialized = []
            for i, m in enumerate(history):
                previous = existing.get(i)
                c_ts = (getattr(m, "created_at", None)
                        or (previous[5] if previous else None) or time.time())
                mid = (getattr(m, "id", None)
                       or (previous[6] if previous else None) or f"msg_{session_id}_{i}")
                serialized.append((
                    m.role, m.content,
                    json.dumps([{
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                        **({"extra_content": tc.extra_content} if getattr(tc, "extra_content", None) else {}),
                    } for tc in m.tool_calls], ensure_ascii=False),
                    m.tool_call_id, m.name, c_ts, mid,
                    json.dumps([{"data_base64": img.data_base64,
                                 "mime_type": img.mime_type or img.mime}
                                for img in m.images], ensure_ascii=False),
                    json.dumps(getattr(m, "metadata", {}) or {}, ensure_ascii=False),
                ))
            for seq, values in enumerate(serialized):
                previous = existing.get(seq)
                if previous is None:
                    self.db.execute(
                        "INSERT INTO messages (session_id, seq, role, content, tool_calls,"
                        " tool_call_id, name, created_at, id, images, metadata)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (session_id, seq, *values))
                    continue
                changed = [i for i, value in enumerate(values) if value != previous[i]]
                if changed:
                    # Column names come only from the fixed tuple above. Updating just
                    # metadata avoids rebinding unchanged screenshot and tool JSON blobs.
                    assignments = ", ".join(columns[i] + "=?" for i in changed)
                    self.db.execute(
                        "UPDATE messages SET " + assignments + " WHERE session_id=? AND seq=?",
                        (*[values[i] for i in changed], session_id, seq))
            if any(seq >= len(serialized) for seq in existing):
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
            out.append(Message(role=role, content=content, tool_calls=tcs,
                               tool_call_id=tcid or "", name=name or "",
                               created_at=c_ts, id=msg_id, images=images,
                               metadata=json.loads(metadata_json or "{}")))
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
            except Exception:
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

        return archived
