"""浏览器视觉控制层与人机接管输入锁（Spec §15.7）。

双层架构：
1. 协议层（CDP 硬件级）：
   - `Emulation.setIgnoreInputEvents(ignore=True)`：Chromium 内核直接丢弃操作系统层面的物理键鼠事件；
   - Playwright 的 Agent 合成点击（CDP `Input.dispatchMouseEvent`）完全不受影响，"人点不动，Agent 照点"；
   - 接管时切换为 `ignore=False`，放行用户操作。
2. 视觉层（DOM 遮罩与状态胶囊）：
   - AGENT_ACTIVE（AI 运行态）：四周蓝紫流光呼吸（#0066FF ↔ #7C3AED）+ 虚拟光标平滑跟踪与水波纹；
   - HUMAN_TAKEOVER（人工接管态）：四周琥珀橙（#F59E0B）+ 顶部横幅悬浮 [✓ 完成接管，继续任务] 与 [✕ 停止任务] 按钮；
   - 网页内按钮点击通过 `page.expose_binding` 直接通知 Python 端唤醒等待线程，与桌面 UI 保持双向同步；
   - IDLE（空闲态）：彻底移除 DOM 遮罩并释放硬件输入锁。
"""
from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger("alterego.browser.input_guard")

ControlMode = Literal["AGENT_ACTIVE", "HUMAN_TAKEOVER", "IDLE"]

