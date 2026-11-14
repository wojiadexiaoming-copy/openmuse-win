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
from agent import APP_VERSION
from agent.context import ContextBuilder
from agent.evolution_tools import make_evolution_tools
from agent.experience import ExperienceStore
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
from agent.skill_distiller import SkillManager
from agent.skill_review import run_skill_review
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

# 会话级 SSE 事件：统一补 session_id，前端据此把微信/桌面的进度、审批
# 分流到各自会话，避免微信任务的思考/授权状态串到当前打开的桌面会话。
SESSION_SCOPED_EVENTS = frozenset({
    "agent_state", "plan_update", "tool_call_started", "tool_call_done",
    "subagent_update", "task_progress", "task_update", "turn_notice",
    "approval_request", "approval_resolved", "takeover_request", "takeover_update",
})


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
    # v2.5 U1：审批类事件不允许被队列满静默丢弃——丢一张卡 = 用户 300s 后
    # 才发现任务"莫名失败"。队列满时挤掉最旧一条普通事件为其腾位。
    PRIORITY_EVENTS = {"approval_request"}

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
                    if event in self.PRIORITY_EVENTS:
                        try:
                            q.get_nowait()  # 丢弃最旧的普通事件，保审批卡必达
                            q.put_nowait(msg)
                        except (queue.Empty, queue.Full):
                            pass


