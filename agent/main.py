"""M1/M2 入口：CLI 对话循环。

桌面 UI（pywebview 壳 + H5）需 Windows 真机验证，见 ui/。
本文件是跨平台可测的 CLI 入口，逻辑与 UI 共用 Orchestrator。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

# 允许从仓库根目录直接 python -m agent.main 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.approval import ApprovalManager
from agent.context import ContextBuilder
from agent.llm import LLMClient
from agent.memory_manager import MemoryManager
from agent.orchestrator import Orchestrator
from agent.resource_lock import ResourceLock
from agent.session_store import SessionStore
from agent.settings import Settings
from agent.task_tracker import TaskTracker
from agent.tools_registry import ToolRegistry
from agent.tracing import Tracer, make_llm_request_observer
from tools import register_all  # noqa: E402
from tools.cache import ToolResultCache  # noqa: E402
from vault import Vault  # noqa: E402


def load_config() -> dict:
    repo_root = Path(__file__).resolve().parent.parent
    cfg_path = Path("config.yaml")
    if not cfg_path.exists():
        cfg_path = repo_root / "config.yaml"
    if not cfg_path.exists():
        print("缺 config.yaml：请复制 config.example.yaml 为 config.yaml 并填写")
        sys.exit(1)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    # 附3.6：配置键校验（M1 只校验用到的键）
    for key in ("llm", "context", "agent"):
        if key not in cfg:
            raise RuntimeError(f"config.yaml 缺少 [{key}]（见 config.example.yaml）")
    from agent.provider_config import load_provider_config
    return load_provider_config(cfg, cfg_path)


def cli_on_ask(card) -> tuple[bool, str]:
    """终端审批卡（M1）。真机 UI 在 ui/ 里实现后替换（见 ui/api.md）。"""
    print(f"\n[审批] {card.action}（风险 {card.risk}）")
    print(f"  参数：{card.detail[:300]}")
    if card.batch:
        print(f"  另有 {len(card.batch)} 个同类操作合并确认")
    ans = input("  通过？[y=本次通过 / s=本任务内免批 / a=永久允许 / n=拒绝] ").strip().lower()
    if ans == "y":
        return True, "once"
    if ans == "s":
        return True, "session"
    if ans == "a":
        if card.risk == "L3":
            print("  L3 操作不允许永久批准（spec §36.1）")
            return False, "once"
        return True, "always"
    return False, "once"


def resolve_llm_key(vault: Vault) -> str:
    """M2：环境变量优先（显式启动意图），fallback Vault（持久化）。

    2026-10-16 修正：之前是 Vault 优先，导致用户按文档设置
    ALTEREGO_LLM_API_KEY 后仍被 Vault 里的旧 key 覆盖，
    报 BadRequestError（"Please pass a valid API key"）。
    按 12-factor 惯例，显式的环境变量应优先于持久化配置。
    """
    env_key = os.environ.get("ALTEREGO_LLM_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return vault.resolve("llm_api_key", caller="agent.main")
    except (PermissionError, KeyError):
        return ""


def resolve_backup_llm_key(vault: Vault) -> str:
    """环境变量优先，fallback Vault（与 resolve_llm_key 一致）。"""
    env_key = os.environ.get("ALTEREGO_BACKUP_LLM_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return vault.resolve("backup_llm_api_key", caller="agent.main")
    except (PermissionError, KeyError):
        return ""


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    cfg = load_config()
    data_dir = Path(os.environ.get("ALTEREGO_DATA", str(repo_root)))
    data_dir.mkdir(parents=True, exist_ok=True)

    vault = Vault.for_machine(data_dir)
    llm_key = resolve_llm_key(vault)
    backup_key = resolve_backup_llm_key(vault)
    llm = LLMClient.from_config(cfg, api_key=llm_key, fallback_api_key=backup_key, vault=vault)

    registry = ToolRegistry()
    register_all(registry, cfg, api_key=llm_key)

    settings = Settings(data_dir / "settings.json")
    approval = ApprovalManager(str(data_dir / "approvals.db"), settings=settings)
    registry.set_approval(approval, on_ask=cli_on_ask)

    # M4 组件（配置开关控制，默认只开无害的）
    registry.set_cache(ToolResultCache(str(data_dir / "cache.db")))
    registry.set_locks(ResourceLock())
    tracker = TaskTracker() if cfg["agent"].get("task_tracker", True) else None
    tracer = Tracer(str(data_dir / "spans.db"))
    llm.set_request_observer(make_llm_request_observer(tracer, channel="cli"))

    memory = MemoryManager(data_dir / "memory")
    ctx = ContextBuilder(max_tokens=cfg["context"]["max_context_tokens"],
                         extra_system=memory.read_main)

    store = SessionStore(str(data_dir / "conversations.db"))
    session_id = store.new_session()

    from agent.planner import Planner
    planner = None
    if cfg["agent"].get("planner", True):
        planner_llm = LLMClient.for_role(cfg, "planner", vault)
        planner_llm.set_request_observer(make_llm_request_observer(tracer, channel="planner"))
        planner = Planner(planner_llm)

    from agent.subagent import SubagentManager, make_subagent_tools
    def _make_sub_orchestrator(task):
        role = "code" if getattr(task, "kind", "") == "code" and (
            cfg.get("llm_router", {}).get("enabled") is True
            or "code" in cfg.get("models", {}).get("roles", {})) else "subagent"
        sub_llm = LLMClient.for_role(cfg, role, vault)
        if getattr(task, "critical", False):
            sub_llm = LLMClient.for_role(
                cfg, cfg.get("models", {}).get("critical_subagent_role", "main"), vault)
        sub_llm.set_request_observer(make_llm_request_observer(tracer, channel="subagent"))
        sub_prompt_file = repo_root / "prompts" / "subagent.md"
        sub_prompt = sub_prompt_file.read_text(encoding="utf-8") if sub_prompt_file.exists() else ""
        sub_ctx = ContextBuilder(
            max_tokens=cfg["context"]["max_context_tokens"] // 2,
            extra_system=sub_prompt,
        )
        return Orchestrator(sub_llm, registry, sub_ctx,
                            max_iters=15, parallel_tools=False,
                            tracer=tracer, tracker=tracker, planner=None)

    sub_mgr = SubagentManager(make_orchestrator=_make_sub_orchestrator)
    for t in make_subagent_tools(sub_mgr):
        registry.register(t)

    # §28 Connectors / §23 Proactivity / §25 Workflow /
    # §26 Artifacts / §30 TTS / §34 备份（2026-10-15 补）
    from agent.connectors import ConnectorManager, make_connector_tools
    from agent.proactivity import ProactivityStore, make_proactivity_tools
    from agent.workflow import WorkflowEngine, make_workflow_tools
    from agent.artifacts import ArtifactManager, make_artifact_tools
    from agent.tts import TTSManager, make_tts_tools
    from agent.backup import make_backup_tools
    for t in make_connector_tools(ConnectorManager(vault=vault)):
        registry.register(t)
    for t in make_proactivity_tools(ProactivityStore()):
        registry.register(t)
    for t in make_workflow_tools(WorkflowEngine()):
        registry.register(t)
    for t in make_artifact_tools(ArtifactManager()):
        registry.register(t)
    for t in make_tts_tools(TTSManager()):
        registry.register(t)
    for t in make_backup_tools():
        registry.register(t)

    # §10 定时：schedule_once 注册成工具（替代 sleep 硬等）
    from agent.tools_registry import ToolDefinition
    _orch_holder: dict = {}  # orch 创建后填入，工具闭包通过它调用
    def _schedule_once(args: dict) -> str:
        try:
            secs = int(args.get("seconds", 0))
            brief = args.get("brief", "")
            if secs <= 0:
                return "[工具执行失败] seconds 必须 > 0"
            orch_ref = _orch_holder.get("orch")
            if orch_ref is None:
                return "[工具执行失败] Orchestrator 未就绪"
            jid = orch_ref.schedule_once(secs, brief)
            return f"[定时已设置] {secs} 秒后提醒：{brief[:50]}（job={jid}）"
        except Exception as e:
            return f"[工具执行失败] {e}"
    registry.register(ToolDefinition(
        name="schedule_once",
        description="N 秒后提醒我做某事（替代 sleep 硬等）。到时会自动注入提醒。",
        parameters={"type": "object",
                     "properties": {
                         "seconds": {"type": "integer", "description": "几秒后"},
                         "brief": {"type": "string", "description": "到时提醒做什么"}},
                     "required": ["seconds", "brief"]},
        func=_schedule_once,
    ))

    orch = Orchestrator(llm, registry, ctx,
                        planner=planner,
                        max_iters=cfg["agent"].get("max_iters", 25),
                        max_iters_hard=cfg["agent"].get("max_iters_hard", 100),
                        parallel_tools=cfg["agent"].get("parallel_tools", False),
                        tracer=tracer, tracker=tracker,
                        sub_mgr=sub_mgr)
    _orch_holder["orch"] = orch  # schedule_once 工具闭包绑定

    print("AlterEgo（输入 quit 退出）")
    try:
        while True:
            try:
                text = input("\n你> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text.lower() in ("quit", "exit", "退出"):
                break
            if not text:
                continue
            # §8.3：用户明确说"记住"就记
            claimed = memory.maybe_remember(text)
            if claimed:
                print(f"[已记住] {claimed}")
            try:
                # 进 LLM / 日志前的文本先过 vault.scrub（§11）
                print(f"\n影仔> {vault.scrub(orch.handle(text))}")
            except NotImplementedError as e:
                print(f"\n[未实现] {e}")
            except Exception as e:  # noqa: BLE001 —— CLI 兜底，不能崩
                print(f"\n[出错] {type(e).__name__}: {e}")
    finally:
        # §33：退出前 history 落盘
        store.save_history(session_id, orch.history)
        print(f"\n[会话已保存] {session_id}")
        # Playwright 的 node 驱动必须先停，否则退出时它对着断开的管道写事件，
        # 控制台会甩一段 EPIPE 崩溃堆栈（2026-10-17 双机实测）。
        _browser_worker = getattr(registry, "browser_worker", None)
        if _browser_worker is not None:
            try:
                _browser_worker.stop()
            except Exception:
                pass

    # [M1-UI 完成] ui/launcher.py 与 ui/server.py 完整实现并已跑通。



if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent.parent)
    main()
