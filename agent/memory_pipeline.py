"""§8 / memory-pipeline.md: 记忆提炼后台管线（对应 Muse 后台记忆 Agent）。

无用户交互的后台 Worker：输入=原始材料（会话记录/工具轨迹/审批日志），输出=精华记忆条目。
模型走 worker 档（最便宜最快）。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .experience import ExperienceStore
    from .memory_manager import MemoryManager
    from .session_store import SessionStore
    from .tracing import Tracer
    from vault.manager import Vault


def run_memory_distill(
    memory: MemoryManager,
    store: SessionStore | None = None,
    tracer: Tracer | None = None,
    experience_store: ExperienceStore | None = None,
    scope: str = "hourly",
    session_id: str | None = None,
    vault: Vault | None = None,
    cfg: dict | None = None,
) -> dict:
    """运行一次记忆提炼流程。

    :param scope: "hourly" | "nightly" | "session"
    """
    from .compaction import slim_tool_result
    from .llm import LLMClient
    from .models import Message

    # 1. 收料
    raw_materials = []
    if scope == "session" and session_id and store:
        history = store.load_history(session_id)
        for m in history:
            if m.name in {"tool_observation", "verification_feedback", "plan_step"}:
                continue
            m_slim = slim_tool_result(m)
            raw_materials.append(f"[{m_slim.role}] {m_slim.content[:400]}")
    elif scope in ("hourly", "nightly") and store:
        sessions = store.list_sessions(limit=5)
        for s in sessions:
            hist = store.load_history(s["session_id"])
            for m in hist[-20:]:
                if m.name in {"tool_observation", "verification_feedback", "plan_step"}:
                    continue
                m_slim = slim_tool_result(m)
                raw_materials.append(f"[{m_slim.role}] {m_slim.content[:400]}")

    # 若是 nightly，同时合并今日 daily log
    if scope == "nightly" and memory.dir:
        daily_file = memory.dir / f"{time.strftime('%Y-%m-%d')}.md"
        if daily_file.exists():
            try:
                raw_materials.append(f"[今日流水日志]\n" + daily_file.read_text(encoding="utf-8")[:3000])
            except Exception:
                pass

    if not raw_materials:
        return {"status": "skipped", "reason": "无原始材料", "extracted_count": 0}

    materials_text = "\n".join(raw_materials[:100])
    if len(materials_text) > 8000:
        materials_text = materials_text[:8000] + "\n[...超长截断...]"

    # 2. 读档：读取当前 MEMORY.md 全文供参照
    current_memory = memory.read_main(max_chars=6000)

    # 3. 提炼：调 Worker 模型
    prompt_file = Path("prompts/worker-memory.md")
    worker_sys = (
        prompt_file.read_text(encoding="utf-8")
        if prompt_file.exists()
        else "你是记忆提炼 Worker。请从材料中提炼长期记忆，输出 ## Facts, ## Preferences, ## Lessons。"
    )

    extracted_text = ""
    try:
        worker_llm = LLMClient.for_role(cfg or {}, "worker", vault) if cfg else None
        if worker_llm:
            user_prompt = (
                f"【当前 MEMORY.md 内容】\n{current_memory}\n\n"
                f"【最新待提炼原始材料（{scope}）】\n{materials_text}\n\n"
                "请严格按照规则提取新的长期记忆，输出 ## Facts / ## Preferences / ## Lessons 分区或旧条目纠正："
            )
            resp = worker_llm.chat([
                Message(role="system", content=worker_sys),
                Message(role="user", content=user_prompt),
            ])
            extracted_text = resp.content.strip()
    except Exception:
        # 降级或测试环境无网络时跳过
        pass

    if not extracted_text:
        return {"status": "ok", "reason": "无新记忆需要提炼", "extracted_count": 0}

    # 4. 落盘
    facts_count, prefs_count, lessons_count, corrections_count = 0, 0, 0, 0
    current_cat = None

    for line in extracted_text.splitlines():
        line_str = line.strip()
        if not line_str:
            continue

        # 检查分类标题
        if line_str.startswith("## Facts"):
            current_cat = "Facts"
            continue
        elif line_str.startswith("## Preferences"):
            current_cat = "Preferences"
            continue
        elif line_str.startswith("## Lessons"):
            current_cat = "Lessons"
            continue

        # 检查旧条目纠正：<旧> → <新>
        if "→" in line_str or "->" in line_str:
            sep = "→" if "→" in line_str else "->"
            parts = line_str.lstrip("-* ").split(sep, 1)
            if len(parts) == 2:
                old_claim = parts[0].strip()
                new_claim = parts[1].strip()
                if old_claim:
                    memory.remove_claim(old_claim)
                if new_claim:
                    memory._append(current_cat or "Facts", new_claim, source=f"{scope}纠正")
                    corrections_count += 1
            continue

        # 提取条目
        if line_str.startswith("- ") or line_str.startswith("* "):
            claim = line_str.lstrip("-* ").strip()
            if not claim:
                continue
            cat = current_cat or "Facts"
            if not memory._is_duplicate(claim):
                memory._append(cat, claim, source=scope)
                if cat == "Facts":
                    facts_count += 1
                elif cat == "Preferences":
                    prefs_count += 1
                elif cat == "Lessons":
                    lessons_count += 1
                    if experience_store:
                        try:
                            experience_store.add(f"lesson_{int(time.time())}", claim)
                        except Exception:
                            pass

    total_extracted = facts_count + prefs_count + lessons_count + corrections_count

    # 5. 审计
    if tracer:
        try:
            tracer.log_event("memory_distill", {
                "scope": scope,
                "input_chars": len(materials_text),
                "extracted_count": total_extracted,
                "corrections": corrections_count,
            })
        except Exception:
            pass

    return {
        "status": "ok",
        "scope": scope,
        "facts": facts_count,
        "preferences": prefs_count,
        "lessons": lessons_count,
        "corrections": corrections_count,
        "extracted_count": total_extracted,
    }