class AgentServer:
    def __init__(self, data_dir: str | Path | None = None, startup_progress=None):
        self._startup_progress = startup_progress
        from agent.paths import resolve_data_dir
        self.data_dir = resolve_data_dir(data_dir)
        self._startup_phase("load_config")
        raw_cfg = load_config()
        from agent.config_schema import Config
        try:
            validated = Config.model_validate(raw_cfg)
            self.cfg = validated.model_dump()
            if not validated.router_enabled:
                logger.warning("llm_router 未启用或配置缺失，走 LLMClient 主备降级")
        except Exception as e:
            logger.error("配置校验失败: %s", e, exc_info=True)
            raise

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

        self._port = 5000
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
        self.llm_key = llm_key = resolve_llm_key(self.vault)
        self.backup_key = backup_key = resolve_backup_llm_key(self.vault)
        self.llm = LLMClient.from_config(
            self.cfg, api_key=llm_key, fallback_api_key=backup_key, vault=self.vault, allow_unconfigured=True)

        # 热计算：后台守护线程预热云端连接池与 TLS 握手，消除用户首轮请求的冷握手延迟
        def _bg_prewarm_connections():
            try:
                if hasattr(self, "llm") and self.llm:
                    self.llm.prewarm()
                    visited = {id(self.llm)}
                    cur = getattr(self.llm, "fallback", None)
                    while cur is not None and id(cur) not in visited:
                        visited.add(id(cur))
                        cur.prewarm()
                        cur = getattr(cur, "fallback", None)
            except Exception:
                pass
        threading.Thread(target=_bg_prewarm_connections, daemon=True, name="alterego-cloud-prewarm").start()

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
            # 审批归属会话：微信发起的任务同样走这条回调，卡片必须记住
            # 自己属于哪个会话，前端才能把授权卡放回微信会话而不是桌面会话。
            card.session_id = getattr(self.orch, "session_id", "") or ""
            card_dict = {
                "approval_id": card.approval_id,
                "session_id": card.session_id,
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
        self.experience = ExperienceStore(str(self.data_dir / "experience.db"))
        self.skills = SkillManager(self.data_dir / "skills.json")
        for tool in make_evolution_tools(self.memory, self.skills):
            if tool.name not in self.registry:
                self.registry.register(tool)
        self.skills.register_all(self.registry)

        self.takeover_mgr = TakeoverManager(
            event_bus=self.event_bus,
            on_event=self.emit_event,
            # §39.18：接管不自动弹桌面浮窗，避免抢焦点盖住待操作的浏览器；
            # 主窗口隐藏时托盘浮窗会按快照自动切到接管卡片。
            auto_show_window=False,
            port=self.port,
            token=self.api_token,
        )
        set_takeover_manager(self.takeover_mgr)

        summarizer = None
        try:
            from agent.compaction import make_worker_summarizer
            summarizer = make_worker_summarizer(self.cfg, self.vault)
        except Exception:
            pass

        self.ctx = ContextBuilder(
            max_tokens=self.cfg["context"]["max_context_tokens"],
            extra_system=self.memory.read_main,
            summarizer=summarizer,
        )
        self._startup_phase("open_conversation_database")
        self.store = SessionStore(str(self.data_dir / "conversations.db"))
        # 启动时检查未完成任务：在归档前先看最新 session 是否有 incomplete 任务
        # 若有，通过 SSE 事件通知前端弹"继续/丢弃"提示
        self._pending_resume_prompt = None
        self._resume_reminded: set[str] = set()
        keep_sid = None
        try:
            sessions = self.store.list_sessions(limit=1)
            if sessions:
                latest_sid = sessions[0].get("session_id") or sessions[0].get("id")
                if latest_sid:
                    keep_sid = latest_sid
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
        # 归档旧 session：只归档更早的历史会话，保留最近会话（keep_session_id=keep_sid）
        _archive_dir = str(self.data_dir / "archive")
        try:
            _archived = self.store.archive_old_sessions(_archive_dir, keep_session_id=keep_sid)
            if _archived:
                import logging as _log
                _log.getLogger(__name__).info("archived %d old session(s): %s", len(_archived), _archived)
        except Exception:
            pass  # 归档失败不阻断启动

        # B4-04 保留期自动清理 (archive, uploads, logs, spans.db)
        try:
            from agent.retention import run_retention_cleanup
            _ret_cfg = self.cfg.get("retention", {}) if isinstance(self.cfg, dict) else {}
            _ret_stats = run_retention_cleanup(
                self.data_dir,
                archive_days=int(_ret_cfg.get("archive_days", 30)),
                upload_days=int(_ret_cfg.get("upload_days", 30)),
                log_days=int(_ret_cfg.get("log_days", 14)),
                span_days=int(_ret_cfg.get("span_days", 14)),
                tracer=self.tracer,
            )
            logger.info("Startup retention cleanup: %s", _ret_stats)
        except Exception as _ret_err:
            logger.debug("Startup retention cleanup error: %s", _ret_err)

        self._startup_phase("initialize_planner")
        planner_llm = LLMClient.for_role(
            self.cfg, "planner", self.vault,
            api_key=getattr(self, "llm_key", ""),
            fallback_api_key=getattr(self, "backup_key", ""),
            allow_unconfigured=True,
        )
        self._bind_model_observer(planner_llm, "planner")
        self.planner = Planner(planner_llm)

        def _make_sub_orchestrator(task):
            role = "code" if getattr(task, "kind", "generic") == "code" else "subagent"
            if (role == "code" and not (self.cfg.get("llm_router") or {}).get("enabled")
                    and "code" not in self.cfg.get("models", {}).get("roles", {})):
                role = "subagent"
            sub_llm = LLMClient.for_role(
                self.cfg, role, self.vault,
                api_key=getattr(self, "llm_key", ""),
                fallback_api_key=getattr(self, "backup_key", ""),
                allow_unconfigured=True,
            )
            if getattr(task, "critical", False):
                sub_llm = LLMClient.for_role(
                    self.cfg, self.cfg.get("models", {}).get("critical_subagent_role", "main"), self.vault,
                    api_key=getattr(self, "llm_key", ""),
                    fallback_api_key=getattr(self, "backup_key", ""),
                    allow_unconfigured=True,
                )
            self._bind_model_observer(sub_llm, "code" if role == "code" else "subagent")
            sub_prompt_file = Path("prompts/subagent.md")
            sub_prompt = sub_prompt_file.read_text(encoding="utf-8") if sub_prompt_file.exists() else ""
            sub_ctx = ContextBuilder(
                max_tokens=self.cfg["context"]["max_context_tokens"] // 2,
                extra_system=sub_prompt,
            )
            agent_cfg = self.cfg.get("agent", {}) or {}
            return Orchestrator(
                sub_llm, self.registry, sub_ctx,
                max_iters=agent_cfg.get("subagent_max_iters", 20),
                max_iters_hard=agent_cfg.get("subagent_max_iters_hard", 60),
                step_grant=agent_cfg.get("subagent_step_grant", 10),
                max_turn_wall_clock_sec=agent_cfg.get(
                    "subagent_max_turn_sec", 300.0),
                turn_extension_sec=agent_cfg.get(
                    "subagent_turn_extension_sec", 120.0),
                turn_max_extensions=agent_cfg.get(
                    "subagent_max_extensions", 2),
                parallel_tools=False,
                tracer=self.tracer, tracker=self.tracker, planner=None,
                on_event=self.emit_event)

        self._startup_phase("initialize_agent")
        self.sub_mgr = SubagentManager(
            make_orchestrator=_make_sub_orchestrator,
            on_queue_update=lambda qsize: self.emit_event("subagent_update", {"queue_waiting": qsize}),
            store_path=self.data_dir / "subagents.db",
        )
        for t in make_subagent_tools(self.sub_mgr):
            self.registry.register(t)

        # §28 Connectors / §23 Proactivity / §25 Workflow /
        # §26 Artifacts / §30 TTS / §34 备份 / 定时单次提醒 (P1-01 对齐 CLI 注册)
        from agent.tools_registry import ToolDefinition
        from agent.connectors import ConnectorManager, make_connector_tools
        from agent.proactivity import ProactivityStore, make_proactivity_tools
        from agent.workflow import WorkflowEngine, make_workflow_tools
        from agent.artifacts import ArtifactManager, make_artifact_tools
        from agent.tts import TTSManager, make_tts_tools
        from agent.backup import make_backup_tools
        try:
            for t in make_connector_tools(ConnectorManager(vault=self.vault)):
                self.registry.register(t)
        except Exception as e:
            logger.warning("[server] Failed to register connector tools: %s", e)

        try:
            for t in make_proactivity_tools(ProactivityStore()):
                self.registry.register(t)
        except Exception as e:
            logger.warning("[server] Failed to register proactivity tools: %s", e)

        try:
            for t in make_workflow_tools(WorkflowEngine()):
                self.registry.register(t)
        except Exception as e:
            logger.warning("[server] Failed to register workflow tools: %s", e)

        try:
            for t in make_artifact_tools(ArtifactManager()):
                self.registry.register(t)
        except Exception as e:
            logger.warning("[server] Failed to register artifact tools: %s", e)

        try:
            for t in make_tts_tools(TTSManager()):
                self.registry.register(t)
        except Exception as e:
            logger.warning("[server] Failed to register tts tools: %s", e)

        try:
            for t in make_backup_tools():
                self.registry.register(t)
        except Exception as e:
            logger.warning("[server] Failed to register backup tools: %s", e)

        def _schedule_once(args: dict) -> str:
            try:
                secs = int(args.get("seconds", 0))
                brief = str(args.get("brief", "") or "").strip()
                if secs <= 0 or secs > 86400:
                    return "[工具执行失败] seconds 须在 1~86400 之间"
                if not brief:
                    return "[工具执行失败] brief 不能为空"
                scheduler = getattr(self, "scheduler", None)
                if scheduler is None:
                    return "[工具执行失败] 定时器未就绪"
                jid = f"once-{uuid.uuid4().hex[:8]}"
                scheduler.add(JobSpec(job_id=jid, kind="once", brief=brief,
                                      trigger_args={"seconds": secs}))
                return f"[定时已设置] {secs} 秒后提醒：{brief[:50]}（job={jid}）"
            except Exception as e:
                return f"[工具执行失败] {e}"

        self.registry.register(ToolDefinition(
            name="schedule_once",
            description="N 秒后提醒我做某事（替代 sleep 硬等）。到时会发 Windows 系统通知，不会注入当前对话。",
            parameters={"type": "object",
                         "properties": {
                             "seconds": {"type": "integer", "description": "几秒后"},
                             "brief": {"type": "string", "description": "到时提醒做什么"}},
                         "required": ["seconds", "brief"]},
            func=_schedule_once,
            parallel_safe=False,
            is_readonly=False,
            cacheable=False,
        ))

        self.orch = Orchestrator(
            self.llm,
            self.registry,
            self.ctx,
            max_iters=self.cfg["agent"].get("max_iters", 25),
            max_iters_hard=self.cfg["agent"].get("max_iters_hard", 100),
            step_grant=self.cfg["agent"].get("step_grant", 10),
            max_turn_wall_clock_sec=self.cfg["agent"].get(
                "max_turn_wall_clock_sec", 600.0),
            turn_extension_sec=self.cfg["agent"].get(
                "turn_extension_sec", 180.0),
            turn_max_extensions=self.cfg["agent"].get(
                "turn_max_extensions", 3),
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
            scope = "nightly" if "nightly" in job_id else "hourly"
            try:
                if job_id.startswith("once-"):
                    try:
                        from agent.notify import notify
                        notify(title="AlterEgo 定时提醒", message=brief, urgent=False)
                    except Exception:
                        pass
                    self.emit_event("schedule_fire",
                                    {"job_id": job_id, "brief": brief})
                    return
                if job_id == "exp_decay":
                    self.experience.decay_job()
                    self.tracer.log_event("experience_decay", {"status": "ok"})
                    return
                if job_id == "skill_review":
                    run_skill_review(
                        store=self.store,
                        skill_manager=self.skills,
                        registry=self.registry,
                        tracer=self.tracer,
                        limit=int(self.cfg["agent"].get("skill_review_sessions", 60)),
                    )
                    return
                run_memory_distill(
                    memory=self.memory,
                    store=self.store,
                    tracer=self.tracer,
                    experience_store=self.experience,
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
            self.scheduler.add(JobSpec(
                job_id="exp_decay",
                kind="cron",
                brief="每晚经验置信度衰减与归档",
                trigger_args={"hour": 2, "minute": 10},
            ))
            self.scheduler.add(JobSpec(
                job_id="skill_review",
                kind="cron",
                brief="每晚技能评审与轨迹蒸馏",
                trigger_args={"hour": 2, "minute": 30},
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
            self.cfg, api_key=llm_key, fallback_api_key=backup_key, vault=self.vault, allow_unconfigured=True)
        new_planner = getattr(self, "planner", None)
        if new_planner is not None or self.cfg.get("agent", {}).get("planner", False):
            planner_llm = LLMClient.for_role(
                self.cfg, "planner", self.vault, api_key=llm_key, fallback_api_key=backup_key, allow_unconfigured=True)
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

    @property
    def port(self) -> int:
        return getattr(self, "_port", 5000)

    @port.setter
    def port(self, value: int):
        self._port = int(value)
        if hasattr(self, "takeover_mgr") and self.takeover_mgr is not None:
            self.takeover_mgr.port = self._port

    def set_ui_window(self, window):
        """保存原生桌面 PyWebView 窗口实例，用于分屏联动与窗口控制。"""
        self._ui_window = window
        self._win_is_max = False
        try:
            def _on_max():
                self._win_is_max = True

            def _on_rest():
                self._win_is_max = False

            window.events.maximized += _on_max
            window.events.restored += _on_rest
        except Exception as e:
            logger.debug("[set_ui_window] 窗口状态事件订阅失败: %s", e)
        self._start_tray()

    def _ulog(self, msg):
        """窗口/托盘事件持久化日志（data/logs/ui-window.log）——❌ 决策可追溯。"""
        try:
            import time as _time
            log_dir = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "data", "logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "ui-window.log"), "a", encoding="utf-8") as fp:
                fp.write(_time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
        except Exception:
            pass
        logger.info("[ui-window] %s", msg)

    def _start_tray(self):
        """系统托盘图标（pystray）：左键召回主窗口，右键菜单 打开主窗口/退出。"""
        if os.environ.get("ALTEREGO_HEADLESS") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
            return
        if getattr(self, "_tray_started", False):
            return
        self._tray_started = True
        try:
            import threading
            from PIL import Image
            import pystray
        except Exception as e:
            self._ulog("托盘启动失败：pystray/Pillow 不可用 %s" % e)
            return

        def _show_main(icon=None, item=None):
            win = getattr(self, "_ui_window", None)
            if win:
                try:
                    if hasattr(win, "show"):
                        win.show()
                    if hasattr(win, "restore"):
                        win.restore()
                except Exception:
                    pass

        menu = pystray.Menu(
            pystray.MenuItem("打开主窗口", _show_main, default=True),
            pystray.MenuItem("退出", lambda icon, item: self._really_exit()),
        )
        try:
            icon_path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "ui", "static", "alterego.png")
            img = Image.open(icon_path).convert("RGBA").resize((64, 64))
        except Exception:
            img = pystray.Icon("AlterEgo").icon or None
            img = None
            try:
                from PIL import ImageDraw
                img = Image.new("RGBA", (64, 64), (24, 24, 28, 255))
                d = ImageDraw.Draw(img)
                d.ellipse((8, 8, 56, 56), fill=(236, 72, 153, 255))
                d.text((26, 20), "A", fill=(255, 255, 255, 255))
            except Exception:
                img = Image.new("RGBA", (64, 64), (236, 72, 153, 255))
        try:
            icon = pystray.Icon("AlterEgo", img, "AlterEgo", menu)
            threading.Thread(target=icon.run, daemon=True).start()
            self._ulog("托盘图标已启动（若未见于托盘区，请点任务栏右下角 ^ 展开隐藏图标）")
        except Exception as e:
            self._ulog("托盘图标启动失败: %s" % e)

    def _really_exit(self):
        """真正退出：销毁（可能隐藏的）主窗口并停服。"""
        import threading as _threading

        def _shutdown():
            try:
                win = getattr(self, "_ui_window", None)
                if win and hasattr(win, "destroy"):
                    win.destroy()
            except Exception:
                pass
            try:
                stop = getattr(self, "shutdown", None)
                if callable(stop):
                    stop()
            except Exception:
                pass
            _threading.Timer(2.0, lambda: os._exit(0)).start()

        _threading.Thread(target=_shutdown, daemon=True).start()

    def _has_active_turn(self):
        """任务运行中判定（浮窗触发，MIGRATION.md v1.1.7）：状态机非 idle / 有待审批 / 有接管。"""
        try:
            st = (self.state_snapshot.get("agent_state") or {}).get("state") or "idle"
            if st in ("thinking", "acting", "waiting_subagent", "awaiting_approval",
                      "paused_takeover", "waiting_user"):
                return True
            if self.state_snapshot.get("approvals"):
                return True
            if self.state_snapshot.get("active_takeover"):
                return True
        except Exception:
            pass
        return False

    def _spawn_float_window(self):
        """弹浮窗子进程（同一时刻只保留一个）；复用 takeover_window 的子进程机制。"""
        if os.environ.get("ALTEREGO_HEADLESS") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
            return None
        proc = getattr(self, "_float_proc", None)
        if proc is not None and proc.poll() is None:
            return proc
        try:
            from ui.takeover_window import show_takeover_window_async
            token = getattr(self, "api_token", "")
            proc = show_takeover_window_async(
                task_id="main", target="", reason="主窗口已最小化，任务后台运行中",
                port=int(request.environ.get("SERVER_PORT") or 5000), token=token)
            self._float_proc = proc
        except Exception as e:
            logger.warning("[float] 弹出浮窗失败（任务继续后台运行）: %s", e)
            return None
        logger.info("[float] 浮窗子进程已启动 pid=%s", getattr(proc, "pid", "?"))
        return proc

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

    @staticmethod
    def _resume_status_hint(status: str) -> str:
        return {
            "incomplete": "步骤/时间耗尽，未完成",
            "waiting_user": "停在等待你补充信息",
            "error": "执行出错中断",
            "running": "进程重启中断",
        }.get(status, "未完成")

    def _current_resume_candidate(self) -> dict | None:
        """启动时发现的遗留任务快照；每次读取都回查数据库，完成/丢弃后自动失效。"""
        candidate = getattr(self, "_pending_resume_prompt", None)
        if not candidate:
            return None
        sid = candidate.get("session_id")
        if not sid:
            return None
        try:
            info = self.store.get_incomplete_task(sid)
        except Exception:
            return candidate  # 查询失败时保留快照：宁可提醒一次，也别悄悄丢掉
        if not info:
            self._pending_resume_prompt = None
            return None
        merged = {**candidate, **info}
        self._pending_resume_prompt = merged
        return merged

    def _resume_notice_text(self, sid: str, user_text: str) -> str:
        """重启遗留任务的一次性提醒文本（进 system prompt，不落会话历史）。

        只在新话题时生效：用户主动「继续/重试」或补充等待中的信息时，
        本来就在处理旧任务，再提醒一次只会制造噪声。
        """
        candidate = self._current_resume_candidate()
        if not candidate:
            return ""
        if not self.orch.is_new_topic(user_text):
            return ""
        key = candidate.get("session_id") or sid
        reminded = getattr(self, "_resume_reminded", None)
        if reminded is None:
            reminded = self._resume_reminded = set()
        if key in reminded:
            return ""
        reminded.add(key)
        desc = (candidate.get("task_desc") or "").strip() or "（任务描述缺失）"
        if len(desc) > 120:
            desc = desc[:120] + "…"
        hint = self._resume_status_hint(candidate.get("task_status") or "")
        when = ""
        ts = candidate.get("last_update")
        if isinstance(ts, (int, float)) and ts > 0:
            when = "，最后更新于 " + time.strftime("%m-%d %H:%M", time.localtime(ts))
        return (
            f"上一进程遗留了一个未完成的任务：「{desc}」（状态：{hint}{when}）。\n"
            "用户本条消息看起来是新话题。请先正常完成当前请求，然后在回复末尾用"
            "一句话询问用户是否需要继续处理那个遗留任务；不要为提醒而打断或推迟"
            "当前请求，也不要在用户确认前擅自开始执行旧任务。"
        )

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
        exc_type = type(exc).__name__
        detail = str(exc)
        if "AuthenticationError" in exc_type or "401" in detail:
            error_text = (
                f"⚠️ 大模型鉴权失败 ({exc_type})：API Key 无效或未授权。\n"
                "提示：若您使用的是本地代理密钥（如 eb-local-...），请勿将其用于云端模型（如 Agnes）；"
                "请点击右上方【设置】填写真实有效的 API Key 后重试。"
            )
        elif "InternalServerError" in exc_type or "500" in detail:
            cur_m = getattr(self.llm, "model", "")
            cur_url = getattr(self.llm, "base_url", "")
            error_text = (
                f"⚠️ 模型端点返回服务内部错误 ({exc_type} 500)。\n"
                f"当前访问端点: {cur_m} ({cur_url})\n"
                "原因排查：\n"
                "1. 若使用的是本地代理 (127.0.0.1:53683)，请检查本地代理服务是否正常运行，其绑定的上游账号是否有效登录；\n"
                "2. 若在虚拟机中运行，请确认代理是否开启局域网共享 (Allow LAN) 并改用宿主机局域网 IP；\n"
                "3. 部分本地代理 auto 模式不支持智能体工具链 (Tool Calling)，建议切换至支持 Function Call 的模型（如 Agnes 或阿里云百炼）。"
            )
        else:
            error_text = f"⚠️ 执行出现异常（{exc_type}）：{detail[:160]}，请检查网络或模型配置后重试。"
        error_message = Message(role="assistant", content=error_text)
        error_message.metadata["task_status"] = "error"
        if getattr(self.orch, "_active_task_origin_id", None):
            error_message.metadata["task_origin_id"] = self.orch._active_task_origin_id
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
            "task_origin_id": error_message.metadata.get("task_origin_id") or "",
            "task_status": "error",
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

        if event in SESSION_SCOPED_EVENTS and not data.get("session_id"):
            data = {**data, "session_id": getattr(self.orch, "session_id", "") or ""}

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
        elif event == "model_failover":
            to_model = data.get("to_model")
            if to_model:
                self.state_snapshot["status"]["current_model"] = to_model
                self.state_snapshot["agent_state"]["model"] = to_model
                if self.cfg.get("behavior", {}).get("auto_failover", True):
                    self.cfg.setdefault("llm", {})["model"] = to_model
                    self.cfg.setdefault("models", {}).setdefault("roles", {}).setdefault("main", {})["model"] = to_model
        elif event == "turn_notice":
            sid = getattr(self.orch, "session_id", None)
            if not sid:
                try:
                    sessions = self.store.list_sessions(limit=1)
                    if sessions:
                        sid = sessions[0].get("session_id") or sessions[0].get("id")
                except Exception:
                    sid = None
            self.event_bus.broadcast("message", {
                "id": f"notice_{data.get('id') or int(time.time() * 1000)}",
                "type": "assistant_notice",
                "role": "agent",
                "content": data.get("text", ""),
                "status": "running",
                "session_id": sid,
                "created_at": time.strftime("%H:%M", time.localtime()),
            })
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
                    and not request.path.startswith("/api/weixin/qr_img")):
                auth_header = (request.headers.get("Authorization") or request.environ.get("HTTP_AUTHORIZATION", "")).strip()
                token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else auth_header
                if not token:
                    token = (request.headers.get("X-AlterEgo-Token") or request.environ.get("HTTP_X_ALTEREGO_TOKEN", "")).strip()
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

        @app.route("/api/version", method="GET")
        def version():
            return {"version": APP_VERSION}

        @app.route("/api/pending_resume", method="GET")
        def pending_resume():
            """前端启动时查询：是否有未完成的任务需要提示继续/丢弃。"""
            prompt = self._current_resume_candidate()
            if prompt:
                return {"has_pending": True, "task": prompt}
            return {"has_pending": False}

        @app.route("/api/dismiss_resume", method="POST")
        def dismiss_resume():
            """用户选择不再提醒未完成任务：清除提示并持久化，重启后不再弹。"""
            prompt = getattr(self, "_pending_resume_prompt", None) or {}
            sid = prompt.get("session_id")
            persisted = False
            if sid:
                try:
                    persisted = self.store.mark_task_dismissed(
                        sid, prompt.get("task_origin_id") or "")
                except Exception:
                    persisted = False
                self._resume_reminded.add(sid)
            self._pending_resume_prompt = None
            return {"ok": True, "persisted": persisted}

        @app.route("/api/backup", method="GET")
        def api_list_backups():
            """列出所有历史备份。"""
            from agent.backup import list_backups
            return {"backups": list_backups(workspace=str(self.data_dir))}

        @app.route("/api/backup", method="POST")
        def api_run_backup():
            """触发手动备份或恢复自检。"""
            action = (request.json or {}).get("action", "backup")
            if action == "verify":
                from agent.backup import verify_restore
                v = verify_restore(workspace=str(self.data_dir))
                return {"success": True, "verification": v}
            from agent.backup import backup_job
            zip_p = backup_job(workspace=str(self.data_dir))
            return {
                "success": True,
                "backup_file": zip_p,
                "size": os.path.getsize(zip_p) if os.path.exists(zip_p) else 0
            }

        # 可切换的主模型列表（支持预设推荐、配置文件动态发现与用户自定义模型）
        PRESET_MODELS = [
            {"id": "auto", "name": "EchoBird 智能路由", "display_name": "EchoBird",
             "display_id": "auto",
             "base_url": "http://127.0.0.1:53683/v1",
             "desc": "EchoBird 智能路由 · 本地网关自动负载调度（系统核心锚点）",
             "tag": "核心锚点 · 智能路由",
             "group": "recommended",
             "supports_images": True,
             "key_env": "ECHOBIRD_API_KEY", "vault_key": "echobird_api_key"},
            {"id": "MiniMax-M2.5", "name": "联通云 MiniMax-M2.5", "display_name": "联通云 MiniMax-M2.5",
             "display_id": "MiniMax-M2.5",
             "base_url": "https://aigw-gzgy2.cucloud.cn:8443/v1",
             "desc": "联通云星罗 Token Plan · 商业高并发 · 低延迟 · 精准 Tool Call",
             "tag": "主力推荐 · 商业级",
             "group": "recommended",
             "supports_images": True,
             "key_env": "CUCLOUD_API_KEY", "vault_key": "cucloud_api_key"},
            {"id": "DeepSeek-V4-Flash", "name": "联通云 DeepSeek-V4-Flash", "display_name": "联通云 DeepSeek-V4-Flash",
             "display_id": "DeepSeek-V4-Flash",
             "base_url": "https://aigw-gzgy2.cucloud.cn:8443/v1",
             "desc": "联通云星罗 Token Plan · 深度长程推理 · 支持 Tool Call",
             "tag": "深度推理 · 商业级",
             "group": "recommended",
             "supports_images": True,
             "key_env": "CUCLOUD_API_KEY", "vault_key": "cucloud_api_key"},
            {"id": "aisp-auto-route", "name": "联通云 aisp-auto-route", "display_name": "联通云 aisp-auto-route",
             "display_id": "aisp-auto-route",
             "base_url": "https://aigw-gzgy2.cucloud.cn:8443/v1",
             "desc": "联通云星罗 Token Plan · 智能自动路由调度",
             "tag": "智能路由 · 商业级",
             "group": "recommended",
             "supports_images": True,
             "key_env": "CUCLOUD_API_KEY", "vault_key": "cucloud_api_key"},
            {"id": "qwen3.8-flash", "name": "通义千问 Qwen3.8 Flash (阿里云百炼)", "display_name": "Qwen 3.8 Flash",
             "display_id": "qwen3.8-flash",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "desc": "阿里云百炼官方极速模型 · 响应敏捷 · 原生 Tool Call 支持",
             "tag": "百炼主力 · 极速",
             "group": "recommended",
             "supports_images": True,
             "key_env": "DASHSCOPE_API_KEY", "vault_key": "dashscope_api_key"},
            {"id": "qwen3.8-max-0902", "name": "通义千问 Qwen3.8 Max (阿里云百炼)", "display_name": "Qwen 3.8 Max",
             "display_id": "qwen3.8-max-0902",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "desc": "阿里云百炼旗舰大模型 0902 · 最强综合逻辑理解与复杂任务规划",
             "tag": "百炼旗舰 · 深度",
             "group": "recommended",
             "supports_images": True,
             "key_env": "DASHSCOPE_API_KEY", "vault_key": "dashscope_api_key"},
            {"id": "deepseek-v4.1-flash-1017", "name": "DeepSeek V4.1 Flash (阿里云百炼)", "display_name": "DeepSeek V4.1 Flash",
             "display_id": "deepseek-v4.1-flash-1017",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "desc": "阿里云百炼 · 极速推理与原生 Agent 函数调用 · 免费/高额度",
             "tag": "百炼免费 · 极速",
             "group": "recommended",
             "supports_images": True,
             "key_env": "DASHSCOPE_API_KEY", "vault_key": "dashscope_api_key"},
            {"id": "kimi-k3.1-pro", "name": "Kimi K3.1 Pro (阿里云百炼)", "display_name": "Kimi K3.1 Pro",
             "display_id": "kimi-k3.1-pro",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "desc": "阿里云百炼 · 超长上下文与深度规划 · 原生 Tool Call 支持",
             "tag": "百炼免费 · 深度",
             "group": "recommended",
             "supports_images": True,
             "key_env": "DASHSCOPE_API_KEY", "vault_key": "dashscope_api_key"},
            {"id": "glm-5.3", "name": "GLM 5.3 (阿里云百炼)", "display_name": "GLM 5.3",
             "display_id": "glm-5.3",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "desc": "智谱 GLM 5.3 旗舰 · 中文认知与长程规划优秀",
             "tag": "百炼推荐 · 旗舰",
             "group": "recommended",
             "supports_images": True,
             "key_env": "DASHSCOPE_API_KEY", "vault_key": "dashscope_api_key"},
            {"id": "kimi-k3", "name": "Kimi K3 (阿里云百炼)", "display_name": "Kimi K3",
             "display_id": "kimi-k3",
             "base_url": "https://llm-wwautliy36g7ora8.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
             "desc": "阿里云百炼专属网关 · 超强长上下文与深度推理",
             "tag": "深度推理 · 存活备用",
             "group": "recommended",
             "supports_images": True,
             "key_env": "DASHSCOPE_API_KEY", "vault_key": "dashscope_api_key"},
            {"id": "agnes-3.0-flash", "name": "Agnes 3.0 Flash", "display_name": "Agnes 3.0 Flash",
             "display_id": "agnes-3.0-flash",
             "base_url": "https://apihub.agnes-ai.com/v1",
             "desc": "Agnes API Hub · 全模态零延迟（公网容灾兜底）",
             "tag": "容灾备用",
             "group": "recommended",
             "supports_images": True,
             "key_env": "ALTEREGO_AGNES_API_KEY", "vault_key": "agnes_api_key"},
        ]
        AVAILABLE_MODELS = PRESET_MODELS

        data_dir = getattr(self, "data_dir", None) or Path(".")
        custom_models_file = data_dir / "custom_models.json"
        deleted_models_file = data_dir / "deleted_models.json"

        def _load_deleted_models() -> set[str]:
            if deleted_models_file.is_file():
                try:
                    with open(deleted_models_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        return set(data)
                except Exception as exc:
                    logger.debug("读取 deleted_models.json 异常: %s", exc)
            return set()

        def _save_deleted_models(model_ids: set[str]):
            try:
                with open(deleted_models_file, "w", encoding="utf-8") as f:
                    json.dump(list(model_ids), f, ensure_ascii=False, indent=2)
            except Exception as exc:
                logger.warning("保存 deleted_models.json 异常: %s", exc)

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

        def _model_display_name(mid: str, base_url: str = "") -> str:
            """EchoBird 的内部 ID 固定为 auto，但 UI 只展示 EchoBird。"""
            url = (base_url or "").lower()
            if str(mid or "").strip().lower() == "auto" and ("53683" in url or "echobird" in url):
                return "EchoBird"
            return mid

        def _get_dynamic_models() -> list[dict]:
            """动态组装模型列表：预设模型 + 用户自定义模型 + config.yaml 中配置项 + 当前生效模型。"""
            seen_ids = set()
            deleted_ids = _load_deleted_models()
            anchor_id = PRESET_MODELS[0]["id"]
            result = []

            # 1. 预置基础模型（排除被用户删除的非核心模型）
            for m in PRESET_MODELS:
                mid = m["id"]
                if mid in deleted_ids and mid != anchor_id:
                    continue
                m_copy = dict(m)
                seen_ids.add(mid)
                result.append(m_copy)

            # 2. 用户持久化的自定义模型
            for cm in _load_custom_models():
                mid = cm.get("id")
                if mid and mid not in seen_ids and (mid not in deleted_ids or mid == anchor_id):
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
                if mid and mid not in seen_ids and (mid not in deleted_ids or mid == anchor_id):
                    seen_ids.add(mid)
                    display_name = _model_display_name(mid, scfg.get("base_url") or "")
                    result.append({
                        "id": mid,
                        "name": display_name,
                        "display_name": display_name,
                        "display_id": display_name,
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
            if current_id and current_id not in seen_ids and (current_id not in deleted_ids or current_id == anchor_id):
                seen_ids.add(current_id)
                current_base_url = getattr(current_llm, "base_url", "")
                current_display = _model_display_name(current_id, current_base_url)
                result.insert(0, {
                    "id": current_id,
                    "name": current_display,
                    "display_name": current_display,
                    "display_id": current_display,
                    "base_url": current_base_url,
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

            # 3. 供应商前缀智能推断 (Agnes / DashScope / Gemini / DeepSeek / EchoBird / Anthropic 等)
            provider_candidates = []
            is_specific_provider = False
            if "agnes" in mid or "agnes" in base_url:
                is_specific_provider = True
                provider_candidates.extend([("ALTEREGO_AGNES_API_KEY", "agnes_api_key"), ("AGNES_API_KEY", "")])
            if any(k in mid or k in base_url for k in ("qwen", "dashscope", "kimi", "aliyuncs")):
                is_specific_provider = True
                provider_candidates.extend([("DASHSCOPE_API_KEY", "dashscope_api_key")])
            if "cucloud" in base_url or "cucloud" in mid:
                is_specific_provider = True
                provider_candidates.extend([("CUCLOUD_API_KEY", "cucloud_api_key")])
            if "gemini" in mid or "google" in base_url:
                is_specific_provider = True
                provider_candidates.extend([("ALTEREGO_GEMINI_API_KEY", "gemini_api_key"), ("GEMINI_API_KEY", "")])
            if "deepseek" in mid or "deepseek" in base_url:
                is_specific_provider = True
                provider_candidates.extend([("DEEPSEEK_API_KEY", "deepseek_api_key")])
            if "echobird" in mid or "127.0.0.1:53683" in base_url or "eb-local" in mid or mid in ("auto", "echobird"):
                is_specific_provider = True
                provider_candidates.extend([("ECHOBIRD_API_KEY", "echobird_api_key")])
            if "claude" in mid or "anthropic" in mid or "anthropic" in base_url:
                is_specific_provider = True
                provider_candidates.extend([("ANTHROPIC_API_KEY", "anthropic_api_key")])

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

            # 4. Fallback 通用环境变量与 Vault key
            # 严格防止将本地专用 token (如 Antigravity / eb-local-) 或 Gemini (AQ.) 错误传递给公网厂商导致 401
            def _is_safe_fallback_key(v: str) -> bool:
                if not v:
                    return False
                # 若针对 Agnes / Claude / DeepSeek 等外部云端模型，绝不可注入本地代理专属 token 或 AQ. 前缀 key
                if any(k in mid or k in base_url for k in ("agnes", "claude", "anthropic", "deepseek", "openai")):
                    if v.startswith("Antigravity") or v.startswith("eb-local-") or v.startswith("AQ."):
                        return False
                # 若针对本地代理 (EchoBird / 127.0.0.1:53683 / auto)，绝不可注入外部专有 key (如 AQ.、sk-、cfat_)
                if any(k in mid or k in base_url for k in ("echobird", "53683", "auto")):
                    if not v.startswith("eb-local-"):
                        return False
                return True

            env_key = os.environ.get("ALTEREGO_LLM_API_KEY", "").strip() or os.environ.get("LLM_API_KEY", "").strip()
            if _is_safe_fallback_key(env_key):
                return env_key

            if vault and hasattr(vault, "resolve"):
                try:
                    val = vault.resolve("llm_api_key", caller="ui.server")
                    if _is_safe_fallback_key(val):
                        return val
                except (PermissionError, KeyError):
                    pass

            return ""

        def _extract_fallback_chain() -> list[dict]:
            raw_chain = (
                self.cfg.get("llm", {}).get("fallback_chain")
                or self.cfg.get("models", {}).get("roles", {}).get("main", {}).get("fallback_chain")
                or []
            )
            all_m = {m["id"]: m for m in _get_dynamic_models()}
            chain = []
            if isinstance(raw_chain, list) and raw_chain:
                for item in raw_chain:
                    mid = (item.get("model") if isinstance(item, dict) else str(item)).strip()
                    if not mid:
                        continue
                    m_def = all_m.get(mid, {})
                    b_url = item.get("base_url") if isinstance(item, dict) else ""
                    b_url = b_url or m_def.get("base_url", "")
                    chain.append({
                        "model": mid,
                        "base_url": b_url,
                        "supports_images": item.get("supports_images", m_def.get("supports_images", True)) if isinstance(item, dict) else m_def.get("supports_images", True),
                        "enabled": item.get("enabled", True) if isinstance(item, dict) else True,
                    })
            if not chain:
                fb = self.cfg.get("llm", {}).get("fallback") or self.cfg.get("models", {}).get("roles", {}).get("main", {}).get("fallback", {})
                if isinstance(fb, dict) and fb.get("model"):
                    mid = str(fb["model"]).strip()
                    m_def = all_m.get(mid, {})
                    chain.append({
                        "model": mid,
                        "base_url": fb.get("base_url", m_def.get("base_url", "")),
                        "supports_images": fb.get("supports_images", False),
                        "enabled": fb.get("enabled", True),
                    })
            return chain

        def _extract_roles_info() -> dict:
            """提取各角色的当前生效模型与端点信息。"""
            current = self._model_name(self.llm)
            roles_cfg = self.cfg.get("models", {}).get("roles", {})
            main_cfg = roles_cfg.get("main") or self.cfg.get("llm", {})
            fb_cfg = self.cfg.get("llm", {}).get("fallback") or main_cfg.get("fallback", {})
            fb_chain_cfg = self.cfg.get("llm", {}).get("fallback_chain") or main_cfg.get("fallback_chain", [])
            if fb_chain_cfg and isinstance(fb_chain_cfg, list) and len(fb_chain_cfg) > 0:
                first_fb = fb_chain_cfg[0]
                if isinstance(first_fb, dict):
                    fb_cfg = {**fb_cfg, **first_fb}
                elif isinstance(first_fb, str):
                    fb_cfg = {**fb_cfg, "model": first_fb}

            planner_cfg = roles_cfg.get("planner", {})
            if isinstance(planner_cfg, dict) and "ref" in planner_cfg:
                ref_role = planner_cfg["ref"]
                ref_cfg = roles_cfg.get(ref_role) or main_cfg
                planner_model = ref_cfg.get("model", current)
                planner_url = ref_cfg.get("base_url", "")
            else:
                planner_model = planner_cfg.get("model", current) if isinstance(planner_cfg, dict) else current
                planner_url = planner_cfg.get("base_url", "") if isinstance(planner_cfg, dict) else ""

            worker_cfg = roles_cfg.get("worker", {}) if isinstance(roles_cfg.get("worker"), dict) else {}
            sub_cfg = roles_cfg.get("subagent", {}) if isinstance(roles_cfg.get("subagent"), dict) else {}

            roles_info = {
                "main": {
                    "name": "👑 主脑 (Main Brain)",
                    "desc": "核心对话、意图识别与任务调度",
                    "model": main_cfg.get("model", current),
                    "base_url": main_cfg.get("base_url", ""),
                    "supports_images": main_cfg.get("supports_images", True),
                },
                "worker": {
                    "name": "⚡ 后台工作者 (Worker)",
                    "desc": "记忆沉淀、历史提炼与上下文压缩",
                    "model": worker_cfg.get("model", main_cfg.get("model", current)),
                    "base_url": worker_cfg.get("base_url", main_cfg.get("base_url", "")),
                    "supports_images": worker_cfg.get("supports_images", False),
                },
                "subagent": {
                    "name": "🤖 子任务智能体 (Subagent)",
                    "desc": "专注复杂单项任务、长文档分析与工具链",
                    "model": sub_cfg.get("model", main_cfg.get("model", current)),
                    "base_url": sub_cfg.get("base_url", main_cfg.get("base_url", "")),
                    "supports_images": sub_cfg.get("supports_images", True),
                },
                "planner": {
                    "name": "📋 任务规划器 (Planner)",
                    "desc": "长程复杂任务分解与甘特图规划",
                    "model": planner_model,
                    "base_url": planner_url,
                    "supports_images": False,
                },
                "fallback": {
                    "name": "🛡️ 容灾兜底 (Fallback)",
                    "desc": "主模型故障或遇到速率限制时自动接管",
                    "model": fb_cfg.get("model", "") if fb_cfg.get("enabled", True) else "",
                    "base_url": fb_cfg.get("base_url", "") if fb_cfg.get("enabled", True) else "",
                    "enabled": fb_cfg.get("enabled", True),
                    "supports_images": fb_cfg.get("supports_images", False),
                }
            }
            for r in roles_info.values():
                m_id = r.get("model") or ""
                b_url = r.get("base_url") or ""
                r["has_key"] = bool(_resolve_model_key({"id": m_id, "base_url": b_url})) if m_id else False
            return roles_info

        def _persist_config_roles() -> bool:
            """将角色与模型更新安全持久化到根目录 config.yaml 中（优先保留所有注释与排版）。"""
            cfg_path = getattr(self, "config_path", None) or (REPO_ROOT / "config.yaml")
            if not cfg_path.is_file():
                return False
            try:
                try:
                    from ruamel.yaml import YAML
                    yaml_loader = YAML()
                    yaml_loader.preserve_quotes = True
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        disk_data = yaml_loader.load(f) or {}
                    dump_func = lambda data, stream: yaml_loader.dump(data, stream)
                except ImportError:
                    import yaml
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        disk_data = yaml.safe_load(f) or {}
                    dump_func = lambda data, stream: yaml.dump(data, stream, allow_unicode=True, sort_keys=False)

                disk_data.setdefault("models", {}).setdefault("roles", {})
                for rk, rv in self.cfg.get("models", {}).get("roles", {}).items():
                    if isinstance(rv, dict):
                        disk_data["models"]["roles"].setdefault(rk, {})
                        if "model" in rv:
                            disk_data["models"]["roles"][rk]["model"] = rv["model"]
                        if "base_url" in rv:
                            disk_data["models"]["roles"][rk]["base_url"] = rv["base_url"]
                        if "supports_images" in rv:
                            disk_data["models"]["roles"][rk]["supports_images"] = rv["supports_images"]
                        if "ref" in rv:
                            disk_data["models"]["roles"][rk]["ref"] = rv["ref"]
                        elif "ref" in disk_data["models"]["roles"][rk]:
                            del disk_data["models"]["roles"][rk]["ref"]
                if "llm" in self.cfg and "llm" in disk_data:
                    disk_data["llm"]["model"] = self.cfg["llm"].get("model")
                    disk_data["llm"]["base_url"] = self.cfg["llm"].get("base_url")
                    if "fallback" in self.cfg["llm"]:
                        disk_data["llm"].setdefault("fallback", {})
                        disk_data["llm"]["fallback"]["model"] = self.cfg["llm"]["fallback"].get("model")
                        disk_data["llm"]["fallback"]["base_url"] = self.cfg["llm"]["fallback"].get("base_url")
                        disk_data["llm"]["fallback"]["enabled"] = self.cfg["llm"]["fallback"].get("enabled", True)
                    if "fallback_chain" in self.cfg["llm"]:
                        disk_data["llm"]["fallback_chain"] = self.cfg["llm"]["fallback_chain"]
                        disk_data.setdefault("models", {}).setdefault("roles", {}).setdefault("main", {})["fallback_chain"] = self.cfg["llm"]["fallback_chain"]

                with open(cfg_path, "w", encoding="utf-8") as f:
                    dump_func(disk_data, f)
                return True
            except Exception as exc:
                logger.error("持久化 config.yaml 角色配置异常: %s", exc, exc_info=True)
                return False

        @app.route("/api/models", method="GET")
        def list_models():
            """前端查询可用模型列表 + 各角色当前生效模型矩阵（动态组装与暴露）。"""
            current = self._model_name(self.llm)
            current_base_url = getattr(self.llm, "base_url", "")
            all_models = _get_dynamic_models()
            public = [{k: v for k, v in m.items() if not k.startswith("key_") and k != "vault_key"}
                      for m in all_models]
            for m in public:
                m["has_key"] = bool(_resolve_model_key(m))
            return {
                "current": current,
                "current_display": _model_display_name(current, current_base_url),
                "models": public,
                "roles": _extract_roles_info(),
                "fallback_chain": _extract_fallback_chain(),
            }

        @app.route("/api/settings/fallback_chain", method=["GET", "POST"])
        def handle_fallback_chain():
            """查询或保存多级容灾降级备用链（热重载 + 持久化）。"""
            from bottle import request
            if request.method == "GET":
                chain = _extract_fallback_chain()
                return {
                    "ok": True,
                    "chain": chain,
                    "fallback_chain": chain,
                    "roles": _extract_roles_info(),
                }

            data = request.json or {}
            raw_chain = data.get("chain") or data.get("fallback_chain") or []
            if not isinstance(raw_chain, list):
                return {"ok": False, "error": "备用链格式错误，需为列表"}

            all_m = {m["id"]: m for m in _get_dynamic_models()}
            normalized_chain = []
            for item in raw_chain:
                mid = item.get("model") if isinstance(item, dict) else str(item)
                mid = mid.strip()
                if not mid:
                    continue
                m_def = all_m.get(mid, {})
                b_url = (item.get("base_url") if isinstance(item, dict) else "") or m_def.get("base_url", "")
                sup_img = item.get("supports_images", m_def.get("supports_images", True)) if isinstance(item, dict) else m_def.get("supports_images", True)
                normalized_chain.append({
                    "model": mid,
                    "base_url": b_url,
                    "supports_images": bool(sup_img),
                    "enabled": True,
                })

            if not normalized_chain:
                return {"ok": False, "error": "备用链不能为空，至少保留一个降级模型"}

            self.cfg.setdefault("llm", {})["fallback_chain"] = normalized_chain
            roles = self.cfg.setdefault("models", {}).setdefault("roles", {})
            roles.setdefault("main", {})["fallback_chain"] = normalized_chain

            first_m = normalized_chain[0]
            self.cfg["llm"]["fallback"] = {
                "model": first_m["model"],
                "base_url": first_m["base_url"],
                "enabled": True,
                "supports_images": first_m.get("supports_images", False),
            }
            roles["main"]["fallback"] = dict(self.cfg["llm"]["fallback"])
            roles["fallback"] = dict(self.cfg["llm"]["fallback"])

            _persist_config_roles()

            try:
                with self.turn_lock:
                    self._reload_model_clients()
            except Exception as exc:
                logger.warning("备用链重载模型客户端失败: %s", exc)
                return {"ok": False, "error": f"重载容灾降级链失败: {exc}"}

            return {
                "ok": True,
                "chain": normalized_chain,
                "fallback_chain": normalized_chain,
                "roles": _extract_roles_info(),
                "fallback_model": first_m["model"],
            }

        @app.route("/api/settings/save_model_config", method="POST")
        @app.route("/api/switch_model", method="POST")
        def switch_or_save_model():
            """保存大模型参数与 API Key，支持按角色分级绑定（主脑/后台Worker/子智能体/规划器/容灾兜底）。"""
            from bottle import request
            data = request.json or {}
            model_id = str(data.get("model_id") or data.get("model") or "").strip()
            if not model_id:
                return {"ok": False, "error": "模型标识不能为空"}

            target_role = str(data.get("target_role") or data.get("role") or "main").strip().lower()
            if target_role not in {"main", "worker", "subagent", "planner", "fallback", "all", "none"}:
                target_role = "main"

            base_url = str(data.get("base_url") or "").strip()
            api_key = str(data.get("api_key") or "").strip()

            # UI 展示名是 EchoBird，但配置与真实请求始终使用内部 ID auto。
            if model_id.lower() == "echobird" and (
                "53683" in base_url.lower() or "echobird" in base_url.lower()
            ):
                model_id = "auto"

            # 1. 如果用户录入了新的 API Key，立即使用 Windows DPAPI 加密存入 Vault
            vault = getattr(self, "vault", None)
            if api_key and vault and hasattr(vault, "set"):
                # 如果是全局或主脑，存为主 key
                if target_role in ("main", "all"):
                    vault.set("llm_api_key", api_key)
                elif target_role == "fallback":
                    vault.set("backup_llm_api_key", api_key)
                else:
                    vault.set(f"{target_role}_api_key", api_key)

                # 按厂商类型智能补存专属命名 key
                mid_lower = model_id.lower()
                burl_lower = (base_url or "").lower()
                if ("agnes" in mid_lower or "agnes" in burl_lower) and not api_key.startswith("eb-local-"):
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
                elif "echobird" in mid_lower or "53683" in burl_lower or "eb-local" in api_key or mid_lower == "auto":
                    try:
                        vault.set("echobird_api_key", api_key)
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

            # 2. 按角色分级更新配置字典
            roles = self.cfg.setdefault("models", {}).setdefault("roles", {})
            supports_img = bool(target.get("supports_images", True))

            if target_role == "none":
                # 仅保存模型定义与密钥，不切换任何角色的活动模型
                pass
            elif target_role == "worker":
                roles.setdefault("worker", {})["model"] = target["id"]
                roles["worker"]["base_url"] = target["base_url"]
                roles["worker"]["supports_images"] = bool(data.get("supports_images", False))
            elif target_role == "subagent":
                roles.setdefault("subagent", {})["model"] = target["id"]
                roles["subagent"]["base_url"] = target["base_url"]
                roles["subagent"]["supports_images"] = supports_img
            elif target_role == "planner":
                if "ref" in roles.get("planner", {}):
                    del roles["planner"]["ref"]
                roles.setdefault("planner", {})["model"] = target["id"]
                roles["planner"]["base_url"] = target["base_url"]
                roles["planner"]["supports_images"] = False
                try:
                    planner_llm = LLMClient.for_role(self.cfg, "planner", self.vault)
                    self._bind_model_observer(planner_llm, "planner")
                    self.planner = Planner(planner_llm)
                    self.orch.planner = self.planner
                except Exception as exc:
                    logger.warning("重新加载 Planner 模型失败: %s", exc)
            elif target_role == "fallback":
                self.cfg.setdefault("llm", {}).setdefault("fallback", {})["model"] = target["id"]
                self.cfg["llm"]["fallback"]["base_url"] = target["base_url"]
                self.cfg["llm"]["fallback"]["enabled"] = True
                self.cfg["llm"]["fallback"]["supports_images"] = False
                roles.setdefault("main", {}).setdefault("fallback", {})["model"] = target["id"]
                roles["main"]["fallback"]["base_url"] = target["base_url"]
                roles["main"]["fallback"]["enabled"] = True
                roles["main"]["fallback"]["supports_images"] = False

                cur_chain = self.cfg.get("llm", {}).get("fallback_chain") or []
                new_chain = []
                if isinstance(cur_chain, list) and cur_chain:
                    new_chain = [dict(c) if isinstance(c, dict) else {"model": str(c)} for c in cur_chain]
                    new_chain[0]["model"] = target["id"]
                    new_chain[0]["base_url"] = target["base_url"]
                else:
                    new_chain = [{"model": target["id"], "base_url": target["base_url"], "enabled": True}]
                self.cfg["llm"]["fallback_chain"] = new_chain
                roles["main"]["fallback_chain"] = new_chain

                try:
                    with self.turn_lock:
                        self._reload_model_clients()
                except Exception as exc:
                    return {"ok": False, "error": f"重载容灾兜底客户端失败: {exc}"}
            elif target_role == "all":
                self.cfg.setdefault("llm", {})["model"] = target["id"]
                self.cfg["llm"]["base_url"] = target["base_url"]
                self.cfg["llm"]["supports_images"] = supports_img
                for r in ("main", "worker", "subagent", "planner"):
                    if r == "planner" and "ref" in roles.get("planner", {}):
                        del roles["planner"]["ref"]
                    roles.setdefault(r, {})["model"] = target["id"]
                    roles[r]["base_url"] = target["base_url"]
                    roles[r]["supports_images"] = supports_img
                try:
                    with self.turn_lock:
                        self._reload_model_clients(api_key_override=model_key)
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}
            else:  # "main"
                self.cfg.setdefault("llm", {})["model"] = target["id"]
                self.cfg["llm"]["base_url"] = target["base_url"]
                self.cfg["llm"]["supports_images"] = supports_img
                roles.setdefault("main", {})["model"] = target["id"]
                roles["main"]["base_url"] = target["base_url"]
                roles["main"]["supports_images"] = supports_img
                try:
                    with self.turn_lock:
                        self._reload_model_clients(api_key_override=model_key)
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}

            # 3. 持久化到 config.yaml
            if not _persist_config_roles():
                logger.warning("模型已在内存中热切换生效，但写入 config.yaml 失败，重启后将恢复旧配置")

            if target_role in ("main", "all"):
                self.state_snapshot["status"]["current_model"] = target["id"]

            self.emit_event("model_switched", {
                "target_role": target_role,
                "model": target["id"],
                "base_url": target["base_url"]
            })

            import logging as _log
            _log.getLogger(__name__).info(
                "[switch_model] 角色 [%s] 模型已成功更新生效: %s (base_url: %s)",
                target_role, target["id"], target["base_url"])

            updated_models = [{k: v for k, v in m.items() if not k.startswith("key_") and k != "vault_key"}
                              for m in _get_dynamic_models()]
            for m in updated_models:
                m["has_key"] = bool(_resolve_model_key(m))

            return {
                "ok": True,
                "current": self._model_name(self.llm),
                "target_role": target_role,
                "base_url": target["base_url"],
                "has_key": True,
                "models": updated_models,
                "roles": _extract_roles_info()
            }

        @app.route("/api/models/<model_id:path>", method="DELETE")
        def delete_custom_model(model_id):
            """删除用户添加的自定义模型。
            
            若被删除的模型正被任何渠道角色（main/worker/subagent/planner/fallback）使用，
            则对应渠道自动回退并重置为第一个可用模型（models[0]），持久化至 config.yaml 并热重载客户端。
            """
            from urllib.parse import unquote
            model_id = unquote(str(model_id or "")).strip()
            if not model_id:
                return {"ok": False, "error": "模型标识不能为空"}

            anchor_id = PRESET_MODELS[0]["id"]
            if model_id in (anchor_id, "auto"):
                return {"ok": False, "error": f"系统核心容灾锚点模型 [{anchor_id}] 不可删除"}

            # 1. 记录到 deleted_models.json，确保在可用模型列表中隐藏并永久排除
            del_set = _load_deleted_models()
            del_set.add(model_id)
            _save_deleted_models(del_set)

            # 2. 如果存在于 custom_models.json，一并从自定义模型列表中移除
            custom_list = _load_custom_models()
            orig_len = len(custom_list)
            new_list = [m for m in custom_list if m.get("id") != model_id]
            if len(new_list) < orig_len:
                _save_custom_models(new_list)

            # 确定回退模型：列表中除去当前删除模型后的第一个可用模型（默认 PRESET_MODELS[0] 即 auto）
            remaining_models = [m for m in _get_dynamic_models() if m.get("id") != model_id]
            auto_m = next((m for m in remaining_models if m.get("id") == "auto"), None)
            default_m = auto_m or (remaining_models[0] if remaining_models else PRESET_MODELS[0])
            def_id = default_m["id"]
            def_url = default_m.get("base_url", "")
            def_img = bool(default_m.get("supports_images", True))

            roles = self.cfg.setdefault("models", {}).setdefault("roles", {})
            reset_roles = []

            # 1. 检查主模型 (main 角色与 top-level cfg["llm"])
            main_cfg = roles.get("main") if isinstance(roles.get("main"), dict) else {}
            llm_cfg = self.cfg.get("llm") if isinstance(self.cfg.get("llm"), dict) else {}
            if (
                llm_cfg.get("model") == model_id
                or self._model_name(self.llm) == model_id
                or main_cfg.get("model") == model_id
            ):
                self.cfg.setdefault("llm", {})["model"] = def_id
                self.cfg["llm"]["base_url"] = def_url
                self.cfg["llm"]["supports_images"] = def_img
                roles.setdefault("main", {})["model"] = def_id
                roles["main"]["base_url"] = def_url
                roles["main"]["supports_images"] = def_img
                reset_roles.append("main")

            # 2. 检查后台工作者 (worker)
            worker_cfg = roles.get("worker")
            if isinstance(worker_cfg, dict) and worker_cfg.get("model") == model_id:
                worker_cfg["model"] = def_id
                worker_cfg["base_url"] = def_url
                worker_cfg["supports_images"] = False
                reset_roles.append("worker")

            # 3. 检查子任务智能体 (subagent)
            sub_cfg = roles.get("subagent")
            if isinstance(sub_cfg, dict) and sub_cfg.get("model") == model_id:
                sub_cfg["model"] = def_id
                sub_cfg["base_url"] = def_url
                sub_cfg["supports_images"] = def_img
                reset_roles.append("subagent")

            # 4. 检查任务规划器 (planner)
            planner_cfg = roles.get("planner")
            if isinstance(planner_cfg, dict) and planner_cfg.get("model") == model_id:
                if "ref" in planner_cfg:
                    del planner_cfg["ref"]
                planner_cfg["model"] = def_id
                planner_cfg["base_url"] = def_url
                planner_cfg["supports_images"] = False
                reset_roles.append("planner")

            # 5. 检查容灾兜底 (fallback)
            fb_llm = llm_cfg.get("fallback") if isinstance(llm_cfg.get("fallback"), dict) else {}
            fb_main = main_cfg.get("fallback") if isinstance(main_cfg.get("fallback"), dict) else {}
            fb_role = roles.get("fallback") if isinstance(roles.get("fallback"), dict) else {}
            if fb_llm.get("model") == model_id or fb_main.get("model") == model_id or fb_role.get("model") == model_id:
                if "fallback" in roles:
                    roles["fallback"]["model"] = def_id
                    roles["fallback"]["base_url"] = def_url
                if "fallback" in self.cfg.get("llm", {}):
                    self.cfg["llm"]["fallback"]["model"] = def_id
                    self.cfg["llm"]["fallback"]["base_url"] = def_url
                if "fallback" in roles.get("main", {}):
                    roles["main"]["fallback"]["model"] = def_id
                    roles["main"]["fallback"]["base_url"] = def_url
                reset_roles.append("fallback")

            # 若有角色重置，则持久化到 config.yaml 并重新加载模型客户端
            if reset_roles:
                _persist_config_roles()
                def_key = _resolve_model_key(default_m)
                try:
                    with self.turn_lock:
                        self._reload_model_clients(api_key_override=def_key or "")
                except Exception as exc:
                    logger.warning("删除模型重置后重载客户端异常: %s", exc)

                if "main" in reset_roles:
                    self.state_snapshot["status"]["current_model"] = def_id
                    self._refresh_model_status()
                    self.emit_event("model_switched", {
                        "target_role": "main",
                        "model": def_id,
                        "base_url": def_url,
                    })

            self.emit_event("model_deleted", {
                "deleted": model_id,
                "fallback_model": def_id,
                "reset_roles": reset_roles,
            })

            updated_models = [{k: v for k, v in m.items() if not k.startswith("key_") and k != "vault_key"}
                              for m in _get_dynamic_models()]
            for m in updated_models:
                m["has_key"] = bool(_resolve_model_key(m))

            return {
                "ok": True,
                "deleted": model_id,
                "fallback_model": def_id,
                "reset_roles": reset_roles,
                "current": self._model_name(self.llm),
                "models": updated_models,
                "roles": _extract_roles_info(),
            }

        # 静态文件
        @app.route("/")
        def index():
            return static_file("index.html", root=str(UI_DIR))

        @app.route("/takeover")
        @app.route("/takeover.html")
        def takeover():
            return static_file("takeover.html", root=str(UI_DIR))

        @app.route("/settings")
        @app.route("/settings.html")
        def settings_page():
            return static_file("settings.html", root=str(UI_DIR))

        @app.route("/api/open_settings_window", method=["GET", "POST"])
        def open_settings_window():
            token = self.api_token
            port = self.port
            try:
                from ui.settings_window import open_settings_window_async
                proc = open_settings_window_async(port=port, token=token)
                return {"ok": bool(proc is not None)}
            except Exception as e:
                logger.warning("[server] 打开独立设置子窗口失败: %s", e)
                return {"ok": False, "error": str(e)}

        @app.route("/static/<filepath:path>")
        def server_static(filepath):
            return static_file(filepath, root=str(UI_DIR / "static"))

        @app.route("/uploads/<filepath:path>")
        def get_upload(filepath):
            res = static_file(filepath, root=str(self.data_dir / "uploads"))
            res.set_header("X-Content-Type-Options", "nosniff")
            ext = Path(filepath).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
                res.set_header("Content-Disposition", f'attachment; filename="{Path(filepath).name}"')
            return res

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
            # 排序与更新时间都取最近一条消息：微信任务会在后台持续写入，
            # 只按 start_ts 排会让活跃的微信会话沉底、前端列表也看不出动静。
            rows = self.store.db.execute(
                "SELECT s.session_id, s.title, s.started_ts,"
                " COALESCE((SELECT MAX(m.created_at) FROM messages m"
                "           WHERE m.session_id = s.session_id), s.started_ts) AS last_ts,"
                " (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id) AS msg_count"
                " FROM sessions s ORDER BY last_ts DESC"
            ).fetchall()
            if not rows:
                sid = self.store.new_session("默认会话")
                rows = [(sid, "默认会话", time.time(), time.time(), 0)]
            items = []
            for sid, title, started_ts, last_ts, msg_count in rows:
                # 微信通道的会话 id 固定为 s-wx-*，标题也可能被历史首条消息覆盖，
                # 两者任一命中都算微信来源，供前端打标签、分流未读。
                source = "weixin" if (str(sid).startswith("s-wx-")
                                      or str(title or "").startswith("微信")) else "desktop"
                items.append({
                    "id": sid,
                    "title": title or "会话",
                    "updated_at": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(last_ts or started_ts)),
                    "last_ts": int((last_ts or started_ts or 0) * 1000),
                    "message_count": int(msg_count or 0),
                    "source": source,
                })
            return json.dumps(items)

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

        @app.route("/api/sessions/reset", method="POST")
        def reset_session():
            """一键重置当前所有测试记录并新建干净会话。"""
            import uuid
            with self.turn_lock:
                from tools.desktop_runtime import clear_scope
                data = request.json or {}
                clear_all = data.get("clear_all", True)
                if clear_all:
                    try:
                        self.store.db.execute("DELETE FROM messages")
                        self.store.db.execute("DELETE FROM sessions")
                        self.store.db.commit()
                    except Exception:
                        pass
                sid = self.store.new_session("新会话")
                clear_scope(sid)
                if hasattr(self, "orch") and hasattr(self.orch, "reset_cancel"):
                    try:
                        self.orch.reset_cancel()
                    except Exception:
                        pass
                channel = getattr(self, "weixin_channel", None)
                if channel:
                    channel.weixin_session_id = f"s-wx-{uuid.uuid4().hex[:8]}"
                return {
                    "ok": True,
                    "id": sid,
                    "title": "新会话",
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
                        if self.vault:
                            if isinstance(result_content, str):
                                result_content = self.vault.scrub(result_content)
                            res_sum = self.vault.scrub(result_summary(res, max_len=60))
                        else:
                            res_sum = result_summary(res, max_len=60)
                        tool_calls_info.append({
                            "tool": tc.name,
                            "args": tc.arguments,
                            "summary": tool_summary(tc.name, tc.arguments),
                            "status": "cancelled" if cancelled else ("error" if is_err else "ok"),
                            "result": result_content,  # 返回全文供右栏/详情查看
                            "result_summary": res_sum,
                        })

                # B7: 历史记录过滤潜在密钥
                if content:
                    content = self.vault.scrub(content)
                task_goal = ""
                task_origin_id = ""
                task_status = ""
                if m.role == "user":
                    metadata = m.metadata or {}
                    task_goal = metadata.get("task_goal") or ""
                    if isinstance(task_goal, str) and task_goal and self.vault:
                        task_goal = self.vault.scrub(task_goal)
                    task_origin_id = metadata.get("task_origin_id") or m.id
                elif m.role == "assistant":
                    metadata = m.metadata or {}
                    task_goal = metadata.get("task_goal") or ""
                    if isinstance(task_goal, str) and task_goal and self.vault:
                        task_goal = self.vault.scrub(task_goal)
                    task_origin_id = metadata.get("task_origin_id") or ""
                    task_status = metadata.get("task_status") or ""

                # 区分消息类型：user / assistant_notice（system 通知）/ assistant_final / tool_step
                if m.role == "user":
                    msg_type, ui_role = "user", "user"
                elif m.role == "system":
                    msg_type, ui_role = "assistant_notice", "agent"
                elif content:
                    msg_type, ui_role = "assistant_final", "agent"
                else:
                    msg_type, ui_role = "tool_step", "agent"
                if not content and not tool_calls_info and m.role in ("assistant", "system"):
                    continue

                created_ts = getattr(m, "created_at", None) or time.time()
                mid = getattr(m, "id", None) or f"msg_{sid}_{i}"
                ui_messages.append({
                    "id": mid,
                    "type": msg_type,
                    "role": ui_role,
                    "content": content,
                    "task_goal": task_goal,
                    "task_origin_id": task_origin_id,
                    "task_status": task_status,
                    "tool_calls": tool_calls_info,
                    "created_at": time.strftime("%H:%M", time.localtime(created_ts)),
                })
            return json.dumps(ui_messages)

        # 2.1.1 停止当前任务（含全部子任务）
        @app.route("/api/stop", method="POST")
        def stop_task():
            try:
                self.orch.request_stop()
                return {"ok": True, "message": "停止指令已发送"}
            except Exception as e:
                logger.warning("Stop request failed: %s", e)
                response.status = 500
                return {"ok": False, "error": str(e)}

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
                # 重启遗留任务提醒：只在新话题时注入一次，进 system prompt 不落库
                resume_notice = self._resume_notice_text(sid, content)

                # 自动基于第一条用户消息更新会话标题
                if not self.orch.history:
                    new_title = content[:15] + ("..." if len(content) > 15 else "")
                    self.store.update_title(sid, new_title)

                # 立即持久化用户消息，支持多模态图片传入（spec §24）
                images_raw = list(data.get("images") or [])
                # 兼容 upload_id / upload_ids 传入及正文中 /uploads/ 附件自动提取
                upload_ids = []
                if isinstance(data.get("upload_id"), str) and data.get("upload_id"):
                    upload_ids.append(data.get("upload_id"))
                if isinstance(data.get("upload_ids"), list):
                    upload_ids.extend([str(uid) for uid in data.get("upload_ids") if uid])

                uploads_dir = (self.data_dir if hasattr(self, "data_dir") else Path(".")) / "uploads"
                import re as _re, base64 as _base64
                found_upload_names = _re.findall(r"/uploads/([a-zA-Z0-9_\-\.]+)", content)
                for uname in found_upload_names:
                    up_path = uploads_dir / uname
                    if up_path.is_file():
                        ext = up_path.suffix.lower()
                        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                            mime = "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else ("image/gif" if ext == ".gif" else "image/jpeg"))
                            try:
                                b64 = _base64.b64encode(up_path.read_bytes()).decode("ascii")
                                images_raw.append({"data_base64": b64, "mime_type": mime})
                            except Exception:
                                pass

                for uid in upload_ids:
                    matches = list(uploads_dir.glob(f"{uid}.*")) if uploads_dir.is_dir() else []
                    for match_file in matches:
                        ext = match_file.suffix.lower()
                        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                            mime = "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else "image/jpeg")
                            try:
                                b64 = _base64.b64encode(match_file.read_bytes()).decode("ascii")
                                images_raw.append({"data_base64": b64, "mime_type": mime})
                            except Exception:
                                pass

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
                    ctx = getattr(self.orch, "ctx", None)
                    if ctx is not None:
                        ctx.turn_notice = resume_notice
                    try:
                        raw_reply = self.orch.handle(user_msg)
                    finally:
                        if ctx is not None:
                            ctx.turn_notice = ""
                    reply_text = self.vault.scrub(raw_reply)

                    # 持久化更新后的会话历史
                    self.store.save_history(sid, self.orch.history)

                    # 获取本轮工具调用情况并丰富人话摘要
                    tool_calls = []
                    for rec in self.orch.last_turn_tool_records:
                        rec_copy = dict(rec)
                        rec_copy["summary"] = tool_summary(rec.get("tool", ""), rec.get("args"))
                        res_sum = result_summary(rec.get("result", ""), max_len=60)
                        if self.vault:
                            if "result" in rec_copy and isinstance(rec_copy["result"], str):
                                rec_copy["result"] = self.vault.scrub(rec_copy["result"])
                            if "summary" in rec_copy and isinstance(rec_copy["summary"], str):
                                rec_copy["summary"] = self.vault.scrub(rec_copy["summary"])
                            res_sum = self.vault.scrub(res_sum)
                        rec_copy["result_summary"] = res_sum
                        tool_calls.append(rec_copy)

                    last_meta = (getattr(self.orch.history[-1], "metadata", {})
                                 if self.orch.history else {})
                    msg_obj = {
                        "id": f"msg_{int(time.time()*1000)}",
                        "type": "assistant_final" if reply_text else "tool_step",
                        "role": "agent",
                        "content": reply_text,
                        "tool_calls": tool_calls,
                        "status": getattr(self.orch, "last_turn_status", "done"),
                        "task_origin_id": last_meta.get("task_origin_id") or "",
                        "task_goal": last_meta.get("task_goal") or "",
                        "task_status": last_meta.get("task_status")
                                        or getattr(self.orch, "last_turn_status", "done"),
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
                                experience_store=self.experience,
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
            version_id = data.get("version_id")
            try:
                res = self.approval.resolve_card(aid, approved, scope, version_id=version_id)
                if not isinstance(res, dict) or not res.get("ok"):
                    reason = res.get("reason", "error") if isinstance(res, dict) else "error"
                    if reason == "not_found":
                        response.status = 404
                    elif reason in ("already_consumed", "version_conflict"):
                        response.status = 409
                    else:
                        response.status = 400
                    return res if isinstance(res, dict) else {"error": reason}
                return {"approval_id": aid, "approved": approved, "scope": scope, "version_id": res.get("version_id")}
            except ValueError as e:
                response.status = 400
                return {"error": str(e)}

        @app.route("/api/approvals/toast_resolve", method=["GET", "POST"])
        def toast_resolve():
            aid = request.query.get("id") or (request.json or {}).get("id")
            token = (
                request.query.get("token")
                or request.headers.get("X-AlterEgo-Token")
                or (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                or ""
            )
            import secrets
            if self.api_token and (not token or not secrets.compare_digest(token, self.api_token)):
                response.status = 401
                if request.headers.get("X-AlterEgo-Token") or "application/json" in request.headers.get("Accept", ""):
                    return {"error": "unauthorized"}
                return "<p style='font-family: sans-serif; color: red;'>未授权：Token 无效或缺失</p>"
            if not aid:
                response.status = 400
                return "<p style='font-family: sans-serif; color: red;'>错误：缺少审批 ID</p>"

            approved_val = request.query.get("approved", "1")
            approved = str(approved_val).lower() in ("1", "true", "yes")
            scope = request.query.get("scope", "once")

            try:
                res = self.approval.resolve_card(aid, approved, scope)
                ok = isinstance(res, dict) and bool(res.get("ok"))
                if ok:
                    self.emit_event("approval_resolved", {
                        "approval_id": aid,
                        "approved": approved,
                        "scope": scope,
                    })
            except Exception:
                ok = False
                res = {"ok": False, "reason": "exception"}

            if request.headers.get("X-AlterEgo-Token") or "application/json" in request.headers.get("Accept", ""):
                if not ok:
                    reason = res.get("reason", "error") if isinstance(res, dict) else "error"
                    if reason == "not_found":
                        response.status = 404
                    elif reason in ("already_consumed", "version_conflict"):
                        response.status = 409
                    else:
                        response.status = 400
                return {"ok": bool(ok), "approval_id": aid, "approved": approved, "scope": scope, "reason": res.get("reason") if isinstance(res, dict) else None}

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

            # 1. 保持 AlterEgo 原生桌面窗口当前位置稳定（严禁在用户交互时强制位移或缩放导致窗口跳动）


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
            token = (
                request.query.get("token")
                or request.headers.get("X-AlterEgo-Token")
                or (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                or ""
            )
            import secrets
            if self.api_token and (not token or not secrets.compare_digest(token, self.api_token)):
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

        ALLOWED_UPLOAD_EXTS = frozenset({
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico",
            ".txt", ".md", ".json", ".csv", ".tsv", ".pdf",
            ".docx", ".xlsx", ".pptx", ".log", ".yaml", ".yml",
        })

        # 2.6 上传接口
        @app.route("/api/uploads", method="POST")
        def upload_file():
            upload = request.files.get("file")
            if not upload:
                response.status = 400
                return {"error": "no_file"}
            raw_ext = Path(upload.filename).suffix.lower()
            if not raw_ext or raw_ext not in ALLOWED_UPLOAD_EXTS:
                response.status = 400
                return {
                    "error": "disallowed_file_type",
                    "message": f"不支持的文件扩展名: {raw_ext or '(无扩展名)'}。出于安全考量，禁止上传 HTML/SVG/脚本等风险格式。",
                }
            uploads_dir = self.data_dir / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            uid = f"up_{uuid.uuid4().hex[:10]}"
            fname = f"{uid}{raw_ext}"
            save_path = uploads_dir / fname
            upload.save(str(save_path), overwrite=True)
            return {
                "upload_id": uid,
                "filename": upload.filename,
                "url": f"/uploads/{fname}",
                "path": str(save_path),
            }


        @app.route("/api/settings/behavior", method=["GET", "POST"])
        def handle_behavior_settings():
            from bottle import request
            beh = self.cfg.setdefault("behavior", {
                "approval_timeout_sec": 300,
                "hazardous_default_scope": "session",
                "tray_resident": True,
                "auto_failover": True,
            })
            if request.method == "GET":
                return {"ok": True, "behavior": beh}
            data = request.json or {}
            for k, v in data.items():
                beh[k] = v
            if "approval_timeout_sec" in beh and hasattr(self, "approval") and self.approval:
                self.approval.timeout_sec = int(beh["approval_timeout_sec"])
            
            # 持久化到 config.yaml
            cfg_path = getattr(self, "config_path", None) or (REPO_ROOT / "config.yaml")
            try:
                import yaml
                disk = {}
                if cfg_path.is_file():
                    disk = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                disk["behavior"] = beh
                cfg_path.write_text(yaml.dump(disk, allow_unicode=True, sort_keys=False), encoding="utf-8")
            except Exception as e:
                logger.warning("保存 behavior 到 config.yaml 异常: %s", e)
            return {"ok": True, "behavior": beh}

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
            router_cfg = self.cfg.get("llm_router") or {}
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
                "behavior": self.cfg.get("behavior", {
                    "approval_timeout_sec": 300,
                    "hazardous_default_scope": "session",
                    "tray_resident": True,
                    "auto_failover": True,
                }),
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

            final_model = test_model or self._model_name(self.llm)
            final_base_url = test_base_url or getattr(self.llm, "base_url", "")
            is_echobird = (
                "53683" in (final_base_url or "")
                or "echobird" in (final_base_url or "").lower()
            )
            # 允许前端在输入框中展示 EchoBird，但真实请求仍固定使用 auto。
            if is_echobird and final_model.lower() == "echobird":
                final_model = "auto"

            target_client = self.llm
            if test_model or test_base_url or test_api_key:
                final_key = test_api_key or _resolve_model_key({"id": final_model, "base_url": final_base_url})
                if not final_key:
                    return {
                        "status": "error",
                        "message": f"模型 [{final_model}] 尚未配置专属 API Key，请在下方「API Key 访问密钥」输入框中填入您的密钥后，再点击连通性测试或保存"
                    }
                from agent.llm import LLMClient
                target_client = LLMClient(
                    base_url=final_base_url,
                    model=final_model,
                    api_key=final_key,
                    max_tokens=64,
                )

            try:
                res = target_client.chat([Message(role="user", content="连通性测试，请回复OK")])
                payload = {"status": "ok", "reply": res.content}
                route_info = target_client.get_echobird_route_info() if hasattr(target_client, "get_echobird_route_info") else None
                if route_info:
                    probe = route_info.get("probe") or {}
                    last_route = route_info.get("last_route") or {}
                    route = last_route or probe.get("route") or {}
                    if route:
                        payload["route"] = route.get("display_name") or route.get("route_key")
                        payload["route_detail"] = route
                    payload["echobird"] = route_info
                    payload["capabilities"] = route_info.get("capabilities")
                return payload
            except Exception as e:
                err_str = str(e)
                if "401" in err_str or "Invalid token" in err_str or "AuthenticationError" in err_str or "Unauthorized" in err_str:
                    if is_echobird:
                        msg = ("EchoBird Key 认证失败 (401/403)。请检查设置中的 EchoBird 密钥，"
                               "或环境变量 ECHOBIRD_API_KEY 是否有效。")
                    else:
                        msg = f"API Key 认证失败 (401 Invalid Token / 密钥无效或未授权)。请检查在下方填入的 API Key 是否为该模型所属厂商颁发的有效密钥。"
                elif "404" in err_str or "Not Found" in err_str:
                    msg = f"API 端点路径不存在 (404 Not Found)。请检查 API Base URL 是否为 OpenAI 兼容的 /v1 路径（例如 https://apihub.agnes-ai.com/v1）。"
                elif "ConnectError" in err_str or "Failed to establish" in err_str or "timeout" in err_str.lower():
                    msg = f"无法连接到端点服务器（网络超时或拒绝连接）。请检查 Base URL 地址及本地网络。"
                else:
                    msg = f"模型调用失败: {err_str}"
                return {"status": "error", "message": msg}

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
                    # D8 修复：pywebview 的 win.maximized 运行时不更新、win.state 是响应式字典，
                    # 旧判据恒 False 导致最大化后永远无法还原；改用 events.maximized/restored 维护的标志
                    if getattr(self, "_win_is_max", False):
                        if hasattr(win, "restore"):
                            win.restore()
                        self._win_is_max = False
                    elif hasattr(win, "maximize"):
                        win.maximize()
                        self._win_is_max = True
                elif action == "close":
                    # 浮窗触发（v1.1.7 需求，v1.1.14 修正）：❌ 一律 = 隐藏主窗口 + 弹浮窗（无论是否有任务）；
                    # 退出只走托盘/浮窗菜单的「退出」（/api/exit）。
                    self._ulog("❌ 关闭点击：active_turn=%s → 隐藏主窗口并弹浮窗" % self._has_active_turn())
                    if hasattr(win, "hide"):
                        win.hide()
                    proc = self._spawn_float_window()
                    self._ulog("浮窗子进程: %s" % (getattr(proc, "pid", "启动失败")))
                    return {"ok": True, "action": action, "hidden": True}
                elif action == "show":
                    # 浮窗「打开主窗口」：主窗口最小化/隐藏时恢复显示
                    if hasattr(win, "show"):
                        win.show()
                    if hasattr(win, "restore"):
                        win.restore()
                return {"ok": True, "action": action}
            except Exception as e:
                logger.warning("[window_control] 操作失败 %s: %s", action, e)
                return {"ok": False, "error": str(e)}

        @app.route("/api/exit", method="POST")
        def exit_app():
            """真正退出（浮窗「退出」菜单调用）：销毁可能隐藏的主窗口并停服。"""
            self._really_exit()
            return {"ok": True}

        @app.route("/api/window/status", method="GET")
        def window_status():
            win = getattr(self, "_ui_window", None)
            if not win:
                return {"is_desktop": False, "maximized": False}
            # D8 修复：与 /api/window/control 共用 events.maximized/restored 维护的标志
            is_max = getattr(self, "_win_is_max", False)
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



        @app.route("/api/ideas", method="GET")
        def get_ideas():
            """提纯提示词模板：标准指令，严禁原始历史水聊。"""
            curated_cards = [
                {
                    "kind": "新建",
                    "title": "新建文件与Markdown",
                    "desc": "新建一个文件夹，新建txt文件测试但要求md格式的内容，排版后保存。",
                    "meta": "提纯模板 · 3 日沉淀",
                    "prompt": "新建一个文件夹，新建txt文件测试但要求md格式的内容。然后保存",
                },
                {
                    "kind": "浏览",
                    "title": "网页元素抓取",
                    "desc": "打开指定目标网页，提取页面关键数据或截图并保存到本地。",
                    "meta": "提纯模板 · 自动化",
                    "prompt": "在网页中抓取关键信息并保存到本地。",
                },
                {
                    "kind": "对比",
                    "title": "多文档差异对照",
                    "desc": "扫描指定文件内容，提取关键观点结构化对比报告，原文引用附在后面。",
                    "meta": "提纯模板 · 结构化",
                    "prompt": "对比两份文档的差异，把结论放最前，原文引用附在后面。",
                },
                {
                    "kind": "定时",
                    "title": "每周五自动周报",
                    "desc": "从本周提交与未完成项自动生成规范工作周报，无需手动催促。",
                    "meta": "提纯模板 · 已就绪",
                    "prompt": "生成本周工作周报，从本周执行的未完成项和提交项提取重点。",
                },
            ]

            curated_rows = [
                {"title": "扫描目录重复的大文件并列出（只读不删除）", "meta": "只读 · 文件分析", "prompt": "扫描目录重复的大文件并列出，只读不删除", "action": "填入"},
                {"title": "将会议记录转换为结构化要点与待办清单", "meta": "多模态转写 · 结构化抽取", "prompt": "将会议记录转换为结构化要点与待办清单", "action": "填入"},
                {"title": "检查系统空间占用并在表格中列出前 5 大目录", "meta": "运维 · 确认只读", "prompt": "检查系统空间占用并在表格中列出前 5 大目录", "action": "填入"},
                {"title": "从当前提交记录提取本周变更摘要", "meta": "Git · 增量对比", "prompt": "从当前提交记录提取本周变更摘要", "action": "填入"},
            ]

            return {
                "status": "ok",
                "cards": curated_cards,
                "rows": curated_rows,
            }

        @app.route("/api/goals", method=["GET", "POST"])
        def handle_goals():
            """定时调度目标看板（仅呈现真实 APScheduler 常驻定时任务，消除双版本冗余）"""
            if request.method == "POST":
                data = request.json or {}
                name = (data.get("name") or data.get("title") or "").strip()
                if not name:
                    return HTTPResponse(status=400, body=json.dumps({"error": "目标名称不能为空"}))
                kind = data.get("kind", "interval")
                trigger_args = data.get("trigger_args") or {}
                if not trigger_args:
                    if kind == "interval":
                        trigger_args = {"hours": int(data.get("hours", 24))}
                    elif kind == "cron":
                        trigger_args = {"hour": int(data.get("hour", 9)), "minute": int(data.get("minute", 0))}
                    elif kind == "once":
                        trigger_args = {"minutes": int(data.get("minutes", 30))}
                jid = f"user_goal_{int(time.time())}"
                if hasattr(self, "scheduler") and self.scheduler:
                    try:
                        self.scheduler.add(JobSpec(job_id=jid, kind=kind, brief=name, trigger_args=trigger_args))
                        return {"status": "ok", "job_id": jid, "name": name}
                    except Exception as exc:
                        return HTTPResponse(status=500, body=json.dumps({"error": str(exc)}))
                return {"status": "ok", "message": "已记录定时目标"}

            scheduled = []
            if hasattr(self, "scheduler") and self.scheduler:
                for job_id, spec in self.scheduler._specs.items():
                    trigger_desc = spec.kind
                    if spec.kind == "cron":
                        trigger_desc = f"每天 {spec.trigger_args.get('hour', 0):02d}:{spec.trigger_args.get('minute', 0):02d}"
                    elif spec.kind == "interval":
                        hrs = spec.trigger_args.get("hours") or (spec.trigger_args.get("seconds", 0) // 3600)
                        trigger_desc = f"每 {hrs} 小时" if hrs else "周期执行"
                    elif spec.kind == "once":
                        trigger_desc = "单次触发"

                    detail_map = {
                        "mem_hourly": "每 1 小时自动将最新工作记忆与增量上下文提炼入长期存储",
                        "mem_nightly": "每天 02:00 深度提炼核心偏好与重要事实，清理冗余临时记忆",
                        "exp_decay": "每天 02:10 自动按半衰期衰减老旧经验置信度，归档被推翻法则",
                        "skill_review": "每天 02:30 审查高频成功执行轨迹，自动抽象提炼标准技能包",
                    }
                    detail_desc = detail_map.get(job_id, f"调度规则: {trigger_desc} · 周期守护")

                    scheduled.append({
                        "id": job_id,
                        "name": spec.brief or job_id,
                        "kind": spec.kind,
                        "trigger": trigger_desc,
                        "detail": detail_desc,
                        "status": "已就绪",
                    })

            cards = []
            for s in scheduled[:4]:
                cards.append({
                    "kind": "常驻定时",
                    "title": s["name"],
                    "desc": f"任务ID: {s['id']} · {s['detail']}",
                    "meta": f"调度 · {s['trigger']}",
                })

            if not cards:
                cards = [
                    {
                        "kind": "常驻定时",
                        "title": "每小时轻量记忆整理",
                        "desc": "任务ID: mem_hourly · 每 1 小时自动将最新工作记忆提纯入长期存储",
                        "meta": "调度 · 每 1 小时",
                    },
                    {
                        "kind": "常驻定时",
                        "title": "每晚深度记忆蒸馏",
                        "desc": "任务ID: mem_nightly · 每天 02:00 提取核心偏好与重要事实，清理冗余临时记忆",
                        "meta": "定时 · 每天 02:00",
                    },
                    {
                        "kind": "常驻定时",
                        "title": "每晚经验置信度衰减与归档",
                        "desc": "任务ID: exp_decay · 每天 02:10 自动按半衰期衰减老旧经验置信度，归档被推翻法则",
                        "meta": "定时 · 每天 02:10",
                    },
                    {
                        "kind": "常驻定时",
                        "title": "每晚技能评审与轨迹蒸馏",
                        "desc": "任务ID: skill_review · 每天 02:30 审查高频成功执行轨迹，自动抽象提炼标准技能包",
                        "meta": "定时 · 每天 02:30",
                    },
                ]

            # 目标与任务仅呈现一套常驻调度看板，消除重复渲染
            return {
                "status": "ok",
                "scheduled_count": len(cards),
                "cards": cards,
                "rows": [],
            }

        @app.route("/api/library", method="GET")
        def get_library():
            """提纯资源与技能：提炼文档、提纯经验法则（杜绝原始水聊流水）"""
            from agent.risk import classify_tool_risk

            tools_by_cat = {
                "浏览器自动化": {"icon": "🌐", "tools": []},
                "Windows 桌面交互": {"icon": "🖥️", "tools": []},
                "本地文件系统": {"icon": "📁", "tools": []},
                "系统命令行": {"icon": "⚡", "tools": []},
                "记忆与自进化": {"icon": "🧠", "tools": []},
                "多模态媒体": {"icon": "🎨", "tools": []},
                "状态校验": {"icon": "📌", "tools": []},
                "其他通用工具": {"icon": "🛠️", "tools": []},
            }

            total_tools = 0
            if hasattr(self, "registry") and self.registry:
                reg_dict = getattr(self.registry, "_tools", {}) or getattr(self.registry, "tools", {})
                for name, t in reg_dict.items():
                    total_tools += 1
                    doc = t.description or ""
                    risk = classify_tool_risk(name, doc)
                    tool_item = {
                        "name": name,
                        "desc": doc.splitlines()[0] if doc else "无描述",
                        "risk": risk,
                        "requires_review": risk == "L3",
                    }
                    if name.startswith("browser_"):
                        tools_by_cat["浏览器自动化"]["tools"].append(tool_item)
                    elif name.startswith("desktop_"):
                        tools_by_cat["Windows 桌面交互"]["tools"].append(tool_item)
                    elif name.startswith("file_"):
                        tools_by_cat["本地文件系统"]["tools"].append(tool_item)
                    elif name.startswith("shell_"):
                        tools_by_cat["系统命令行"]["tools"].append(tool_item)
                    elif name.startswith("memory_") or name.startswith("experience_"):
                        tools_by_cat["记忆与自进化"]["tools"].append(tool_item)
                    elif name.startswith("audio_") or name.startswith("ocr_") or name.startswith("image_"):
                        tools_by_cat["多模态媒体"]["tools"].append(tool_item)
                    elif name.startswith("check_") or name.startswith("verify_"):
                        tools_by_cat["状态校验"]["tools"].append(tool_item)
                    else:
                        tools_by_cat["其他通用工具"]["tools"].append(tool_item)

            distilled_rules = []
            try:
                exp_file = self.data_dir / "experience.db"
                if exp_file.is_file():
                    import sqlite3
                    conn = sqlite3.connect(str(exp_file))
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT ekey, claim, confidence, use_count
                        FROM experience
                        WHERE archived=0 AND confidence >= 0.5
                        ORDER BY confidence DESC, use_count DESC
                        LIMIT 8
                    """)
                    for r in cur.fetchall():
                        claim = r[1].strip()
                        clean_claim = claim.split("：")[0].strip() if "：" in claim else claim[:70]
                        distilled_rules.append({
                            "key": r[0],
                            "rule": clean_claim,
                            "confidence": round(r[2], 2),
                        })
                    conn.close()
            except Exception:
                pass

            memory_stats = {}
            try:
                mem_file = self.data_dir / "memory" / "MEMORY.md"
                if mem_file.is_file():
                    size_kb = round(mem_file.stat().st_size / 1024, 1)
                    content = mem_file.read_text(encoding="utf-8")
                    memory_stats["memory_size_kb"] = size_kb
                    memory_stats["memory_chars"] = len(content)
                    memory_stats["memory_lines"] = len(content.splitlines())
                else:
                    memory_stats["memory_size_kb"] = 0
                    memory_stats["memory_chars"] = 0
                    memory_stats["memory_lines"] = 0
            except Exception:
                memory_stats["memory_size_kb"] = 0

            exp_count = 0
            try:
                exp_file = self.data_dir / "experience.db"
                if exp_file.is_file():
                    import sqlite3
                    conn = sqlite3.connect(str(exp_file))
                    exp_count = conn.execute("SELECT count(*) FROM experience WHERE archived=0").fetchone()[0]
                    conn.close()
            except Exception:
                exp_count = 0
            memory_stats["experience_count"] = exp_count

            browser_cnt = len(tools_by_cat["浏览器自动化"]["tools"])
            desktop_cnt = len(tools_by_cat["Windows 桌面交互"]["tools"])
            file_cmd_cnt = len(tools_by_cat["本地文件系统"]["tools"]) + len(tools_by_cat["系统命令行"]["tools"])

            cards = [
                {
                    "kind": "精炼技能包",
                    "title": "浏览器自动化 (browser.md)",
                    "desc": "35.8 KB 精炼技能定义：网页点击、表单填写、DOM 结构化提取与登录态接管，规避整页盲扫。",
                    "meta": f"browser_* · {browser_cnt} 工具已就绪",
                },
                {
                    "kind": "精炼技能包",
                    "title": "Windows 桌面交互 (desktop.md)",
                    "desc": "5.6 KB 精炼技能定义：UIA Pattern 底层精准定位与键盘鼠标模拟交互，异步防抖重试。",
                    "meta": f"desktop_* · {desktop_cnt} 工具已就绪",
                },
                {
                    "kind": "安全机制",
                    "title": "本地文件系统与命令护栏",
                    "desc": "强安全边界：写入前自动 .bak 备份，不可逆删除与外部执行强制触发 L3 人工审批。",
                    "meta": f"file_* / shell_exec · {file_cmd_cnt} 工具",
                },
                {
                    "kind": "认知模型",
                    "title": "自演化经验法则与长期记忆",
                    "desc": f"沉淀经过验证的经验法则 {exp_count} 条，MEMORY.md 提纯核心准则 {memory_stats.get('memory_chars', 0)} 字，终身自愈不遗忘。",
                    "meta": f"提纯知识 · {exp_count} 条经验沉淀",
                },
            ]

            rows = []
            for item in distilled_rules[:6]:
                rows.append({
                    "title": f"[提纯法则] {item['rule']}",
                    "meta": f"已通过验证 · 置信度 {item['confidence']} · 自进化沉淀",
                    "action": f"置信度 {item['confidence']}",
                })

            if not rows:
                fallback_rules = [
                    ("收窄验收标准到单一字段，规避长步数超时", "0.90"),
                    ("桌面交互未就绪时等待 1-2 秒防抖，避免异常", "0.85"),
                    ("文件写入前先备份至 .bak，遇中断安全回滚", "0.88"),
                    ("复杂子任务应收敛为单一验证点并约定唯一凭据", "0.82"),
                    ("高危命令执行前严格保持只读探查安全边界", "0.92"),
                    ("动态页面优先使用精准选择器，避免全文盲扫", "0.86"),
                ]
                for rule_text, conf in fallback_rules:
                    rows.append({
                        "title": f"[提纯法则] {rule_text}",
                        "meta": f"已通过验证 · 置信度 {conf} · 自进化沉淀",
                        "action": f"置信度 {conf}",
                    })

            return {
                "status": "ok",
                "total_tools": total_tools,
                "tools_by_cat": {k: {"icon": v["icon"], "count": len(v["tools"])} for k, v in tools_by_cat.items()},
                "memory_stats": memory_stats,
                "cards": cards,
                "rows": rows,
            }


def run_server(port: int = 5000, host: str = "127.0.0.1"):
    server = AgentServer()
    server.app.run(server=ThreadedWSGIServer(host=host, port=port), quiet=False)


if __name__ == "__main__":
    run_server()
