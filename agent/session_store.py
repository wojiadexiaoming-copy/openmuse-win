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
from datetime import datetime, timedelta

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
        """Atomically save a snapshot, writing only changed rows and columns.

        v2.5 P1：水位差量。水位缓存（_wm）按 (session_id, seq) 键控存放在
        **Store 实例内**而非消息对象上——签名跨库共享会误命中（同一批对象
        存到另一个库时会被跳过写入）。命中条件 = 文本签名与图片签名均与
        上次成功保存到本库时一致，才跳过该行的点读与比对。取代旧版「每次
        保存全量回读全部历史 content」的模式（编排器每步 checkpoint，长会话
        O(N²) 放大）。缓存仅在事务成功提交后回填，回滚不标记（偏保守但正确）。
        """
        import hashlib
        columns = ("role", "content", "tool_calls", "tool_call_id", "name",
                   "created_at", "id", "images", "metadata")
        wm = getattr(self, "_wm", None)
        if wm is None:
            wm = self._wm = {}
        with self._lock, self.db:
            # 确保 sessions 表中存在该 session_id 记录
            s_row = self.db.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if not s_row:
                title = history[0].content[:20] if (history and history[0].content) else ""
                self.db.execute("INSERT INTO sessions VALUES (?,?,?)", (session_id, time.time(), title))

            # Finish serialization before any mutation. A malformed tool/image/metadata
            # value must never leave a partly replaced checkpoint in the connection.
            serialized = []
            sigs = []
            img_sigs = []  # v2.5：并行保留每条消息的图片签名（旧版在比对循环里
            #               误引用 serialize 循环泄漏的最后一条 imgs_sig，属潜在错位）
            for i, m in enumerate(history):
                # created_at / id 回填稳定化：否则 c_ts 每次重算 time.time()，
                # 签名永不相等，水位机制失效。
                c_ts = getattr(m, "created_at", None) or time.time()
                mid = getattr(m, "id", None) or f"msg_{session_id}_{i}"
                try:
                    m.created_at = c_ts
                    m.id = mid
                except Exception:
                    pass

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
                img_sigs.append(imgs_sig)

                from vault.manager import Vault
                # v2.5 P1：消毒结果按字符串对象身份 memo（字符串不可变，is 即等值；
                # content 重绑定后自然失效重算），避免每次 checkpoint 对全量历史
                # 重复跑 scrub_text 正则——500 条消息时该项占每步 ~150ms。
                if getattr(m, "_ss_scrub_src", None) is m.content and m.content:
                    clean_content = m._ss_scrubbed
                else:
                    clean_content = Vault.scrub_text(m.content) if m.content else m.content
                    try:
                        m._ss_scrub_src = m.content
                        m._ss_scrubbed = clean_content
                    except Exception:
                        pass
                clean_tcs_json = Vault.scrub_text(tcs_json) if tcs_json else tcs_json

                meta_json = json.dumps(getattr(m, "metadata", {}) or {}, ensure_ascii=False)

                values = (m.role, clean_content, clean_tcs_json, m.tool_call_id, m.name,
                          c_ts, mid, imgs_json, meta_json)
                serialized.append(values)
                # 签名不含 images 列（可达数 MB）：图片变更由 imgs_sig 单独守卫，
                # 水位命中条件 = 文本签名一致 且 图片缓存签名一致且已落库。
                sig_payload = values[:7] + (values[8],)
                sigs.append(hashlib.sha1(repr(sig_payload).encode("utf-8", "replace")).hexdigest())

            # 尾部截断检测只需最大 seq（廉价聚合查询），不再全表回读
            row = self.db.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE session_id=?",
                (session_id,)).fetchone()
            existing_max_seq = row[0] if row else -1

            new_rows = []
            for seq, values in enumerate(serialized):
                m = history[seq]
                # 水位命中（本库、本会话、本行）：文本+图片签名均与上次成功保存一致
                if wm.get((session_id, seq)) == (sigs[seq], img_sigs[seq]):
                    continue
                row = self.db.execute(
                    "SELECT role, content, tool_calls, tool_call_id, name,"
                    " created_at, id, length(images), metadata FROM messages"
                    " WHERE session_id=? AND seq=?", (session_id, seq)).fetchone()
                if row is None:
                    new_rows.append((session_id, seq, *values))
                    m._cached_images_saved = True
                    continue
                prev = {"role": row[0], "content": row[1], "tool_calls": row[2],
                        "tool_call_id": row[3], "name": row[4], "created_at": row[5],
                        "id": row[6], "img_len": row[7], "metadata": row[8]}

                changed = []
                if values[0] != prev["role"]: changed.append(0)
                if values[1] != prev["content"]: changed.append(1)
                if values[2] != prev["tool_calls"]: changed.append(2)
                if values[3] != prev["tool_call_id"]: changed.append(3)
                if values[4] != prev["name"]: changed.append(4)
                if values[5] != prev["created_at"]: changed.append(5)
                if values[6] != prev["id"]: changed.append(6)

                # Check images
                if getattr(m, "_cached_images_saved", False) and getattr(m, "_cached_imgs_sig", None) == img_sigs[seq]:
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

            if existing_max_seq >= len(serialized):
                self.db.execute("DELETE FROM messages WHERE session_id=? AND seq>=?",
                                (session_id, len(serialized)))
                for seq in [s for (s_id, s) in wm if s_id == session_id and s >= len(serialized)]:
                    wm.pop((session_id, seq), None)

        # 事务提交成功后才回填水位（回滚则下次全量重比，偏保守但正确）
        for seq in range(len(serialized)):
            wm[(session_id, seq)] = (sigs[seq], img_sigs[seq])

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

    _INCOMPLETE_STATUSES = frozenset({"incomplete", "waiting_user", "error", "running"})

    def _dismissed_chain_ids(self, history: list[Message]) -> set[str]:
        """已被用户「丢弃提醒」的任务链 id（origin id 与消息 id 都算）。"""
        dismissed: set[str] = set()
        for m in history:
            meta = m.metadata or {}
            if meta.get("resume_dismissed"):
                dismissed.add(m.id)
                origin_id = meta.get("task_origin_id")
                if origin_id:
                    dismissed.add(origin_id)
        return dismissed

    def get_incomplete_task(self, session_id: str) -> dict | None:
        """检查指定会话是否有未完成的任务。

        返回 None 表示无未完成任务（或用户已明确丢弃提醒）；否则返回摘要：
        {"task_desc": str, "task_status": str, "task_origin_id": str,
         "last_update": float, "message_count": int}

        task_desc 取任务起点的用户原话，而不是最近一条用户消息——否则用户
        聊了新话题后，提醒文案会把新话题说成未完成的任务。同一任务链只看
        最新一条状态：链内早期遗留的 incomplete 记录不能覆盖后来的 done。
        """
        history = self.load_history(session_id)
        if not history:
            return None
        dismissed = self._dismissed_chain_ids(history)
        by_id = {m.id: m for m in history}
        latest_index: dict[str, int] = {}
        latest_status: dict[str, str] = {}
        latest_msg: dict[str, Message] = {}
        for idx, msg in enumerate(history):
            meta = msg.metadata or {}
            status = meta.get("task_status")
            if not status:
                continue
            chain_id = meta.get("task_origin_id") or msg.id
            latest_index[chain_id] = idx
            latest_status[chain_id] = status
            latest_msg[chain_id] = msg
        for chain_id in sorted(latest_index, key=lambda c: latest_index[c], reverse=True):
            status = latest_status[chain_id]
            if status not in self._INCOMPLETE_STATUSES:
                continue
            if chain_id in dismissed:
                continue
            msg = latest_msg[chain_id]
            origin = by_id.get(chain_id) or msg
            task_desc = (origin.content or "").strip()[:200]
            if not task_desc:
                task_desc = (msg.content or "").strip()[:200]
            return {
                "task_desc": task_desc,
                "task_status": status,
                "task_origin_id": chain_id,
                "last_update": msg.created_at,
                "message_count": len(history),
            }
        return None

    def mark_task_dismissed(self, session_id: str, origin_id: str = "") -> bool:
        """把未完成任务的整条链标记为「用户已放弃提醒」，重启后不再弹。"""
        history = self.load_history(session_id)
        if not history:
            return False
        target = None
        if origin_id:
            target = next((m for m in history if m.id == origin_id), None)
        if target is None:
            for msg in reversed(history):
                if (msg.metadata or {}).get("task_status") in self._INCOMPLETE_STATUSES:
                    target = msg
                    break
        if target is None:
            return False
        chain_id = (target.metadata or {}).get("task_origin_id") or target.id
        for m in history:
            meta = m.metadata or {}
            if m.id == chain_id or meta.get("task_origin_id") == chain_id:
                meta["resume_dismissed"] = True
                m.metadata = meta
        self.save_history(session_id, history)
        return True

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

    @staticmethod
    def estimate_token_cost(model_name: str, input_tokens: int, output_tokens: int) -> tuple[float, str, str]:
        """估算指定模型的 Token 消耗折算花费（CNY）。
        返回: (cost_cny, provider_name, display_name)
        """
        m = (model_name or "").lower()
        if any(local_kw in m for local_kw in ("local", "agnes", "ollama", "vllm", "self-hosted", "embedded")):
            return 0.0, "本地/自建（免费）", model_name or "Local Model"

        if "deepseek" in m:
            in_rate, out_rate = 1.00, 2.00
            provider, display = "DeepSeek 官方 API", "DeepSeek"
        elif "gpt-4o-mini" in m:
            in_rate, out_rate = 1.08, 4.32
            provider, display = "OpenAI 官方 API", "GPT-4o Mini"
        elif "gpt-4o" in m:
            in_rate, out_rate = 18.00, 72.00
            provider, display = "OpenAI 官方 API", "GPT-4o"
        elif "gpt-4" in m:
            in_rate, out_rate = 216.00, 432.00
            provider, display = "OpenAI 官方 API", "GPT-4"
        elif "gpt-3.5" in m:
            in_rate, out_rate = 3.60, 10.80
            provider, display = "OpenAI 官方 API", "GPT-3.5"
        elif "claude-3-5" in m or "claude-3.5" in m:
            in_rate, out_rate = 21.60, 108.00
            provider, display = "Anthropic 官方 API", "Claude 3.5 Sonnet"
        elif "claude-3-haiku" in m:
            in_rate, out_rate = 1.80, 9.00
            provider, display = "Anthropic 官方 API", "Claude 3 Haiku"
        elif "qwen" in m:
            in_rate, out_rate = 0.80, 2.00
            provider, display = "通义千问 官方 API", "Qwen"
        elif "glm" in m:
            in_rate, out_rate = 1.00, 2.00
            provider, display = "智谱 GLM 官方 API", "GLM"
        elif "moonshot" in m or "kimi" in m:
            in_rate, out_rate = 12.00, 12.00
            provider, display = "Moonshot 官方 API", "Kimi"
        else:
            in_rate, out_rate = 1.00, 2.00
            provider, display = "官方 API 目录价", model_name or "Cloud Model"

        cost = (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate
        return round(cost, 4), provider, display

    def get_token_usage_stats(self, range_days: int = 30) -> dict:
        """从 conversations.db 中聚合提取 Token 消耗与折算花费统计。
        
        :param range_days: 7, 30 或 0（0 代表全部历史）
        :return: 包含 summary, range, days, models 的聚合数据字典
        """
        min_ts = (time.time() - range_days * 86400) if range_days > 0 else None
        with self._lock:
            if min_ts is not None:
                rows = self.db.execute(
                    "SELECT created_at, metadata FROM messages WHERE role='assistant' AND created_at >= ?",
                    (min_ts,)
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT created_at, metadata FROM messages WHERE role='assistant'"
                ).fetchall()

        total_in, total_out, total_req = 0, 0, 0
        by_model: dict[str, dict] = {}
        by_date: dict[str, dict] = {}

        for created_at, meta_json in rows:
            if not meta_json:
                continue
            try:
                meta = json.loads(meta_json)
            except Exception:
                continue
            pin = meta.get("prompt_tokens")
            pout = meta.get("completion_tokens")
            if type(pin) is not int or type(pout) is not int or pin < 0 or pout < 0:
                continue
            total_in += pin
            total_out += pout
            total_req += 1

            echo = meta.get("echobird_model")
            act = meta.get("actual_model") or meta.get("model")
            if act == "auto" and echo:
                model_name = echo
            else:
                model_name = echo or act or "default"

            if model_name not in by_model:
                by_model[model_name] = {"in": 0, "out": 0, "total": 0, "req": 0}
            by_model[model_name]["in"] += pin
            by_model[model_name]["out"] += pout
            by_model[model_name]["total"] += (pin + pout)
            by_model[model_name]["req"] += 1

            ts = created_at or time.time()
            d_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            if d_str not in by_date:
                by_date[d_str] = {"in": 0, "out": 0, "total": 0, "req": 0}
            by_date[d_str]["in"] += pin
            by_date[d_str]["out"] += pout
            by_date[d_str]["total"] += (pin + pout)
            by_date[d_str]["req"] += 1

        total_tokens = total_in + total_out

        # 构建按模型拆分列表
        models_list = []
        total_cost = 0.0
        for m_name, d in by_model.items():
            cost, provider, display = self.estimate_token_cost(m_name, d["in"], d["out"])
            total_cost += cost
            ratio = round(d["total"] / max(1, total_tokens), 4)
            models_list.append({
                "model": m_name,
                "display_name": display,
                "provider": provider,
                "total_tokens": d["total"],
                "input_tokens": d["in"],
                "output_tokens": d["out"],
                "requests": d["req"],
                "ratio": ratio,
                "cost": cost,
                "cost_formatted": f"¥{cost:.2f}" if cost >= 0.01 else "¥<0.01",
            })
        models_list.sort(key=lambda x: x["total_tokens"], reverse=True)

        # 构建每日数据网格（用于热力图与走势图）
        today = datetime.now().date()
        days_list = []
        num_days = 7 if range_days == 7 else 35
        # 35 天正好构成 5 周 x 7 天（周一至周日）的整齐列布局
        end_date = today + timedelta(days=(6 - today.weekday())) if range_days != 7 else today
        start_date = end_date - timedelta(days=num_days - 1)

        cur = start_date
        while cur <= end_date:
            ds = cur.strftime("%Y-%m-%d")
            data = by_date.get(ds, {"in": 0, "out": 0, "total": 0, "req": 0})
            tot = data["total"]
            if tot == 0:
                lv = 0
            elif tot < 10000:
                lv = 1
            elif tot < 50000:
                lv = 2
            elif tot < 200000:
                lv = 3
            else:
                lv = 4

            day_cost, _, _ = self.estimate_token_cost("default", data["in"], data["out"])
            days_list.append({
                "date": ds,
                "day_label": cur.strftime("%m-%d"),
                "weekday": cur.weekday(),
                "total_tokens": tot,
                "input_tokens": data["in"],
                "output_tokens": data["out"],
                "requests": data["req"],
                "cost": day_cost,
                "cost_formatted": f"¥{day_cost:.2f}" if day_cost >= 0.01 else "¥<0.01",
                "level": lv,
                "is_today": (cur == today),
                "is_future": (cur > today),
            })
            cur += timedelta(days=1)

        return {
            "summary": {
                "total_tokens": total_tokens,
                "input_tokens": total_in,
                "output_tokens": total_out,
                "requests": total_req,
                "total_cost": round(total_cost, 4),
                "total_cost_formatted": f"¥{total_cost:.2f}",
            },
            "range": range_days,
            "days": days_list,
            "models": models_list,
        }