# 注入浏览器的全套样式与交互图层（支持蓝紫锁定态、琥珀橙接管态及网页内一键回调）
INJECT_OVERLAY_JS = """
(state) => {
  const { mode, reason, taskId } = state || {};
  try {
    Object.defineProperty(window, '__alterego_mode', {
      value: mode,
      writable: true,
      enumerable: false,
      configurable: true
    });
    Object.defineProperty(window, '__alterego_task_id', {
      value: taskId || '',
      writable: true,
      enumerable: false,
      configurable: true
    });
  } catch(e) {
    window.__alterego_mode = mode;
    window.__alterego_task_id = taskId || '';
  }

  // 若为 IDLE 则彻底清理图层
  if (mode === 'IDLE') {
    document.getElementById('__alterego_root')?.remove();
    document.getElementById('__alterego_style')?.remove();
    document.getElementById('__alterego_lock_frame')?.remove();
    return;
  }

  // 1. 注入全局 CSS（仅首次注入）
  if (!document.getElementById('__alterego_style')) {
    const style = document.createElement('style');
    style.id = '__alterego_style';
    style.textContent = `
      @keyframes __ae_agent_glow {
        0%, 100% {
          box-shadow: inset 0 0 0 3.5px rgba(0, 102, 255, 0.9),
                      inset 0 0 36px rgba(0, 102, 255, 0.32),
                      inset 0 0 72px rgba(124, 58, 237, 0.18);
        }
        50% {
          box-shadow: inset 0 0 0 4.5px rgba(124, 58, 237, 0.95),
                      inset 0 0 52px rgba(0, 102, 255, 0.45),
                      inset 0 0 96px rgba(56, 189, 248, 0.28);
        }
      }
      @keyframes __ae_human_glow {
        0%, 100% {
          box-shadow: inset 0 0 0 3.5px rgba(245, 158, 11, 0.95),
                      inset 0 0 28px rgba(245, 158, 11, 0.25);
        }
        50% {
          box-shadow: inset 0 0 0 4px rgba(217, 119, 6, 1),
                      inset 0 0 40px rgba(245, 158, 11, 0.38);
        }
      }
      @keyframes __ae_pulse_dot {
        0%, 100% { transform: scale(1); opacity: 1; }
        50% { transform: scale(0.8); opacity: 0.45; }
      }
      #__ae_mask {
        position: fixed; inset: 0; width: 100vw; height: 100vh;
        pointer-events: none; z-index: 2147483645;
        transition: all 0.25s ease;
      }
      #__ae_mask.mode-agent {
        background: radial-gradient(circle at center, transparent 65%, rgba(0, 50, 160, 0.06) 100%);
        animation: __ae_agent_glow 2.4s infinite ease-in-out;
      }
      #__ae_mask.mode-human {
        background: radial-gradient(circle at center, transparent 75%, rgba(180, 83, 9, 0.05) 100%);
        animation: __ae_human_glow 2s infinite ease-in-out;
      }
      #__ae_banner {
        position: fixed; top: 14px; left: 50%; transform: translateX(-50%);
        z-index: 2147483646; display: flex; align-items: center; gap: 10px;
        padding: 7px 16px; border-radius: 9999px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", sans-serif;
        font-size: 13px; font-weight: 500; color: #FFFFFF;
        background: rgba(24, 24, 27, 0.90); backdrop-filter: blur(10px);
        box-shadow: 0 8px 24px rgba(0,0,0,0.22), 0 0 0 1px rgba(255,255,255,0.14);
        user-select: none;
      }
      .__ae_btn {
        pointer-events: auto; cursor: pointer; border: none; outline: none;
        padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 600;
        transition: transform 0.12s ease, opacity 0.12s ease;
        display: inline-flex; align-items: center; gap: 4px;
      }
      .__ae_btn:hover { opacity: 0.92; transform: scale(1.03); }
      .__ae_btn:active { transform: scale(0.97); }
      #__ae_cursor {
        position: fixed; top: 0; left: 0; width: 24px; height: 24px;
        pointer-events: none; z-index: 2147483647;
        transition: transform 0.045s linear;
        filter: drop-shadow(0 3px 6px rgba(0,0,0,0.35));
      }
    `;
    try {
      (document.head || document.documentElement).appendChild(style);
    } catch(e) {}
  }

  // 2. 创建根容器（仅首次）
  let root = document.getElementById('__alterego_root');
  if (!root) {
    root = document.createElement('div');
    root.id = '__alterego_root';
    root.innerHTML = `
      <div id="__ae_mask"></div>
      <div id="__ae_banner"></div>
      <div id="__ae_cursor" style="transform: translate(-100px, -100px); display: none;">
        <svg viewBox="0 0 24 24" width="24" height="24" fill="#0066FF" stroke="#FFFFFF" stroke-width="2" stroke-linejoin="round">
          <path d="M4 3l16 7.5-7.5 2L10 20 4 3z"/>
        </svg>
      </div>
    `;
    try {
      (document.body || document.documentElement).appendChild(root);
    } catch(e) {}

    // 绑定虚拟鼠标移动与波纹（仅响应 Agent 的合成事件，绝不跟随用户的真实物理鼠标）
    window.addEventListener('mousemove', (e) => {
      if (e.isTrusted) return; // 物理鼠标划过绝对不移动内置虚拟光标！
      const c = document.getElementById('__ae_cursor');
      if (c && window.__alterego_mode === 'AGENT_ACTIVE') {
        c.style.display = 'block';
        c.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
      }
    }, { capture: true, passive: true });

    window.addEventListener('mousedown', (e) => {
      if (e.isTrusted) return; // 物理鼠标误点不触发波纹
      if (window.__alterego_mode !== 'AGENT_ACTIVE') return;
      const ripple = document.createElement('div');
      ripple.style.cssText = `
        position: fixed; left: ${e.clientX - 16}px; top: ${e.clientY - 16}px;
        width: 32px; height: 32px; border-radius: 50%;
        background: rgba(0, 102, 255, 0.35); border: 2px solid #0066FF;
        pointer-events: none; z-index: 2147483646;
        transform: scale(0.3); opacity: 1;
        transition: transform 0.36s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.36s ease-out;
      `;
      try {
        (document.body || document.documentElement).appendChild(ripple);
        requestAnimationFrame(() => {
          ripple.style.transform = 'scale(1.85)';
          ripple.style.opacity = '0';
        });
        setTimeout(() => ripple.remove(), 400);
      } catch(e) {}
    }, { capture: true, passive: true });

    // 绑定物理输入事件捕获拦截器（AGENT_ACTIVE 态下完全阻止用户误点）
    if (!window.__ae_input_blocker_attached) {
      window.__ae_input_blocker_attached = true;

      function __ae_show_lock_toast(x, y) {
        let toast = document.getElementById('__ae_lock_toast');
        if (!toast) {
          toast = document.createElement('div');
          toast.id = '__ae_lock_toast';
          toast.style.cssText = `
            position: fixed; z-index: 2147483647; pointer-events: none;
            background: rgba(15, 23, 42, 0.92); color: #F8FAFC;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 12px; font-weight: 500; padding: 6px 14px; border-radius: 9999px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3), 0 0 0 1px rgba(56, 189, 248, 0.4);
            transition: opacity 0.25s ease, transform 0.25s ease; opacity: 0; transform: translate(-50%, -100%);
          `;
          try {
            (document.body || document.documentElement).appendChild(toast);
          } catch(e) {}
        }
        toast.textContent = '🔒 页面由 AlterEgo 控制中 · 鼠标键盘已锁定防误触（可点击顶部【申请接管】）';
        toast.style.left = (x || window.innerWidth / 2) + 'px';
        toast.style.top = Math.max(50, (y || 80) - 16) + 'px';
        toast.style.opacity = '1';
        toast.style.transform = 'translate(-50%, -100%) scale(1)';
        clearTimeout(window.__ae_toast_timer);
        window.__ae_toast_timer = setTimeout(() => {
          if (toast) toast.style.opacity = '0';
        }, 1400);
      }

      const blockUserInput = (e) => {
        if (window.__alterego_mode === 'AGENT_ACTIVE') {
          const el = e.target && e.target.nodeType === 1 ? e.target : (e.target && e.target.parentElement);
          if (el && el.closest && el.closest('#__ae_banner')) {
            return; // 顶部横幅胶囊完全放行（包括按钮与内部文字）
          }
          if (e.isTrusted) {
            e.stopPropagation();
            e.preventDefault();
            e.stopImmediatePropagation();
            if (e.type === 'mousedown' || e.type === 'click' || e.type === 'pointerdown') {
              __ae_show_lock_toast(e.clientX, e.clientY);
            }
          }
        }
      };

      window.addEventListener('mousedown', blockUserInput, true);
      window.addEventListener('mouseup', blockUserInput, true);
      window.addEventListener('click', blockUserInput, true);
      window.addEventListener('dblclick', blockUserInput, true);
      window.addEventListener('pointerdown', blockUserInput, true);
      window.addEventListener('pointerup', blockUserInput, true);
      window.addEventListener('contextmenu', blockUserInput, true);
      // ⚠️ 极其关键的反检测防线：严禁在 capture 阶段吃掉 mousemove/pointermove！
      // 放行物理鼠标滑动，让网页端风控 SDK（TikTok web-sec-sdk、Cloudflare Turnstile）能采集到真人生理运动熵；
      // 内置虚拟蓝紫光标由 line 131 的 if (e.isTrusted) return 保护，绝不跟随物理鼠标乱晃。
      window.addEventListener('keydown', blockUserInput, true);
      window.addEventListener('keypress', blockUserInput, true);
      window.addEventListener('keyup', blockUserInput, true);
      window.addEventListener('wheel', blockUserInput, { capture: true, passive: false });
    }
  }

  // 3. 根据当前模式更新视觉样式与顶部胶囊内容
  const mask = document.getElementById('__ae_mask');
  const banner = document.getElementById('__ae_banner');
  const cursor = document.getElementById('__ae_cursor');

  if (mode === 'AGENT_ACTIVE') {
    if (mask) mask.className = 'mode-agent';
    if (cursor) cursor.style.display = 'none'; // 默认隐形，仅当 Agent 合成移动时呈现
    if (banner) {
      banner.style.display = 'flex';
      banner.innerHTML = `
        <span style="width:8px;height:8px;border-radius:50%;background:#38BDF8;animation:__ae_pulse_dot 1.4s infinite;flex-shrink:0;"></span>
        <span>AlterEgo 正在控制此页面 · 鼠标键盘已锁定防误触</span>
        <button id="__ae_btn_req_takeover" class="__ae_btn" style="background:#F59E0B;color:#fff;margin-left:6px;padding:3px 10px;font-size:11px;cursor:pointer;pointer-events:auto;">
          👉 申请人工接管
        </button>
      `;
      const btnReq = document.getElementById('__ae_btn_req_takeover');
      if (btnReq) {
        const triggerTakeover = (e) => {
          if (e) {
            e.stopPropagation();
            e.preventDefault();
          }
          if (btnReq.dataset.triggered) return;
          btnReq.dataset.triggered = "true";
          btnReq.textContent = "⏳ 正在切入接管...";
          if (typeof window.__alterego_finish_takeover === 'function') {
            window.__alterego_finish_takeover('request_takeover', window.__alterego_task_id || '');
          }
        };
        btnReq.onclick = triggerTakeover;
        btnReq.onpointerdown = (e) => { e.stopPropagation(); };
        btnReq.onmousedown = (e) => { e.stopPropagation(); };
      }
    }
  } else if (mode === 'HUMAN_TAKEOVER') {
    if (mask) mask.className = 'mode-human';
    if (cursor) cursor.style.display = 'none';
    const tip = reason ? `请手动处理：${reason}` : '您已接管页面，可直接操作鼠标键盘';
    if (banner) {
      banner.style.display = 'flex';
      banner.innerHTML = `
        <span style="width:8px;height:8px;border-radius:50%;background:#F59E0B;animation:__ae_pulse_dot 1s infinite;flex-shrink:0;"></span>
        <span style="margin-right:4px;">${tip}</span>
        <button id="__ae_btn_done" class="__ae_btn" style="background:#10B981;color:#fff;">
          ✓ 完成接管，继续任务
        </button>
        <button id="__ae_btn_cancel" class="__ae_btn" style="background:#EF4444;color:#fff;">
          ✕ 停止任务
        </button>
      `;
      const btnDone = document.getElementById('__ae_btn_done');
      if (btnDone) {
        btnDone.onclick = (e) => {
          e.stopPropagation();
          if (window.__alterego_finish_takeover) {
            window.__alterego_finish_takeover('completed', window.__alterego_task_id || '');
          }
        };
      }
      const btnCancel = document.getElementById('__ae_btn_cancel');
      if (btnCancel) {
        btnCancel.onclick = (e) => {
          e.stopPropagation();
          if (window.__alterego_finish_takeover) {
            window.__alterego_finish_takeover('cancelled', window.__alterego_task_id || '');
          }
        };
      }
    }
  }
}
"""


