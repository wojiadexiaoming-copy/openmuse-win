"""用户接管管理器（spec §15 / §31.5）。

当浏览器/桌面 Agent 遇到需要人工处理的环节（验证码、扫码、登录墙、复杂人工操作），
通过 TakeoverManager 进入 paused_takeover 状态，弹出置顶小浮窗，等待用户操作完成后
再从暂停点自动恢复执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import logging
import threading
from typing import Any, Callable, Literal

log = logging.getLogger("alterego.takeover")


@dataclass
class TakeoverRequest:
    task_id: str
    reason: str
    target: str = "Chrome"
    page_url: str = ""
    screenshot_path: str = ""
    requested_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    expires_at: str = ""
    status: Literal["active", "completed", "cancelled", "timeout"] = "active"

    def __post_init__(self):
        if not self.expires_at:
            # 默认 2 小时超时（spec §15.2）
            now = datetime.datetime.now(datetime.timezone.utc)
            self.expires_at = (now + datetime.timedelta(hours=2)).isoformat()


@dataclass
class TakeoverResult:
    task_id: str
    outcome: Literal["completed", "cancelled", "timeout"]
    finished_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    summary: str = ""


class TakeoverManager:
    """集中管理接管请求的生命周期与线程同步。"""

    def __init__(
        self,
        event_bus=None,
        auto_show_window: bool = True,
        port: int = 5000,
        on_event: Callable | None = None,
    ):
        self.event_bus = event_bus
        self.auto_show_window = auto_show_window
        self.port = port
        self.on_event = on_event
        self._requests: dict[str, TakeoverRequest] = {}
        self._events: dict[str, threading.Event] = {}
        self._results: dict[str, TakeoverResult] = {}
        self._lock = threading.RLock()

    def request_takeover(
        self,
        task_id: str,
        reason: str,
        target: str = "Chrome",
        page_url: str = "",
        screenshot_path: str = "",
        timeout_sec: float = 7200.0,
    ) -> TakeoverRequest:
        """发起接管请求并触发浮窗。"""
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            exp = (now + datetime.timedelta(seconds=timeout_sec)).isoformat()
            req = TakeoverRequest(
                task_id=task_id,
                reason=reason,
                target=target,
                page_url=page_url,
                screenshot_path=screenshot_path,
                expires_at=exp,
                status="active",
            )
            self._requests[task_id] = req
            self._events[task_id] = threading.Event()

        log.info(f"[takeover] 发起接管 task_id={task_id} target={target} reason={reason}")

        try:
            from agent.notify import notify
            notify(
                title="AlterEgo 需要人工介入协同",
                message=f"【{target}】{reason}\n请在浏览器完成操作后交还控制权。",
                urgent=True,
            )
        except Exception as e:
            log.debug(f"[takeover] 发送系统通知失败: {e}")

        payload = {
            "task_id": task_id,
            "target": target,
            "reason": reason,
            "page_url": page_url,
            "screenshot_path": screenshot_path,
            "requested_at": req.requested_at,
            "expires_at": req.expires_at,
        }
        if self.on_event:
            self.on_event("takeover_request", payload)
        elif self.event_bus:
            self.event_bus.broadcast("takeover_request", payload)

        if self.auto_show_window:
            try:
                from ui.takeover_window import show_takeover_window_async
                show_takeover_window_async(
                    task_id=task_id,
                    target=target,
                    reason=reason,
                    port=self.port,
                )
            except Exception as e:
                log.warning(f"[takeover] 启动接管浮窗失败（继续保持等待）: {e}")

        return req

    def resolve(
        self,
        task_id: str,
        outcome: Literal["completed", "cancelled", "timeout"],
        summary: str = "",
    ) -> TakeoverResult:
        """完成/取消/超时接管请求，唤醒等待线程。"""
        with self._lock:
            req = self._requests.get(task_id)
            if req:
                req.status = outcome
            res = TakeoverResult(task_id=task_id, outcome=outcome, summary=summary)
            self._results[task_id] = res
            ev = self._events.get(task_id)
            if ev:
                ev.set()

        log.info(f"[takeover] 接管解决 task_id={task_id} outcome={outcome}")

        payload = {"task_id": task_id, "status": outcome, "summary": summary}
        if self.on_event:
            self.on_event("takeover_update", payload)
        elif self.event_bus:
            self.event_bus.broadcast("takeover_update", payload)
        return res

    def get_status(self, task_id: str) -> dict[str, Any]:
        """获取当前接管状态。"""
        with self._lock:
            req = self._requests.get(task_id)
            if not req:
                return {"task_id": task_id, "target": "Chrome", "status": "unknown"}
            res = self._results.get(task_id)
            return {
                "task_id": req.task_id,
                "target": req.target,
                "reason": req.reason,
                "page_url": req.page_url,
                "status": req.status,
                "summary": res.summary if res else "",
                "requested_at": req.requested_at,
                "expires_at": req.expires_at,
            }

    def wait_for_takeover(
        self,
        task_id: str,
        timeout_sec: float = 7200.0,
    ) -> TakeoverResult:
        """阻塞当前线程等待用户完成接管（或超时）。"""
        with self._lock:
            ev = self._events.get(task_id)
        if not ev:
            return TakeoverResult(task_id=task_id, outcome="cancelled", summary="无对应事件")

        ok = ev.wait(timeout=timeout_sec)
        if not ok:
            return self.resolve(task_id, "timeout", "用户接管超时")

        with self._lock:
            return self._results.get(
                task_id,
                TakeoverResult(task_id=task_id, outcome="completed", summary="完成"),
            )


# 全局单例管理器
_global_takeover_mgr: TakeoverManager | None = None


def get_takeover_manager() -> TakeoverManager:
    global _global_takeover_mgr
    if _global_takeover_mgr is None:
        _global_takeover_mgr = TakeoverManager()
    return _global_takeover_mgr


def set_takeover_manager(mgr: TakeoverManager) -> None:
    global _global_takeover_mgr
    _global_takeover_mgr = mgr
