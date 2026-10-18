"""UI 后端服务：提供 ui/api.md 定义的 REST 接口与 SSE 实时事件。

基于 bottle 与标准库 ThreadingWSGIServer 实现多线程无阻塞服务，支持：
1. 静态资源托管（H5 桌面界面、CSS、JS、文件上传）
2. 会话管理与消息历史读取（SessionStore）
3. 聊天调度与工具执行记录（Orchestrator.handle）
4. 审批流拦截、SSE 推送与决策解决（ApprovalManager）
5. 任务与活动状态实时查询（TaskTracker）
6. 实时 Server-Sent Events（EventBus 广播）
7. 设置与记忆库在线管理
"""
from __future__ import annotations

import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

from bottle import Bottle, ServerAdapter, request, response, static_file

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.approval import ApprovalManager
from agent.context import ContextBuilder
from agent.llm import LLMClient
from agent.main import cli_on_ask, load_config, resolve_llm_key, resolve_backup_llm_key
from agent.memory_manager import MemoryManager
from agent.memory_pipeline import run_memory_distill
from agent.models import Message, ImagePart, tool_result_is_error
from agent.orchestrator import Orchestrator
from agent.planner import Planner
from agent.resource_lock import ResourceLock
from agent.scheduler import Scheduler, JobSpec
from agent.session_store import SessionStore
from agent.settings import Settings
from agent.state_text import tool_summary, result_summary
from agent.subagent import SubagentManager, make_subagent_tools
from agent.takeover import TakeoverManager, set_takeover_manager
from agent.task_tracker import TaskProgress, TaskTracker
from agent.tools_registry import ToolRegistry
from agent.tracing import Tracer, make_llm_request_observer
from tools import register_all
from tools.cache import ToolResultCache
from vault import Vault

UI_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("alterego.ui.server")


class _ConcurrentWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR permits two listeners on the same endpoint.
    # Exclusive ownership prevents a second desktop instance stealing requests.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _QuietWSGIRequestHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        # The desktop entry URL contains the API token.
        pass


class ThreadedWSGIServer(ServerAdapter):
    """支持多线程并发与 SSE 保持连接，并允许启动器同步检查端口绑定。"""
    def create_server(self, handler):
        return make_server(self.host, self.port, handler,
                           server_class=_ConcurrentWSGIServer,
                           handler_class=_QuietWSGIRequestHandler)

    def run(self, handler):
        with self.create_server(handler) as server:
            server.serve_forever()


class EventBus:
    """Server-Sent Events 广播总线。"""
    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def broadcast(self, event: str, data: dict | str):
        payload = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        msg = f"event: {event}\ndata: {payload}\n\n"
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