class BrowserInputGuard:
    """控制浏览器页面的"AI独占态"、"人类接管态"与"空闲态"流转（spec §15.7）。"""

    def __init__(self, page):
        self.page = page
        self._cdp = None
        self.mode: ControlMode = "IDLE"
        self.reason: str = ""
        self.current_task_id: str = ""
        self._locked: bool = False

        # 暴露 JS 回调给网页内的「完成接管」与「停止任务」按钮
        self._expose_binding_safe()

        # 页面发生跳转、DOM 就绪或刷新时，自动重绘遮罩并维持锁状态
        try:
            self.page.on("load", self._on_page_load)
            self.page.on("domcontentloaded", self._on_page_load)
            self.page.on("framenavigated", self._on_page_load)
        except Exception:
            pass

    def _expose_binding_safe(self):
        if self.page is None:
            return
        try:
            self.page.expose_binding("__alterego_finish_takeover", self._on_human_finished_from_ui)
        except Exception:
            pass  # 已存在绑定时不重复绑定

    def _on_page_load(self, _):
        if self.mode != "IDLE":
            self._sync_state()

    def _on_human_finished_from_ui(self, source, outcome: str, task_id: str = ""):
        """用户在网页横幅中点击按钮的回调。"""
        if outcome == "request_takeover":
            logger.info("[input_guard] 用户点击横幅主动申请人工接管")
            try:
                import uuid
                from agent.takeover import get_takeover_manager
                mgr = get_takeover_manager()
                tid = task_id or f"takeover_{uuid.uuid4().hex[:8]}"
                self.unlock_for_human(reason="用户主动申请接管", task_id=tid)
                mgr.request_takeover(
                    task_id=tid,
                    reason="用户在网页中主动申请接管控制",
                    target="Chrome",
                    page_url=getattr(self.page, "url", ""),
                    timeout_sec=7200.0,
                )
            except Exception as e:
                logger.warning(f"[input_guard] 主动接管触发失败: {e}")
            return

        outcome = outcome if outcome in ("completed", "cancelled") else "completed"
        tid = task_id or self.current_task_id
        logger.info(f"[input_guard] 收到网页内接管交互: outcome={outcome} tid={tid}")
        try:
            from agent.takeover import get_takeover_manager
            mgr = get_takeover_manager()
            if tid:
                mgr.resolve(
                    tid,
                    outcome=outcome,
                    summary="用户在网页横幅内确认完成接管" if outcome == "completed" else "用户在网页横幅内取消接管",
                )
            else:
                # 寻找第一个活跃状态的接管请求
                with getattr(mgr, "_lock"):
                    for aid, req in getattr(mgr, "_requests", {}).items():
                        if req.status == "active":
                            mgr.resolve(
                                aid,
                                outcome=outcome,
                                summary="用户在网页横幅内确认完成接管" if outcome == "completed" else "用户在网页横幅内取消接管",
                            )
                            break
        except Exception as e:
            logger.warning(f"[input_guard] 处理网页内接管回调失败: {e}")

    def _cdp_session(self):
        if self._cdp is None and self.page is not None:
            try:
                self._cdp = self.page.context.new_cdp_session(self.page)
            except Exception:
                self._cdp = None
        return self._cdp

    def _sync_state(self) -> bool:
        """将当前状态同步到网页 DOM。"""
        if self.page is None:
            return False
        try:
            self.page.evaluate(
                INJECT_OVERLAY_JS,
                {"mode": self.mode, "reason": self.reason, "taskId": self.current_task_id},
            )
            return True
        except Exception:
            return False

    def lock_for_agent(self) -> bool:
        """进入 running / AGENT_ACTIVE 态：DOM 捕获阶段拦截误触 + 蓝紫流光呼吸遮罩。
        注意：必须保证 CDP Emulation.setIgnoreInputEvents 为 False，
        由 window 捕获阶段的 blockUserInput 精确拦截底层网页事件，同时放行顶部胶囊横幅交互，
        确保用户随时可以点击【👉 申请人工接管】进行主动介入。
        """
        self.mode = "AGENT_ACTIVE"
        self.reason = ""
        ok = True
        cdp = self._cdp_session()
        if cdp is not None:
            try:
                cdp.send("Emulation.setIgnoreInputEvents", {"ignore": False})
            except Exception:
                ok = False
        ui_ok = self._sync_state()
        self._locked = ok
        return ok and ui_ok

    def unlock_for_human(self, reason: str = "", task_id: str = "") -> bool:
        """进入 paused_takeover / HUMAN_TAKEOVER 态：CDP 放行人类物理输入 + 琥珀橙接管横幅与按钮。"""
        self.mode = "HUMAN_TAKEOVER"
        self.reason = reason
        self.current_task_id = task_id
        ok = True
        cdp = self._cdp_session()
        if cdp is not None:
            try:
                cdp.send("Emulation.setIgnoreInputEvents", {"ignore": False})
            except Exception:
                ok = False
        ui_ok = self._sync_state()
        self._locked = False
        return ok and ui_ok

    def release_all(self) -> bool:
        """彻底退出所有遮罩与输入锁，回到 IDLE 态。"""
        self.mode = "IDLE"
        self.reason = ""
        self.current_task_id = ""
        ok = True
        cdp = self._cdp_session()
        if cdp is not None:
            try:
                cdp.send("Emulation.setIgnoreInputEvents", {"ignore": False})
            except Exception:
                ok = False
        self._sync_state()
        self._locked = False
        return ok

    @property
    def is_locked(self) -> bool:
        return self._locked
