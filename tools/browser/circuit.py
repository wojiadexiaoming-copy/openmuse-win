"""§9.6 熔断机制：分级阈值控制。

L0 正常 → L1 观察（降频）→ L2 熔断（暂停该 profile，需手动恢复）→ L3 冻结（永久停用）。
阈值可在 profiles.yaml 按 profile 覆盖（本文件用默认阈值 + 构造参数覆盖）。
"""
from __future__ import annotations

import time


class CircuitBreaker:
    # 默认阈值（spec §9.6）
    L1_CAPTCHA_PER_HOUR = 2
    L2_CAPTCHA_PER_HOUR = 5
    L2_CONSECUTIVE_CAPTCHA = 3
    L2_CONSECUTIVE_DENIED = 3
    L1_RECOVER_HOURS = 24

    def __init__(self, profile: str, **overrides):
        self.profile = profile
        for k, v in overrides.items():
            setattr(self, k.upper(), v)
        self.level = 0  # 0=L0 1=L1 2=L2 3=L3
        self._events: list[tuple[float, str]] = []  # (ts, kind)
        self._l1_at: float = 0.0
        self.paused = False

    # ---------- 信号上报（浏览器工具层调） ----------

    def record(self, kind: str):
        """kind: captcha | denied | warning_banner | kicked | banned"""
        now = time.time()
        self._events.append((now, kind))
        # 只保留 1 小时内的
        self._events = [(t, k) for t, k in self._events if now - t < 3600]
        if kind == "banned":
            self._set_level(3, "账号封禁/冻结通知")
            return
        if kind in ("warning_banner", "kicked"):
            self._set_level(2, f"直接 L2：{kind}")
            return
        self._evaluate()

    def _consecutive(self, kind: str) -> int:
        n = 0
        for _, k in reversed(self._events):
            if k == kind:
                n += 1
            else:
                break
        return n

    def _count(self, kind: str) -> int:
        return sum(1 for _, k in self._events if k == kind)

    def _evaluate(self):
        if self._count("captcha") >= self.L2_CAPTCHA_PER_HOUR:
            self._set_level(2, "1 小时内验证码超 5 次")
        elif self._consecutive("captcha") >= self.L2_CONSECUTIVE_CAPTCHA:
            self._set_level(2, "连续 3 次验证码")
        elif self._consecutive("denied") >= self.L2_CONSECUTIVE_DENIED:
            self._set_level(2, "连续 3 次操作被拒")
        elif self._count("captcha") >= self.L1_CAPTCHA_PER_HOUR:
            self._set_level(1, "1 小时内验证码 2 次")

    def _set_level(self, level: int, reason: str):
        if level > self.level:
            self.level = level
            if level >= 2:
                self.paused = True  # L2+：暂停该 profile 全部任务

    # ---------- 恢复 ----------

    def try_recover(self) -> bool:
        """L1：24 小时无异常自动回 L0。L2：需手动确认。L3：永不自动恢复。"""
        if self.level == 3:
            return False
        if self.level == 2:
            return False  # 需用户手动确认（UI 按钮调 manual_resume）
        if self.level == 1 and time.time() - self._l1_at > self.L1_RECOVER_HOURS * 3600:
            recent = [k for t, k in self._events
                      if time.time() - t < self.L1_RECOVER_HOURS * 3600]
            if not recent:
                self.level = 0
                return True
        return False

    def manual_resume(self):
        """用户手动确认恢复（L2）。"""
        if self.level == 2:
            self.level = 0
            self.paused = False
            self._events.clear()

    # ---------- 给执行层 ----------

    def delay_factor(self) -> float:
        """L1：动作间隔 ×2（任务降频 50% 的实现方式）。"""
        return 2.0 if self.level == 1 else 1.0

    def check_before_action(self, profile: str) -> str | None:
        """动作前检查：返回 None=可执行，否则返回阻止原因。"""
        if self.paused:
            return f"[熔断] profile={profile} 已暂停（L{self.level}），需手动恢复"
        return None