class AgentServer:
    def __init__(self, data_dir: str | Path | None = None, startup_progress=None):
        self._startup_progress = startup_progress
        self.data_dir = Path(data_dir or os.environ.get("ALTEREGO_DATA", str(REPO_ROOT)))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._startup_phase("load_config")
        self.cfg = load_config()

        # P0-2 补丁：启动时生成或读取 API Token 并持久化至 data_dir/.api_token
        import secrets
        token_file = self.data_dir / ".api_token"
        if token_file.exists():
            self.api_token = token_file.read_text(encoding="utf-8").strip()
        else:
            self.api_token = secrets.token_urlsafe(32)
            token_file.write_text(self.api_token, encoding="utf-8")
            try:
                os.chmod(token_file, 0o600)  # 仅本用户可读
            except Exception:
                pass

        self.port = 5000
        self._ui_window = None
        self._current_browser_ratio = 0.80
        self.event_bus = EventBus()
        self.state_snapshot = {
            "agent_state": {
                "state": "idle",
                "detail": "就绪",
                "model": ""
            },
            "plan": [],
            "activity": [],
            "approvals": [],
            "active_takeover": None,
            "status": {
                "main_model": "",
                "fallback_model": "",
                "current_model": "",
                "token_usage": {},
                "memory_writes": [],
            }
        }

        self._init_token_usage()

        self._startup_phase("open_vault")
        self.vault = Vault.for_machine(self.data_dir) if hasattr(Vault, "for_machine") else Vault(self.data_dir)
        self._startup_phase("initialize_models_and_keys")
        llm_key = resolve_llm_key(self.vault)
        backup_key = resolve_backup_llm_key(self.vault)
        self.llm = LLMClient.from_config(
            self.cfg, api_key=llm_key, fallback_api_key=backup_key, vault=self.vault)

        self.registry = ToolRegistry()
        media_key = ""
        try:
            if self.vault.has("media_api_key"):
                media_key = self.vault.resolve("media_api_key", caller="ui.server")
        except Exception:
            pass
        self._startup_phase("register_tools")
        register_all(self.registry, self.cfg, api_key=media_key or backup_key or llm_key)

        self._startup_phase("open_settings_and_approvals")
        self.settings = Settings(self.data_dir / "settings.json")
        self.approval = ApprovalManager(str(self.data_dir / "approvals.db"), settings=self.settings)

        # UI 审批回调：等待用户在网页上批准或拒绝，并实时推送到网页
        def ui_on_ask(card):
            risk = getattr(card, "risk_level", getattr(card, "risk", "L2"))
            created = getattr(card, "requested_at", getattr(card, "created_at", time.time()))
            timeout = getattr(card, "timeout_sec", 300)
            card_dict = {
                "approval_id": card.approval_id,
                "action": card.action,
                "action_cn": getattr(card, "action_cn", "") or card.action,
                "summary_cn": getattr(card, "summary_cn", "") or card.detail,
                "detail_cn": getattr(card, "detail_cn", "") or card.detail,
                "intent_cn": getattr(card, "intent_cn", "") or "",
                "detail": card.detail,
                "risk_level": risk,
                "risk_note": getattr(card, "risk_note", None) or ("高风险操作，需人工授权" if risk == "L3" else "操作需要确认"),
                "scope_options": ["once", "session"] if risk == "L3" else ["once", "session", "always"],
                "requested_at": created,
                "expires_at": (time.time() + timeout) * 1000,
                "status": "pending",
            }
            # 更新微信通道推送状态后单次广播给前端，避免双发重复卡片
            if hasattr(self, "weixin_channel") and self.weixin_channel:
                try:
                    weixin_status = self.weixin_channel.notify_approval(card) or "failed:empty_result"
                except Exception as _e:
                    self._log_runtime_error("weixin_approval_notify_error", _e, channel="weixin")
                    weixin_status = f"failed:{_e}"
            else:
                weixin_status = "skipped:no_channel"
            card_dict["weixin_notify"] = weixin_status

            # 单次广播给前端并更新快照
            self.emit_event("approval_request", card_dict)
            self.emit_event("agent_state", {
                "state": "awaiting_approval",
                "detail": f"等待确认：{card_dict['action_cn']}",
                "model": getattr(self.llm, "model", "")
            })

            # 向 Windows 屏幕右下角发送带交互按钮的系统审批授权通知
            try:
                from tools.windows_notify import show_approval_toast
                show_approval_toast(
                    card_dict,
                    port=getattr(self, "port", 5000),
                    api_token=self.api_token,
                )
            except Exception as _toast_err:
                logger.debug("Windows approval toast error: %s", _toast_err)

            p = self.approval._pending.get(card.approval_id)
            if not p:
                return False, "once"

            def _log_approval_decision(decision: str, scope: str = "once"):
                # 审批决议写进对话历史：等级/工具/等待时长/结果/微信推送状态，复盘时可见，
                # 不再是只存在于 UI 层的黑盒。失败也不影响主流程。
                try:
                    wait_s = round(time.time() - created, 1)
                    summary = (card_dict.get("summary_cn") or "")[:40]
                    self.orch.history.append(Message(
                        role="assistant",
                        content=(f"[审批] {risk} {card.action}（{summary}）→ {decision}"
                                 f"（等待 {wait_s}s，scope={scope}，"
                                 f"微信推送={card_dict.get('weixin_notify', 'unknown')}）"),
                    ))
                except Exception:
                    pass

            ok = p["event"].wait(timeout=card.timeout_sec)
            if not ok:
                self.emit_event("approval_resolved", {
                    "approval_id": card.approval_id,
                    "approved": False,
                    "scope": "once",
                    "reason": "timeout",
                })
                self.emit_event("agent_state", {
                    "state": "thinking",
                    "detail": "审批超时，继续执行…",
                    "model": getattr(self.llm, "model", "")
                })
                _log_approval_decision("超时拒绝")
                return False, "once"

            approved = bool(p.get("decision"))
            scope = p.get("scope", "once")
            self.emit_event("approval_resolved", {
                "approval_id": card.approval_id,
                "approved": approved,
                "scope": scope,
            })
            self.emit_event("agent_state", {
                "state": "acting" if approved else "thinking",
                "detail": "已通过授权，继续执行…" if approved else "已拒绝授权，调整策略…",
                "model": getattr(self.llm, "model", "")
            })
            _log_approval_decision("通过" if approved else "拒绝", scope)
            return approved, scope

        self.registry.set_approval(self.approval, on_ask=ui_on_ask)
        self._startup_phase("open_tool_cache")
        self.registry.set_cache(ToolResultCache(str(self.data_dir / "cache.db")))
        self.registry.set_locks(ResourceLock())

        def on_task_update(progress: TaskProgress):
            steps = [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "status": s.status,
                    "started_at": time.strftime("%H:%M:%S", time.localtime(s.started_at)) if s.started_at else "",
                    "ended_at": time.strftime("%H:%M:%S", time.localtime(s.ended_at)) if s.ended_at else "",
                    "detail": s.detail,
                }
                for s in progress.steps
            ]
            self.event_bus.broadcast("task_update", {
                "task_id": progress.task_id,
                "name": progress.name,
                "status": progress.status,
                "progress": round(progress.progress, 1),
                "steps": steps,
            })

        self.tracker = TaskTracker(on_update=on_task_update) if self.cfg["agent"].get("task_tracker", True) else None
        self._startup_phase("open_trace_database")
        self.tracer = Tracer(str(self.data_dir / "spans.db"))
        self._observed_model = ""
        self._bind_model_observer(self.llm, "ui")
        self._refresh_model_status()
        self._startup_phase("initialize_memory")
        self.memory = MemoryManager(self.data_dir / "memory")

        self.takeover_mgr = TakeoverManager(event_bus=self.event_bus, on_event=self.emit_event)
        set_takeover_manager(self.takeover_mgr)

        self.ctx = ContextBuilder(
            max_tokens=self.cfg["context"]["max_context_tokens"],
            extra_system=self.memory.read_main,
        )
        self._startup_phase("open_conversation_database")
        self.store = SessionStore(str(self.data_dir / "conversations.db"))
        # 启动时检查未完成任务：在归档前先看最新 session 是否有 incomplete 任务
        # 若有，通过 SSE 事件通知前端弹"继续/丢弃"提示
        self._pending_resume_prompt = None
        try:
            sessions = self.store.list_sessions(limit=1)
            if sessions:
                latest_sid = sessions[0].get("session_id") or sessions[0].get("id")
                if latest_sid:
                    incomplete = self.store.get_incomplete_task(latest_sid)
                    if incomplete:
                        self._pending_resume_prompt = {
                            "session_id": latest_sid,
                            **incomplete,
                        }
                        import logging as _log
                        _log.getLogger(__name__).info(
                            "[resume_prompt] 发现未完成任务: %s (status=%s)",
                            incomplete["task_desc"][:50],
                            incomplete["task_status"])
        except Exception:
            pass  # 检查失败不阻断启动
        # 归档旧 session：每次启动时把上一轮的 session 导出到 archive/，主库只保留新 session
        _archive_dir = str(self.data_dir / "archive")
        try:
            _archived = self.store.archive_old_sessions(_archive_dir, keep_session_id=None)
            if _archived:
                import logging as _log
                _log.getLogger(__name__).info("archived %d old session(s): %s", len(_archived), _archived)
        except Exception:
            pass  # 归档失败不阻断启动
        self._startup_phase("initialize_planner")
        planner_llm = LLMClient.for_role(self.cfg, "planner", self.vault)
        self._bind_model_observer(planner_llm, "planner")
        self.planner = Planner(planner_llm)

        def _make_sub_orchestrator(task):
            role = "code" if getattr(task, "kind", "generic") == "code" else "subagent"
            if (role == "code" and not self.cfg.get("llm_router", {}).get("enabled")
                    and "code" not in self.cfg.get("models", {}).get("roles", {})):
                role = "subagent"
            sub_llm = LLMClient.for_role(self.cfg, role, self.vault)
            if getattr(task, "critical", False):
                sub_llm = LLMClient.for_role(
                    self.cfg, self.cfg.get("models", {}).get("critical_subagent_role", "main"), self.vault)
            self._bind_model_observer(sub_llm, "code" if role == "code" else "subagent")
            sub_prompt_file = Path("prompts/subagent.md")
            sub_prompt = sub_prompt_file.read_text(encoding="utf-8") if sub_prompt_file.exists() else ""
            sub_ctx = ContextBuilder(
                max_tokens=self.cfg["context"]["max_context_tokens"] // 2,
                extra_system=sub_prompt,
            )
            return Orchestrator(sub_llm, self.registry, sub_ctx,
                                max_iters=15, parallel_tools=False,
                                tracer=self.tracer, tracker=self.tracker, planner=None,
                                on_event=self.emit_event)

        self._startup_phase("initialize_agent")
        self.sub_mgr = SubagentManager(make_orchestrator=_make_sub_orchestrator)
        for t in make_subagent_tools(self.sub_mgr):
            self.registry.register(t)

        self.orch = Orchestrator(
            self.llm,
            self.registry,
            self.ctx,
            max_iters=self.cfg["agent"].get("max_iters", 25),
            max_iters_hard=self.cfg["agent"].get("max_iters_hard", 100),
            parallel_tools=self.cfg["agent"].get("parallel_tools", False),
            tracer=self.tracer,
            tracker=self.tracker,
            planner=self.planner,
            sub_mgr=self.sub_mgr,
            on_event=self.emit_event,
        )

        self.turn_lock = self.orch.turn_lock

        # 后台记忆管线调度 (spec §8 / memory-pipeline.md / B4 优化)

        def _on_scheduled_job(job_id: str, brief: str):
            try:
                scope = "nightly" if "nightly" in job_id else "hourly"
                run_memory_distill(
                    memory=self.memory,
                    store=self.store,
                    tracer=self.tracer,
                    scope=scope,
                    vault=self.vault,
                    cfg=self.cfg,
                )
            except Exception as exc:
                self._log_runtime_error("memory_scheduled_error", exc, scope=scope)

        try:
            self._startup_phase("initialize_scheduler")
            self.scheduler = Scheduler(
                db_url=f"sqlite:///{self.data_dir / 'jobs.sqlite'}",
                on_fire=_on_scheduled_job,
            )
            self.scheduler.start()
            self.scheduler.add(JobSpec(
                job_id="mem_hourly",
                kind="interval",
                brief="每小时轻量记忆蒸馏",
                trigger_args={"hours": 1},
            ))
            self.scheduler.add(JobSpec(
                job_id="mem_nightly",
                kind="cron",
                brief="每晚深度记忆蒸馏",
                trigger_args={"hour": 2, "minute": 0},
            ))
        except Exception as e:
            self._log_runtime_error("memory_scheduler_start_error", e)

        # 微信 ClawBot 通道初始化（基于 Tencent/openclaw-weixin 官方协议）
        try:
            self._startup_phase("initialize_weixin")
            from channels.weixin import WeixinChannel
            self.weixin_channel = WeixinChannel(
                data_dir=self.data_dir / "weixin",
                orch=self.orch,
                approval_mgr=self.approval,
                session_store=self.store,
                on_event=self.emit_event,
                tracer=self.tracer,
                vault=self.vault,
                turn_lock=self.turn_lock,
            )
        except Exception as e:
            self._log_runtime_error("weixin_init_error", e, channel="weixin")
            self.weixin_channel = None

        self._startup_phase("setup_http_routes")
        self.app = Bottle()
        self._setup_routes()
        self._startup_phase("backend_initialized")

    def _startup_phase(self, phase: str):
        progress = getattr(self, "_startup_progress", None)
        if callable(progress):
            try:
                progress(phase)
            except Exception as exc:
                logger.warning("Startup progress callback failed (%s)", type(exc).__name__)

    @staticmethod
    def _model_name(client) -> str:
        model = getattr(client, "model", "")
        return model if isinstance(model, str) else ""

    def _refresh_model_status(self):
        main_model = self._model_name(self.llm)
        fallback_model = self._model_name(getattr(self.llm, "fallback", None))
        self.state_snapshot["status"].update({
            "main_model": main_model, "fallback_model": fallback_model,
            "current_model": main_model,
        })
        self.state_snapshot["agent_state"]["model"] = main_model

    def _init_token_usage(self):
        """Usage belongs to recorded requests in this application process."""
        self._token_usage_lock = threading.Lock()
        self._token_usage_seen = set()
        self.state_snapshot["status"]["token_usage"] = {
            "source_id": uuid.uuid4().hex, "revision": 0,
            "scope": "application_recorded_requests", "input": None, "output": None,
            "known_requests": 0, "unknown_requests": 0,
        }

    def _record_token_usage(self, metadata):
        if not isinstance(metadata, dict) or metadata.get("request_attempted") is not True:
            return None
        request_id = metadata.get("request_id")
        model = metadata.get("actual_model")
        provider = metadata.get("provider") or metadata.get("base_url_host") or ""
        attempt = metadata.get("attempt", metadata.get("fallback_depth", 0))
        if (not isinstance(request_id, str) or not request_id
                or not isinstance(model, str) or not model or not isinstance(provider, str)
                or type(attempt) is not int or attempt < 0):
            return None
        identity = (provider, model, request_id, attempt)
        prompt, completion = metadata.get("prompt_tokens"), metadata.get("completion_tokens")
        known = (metadata.get("usage_available") is True
                 and type(prompt) is int and 0 <= prompt <= 2**63 - 1
                 and type(completion) is int and 0 <= completion <= 2**63 - 1)
        # This independent lock never covers an HTTP call, tracing, or broadcasting.
        with self._token_usage_lock:
            if identity in self._token_usage_seen:
                return None
            self._token_usage_seen.add(identity)
            usage = dict(self.state_snapshot["status"]["token_usage"])
            usage["revision"] += 1
            if known:
                usage["input"] = (usage["input"] or 0) + prompt
                usage["output"] = (usage["output"] or 0) + completion
                usage["known_requests"] += 1
            else:
                usage["unknown_requests"] += 1
            # Replace the complete immutable snapshot rather than mutating counters
            # while an API reader or another observer is serializing it.
            self.state_snapshot["status"]["token_usage"] = usage
            return dict(usage)

    def _bind_model_observer(self, client, channel: str):
        hook = getattr(client, "set_request_observer", None)
        if not callable(hook):
            return
        trace_observer = make_llm_request_observer(self.tracer, channel=channel)

        def observe(metadata):
            try:
                trace_observer(metadata)
            finally:
                usage = self._record_token_usage(metadata)
                if usage is not None:
                    self.event_bus.broadcast("token_usage", usage)
                actual_model = metadata.get("actual_model")
                if (metadata.get("request_attempted", True)
                        and isinstance(actual_model, str) and actual_model):
                    self._observed_model = actual_model
                    self.state_snapshot["status"]["current_model"] = actual_model
                    self.state_snapshot["agent_state"]["model"] = actual_model
                    self.event_bus.broadcast(
                        "agent_state", dict(self.state_snapshot["agent_state"]))

        hook(observe)

    def _reload_model_clients(self, api_key_override: str = ""):
        """Build new provider-bound clients before replacing live references.

        api_key_override: 切换模型时传入目标模型的 key；为空则按原逻辑解析。
        """
        if api_key_override:
            llm_key = api_key_override
        else:
            llm_key = resolve_llm_key(self.vault)
        backup_key = resolve_backup_llm_key(self.vault)
        new_llm = LLMClient.from_config(
            self.cfg, api_key=llm_key, fallback_api_key=backup_key, vault=self.vault)
        new_planner = getattr(self, "planner", None)
        if new_planner is not None or self.cfg.get("agent", {}).get("planner", False):
            planner_llm = LLMClient.for_role(self.cfg, "planner", self.vault)
            self._bind_model_observer(planner_llm, "planner")
            new_planner = Planner(planner_llm)
        self._bind_model_observer(new_llm, "ui")
        self.llm = new_llm
        if new_planner is not None:
            self.planner = new_planner
            self.orch.planner = new_planner
        self.orch.llm = new_llm
        self._observed_model = ""
        self._refresh_model_status()

    def set_ui_window(self, window):
        """保存原生桌面 PyWebView 窗口实例，用于分屏联动。"""
        self._ui_window = window

    @contextmanager
    def _conversation_turn(self, sid: str):
        """Keep history, runtime scope and persistence callback in one turn."""
        with self.turn_lock:
            previous_session = getattr(self.orch, "session_id", None)
            previous_callback = getattr(self.orch, "on_history", None)
            self.orch.session_id = sid
            self.orch.on_history = lambda history, session_id=sid: self.store.save_history(
                session_id, history)
            try:
                yield
            finally:
                self.orch.on_history = previous_callback
                self.orch.session_id = previous_session

    def _log_runtime_error(self, event: str, exc: Exception, **metadata):
        """异常日志只保留类型与诊断元数据，避免凭证或工具参数落日志。"""
        logger.error("%s (%s)", event, type(exc).__name__)
        tracer = getattr(self, "tracer", None)
        if tracer:
            try:
                tracer.log_event(event, {"error_type": type(exc).__name__, **metadata})
            except Exception:
                logger.error("runtime_log_write_failed")

    def _record_chat_error(self, sid: str, exc: Exception) -> dict:
        error_text = f"⚠️ 执行出现异常（{type(exc).__name__}），本轮未完成，请重试。"
        error_message = Message(role="assistant", content=error_text)
        self.orch.history.append(error_message)
        self.orch.last_turn_status = "error"
        try:
            self.store.save_history(sid, self.orch.history)
        except Exception as save_exc:
            self._log_runtime_error(
                "chat_history_save_error", save_exc, channel="ui", session_id=sid)
        self._log_runtime_error("chat_error", exc, channel="ui", session_id=sid)
        self.emit_event("agent_state", {
            "state": "error", "detail": error_text,
            "model": getattr(self.llm, "model", ""),
        })
        msg_obj = {
            "id": error_message.id, "type": "assistant_final", "role": "agent",
            "content": error_text, "tool_calls": [], "status": "error",
            "session_id": sid,
            "created_at": time.strftime("%H:%M", time.localtime(error_message.created_at)),
        }
        self.event_bus.broadcast("message", msg_obj)
        return msg_obj

    def emit_event(self, event: str, data: dict | str):
        """记录状态快照并广播 SSE 事件"""
        if not isinstance(data, dict):
            self.event_bus.broadcast(event, data)
            return

        if event == "agent_state":
            if getattr(self, "_observed_model", ""):
                data = {**data, "model": self._observed_model}
            self.state_snapshot["agent_state"].update(data)
            if data.get("model"):
                self.state_snapshot["status"]["current_model"] = data["model"]
        elif event == "plan_update":
            self.state_snapshot["plan"] = data.get("steps", [])
        elif event == "tool_call_started":
            item = {
                "id": data.get("id"),
                "tool": data.get("tool"),
                "summary": data.get("summary", ""),
                "status": "running",
                "args": data.get("args"),
                "started_at": time.strftime("%H:%M:%S", time.localtime()),
                "duration_ms": None,
                "result_summary": "",
            }
            self.state_snapshot["activity"].insert(0, item)
            self.state_snapshot["activity"] = self.state_snapshot["activity"][:50]
        elif event == "tool_call_done":
            tid = data.get("id")
            for act in self.state_snapshot["activity"]:
                if act.get("id") == tid:
                    act["status"] = data.get("status", "ok")
                    act["duration_ms"] = data.get("duration_ms")
                    act["result_summary"] = data.get("result_summary", "")
                    if "result" in data:
                        act["result"] = data["result"]
                    break
        elif event == "subagent_update":
            existing = next((a for a in self.state_snapshot["activity"] if a.get("id") == data.get("id")), None)
            if existing:
                existing["status"] = data.get("status", "running")
                if data.get("result_summary"):
                    existing["result_summary"] = data["result_summary"]
            else:
                item = {
                    "id": data.get("id"),
                    "tool": "subagent",
                    "summary": f"子任务: {data.get('brief', '')}",
                    "status": data.get("status", "running"),
                    "started_at": time.strftime("%H:%M:%S", time.localtime()),
                    "duration_ms": None,
                    "result_summary": data.get("result_summary", ""),
                }
                self.state_snapshot["activity"].insert(0, item)
                self.state_snapshot["activity"] = self.state_snapshot["activity"][:50]
        elif event == "token_usage":
            data = self._record_token_usage(data)
            if data is None:
                return
        elif event == "memory_write":
            mem_item = {
                "kind": data.get("kind", "fact"),
                "text": data.get("text", ""),
                "time": time.strftime("%H:%M:%S", time.localtime()),
            }
            self.state_snapshot["status"]["memory_writes"].insert(0, mem_item)
            self.state_snapshot["status"]["memory_writes"] = self.state_snapshot["status"]["memory_writes"][:20]
        elif event in ("approval_request", "approval_resolved"):
            self.state_snapshot["approvals"] = self.approval.list_pending()
        elif event == "takeover_request":
            self.state_snapshot["active_takeover"] = data
            self.emit_event("agent_state", {
                "state": "paused_takeover",
                "detail": f"等待人工介入：{data.get('reason', '')}",
                "model": getattr(self.llm, "model", "")
            })
            try:
                sid = getattr(self.orch, "session_id", None)
                if not sid:
                    sessions = self.store.list_sessions(limit=1)
                    if sessions:
                        sid = sessions[0].get("session_id") or sessions[0].get("id")
                if sid:
                    task_id = data.get("task_id", "")
                    target = data.get("target") or "Chrome"
                    reason = data.get("reason") or "需要人工介入"
                    page_url = data.get("page_url") or ""
                    msg_obj = {
                        "id": f"takeover_{task_id}",
                        "type": "takeover_request",
                        "role": "agent",
                        "content": (
                            f"⚠️ **需要人工介入协同**\n\n"
                            f"- **目标程序**: {target}\n"
                            f"- **介入原因**: {reason}\n"
                            + (f"- **网页地址**: {page_url}\n" if page_url else "")
                            + "\n👉 **请直接在已打开的浏览器窗口中完成操作**（如滑动拼图/点选验证码/扫码登录）。完成或取消请点击下方按钮："
                        ),
                        "takeover_card": data,
                        "status": "pending",
                        "session_id": sid,
                        "created_at": time.strftime("%H:%M", time.localtime(time.time())),
                    }
                    self.event_bus.broadcast("message", msg_obj)
            except Exception as e:
                logger.debug("Failed to record takeover message: %s", e)

            # 向 Windows 屏幕右下角发送带前往接管按钮的系统通知
            try:
                from tools.windows_notify import show_takeover_toast
                show_takeover_toast(
                    data,
                    port=getattr(self, "port", 5000),
                    api_token=self.api_token,
                )
            except Exception as _toast_err:
                logger.debug("Windows takeover toast error: %s", _toast_err)
        elif event == "takeover_update":
            self.state_snapshot["active_takeover"] = None
            outcome = data.get("status")
            self.emit_event("agent_state", {
                "state": "acting" if outcome == "completed" else "idle",
                "detail": "人工接管已完成，正在恢复执行…" if outcome == "completed" else f"人工接管已结束（{outcome}）",
                "model": getattr(self.llm, "model", "")
            })

        self.event_bus.broadcast(event, data)

    def _setup_routes(self):
        if not hasattr(self, "turn_lock"):
            self.turn_lock = getattr(self.orch, "turn_lock", None) or threading.RLock()
            self.orch.turn_lock = self.turn_lock
        app = self.app

        ALLOWED_ORIGINS = {"http://127.0.0.1:5000", "http://localhost:5000", "null"}

        # 鉴权钩子：拦截 /api/*，放行 /api/health 与 OPTIONS 预检
        @app.hook("before_request")
        def _require_auth():
            if request.method == "OPTIONS":
                return
            if (request.path.startswith("/api/")
                    and not request.path.startswith("/api/health")
                    and not request.path.startswith("/api/avatar/")
                    and not request.path.startswith("/api/weixin/qr_img")
                    and not request.path.startswith("/api/approvals/toast_resolve")
                    and not request.path.endswith("/focus")):
                auth_header = request.headers.get("Authorization", "").strip()
                token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else auth_header
                if not token:
                    token = request.headers.get("X-AlterEgo-Token", "").strip()
                if not token:
                    token = request.query.get("token", "").strip()
                import secrets
                from bottle import HTTPResponse
                if not token or not secrets.compare_digest(token, self.api_token):
                    raise HTTPResponse(
                        body=json.dumps({"error": "unauthorized"}),
                        status=401,
                        headers={"Content-Type": "application/json"}
                    )

        # 跨域与基础安全响应头（CORS 收紧）
        @app.hook("after_request")
        def enable_cors():
            origin = request.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-AlterEgo-Token"

        @app.route("/<:re:.*>", method="OPTIONS")
        def options_handler():
            return {}

        @app.route("/api/health", method="GET")
        def health():
            return {"status": "ok"}

        @app.route("/api/pending_resume", method="GET")
        def pending_resume():
            """前端启动时查询：是否有未完成的任务需要提示继续/丢弃。"""
            prompt = getattr(self, "_pending_resume_prompt", None)
            if prompt:
                return {"has_pending": True, "task": prompt}
            return {"has_pending": False}

        @app.route("/api/dismiss_resume", method="POST")
        def dismiss_resume():
            """用户选择丢弃未完成任务：清除提示，不恢复会话。"""
            self._pending_resume_prompt = None
            return {"ok": True}

        # 可切换的主模型列表（支持预设推荐、配置文件动态发现与用户自定义模型）
        PRESET_MODELS = [
            {"id": "agnes-3.0-flash", "name": "Agnes 3.0 Flash",
             "base_url": "https://apihub.agnes-ai.com/v1",
             "desc": "Agnes API Hub · 全模态零延迟无速率限制",
             "tag": "主力推荐 · 极速",
             "group": "recommended",
             "supports_images": True,
             "key_env": "ALTEREGO_AGNES_API_KEY", "vault_key": "agnes_api_key"},
            {"id": "qwen3.8-omni-flash", "name": "Qwen 3.8 Omni Flash",
             "base_url": "https://llm-wwautliy36g7ora8.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
             "desc": "阿里云百炼专属全模态 · 低延迟多模态识图与实时交互",
             "tag": "阿里云百炼 · 极速",
             "group": "recommended",
             "supports_images": True,
             "key_env": "DASHSCOPE_API_KEY", "vault_key": "dashscope_api_key"},
            {"id": "kimi-k3", "name": "Kimi K3",
             "base_url": "https://llm-wwautliy36g7ora8.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
             "desc": "阿里云百炼专属网关 · 超强长上下文与深度推理",
             "tag": "深度推理",
             "group": "recommended",
             "supports_images": True,
             "key_env": "DASHSCOPE_API_KEY", "vault_key": "dashscope_api_key"},
            {"id": "qwen3.8-max", "name": "Qwen 3.8 Max",
             "base_url": "https://llm-wwautliy36g7ora8.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
             "desc": "阿里云百炼旗舰超大多模态 · 复杂图表与高难度任务拆解",
             "tag": "超大旗舰",
             "group": "recommended",
             "supports_images": True,
             "key_env": "DASHSCOPE_API_KEY", "vault_key": "dashscope_api_key"},
            {"id": "gemini-3.5-flash", "name": "Gemini 3.5 Flash",
             "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
             "desc": "Google 官方高效模型 · 视觉与代码理解（备选通道）",
             "tag": "海外极速",
             "group": "recommended",
             "supports_images": True,
             "key_env": "ALTEREGO_GEMINI_API_KEY", "vault_key": "gemini_api_key"},
            {"id": "gemini-3.6-flash", "name": "Gemini 3.6 Flash",
             "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
             "desc": "Google 最新架构高效模型备选",
             "tag": "海外",
             "group": "recommended",
             "supports_images": True,
             "key_env": "ALTEREGO_GEMINI_API_KEY", "vault_key": "gemini_api_key"},
            {"id": "gemini-3.5-flash-lite", "name": "Gemini 3.5 Flash Lite",
             "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
             "desc": "Google 轻量级低延迟",
             "tag": "轻量",
             "group": "recommended",
             "supports_images": True,
             "key_env": "ALTEREGO_GEMINI_API_KEY", "vault_key": "gemini_api_key"},
             {"id": "gemini-flash-latest", "name": "Gemini Flash Latest",
             "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
             "desc": "指向 Google 最新 Flash 版本",
             "tag": "海外",
             "group": "recommended",
             "supports_images": True,
             "key_env": "ALTEREGO_GEMINI_API_KEY", "vault_key": "gemini_api_key"},
        ]
        AVAILABLE_MODELS = PRESET_MODELS

        data_dir = getattr(self, "data_dir", None) or Path(".")
        custom_models_file = data_dir / "custom_models.json"

        def _load_custom_models() -> list[dict]:
            if custom_models_file.is_file():
                try:
                    with open(custom_models_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        return [m for m in data if isinstance(m, dict) and m.get("id")]
                except Exception as exc:
                    logger.debug("读取 custom_models.json 异常: %s", exc)
            return []

        def _save_custom_models(models: list[dict]):
            try:
                with open(custom_models_file, "w", encoding="utf-8") as f:
                    json.dump(models, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                logger.warning("保存 custom_models.json 异常: %s", exc)

        def _get_dynamic_models() -> list[dict]:
            """动态组装模型列表：预设模型 + 用户自定义模型 + config.yaml 中配置项 + 当前生效模型。"""
            seen_ids = set()
            result = []

            # 1. 预置基础模型
            for m in PRESET_MODELS:
                m_copy = dict(m)
                seen_ids.add(m_copy["id"])
                result.append(m_copy)

            # 2. 用户持久化的自定义模型（优先识别为自定义标签与属性）
            for cm in _load_custom_models():
                mid = cm.get("id")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    cm_entry = dict(cm)
                    cm_entry.setdefault("tag", "自定义")
                    cm_entry.setdefault("group", "custom")
                    cm_entry["is_custom"] = True
                    result.append(cm_entry)

            # 3. 从 self.cfg (config.yaml) 中动态发现未在预置或自定义中的配置模型
            cfg = getattr(self, "cfg", None) or {}
            cfg_sources = []
            if isinstance(cfg.get("llm"), dict):
                cfg_sources.append(("主模型 (config)", cfg["llm"]))
            roles = cfg.get("models", {}).get("roles", {})
            if isinstance(roles, dict):
                for rname, rcfg in roles.items():
                    if isinstance(rcfg, dict) and "model" in rcfg:
                        cfg_sources.append((f"角色 {rname} (config)", rcfg))
            router_cfg = cfg.get("llm_router", {})
            if isinstance(router_cfg, dict) and router_cfg.get("enabled"):
                for pname, pcfg in router_cfg.get("providers", {}).items():
                    if isinstance(pcfg, dict) and "model" in pcfg:
                        cfg_sources.append((f"路由 {pname}", pcfg))

            for source_name, scfg in cfg_sources:
                mid = str(scfg.get("model") or "").strip()
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    result.append({
                        "id": mid,
                        "name": mid,
                        "base_url": scfg.get("base_url") or "",
                        "desc": f"来源于配置文件：{source_name}",
                        "tag": "配置模型",
                        "group": "config",
                        "supports_images": scfg.get("supports_images", True),
                        "key_env": scfg.get("api_key_env", ""),
                        "vault_key": "",
                    })

            # 4. 确保当前生效模型一定在列表中
            current_llm = getattr(self, "llm", None)
            current_id = self._model_name(current_llm)
            if current_id and current_id not in seen_ids:
                seen_ids.add(current_id)
                result.insert(0, {
                    "id": current_id,
                    "name": current_id,
                    "base_url": getattr(current_llm, "base_url", ""),
                    "desc": "当前运行时生效模型",
                    "tag": "当前生效",
                    "group": "runtime",
                    "supports_images": getattr(current_llm, "supports_images", True),
                    "key_env": "",
                    "vault_key": "",
                })

            return result

        def _resolve_model_key(model_def):
            """按模型定义解析 key：环境变量 > 专属 vault key > 供应商智能匹配 > 通用 llm_api_key。"""
            import os
            base_url = str(model_def.get("base_url") or "")
            mid = str(model_def.get("id") or "").lower()

            # 1. 显式指定的 key_env
            key_env = model_def.get("key_env", "").strip()
            if key_env:
                env_key = os.environ.get(key_env, "").strip()
                if env_key:
                    return env_key

            vault = getattr(self, "vault", None)

            # 2. 显式指定的 vault_key
            vault_key = model_def.get("vault_key", "").strip()
            if vault_key and vault and hasattr(vault, "resolve"):
                try:
                    v = vault.resolve(vault_key, caller="ui.server")
                    if v:
                        return v
                except (PermissionError, KeyError):
                    pass

            # 3. 供应商前缀智能推断 (Agnes / DashScope / Gemini / DeepSeek 等)
            provider_candidates = []
            if "agnes" in mid or "agnes" in base_url:
                provider_candidates.extend([("ALTEREGO_AGNES_API_KEY", "agnes_api_key"), ("AGNES_API_KEY", "")])
            if any(k in mid or k in base_url for k in ("qwen", "dashscope", "kimi", "aliyuncs")):
                provider_candidates.extend([("DASHSCOPE_API_KEY", "dashscope_api_key")])
            if "gemini" in mid or "google" in base_url:
                provider_candidates.extend([("ALTEREGO_GEMINI_API_KEY", "gemini_api_key"), ("GEMINI_API_KEY", "")])
            if "deepseek" in mid or "deepseek" in base_url:
                provider_candidates.extend([("DEEPSEEK_API_KEY", "deepseek_api_key")])

            for penv, pvault in provider_candidates:
                if penv:
                    val = os.environ.get(penv, "").strip()
                    if val:
                        return val
                if pvault and vault and hasattr(vault, "resolve"):
                    try:
                        val = vault.resolve(pvault, caller="ui.server")
                        if val:
                            return val
                    except (PermissionError, KeyError):
                        pass

            # 4. Fallback 通用 key
            if vault and hasattr(vault, "resolve"):
                try:
                    val = vault.resolve("llm_api_key", caller="ui.server")
                    if val:
                        return val
                except (PermissionError, KeyError):
                    pass
            return os.environ.get("ALTEREGO_LLM_API_KEY", "").strip()

        @app.route("/api/models", method="GET")
        def list_models():
            """前端查询可用模型列表 + 当前主模型（动态组装与暴露）。"""
            current = self._model_name(self.llm)
            all_models = _get_dynamic_models()
            public = [{k: v for k, v in m.items() if not k.startswith("key_") and k != "vault_key"}
                      for m in all_models]
            for m in public:
                m["has_key"] = bool(_resolve_model_key(m))
            return {"current": current, "models": public}

        @app.route("/api/settings/save_model_config", method="POST")
        @app.route("/api/switch_model", method="POST")
        def switch_or_save_model():
            """保存大模型参数与 API Key，并即时热切换生效，无需重启。"""
            from bottle import request
            data = request.json or {}
            model_id = str(data.get("model_id") or data.get("model") or "").strip()
            if not model_id:
                return {"ok": False, "error": "模型标识不能为空"}

            base_url = str(data.get("base_url") or "").strip()
            api_key = str(data.get("api_key") or "").strip()

            # 1. 如果用户录入了新的 API Key，立即使用 Windows DPAPI 加密存入 Vault
            vault = getattr(self, "vault", None)
            if api_key and vault and hasattr(vault, "set"):
                vault.set("llm_api_key", api_key)
                mid_lower = model_id.lower()
                burl_lower = (base_url or "").lower()
                if "agnes" in mid_lower or "agnes" in burl_lower:
                    try:
                        vault.set("agnes_api_key", api_key)
                    except Exception:
                        pass
                elif any(k in mid_lower or k in burl_lower for k in ("qwen", "dashscope", "kimi", "aliyuncs")):
                    try:
                        vault.set("dashscope_api_key", api_key)
                    except Exception:
                        pass
                elif "gemini" in mid_lower or "google" in burl_lower:
                    try:
                        vault.set("gemini_api_key", api_key)
                    except Exception:
                        pass
                elif "deepseek" in mid_lower or "deepseek" in burl_lower:
                    try:
                        vault.set("deepseek_api_key", api_key)
                    except Exception:
                        pass

            all_models = _get_dynamic_models()
            target = next((m for m in all_models if m["id"] == model_id), None)

            if not target:
                # 动态添加并持久化自定义模型
                if not base_url:
                    base_url = (self.cfg.get("llm", {}).get("base_url")
                                or "https://apihub.agnes-ai.com/v1")
                supports_images = bool(data.get("supports_images", True))
                target = {
                    "id": model_id,
                    "name": data.get("name") or model_id,
                    "base_url": base_url,
                    "desc": data.get("desc") or "用户自定义模型",
                    "tag": "自定义",
                    "group": "custom",
                    "supports_images": supports_images,
                    "is_custom": True,
                }
                custom_list = _load_custom_models()
                if not any(m.get("id") == model_id for m in custom_list):
                    custom_list.append(target)
                    _save_custom_models(custom_list)
            else:
                if base_url and base_url != target.get("base_url"):
                    target["base_url"] = base_url
                    if target.get("is_custom"):
                        custom_list = _load_custom_models()
                        for c in custom_list:
                            if c.get("id") == model_id:
                                c["base_url"] = base_url
                        _save_custom_models(custom_list)

            # 解析目标模型的 key，没有 key 直接报错不切换
            model_key = api_key or _resolve_model_key(target)
            if not model_key:
                return {
                    "ok": False,
                    "error": f"模型 {model_id} 没有可用 API Key，请在下方录入密钥后保存"
                }

            # 更新内存中的配置（setdefault 防御缺字段的旧 config）
            self.cfg.setdefault("llm", {})["model"] = target["id"]
            self.cfg["llm"]["base_url"] = target["base_url"]
            if "supports_images" in target:
                self.cfg["llm"]["supports_images"] = target["supports_images"]
            roles = self.cfg.setdefault("models", {}).setdefault("roles", {})
            roles.setdefault("main", {})["model"] = target["id"]
            roles["main"]["base_url"] = target["base_url"]
            if "supports_images" in target:
                roles["main"]["supports_images"] = target["supports_images"]

            # 重建客户端（无需重启），传入目标模型的 key
            try:
                with self.turn_lock:
                    self._reload_model_clients(api_key_override=model_key)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

            self.state_snapshot["status"]["current_model"] = target["id"]
            self.emit_event("model_switched", {"model": target["id"], "base_url": target["base_url"]})

            import logging as _log
            _log.getLogger(__name__).info(
                "[switch_model] 主模型与密钥已关联保存生效: %s (base_url: %s)", target["id"], target["base_url"])

            updated_models = [{k: v for k, v in m.items() if not k.startswith("key_") and k != "vault_key"}
                              for m in _get_dynamic_models()]
            for m in updated_models:
                m["has_key"] = bool(_resolve_model_key(m))
            return {
                "ok": True,
                "current": target["id"],
                "base_url": target["base_url"],
                "has_key": True,
                "models": updated_models
            }

        @app.route("/api/models/<model_id:path>", method="DELETE")
        def delete_custom_model(model_id):
            """删除用户添加的自定义模型。"""
            custom_list = _load_custom_models()
            new_list = [m for m in custom_list if m.get("id") != model_id]
            if len(new_list) < len(custom_list):
                _save_custom_models(new_list)
                # 若删除的模型正是当前配置中生效的主模型，回退至默认主模型
                if self.cfg.get("llm", {}).get("model") == model_id or self._model_name(self.llm) == model_id:
                    default_m = PRESET_MODELS[0]
                    self.cfg.setdefault("llm", {})["model"] = default_m["id"]
                    self.cfg["llm"]["base_url"] = default_m["base_url"]
                    roles = self.cfg.setdefault("models", {}).setdefault("roles", {})
                    roles.setdefault("main", {})["model"] = default_m["id"]
                    roles["main"]["base_url"] = default_m["base_url"]
                    model_key = _resolve_model_key(default_m)
                    if model_key:
                        try:
                            with self.turn_lock:
                                self._reload_model_clients(api_key_override=model_key)
                        except Exception:
                            pass
                return {"ok": True, "deleted": model_id}
            return {"ok": False, "error": "未找到该自定义模型或预设模型不可删除"}

        # 静态文件
        @app.route("/")
        def index():
            return static_file("index.html", root=str(UI_DIR))

        @app.route("/takeover")
        @app.route("/takeover.html")
        def takeover():
            return static_file("takeover.html", root=str(UI_DIR))

        @app.route("/static/<filepath:path>")
        def server_static(filepath):
            return static_file(filepath, root=str(UI_DIR / "static"))

        @app.route("/uploads/<filepath:path>")
        def get_upload(filepath):
            return static_file(filepath, root=str(self.data_dir / "uploads"))

        # 智能体形象动图路由（支持纯英文别名与原始中文路径）
        AVATAR_MAP = {
            "panda": {
                "dir": "熊猫",
                "idle.gif": "安静坐着，眨眼.gif",
                "thinking.gif": "连接中：加载_转圈圈.gif",
                "acting.gif": "工作中：埋头忙碌的样子.gif",
                "crafting.gif": "正在制作：动手做东西（敲敲打打）.gif",
                "waiting.gif": "等子任务：张望_等待状.gif",
                "done.gif": "里程碑升级：欢呼庆祝.gif",
                "preview.png": "ChatGPT 图像 2026年10月12日 21_39_04-1.png",
            },
            "dragon": {
                "dir": "龙",
                "idle.gif": "安静坐着，眨眼.gif",
                "thinking.gif": "连接中：加载_转圈圈.gif",
                "acting.gif": "工作中：埋头忙碌的样子.gif",
                "crafting.gif": "正在制作：动手做东西（敲敲打打）.gif",
                "waiting.gif": "等子任务：张望_等待状.gif",
                "done.gif": "里程碑升级：欢呼庆祝.gif",
                "preview.png": "ChatGPT 图像 2026年10月12日 21_41_02-1.png",
            }
        }

        @app.route("/avatar/<filepath:path>")
        def server_avatar(filepath):
            import urllib.parse
            filepath = urllib.parse.unquote(filepath, encoding="utf-8")
            parts = filepath.strip("/").split("/", 1)
            avatar_root = UI_DIR / "形象"
            if len(parts) == 2 and parts[0] in AVATAR_MAP:
                theme_cfg = AVATAR_MAP[parts[0]]
                file_alias = parts[1]
                target_filename = theme_cfg.get(file_alias, file_alias)
                target_dir = avatar_root / theme_cfg["dir"]
                return static_file(target_filename, root=str(target_dir))
            return static_file(filepath, root=str(avatar_root))

        @app.route("/api/avatar/themes", method="GET")
        def avatar_themes():
            return {
                "themes": [
                    {
                        "id": "panda",
                        "name": "熊猫 (Panda)",
                        "desc": "国宝呆萌 · 专注高效",
                        "icon": "🐼",
                        "preview": "/avatar/panda/preview.png",
                        "states": {
                            "idle": "/avatar/panda/idle.gif",
                            "thinking": "/avatar/panda/thinking.gif",
                            "acting": "/avatar/panda/acting.gif",
                            "crafting": "/avatar/panda/crafting.gif",
                            "awaiting_approval": "/avatar/panda/waiting.gif",
                            "done": "/avatar/panda/done.gif",
                        }
                    },
                    {
                        "id": "dragon",
                        "name": "龙 (Dragon)",
                        "desc": "祥瑞灵动 · 气势磅礴",
                        "icon": "🐲",
                        "preview": "/avatar/dragon/preview.png",
                        "states": {
                            "idle": "/avatar/dragon/idle.gif",
                            "thinking": "/avatar/dragon/thinking.gif",
                            "acting": "/avatar/dragon/acting.gif",
                            "crafting": "/avatar/dragon/crafting.gif",
                            "awaiting_approval": "/avatar/dragon/waiting.gif",
                            "done": "/avatar/dragon/done.gif",
                        }
                    },
                    {
                        "id": "otter",
                        "name": "水獭 (Otter)",
                        "desc": "经典原版 · 活泼灵动",
                        "icon": "🦦",
                        "preview": "",
                        "states": {}
                    }
                ]
            }

        # SSE 实时事件流
        @app.route("/api/events")
        def events():
            response.content_type = "text/event-stream"
            response.headers["Cache-Control"] = "no-cache"
            response.headers["X-Accel-Buffering"] = "no"

            q = self.event_bus.subscribe()

            def stream():
                try:
                    yield f"event: ping\ndata: {json.dumps({'time': time.time()})}\n\n"
                    while True:
                        try:
                            msg = q.get(timeout=15)
                            yield msg
                        except queue.Empty:
                            yield ": ping\n\n"
                except GeneratorExit:
                    pass
                finally:
                    self.event_bus.unsubscribe(q)

            return stream()

        # 状态快照接口（重连与刷新预热）
        @app.route("/api/state/snapshot", method="GET")
        def get_snapshot():
            self.state_snapshot["approvals"] = self.approval.list_pending()
            if hasattr(self.approval, "list_history"):
                self.state_snapshot["approval_history"] = self.approval.list_history(limit=50)
            else:
                self.state_snapshot["approval_history"] = []
            active_takeover = None
            if hasattr(self, "takeover_mgr") and self.takeover_mgr:
                with self.takeover_mgr._lock:
                    for tid, req in self.takeover_mgr._requests.items():
                        if req.status == "active":
                            active_takeover = {
                                "task_id": req.task_id,
                                "target": req.target,
                                "reason": req.reason,
                                "page_url": req.page_url,
                                "screenshot_path": req.screenshot_path,
                                "requested_at": req.requested_at,
                                "expires_at": req.expires_at,
                            }
                            break
            self.state_snapshot["active_takeover"] = active_takeover
            return json.dumps(self.state_snapshot, ensure_ascii=False)

        # 2.1 会话接口
        @app.route("/api/sessions", method="GET")
        def list_sessions():
            rows = self.store.db.execute(
                "SELECT session_id, title, started_ts FROM sessions ORDER BY started_ts DESC"
            ).fetchall()
            if not rows:
                sid = self.store.new_session("默认会话")
                rows = [(sid, "默认会话", time.time())]
            return json.dumps([
                {
                    "id": r[0],
                    "title": r[1] or "会话",
                    "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[2])),
                }
                for r in rows
            ])

        @app.route("/api/sessions", method="POST")
        def create_session():
            data = request.json or {}
            title = data.get("title", "新会话")
            sid = self.store.new_session(title)
            return {
                "id": sid,
                "title": title,
                "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time())),
            }

        @app.route("/api/sessions/<sid>", method="DELETE")
        def delete_session(sid):
            with self.turn_lock:
                from tools.desktop_runtime import clear_scope
                self.store.delete_session(sid)
                clear_scope(sid)
                channel = getattr(self, "weixin_channel", None)
                if channel and getattr(channel, "weixin_session_id", None) == sid:
                    channel.weixin_session_id = None
            return {"deleted": sid}

        @app.route("/api/sessions/<sid>/messages", method="GET")
        def get_messages(sid):
            history = self.store.load_history(sid)
            # 建立 tool_call_id -> result 映射
            tool_results = {m.tool_call_id: m for m in history if m.role == "tool" and m.tool_call_id}

            ui_messages = []
            for i, m in enumerate(history):
                if m.role == "tool" or (m.role == "user" and m.name in {"tool_observation", "verification_feedback", "plan_step"}):
                    # 工具返回与自动视觉观察保留在模型历史中，不展示为用户聊天气泡。
                    continue

                content = m.content or ""
                stripped = content.strip()
                # 过滤历史记录中残存的裸 JSON 气泡
                if stripped.startswith('{"name":') or stripped.startswith('{\n  "name":'):
                    content = ""

                tool_calls_info = []
                if m.tool_calls:
                    for tc in m.tool_calls:
                        res = tool_results.get(tc.id, "")
                        cancelled = isinstance(res, Message) and res.metadata.get("tool_cancelled") is True
                        is_err = cancelled or tool_result_is_error(res)
                        result_content = res.content if isinstance(res, Message) else res
                        tool_calls_info.append({
                            "tool": tc.name,
                            "args": tc.arguments,
                            "summary": tool_summary(tc.name, tc.arguments),
                            "status": "cancelled" if cancelled else ("error" if is_err else "ok"),
                            "result": result_content,  # 返回全文供右栏/详情查看
                            "result_summary": result_summary(res, max_len=60),
                        })

                # B7: 历史记录过滤潜在密钥
                if content:
                    content = self.vault.scrub(content)

                # 区分消息类型：user, assistant_final, tool_step
                msg_type = "user" if m.role == "user" else ("assistant_final" if content else "tool_step")
                if not content and not tool_calls_info and m.role == "assistant":
                    continue

                created_ts = getattr(m, "created_at", None) or time.time()
                mid = getattr(m, "id", None) or f"msg_{sid}_{i}"
                ui_messages.append({
                    "id": mid,
                    "type": msg_type,
                    "role": "agent" if m.role == "assistant" else "user",
                    "content": content,
                    "tool_calls": tool_calls_info,
                    "created_at": time.strftime("%H:%M", time.localtime(created_ts)),
                })
            return json.dumps(ui_messages)

        # 2.2 聊天接口
        @app.route("/api/chat", method="POST")
        def chat():
            data = request.json or {}
            sid = data.get("session_id")
            content = (data.get("content") or "").strip()
            if not content:
                response.status = 400
                return {"error": "empty_content"}

            if not sid:
                sid = self.store.new_session(content[:15] or "新会话")

            with self._conversation_turn(sid):
                # 恢复会话历史
                self.orch.history = self.store.load_history(sid)

                # 自动基于第一条用户消息更新会话标题
                if not self.orch.history:
                    new_title = content[:15] + ("..." if len(content) > 15 else "")
                    self.store.update_title(sid, new_title)

                # 立即持久化用户消息，支持多模态图片传入（spec §24）
                images_raw = data.get("images") or []
                image_parts = []
                for img_item in images_raw:
                    if isinstance(img_item, dict):
                        b64 = img_item.get("data_base64") or img_item.get("data") or ""
                        mime = img_item.get("mime_type") or img_item.get("mime") or "image/jpeg"
                    elif isinstance(img_item, str):
                        if img_item.startswith("data:"):
                            header, b64 = img_item.split(",", 1)
                            mime = header.split(";")[0].replace("data:", "")
                        else:
                            b64, mime = img_item, "image/jpeg"
                    else:
                        continue
                    image_parts.append(ImagePart(data_base64=b64, mime_type=mime))

                user_msg = Message(role="user", content=content, images=image_parts)
                self.orch.history.append(user_msg)
                self.store.save_history(sid, self.orch.history)

                try:
                    self.memory.maybe_remember(content)
                    raw_reply = self.orch.handle(user_msg)
                    reply_text = self.vault.scrub(raw_reply)

                    # 持久化更新后的会话历史
                    self.store.save_history(sid, self.orch.history)

                    # 获取本轮工具调用情况并丰富人话摘要
                    tool_calls = []
                    for rec in self.orch.last_turn_tool_records:
                        rec_copy = dict(rec)
                        rec_copy["summary"] = tool_summary(rec.get("tool", ""), rec.get("args"))
                        rec_copy["result_summary"] = result_summary(rec.get("result", ""), max_len=60)
                        tool_calls.append(rec_copy)

                    msg_obj = {
                        "id": f"msg_{int(time.time()*1000)}",
                        "type": "assistant_final" if reply_text else "tool_step",
                        "role": "agent",
                        "content": reply_text,
                        "tool_calls": tool_calls,
                        "status": getattr(self.orch, "last_turn_status", "done"),
                        "session_id": sid,
                        "created_at": time.strftime("%H:%M", time.localtime(time.time())),
                    }

                    # 广播给客户端
                    self.event_bus.broadcast("message", msg_obj)

                    # 异步触发会话记忆提炼 (memory-pipeline.md)
                    def _bg_distill_session(session_id=sid):
                        try:
                            run_memory_distill(
                                memory=self.memory,
                                store=self.store,
                                tracer=self.tracer,
                                scope="session",
                                session_id=session_id,
                                vault=self.vault,
                                cfg=self.cfg,
                            )
                        except Exception as exc:
                            if self.tracer:
                                try:
                                    self.tracer.log_event(
                                        "memory_distill_error",
                                        {"error_type": type(exc).__name__, "session_id": session_id},
                                    )
                                except Exception:
                                    pass

                    try:
                        threading.Thread(
                            target=_bg_distill_session,
                            daemon=True,
                        ).start()
                    except Exception:
                        pass

                    return {"message": msg_obj}

                except Exception as e:
                    msg_obj = self._record_chat_error(sid, e)
                    response.status = 500
                    return {"error": msg_obj["content"], "message": msg_obj}

        # 2.3 审批接口
        @app.route("/api/approvals/pending", method="GET")
        def get_approvals():
            return json.dumps(self.approval.list_pending())

        @app.route("/api/approvals/history", method="GET")
        def get_approval_history():
            try:
                limit = int(request.query.get("limit", 50))
            except Exception:
                limit = 50
            return json.dumps(self.approval.list_history(limit=limit))

        @app.route("/api/approvals/<aid>/resolve", method="POST")
        def resolve_approval(aid):
            data = request.json or {}
            approved = bool(data.get("approved"))
            scope = data.get("scope", "once")
            try:
                ok = self.approval.resolve_card(aid, approved, scope)
                if not ok:
                    response.status = 404
                    return {"error": "not_found"}
                return {"approval_id": aid, "approved": approved, "scope": scope}
            except ValueError as e:
                response.status = 400
                return {"error": str(e)}

        @app.route("/api/approvals/toast_resolve", method=["GET", "POST"])
        def toast_resolve():
            aid = request.query.get("id") or (request.json or {}).get("id")
            token = request.query.get("token") or request.headers.get("X-AlterEgo-Token") or ""
            import secrets
            if self.api_token and token and not secrets.compare_digest(token, self.api_token):
                response.status = 401
                return "<p style='font-family: sans-serif; color: red;'>未授权：Token 无效</p>"
            if not aid:
                response.status = 400
                return "<p style='font-family: sans-serif; color: red;'>错误：缺少审批 ID</p>"

            approved_val = request.query.get("approved", "1")
            approved = str(approved_val).lower() in ("1", "true", "yes")
            scope = request.query.get("scope", "once")

            try:
                ok = self.approval.resolve_card(aid, approved, scope)
                if ok:
                    self.emit_event("approval_resolved", {
                        "approval_id": aid,
                        "approved": approved,
                        "scope": scope,
                    })
            except Exception:
                ok = False

            if request.headers.get("X-AlterEgo-Token") or "application/json" in request.headers.get("Accept", ""):
                return {"ok": bool(ok), "approval_id": aid, "approved": approved, "scope": scope}

            action_text = "✔ 已批准授权" if approved else "✕ 已拒绝授权"
            color = "#10b981" if approved else "#ef4444"
            status_text = "操作成功生效，AlterEgo 正在继续推进" if ok else "审批已过期或此前已处理"

            html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>AlterEgo 授权决断</title>
    <style>
        body {{
            background: #0f172a;
            color: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }}
        .card {{
            background: #1e293b;
            border: 1px solid #334155;
            padding: 32px 40px;
            border-radius: 16px;
            text-align: center;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            max-width: 400px;
        }}
        h2 {{ margin: 0 0 12px; color: {color}; font-size: 22px; }}
        p {{ color: #94a3b8; font-size: 14px; line-height: 1.5; margin: 0 0 20px; }}
        .hint {{ font-size: 12px; color: #64748b; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>{action_text}</h2>
        <p>{status_text}</p>
        <div class="hint">窗口将在 1 秒后自动关闭…</div>
    </div>
    <script>
        setTimeout(() => {{
            window.close();
        }}, 1200);
    </script>
</body>
</html>"""
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            return html

        # 2.3.2 桌面窗口分屏比例动态联动接口
        @app.route("/api/window/split_ratio", method=["POST", "GET"])
        def update_split_ratio():
            if request.method == "GET":
                from ui.window_layout import get_split_layout
                cur_ratio = getattr(self, "_current_browser_ratio", 0.80)
                return json.dumps(get_split_layout(browser_ratio=cur_ratio))

            data = request.json or {}
            try:
                browser_ratio = float(data.get("browser_ratio", 0.80))
            except Exception:
                browser_ratio = 0.80

            self._current_browser_ratio = browser_ratio
            from ui.window_layout import get_split_layout
            layout = get_split_layout(browser_ratio=browser_ratio)
            ui_geo = layout["ui"]
            b_geo = layout["browser"]

            # 1. 调整原生 AlterEgo UI 桌面窗口尺寸与坐标
            if getattr(self, "_ui_window", None):
                try:
                    self._ui_window.move(ui_geo["x"], ui_geo["y"])
                    self._ui_window.resize(ui_geo["width"], ui_geo["height"])
                except Exception as _werr:
                    logger.debug("Move/resize ui window error: %s", _werr)

            # 2. 调整 Edge 浏览器窗口边界
            bw = getattr(getattr(self, "registry", None), "browser_worker", None)
            if bw and hasattr(bw, "set_split_ratio"):
                try:
                    bw.set_split_ratio(browser_ratio)
                except Exception as _berr:
                    logger.debug("Set browser worker split ratio error: %s", _berr)

            return json.dumps({
                "ok": True,
                "browser_ratio": browser_ratio,
                "layout": layout,
            })

        # 2.4 任务接口
        @app.route("/api/tasks/running", method="GET")
        def get_tasks():
            if not self.tracker:
                return json.dumps([])
            tasks = []
            for tid, t in self.tracker._tasks.items():
                if t.status == "running":
                    tasks.append({
                        "task_id": tid,
                        "name": t.name or "执行任务",
                        "progress": round(t.progress, 1),
                        "status": "running",
                    })
            return json.dumps(tasks)

        @app.route("/api/tasks/history", method="GET")
        def get_tasks_history():
            if not self.tracker:
                return json.dumps([])
            tasks = []
            for p in self.tracker.list_all():
                tasks.append({
                    "task_id": p.task_id,
                    "name": p.name,
                    "status": p.status,
                    "progress": round(p.progress, 1),
                    "steps": [
                        {
                            "step_id": s.step_id,
                            "name": s.name,
                            "status": s.status,
                            "started_at": time.strftime("%H:%M:%S", time.localtime(s.started_at)) if s.started_at else "",
                            "ended_at": time.strftime("%H:%M:%S", time.localtime(s.ended_at)) if s.ended_at else "",
                            "detail": s.detail,
                        }
                        for s in p.steps
                    ],
                })
            return json.dumps(tasks)

        # 2.5 接管接口
        @app.route("/api/takeover/<tid>/status", method="GET")
        def takeover_status(tid):
            return self.takeover_mgr.get_status(tid)

        @app.route("/api/takeover/<tid>/done", method="POST")
        def takeover_done(tid):
            res = self.takeover_mgr.resolve(tid, "completed", summary="用户点击完成")
            return {"task_id": tid, "status": res.outcome}

        @app.route("/api/takeover/<tid>/cancel", method="POST")
        def takeover_cancel(tid):
            res = self.takeover_mgr.resolve(tid, "cancelled", summary="用户点击取消")
            return {"task_id": tid, "status": res.outcome}

        @app.route("/api/takeover/<tid>/focus", method=["GET", "POST"])
        def takeover_focus(tid):
            token = request.query.get("token") or request.headers.get("X-AlterEgo-Token") or ""
            import secrets
            if self.api_token and token and not secrets.compare_digest(token, self.api_token):
                response.status = 401
                return {"error": "unauthorized"}

            bw = getattr(getattr(self, "registry", None), "browser_worker", None)
            if bw and hasattr(bw, "focus_window"):
                try:
                    bw.focus_window()
                except Exception:
                    pass
            else:
                try:
                    from tools.windows_notify import focus_edge_browser_window
                    focus_edge_browser_window()
                except Exception:
                    pass
            return {"task_id": tid, "status": "focused"}

        @app.route("/api/takeover/request", method="POST")
        def takeover_request_api():
            data = request.json or {}
            tid = data.get("task_id") or f"takeover_{uuid.uuid4().hex[:8]}"
            reason = data.get("reason") or "用户主动接管"
            target = data.get("target") or "Chrome"
            page_url = data.get("page_url") or ""
            req = self.takeover_mgr.request_takeover(
                task_id=tid,
                reason=reason,
                target=target,
                page_url=page_url,
            )
            return {"task_id": tid, "status": req.status}

        # 2.6 上传接口
        @app.route("/api/uploads", method="POST")
        def upload_file():
            upload = request.files.get("file")
            if not upload:
                response.status = 400
                return {"error": "no_file"}
            uploads_dir = self.data_dir / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            uid = f"up_{uuid.uuid4().hex[:10]}"
            ext = Path(upload.filename).suffix or ".bin"
            fname = f"{uid}{ext}"
            save_path = uploads_dir / fname
            upload.save(str(save_path), overwrite=True)
            return {
                "upload_id": uid,
                "filename": upload.filename,
                "url": f"/uploads/{fname}",
                "path": str(save_path),
            }

        # 2.7 设置与监控接口
        @app.route("/api/settings", method="GET")
        def get_settings():
            sess_count = len(self.store.list_sessions(1000))
            appr_stats = self.approval.stats()
            cur_model = self._model_name(self.llm)
            cur_base_url = getattr(self.llm, "base_url", "")
            has_cur_key = bool(_resolve_model_key({"id": cur_model, "base_url": cur_base_url}))
            has_llm_key = self.vault.has("llm_api_key") or has_cur_key
            has_backup_key = self.vault.has("backup_llm_api_key")
            provider_keys = {}
            router_cfg = self.cfg.get("llm_router", {})
            if router_cfg.get("enabled"):
                for provider in router_cfg.get("providers", {}):
                    provider_keys[provider] = self.vault.has(f"{provider}_api_key")
                has_llm_key = has_llm_key or any(provider_keys.values())
                has_backup_key = has_backup_key or sum(provider_keys.values()) > 1
            return {
                "model": cur_model,
                "base_url": cur_base_url,
                "has_key": has_cur_key,
                "fallback_model": self._model_name(getattr(self.llm, "fallback", None)) or None,
                "current_model": self.state_snapshot["status"].get("current_model", ""),
                "provider_keys": provider_keys,
                "max_context_tokens": self.cfg.get("context", {}).get("max_context_tokens", 8000),
                "has_llm_key": has_llm_key,
                "has_backup_key": has_backup_key,
                "vault_backend": "Windows DPAPI (Hardware / Current User bound)",
                "sessions_count": sess_count,
                "approvals_stats": appr_stats,
                "tools_count": len(self.registry._tools),
            }

        @app.route("/api/settings/test_llm", method="POST")
        def test_llm():
            try:
                data = request.json or {}
            except Exception:
                data = {}
            test_model = str(data.get("model") or data.get("model_id") or "").strip()
            test_base_url = str(data.get("base_url") or "").strip()
            test_api_key = str(data.get("api_key") or "").strip()

            target_client = self.llm
            if test_model or test_base_url or test_api_key:
                final_model = test_model or self._model_name(self.llm)
                final_base_url = test_base_url or getattr(self.llm, "base_url", "")
                final_key = test_api_key or _resolve_model_key({"id": final_model, "base_url": final_base_url})
                if not final_key:
                    response.status = 400
                    return {"status": "error", "message": f"模型 {final_model} 尚未配置 API Key，请先输入密钥"}
                from agent.llm import LLMClient
                target_client = LLMClient(
                    base_url=final_base_url,
                    model=final_model,
                    api_key=final_key,
                    max_tokens=64,
                )

            try:
                res = target_client.chat([Message(role="user", content="连通性测试，请回复OK")])
                return {"status": "ok", "reply": res.content}
            except Exception as e:
                response.status = 500
                return {"status": "error", "message": str(e)}

        @app.route("/api/settings/vault_key", method="POST")
        def set_vault_key():
            data = request.json or {}
            key = (data.get("key") or "").strip()
            name = (data.get("name") or "llm_api_key").strip()
            if not key:
                response.status = 400
                return {"error": "empty_key"}
            if name not in self.vault.ALLOWLIST:
                response.status = 400
                return {"error": "disallowed_key_name"}
            with self.turn_lock:
                self.vault.set(name, key)
                self._reload_model_clients()
            return {"status": "saved"}

        # 2.7.1 桌面窗口控制接口 (最小化、最大化/还原、关闭)
        @app.route("/api/window/control", method="POST")
        def window_control():
            data = request.json or {}
            action = str(data.get("action") or "").lower().strip()
            win = getattr(self, "_ui_window", None)
            if not win:
                return {"ok": False, "reason": "no_desktop_window"}
            try:
                if action == "minimize":
                    if hasattr(win, "minimize"):
                        win.minimize()
                elif action == "maximize":
                    is_max = getattr(win, "maximized", False) or getattr(win, "state", None) == "maximized"
                    if is_max:
                        if hasattr(win, "restore"):
                            win.restore()
                    else:
                        if hasattr(win, "maximize"):
                            win.maximize()
                elif action == "close":
                    if hasattr(win, "destroy"):
                        win.destroy()
                return {"ok": True, "action": action}
            except Exception as e:
                logger.warning("[window_control] 操作失败 %s: %s", action, e)
                return {"ok": False, "error": str(e)}

        @app.route("/api/window/status", method="GET")
        def window_status():
            win = getattr(self, "_ui_window", None)
            if not win:
                return {"is_desktop": False, "maximized": False}
            is_max = getattr(win, "maximized", False) or getattr(win, "state", None) == "maximized"
            return {"is_desktop": True, "maximized": bool(is_max)}

        # 2.8 记忆库接口
        @app.route("/api/memory", method="GET")
        def get_memory():
            content = self.memory.read_main()
            return {"content": content}

        @app.route("/api/memory", method="POST")
        def update_memory():
            data = request.json or {}
            content = data.get("content", "")
            (self.data_dir / "memory" / "MEMORY.md").write_text(content, encoding="utf-8")
            self.ctx.extra_system = content
            return {"status": "saved"}

        # 2.9 微信 ClawBot 通道接口
        @app.route("/api/weixin/status", method="GET")
        def weixin_status():
            if not getattr(self, "weixin_channel", None):
                return {"connected": False, "status": "disabled", "message": "微信通道未启用"}
            return self.weixin_channel.get_status()

        @app.route("/api/weixin/login", method="POST")
        def weixin_login():
            if not getattr(self, "weixin_channel", None):
                return HTTPResponse(status=500, body=json.dumps({"error": "微信通道未启用"}))
            res = self.weixin_channel.start_qr_login(force=True)
            return res

        @app.route("/api/weixin/qr_img", method="GET")
        def weixin_qr_img():
            if not getattr(self, "weixin_channel", None) or not self.weixin_channel.qr_img_path.exists():
                return HTTPResponse(status=404, body="QR image not found")
            return static_file(
                self.weixin_channel.qr_img_path.name,
                root=str(self.weixin_channel.qr_img_path.parent),
                mimetype="image/png",
            )



def run_server(port: int = 5000, host: str = "127.0.0.1"):
    server = AgentServer()
    server.app.run(server=ThreadedWSGIServer(host=host, port=port), quiet=False)


if __name__ == "__main__":
    run_server()
