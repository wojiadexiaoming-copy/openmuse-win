"""§22 Hooks：事件驱动。轻量轮询（纯代码，零 LLM）+ 语义去重（§22.5）+ 重型唤醒。

三级过滤：代码级（hash 变了没）→ 语义级（关心的字段真变了吗+弱模型判意义）
→ 用户级（主 Agent 决定推不推）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Hook:
    hook_id: str
    name: str
    check_fn: object                  # () -> dict/str，无 LLM，<5 秒
    check_interval_sec: int = 600
    cooldown_sec: int = 3600
    enabled: bool = True
    semantic_fields: list[str] = field(default_factory=list)
    semantic_check: bool = True
    last_state: str = ""
    last_fields: dict = field(default_factory=dict)  # 上次关心的字段快照
    last_fired: float = 0.0


@dataclass
class HookEvent:
    hook_id: str
    change_summary: str
    evidence: str = ""


def hash_state(current) -> str:
    raw = json.dumps(current, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def extract_fields(state, fields: list[str]) -> dict:
    if isinstance(state, dict):
        return {f: state.get(f) for f in fields}
    return {}


class HookPoller:
    def __init__(self, on_wake=None, semantic_judge=None):
        """on_wake(hook, event)：唤醒主 Agent。
        semantic_judge(prompt) -> "YES"/"NO"：弱模型，调用方注入。"""
        self.on_wake = on_wake
        self.semantic_judge = semantic_judge
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()

    def add(self, hook: Hook):
        t = threading.Thread(target=self._loop, args=(hook,), daemon=True)
        self._threads[hook.hook_id] = t
        t.start()

    def stop_all(self):
        self._stop.set()

    def _loop(self, hook: Hook):
        while hook.enabled and not self._stop.is_set():
            try:
                current = hook.check_fn()
                fp = hash_state(current)
                if fp != hook.last_state:
                    hook.last_state = fp
                    # §22.5 语义去重（唤醒前）：先字段对比，再弱模型
                    if hook.semantic_check and not self._semantic_changed(hook, current):
                        time.sleep(hook.check_interval_sec)
                        continue
                    if time.time() - hook.last_fired < hook.cooldown_sec:
                        pass  # 冷静期，跳过
                    else:
                        hook.last_fired = time.time()
                        if self.on_wake:
                            self.on_wake(hook, HookEvent(
                                hook_id=hook.hook_id,
                                change_summary=json.dumps(
                                    current, ensure_ascii=False)[:500]))
            except Exception:  # noqa: BLE001 —— 检查失败只记，不崩轮询
                pass
            time.sleep(hook.check_interval_sec)

    def _semantic_changed(self, hook: Hook, current) -> bool:
        """§22.5：先字段对比（零 LLM），再弱模型判意义。"""
        new_fields = extract_fields(current, hook.semantic_fields)
        old_fields = hook.last_fields
        hook.last_fields = new_fields
        if not hook.semantic_fields:
            return True  # 没指定关心字段 → hash 变了就算变
        if old_fields == new_fields:
            return False  # 关心的字段没变 → 不唤醒（连 LLM 都不调）
        if self.semantic_judge is None:
            return True  # 无 judge 函数 → 保守唤醒
        verdict = self.semantic_judge(
            f"监测项：{hook.name}\n关心字段：{hook.semantic_fields}\n"
            f"旧值：{json.dumps(old_fields, ensure_ascii=False)}\n"
            f"新值：{json.dumps(new_fields, ensure_ascii=False)}\n"
            "这次变化对用户有意义吗？只答 YES 或 NO。")
        return verdict.strip().upper().startswith("YES")
