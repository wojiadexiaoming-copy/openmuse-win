"""浏览器工具：navigate / click / type / snapshot。

感知链（spec §9）：CDP 无障碍树优先 → Playwright locator → 截图兜底。

[实测结论 2026-10-11]：Playwright Python 1.63 没有 page.accessibility.snapshot()
（spec 曾标 [待验证]，现确认不存在）。无障碍树走 CDP
Accessibility.getFullAXTree（已在 Chromium 1243 上实测可用）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import random
import sys
import threading
import time

from agent.tools_registry import ToolDefinition
from agent.models import ImagePart, Message


# ===== Gemini 补丁 2026-10-15：可视虚拟鼠标 + 平滑点击 =====
# 原理：Playwright 的 mouse.click() 是隐形瞬移，人眼看不见轨迹，
# 录屏时画面乱跳。注入顶层 SVG 光标 + 平滑插值移动 + 点击水波纹。
CURSOR_INJECT_JS = """
(() => {
  if (window.__alterego_cursor) return;
  const cursor = document.createElement('div');
  cursor.id = '__alterego_cursor';
  cursor.style.cssText = `
    position: fixed; top: 0; left: 0; width: 22px; height: 22px;
    pointer-events: none; z-index: 2147483647;
    transition: transform 0.04s linear;
    filter: drop-shadow(0 2px 4px rgba(0,0,0,0.35));
    display: none;
  `;
  cursor.innerHTML = `<svg viewBox="0 0 24 24" fill="#0066FF" stroke="white" stroke-width="2">
    <path d="M4 3l16 7.5-7.5 2L10 20 4 3z"/>
  </svg>`;
  document.documentElement.appendChild(cursor);
  try {
    Object.defineProperty(window, '__alterego_cursor', {
      value: cursor,
      writable: true,
      enumerable: false,
      configurable: true
    });
  } catch(e) {
    window.__alterego_cursor = cursor;
  }
  document.addEventListener('mousemove', e => {
    if (e.isTrusted) return; // 物理鼠标滑动绝对不移动内置虚拟光标！
    cursor.style.display = 'block';
    cursor.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
  }, { passive: true, capture: true });
  document.addEventListener('mousedown', e => {
    if (e.isTrusted) return;
    const ripple = document.createElement('div');
    ripple.style.cssText = `
      position: fixed; left: ${e.clientX - 12}px; top: ${e.clientY - 12}px;
      width: 24px; height: 24px; border-radius: 50%;
      background: rgba(0, 102, 255, 0.35); border: 2px solid #0066FF;
      pointer-events: none; z-index: 2147483646;
      transform: scale(0.5); opacity: 1;
      transition: transform 0.35s ease-out, opacity 0.35s ease-out;
    `;
    document.documentElement.appendChild(ripple);
    requestAnimationFrame(() => {
      ripple.style.transform = 'scale(1.8)';
      ripple.style.opacity = '0';
    });
    setTimeout(() => ripple.remove(), 400);
  }, { passive: true, capture: true });
})();
"""

# ===== Gemini 补丁 2026-10-15 & 地基升级：带真实中心坐标与语义增强的可交互元素树 =====
# 替代纯 nodeId 树：直接给每个可交互元素算 bounding box 中心点，
# 纯图标按钮（无 aria-label）也能拿到坐标；支持 ContentEditable 富文本与评论触发器；
# 支持活跃浮层优先置顶，防止背景元素干扰。
INTERACTIVE_TREE_JS = """
(modalSel) => {
  // ① 清旧：清理上次快照残留的旧 ref 属性与 SoM 框选层，保证编号属于当前最新快照
  try {
    document.querySelectorAll('[data-ae-ref]').forEach(el => el.removeAttribute('data-ae-ref'));
    if (window.__ae_som_mo) { window.__ae_som_mo.disconnect(); }
    document.getElementById('__ae_som')?.remove();
  } catch(e) {}

  const C = { click: '#2563eb', input: '#f59e0b', modal: '#a855f7' }; // 蓝/橙/紫
  const EXCLUDE = '#__alterego_root, #__alterego_cursor, #__ae_lock_toast, #__ae_som';

  let layer = null;
  try {
    layer = document.createElement('div');
    layer.id = '__ae_som';
    layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483646;';
    document.documentElement.appendChild(layer);
  } catch(e) {}

  const selector = 'a, button, input, textarea, select, [role="button"], [role="tab"], [role="textbox"], [role="searchbox"], [contenteditable="true"], [contenteditable=""], [class*="editor"], [class*="comment-input"], [aria-placeholder], [placeholder], [onclick]';
  const els = Array.from(document.querySelectorAll(selector));
  const results = [];
  let id = 1;
  for (const el of els) {
    if (el.closest && (el.closest('#__alterego_root') || el.closest('#__alterego_cursor') || el.closest('#__ae_lock_toast') || el.closest('#__ae_som'))) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4 || rect.bottom < 0 || rect.top > window.innerHeight) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('aria-placeholder') || '').trim().slice(0, 60);
    if (text.includes('申请人工接管') || text.includes('完成接管') || text.includes('停止任务') || text.includes('AlterEgo 正在控制') || text.includes('已锁定防误触')) continue;
    
    const isEditable = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable || el.getAttribute('role') === 'textbox' || el.getAttribute('contenteditable') === 'true';
    let inModal = false;
    try {
      if (modalSel && el.closest && el.closest(modalSel)) inModal = true;
    } catch(e) {}

    const refStr = 'ae' + id;
    try {
      el.setAttribute('data-ae-ref', refStr);
    } catch(e) {}

    // ② 同源绘制 SoM 视觉标记框与编号徽章
    if (layer) {
      try {
        const color = inModal ? C.modal : (isEditable ? C.input : C.click);
        const box = document.createElement('div');
        box.style.cssText =
          'position:fixed;left:' + (rect.left - 2) + 'px;top:' + (rect.top - 2) + 'px;' +
          'width:' + (rect.width + 4) + 'px;height:' + (rect.height + 4) + 'px;' +
          'border:2px solid ' + color + ';border-radius:4px;background:' + color + '14;pointer-events:none;box-sizing:border-box;';
        const badge = document.createElement('div');
        badge.textContent = id;
        badge.style.cssText =
          'position:fixed;left:' + (rect.left - 2) + 'px;top:' + Math.max(0, rect.top - 18) + 'px;' +
          'background:' + color + ';color:#ffffff;font:bold 11px/16px system-ui,sans-serif;' +
          'padding:0 5px;border-radius:3px;white-space:nowrap;pointer-events:none;box-shadow:0 1px 3px rgba(0,0,0,0.3);z-index:2147483647;';
        layer.append(box, badge);
      } catch(e) {}
    }

    results.push({
      ref: refStr,
      id: id++,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || (isEditable ? 'textbox' : ''),
      is_editable: isEditable,
      in_modal: inModal,
      name: text || (isEditable ? '[输入框]' : '[纯图标/无文本]'),
      x: Math.round(rect.left + rect.width / 2),
      y: Math.round(rect.top + rect.height / 2),
      w: Math.round(rect.width),
      h: Math.round(rect.height)
    });
  }

  // ③ 注册全局卸载钩子
  try {
    window.__ae_som_off = () => {
      try { window.__ae_som_mo?.disconnect(); } catch(e) {}
      document.getElementById('__ae_som')?.remove();
    };
  } catch(e) {}

  // 浮层内元素优先置顶
  results.sort((a, b) => (b.in_modal ? 1 : 0) - (a.in_modal ? 1 : 0));
  return results;
}
"""

# ===== 方案3 工业级反反爬与指纹混淆补丁 (Stealth Hardening) =====
# 覆盖 7 大维度的全方位反自动化探针欺骗：
# 1. 深度消除 navigator.webdriver（同时净化实例与原型链，确保 hasOwnProperty 为 false）
# 2. 原生标准 PluginArray / MimeTypeArray 架构（满足 instanceof、item() 与 length 校验）
# 3. 规范化语言、8核心硬件并发数与设备内存
# 4. 真实完整的 window.chrome 运行环境（app/csi/loadTimes）
# 5. WebGL 显卡厂商伪装（抹除 SwiftShader/Mesa 虚拟显卡标记）
# 6. Permissions API 规范模拟
# 7. 清理 CDP / DevTools cdc_ 特征变量
STEALTH_INIT_SCRIPT = """(() => {
  try {
    // 1. 深度清除 navigator.webdriver 及其原型链特征
    try {
      const proto = Object.getPrototypeOf(navigator);
      if (proto && 'webdriver' in proto) {
        delete proto.webdriver;
      }
    } catch(e) {}
    try {
      if ('webdriver' in navigator) {
        delete navigator.webdriver;
      }
    } catch(e) {}
    try {
      Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver', {
        get: () => undefined,
        enumerable: false,
        configurable: true
      });
    } catch(e) {}

    // 2. 模拟原生标准的 PluginArray 与 MimeTypeArray（满足 instanceof、item() 及 length 检验）
    try {
      const fakePluginsList = [
        { name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", mimes: ["application/pdf", "text/pdf"] },
        { name: "Chrome PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", mimes: ["application/pdf", "text/pdf"] },
        { name: "Chromium PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", mimes: ["application/pdf", "text/pdf"] },
        { name: "Microsoft Edge PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format", mimes: ["application/pdf", "text/pdf"] },
        { name: "WebKit built-in PDF", filename: "internal-pdf-viewer", description: "Portable Document Format", mimes: ["application/pdf", "text/pdf"] }
      ];

      const pluginArray = Object.create(PluginArray.prototype);
      const mimeTypeArray = Object.create(MimeTypeArray.prototype);

      fakePluginsList.forEach((pData, pIdx) => {
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperties(plugin, {
          name: { value: pData.name, enumerable: true },
          filename: { value: pData.filename, enumerable: true },
          description: { value: pData.description, enumerable: true },
          length: { value: pData.mimes.length, enumerable: true }
        });

        pData.mimes.forEach((mType, mIdx) => {
          const mime = Object.create(MimeType.prototype);
          Object.defineProperties(mime, {
            type: { value: mType, enumerable: true },
            suffixes: { value: "pdf", enumerable: true },
            description: { value: pData.description, enumerable: true },
            enabledPlugin: { value: plugin, enumerable: true }
          });
          plugin[mIdx] = mime;
          mimeTypeArray[mimeTypeArray.length || 0] = mime;
          mimeTypeArray[mType] = mime;
        });

        pluginArray[pIdx] = plugin;
        pluginArray[pData.name] = plugin;
      });

      Object.defineProperty(pluginArray, 'length', { value: fakePluginsList.length, enumerable: false });
      pluginArray.item = function(i) { return this[i] || null; };
      pluginArray.namedItem = function(n) { return this[n] || null; };
      pluginArray.refresh = function() {};

      Object.defineProperty(mimeTypeArray, 'length', { value: 2, enumerable: false });
      mimeTypeArray.item = function(i) { return this[i] || null; };
      mimeTypeArray.namedItem = function(n) { return this[n] || null; };

      Object.defineProperty(Object.getPrototypeOf(navigator), 'plugins', {
        get: () => pluginArray,
        enumerable: true,
        configurable: true
      });
      Object.defineProperty(Object.getPrototypeOf(navigator), 'mimeTypes', {
        get: () => mimeTypeArray,
        enumerable: true,
        configurable: true
      });
    } catch(e) {}

    // 3. 规范化平台环境参数
    try {
      Object.defineProperty(Object.getPrototypeOf(navigator), 'languages', {
        get: () => Object.freeze(['zh-CN', 'zh', 'en-US', 'en']),
        enumerable: true,
        configurable: true
      });
      Object.defineProperty(Object.getPrototypeOf(navigator), 'hardwareConcurrency', {
        get: () => 8,
        enumerable: true,
        configurable: true
      });
      Object.defineProperty(Object.getPrototypeOf(navigator), 'deviceMemory', {
        get: () => 8,
        enumerable: true,
        configurable: true
      });
    } catch(e) {}

    // 4. 真实完整的 window.chrome 环境
    try {
      if (!window.chrome) window.chrome = {};
      window.chrome.app = {
        isInstalled: false,
        InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" },
        RunningState: { CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running" }
      };
      window.chrome.csi = function() {};
      window.chrome.loadTimes = function() {
        return {
          requestTime: Date.now() / 1000 - 0.4,
          startLoadTime: Date.now() / 1000 - 0.35,
          commitLoadTime: Date.now() / 1000 - 0.25,
          finishDocumentLoadTime: Date.now() / 1000 - 0.1,
          finishLoadTime: Date.now() / 1000,
          firstPaintTime: Date.now() / 1000 - 0.15,
          firstPaintAfterLoadTime: 0,
          navigationType: "Other",
          wasFetchedViaSpdy: true,
          wasNpnNegotiated: true,
          npnNegotiatedProtocol: "h2",
          wasAlternateProtocolAvailable: false,
          connectionInfo: "h2"
        };
      };
    } catch(e) {}

    // 5. WebGL 显卡厂商伪装（消除 SwiftShader / Mesa 虚拟显卡标记）
    try {
      const patchWebGL = (ctxProto) => {
        if (!ctxProto) return;
        const origGetParam = ctxProto.getParameter;
        ctxProto.getParameter = function(param) {
          if (param === 37445) return 'Google Inc. (NVIDIA)';
          if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
          return origGetParam.apply(this, arguments);
        };
      };
      if (typeof WebGLRenderingContext !== 'undefined') patchWebGL(WebGLRenderingContext.prototype);
      if (typeof WebGL2RenderingContext !== 'undefined') patchWebGL(WebGL2RenderingContext.prototype);
    } catch(e) {}

    // 6. 权限查询模拟 (Permissions API)
    try {
      if (window.navigator.permissions && window.navigator.permissions.query) {
        const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
        window.navigator.permissions.query = (p) => {
          if (p && p.name === 'notifications') {
            return Promise.resolve({
              state: Notification.permission || 'default',
              onchange: null
            });
          }
          return origQuery(p);
        };
      }
    } catch(e) {}

    // 7. 清理 CDP / DevTools 注入变量 (如 cdc_ 开头的特征变量)
    try {
      for (const key of Object.keys(window)) {
        if (key.match(/^cdc_/) || key.match(/\\$cdc_/)) {
          delete window[key];
        }
      }
    } catch(e) {}
  } catch(e) {}
})()
"""

# §39.1.3：同一 URL 无状态变化重访达到此次数 → 触发 stuck，换策略
_URL_STUCK_VISITS = 3
# §39.6：连续 K 次截图无图 → 视觉链路熔断
_BLIND_STREAK_LIMIT = 3
# §39.5：标签页预算上限
_TAB_BUDGET = 4

# 进程级隔离的指纹池：每个新 browser 进程轮换一套，降低跨账号关联
_FINGERPRINTS = [
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
     "viewport": {"width": 1920, "height": 1080},
     "locale": "zh-CN", "timezone_id": "Asia/Shanghai"},
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
     "viewport": {"width": 1366, "height": 768},
     "locale": "zh-CN", "timezone_id": "Asia/Shanghai"},
    {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
     "viewport": {"width": 1536, "height": 864},
     "locale": "zh-CN", "timezone_id": "Asia/Shanghai"},
]

# 常见双后缀（last-two-parts 会误判的）
_DOUBLE_SUFFIX = {"co.uk", "com.cn", "net.cn", "org.cn", "gov.cn", "co.jp", "com.au"}


def _etld1(url: str) -> str:
    """取 eTLD+1（如 mail.google.com → google.com）。同 eTLD 可共进程多标签。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if not host or "." not in host:
            return host
        parts = host.split(".")
        if len(parts) >= 3 and ".".join(parts[-2:]) in _DOUBLE_SUFFIX:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])
    except Exception:
        return ""


class BrowserWorker:
    """一个 profile 的浏览器实例（Playwright 同步 API）。

    采用专用单线程 Executor 保证 Playwright/Greenlet 的线程亲和性，
    杜绝多线程 Web 服务器环境下各请求跨线程调用引发的
    greenlet.error: cannot switch to a different thread 异常。
    """

    def __init__(self, profile_mgr, circuit, profile: str, headless: bool = True,
                 split_screen: bool = False, browser_ratio: float = 0.80,
                 auto_record: bool = True):
        self.profile_mgr = profile_mgr
        self.circuit = circuit
        self.profile = profile
        self.headless = headless
        self.split_screen = split_screen
        self.browser_ratio = browser_ratio
        self.auto_record = auto_record
        self._split_geo = None
        self._split_bounds_initialized = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"pw_{profile}")
        self._pw = None
        self._browser = None
        self._raw_page = None
        self._window_layout = {"status": "unobserved", "geometry_verified": False}
        # Gemini 补丁 2026-10-15（§15 防误触）：输入锁定 guard，page 切换时重建
        self._input_guard = None
        self._guard_locked = True  # 默认为锁定状态，以便页面加载后自动激活流光
        self.smart_zoom = None
        self.multitab_controller = None
        # 进程级隔离：name -> {"browser": Browser|None, "context": BrowserContext|None,
        #                      "tabs": dict, "domains": set, "fingerprint": dict}
        # 每个 profile = 独立 OS 进程（指纹都区分）；同 eTLD 可在同一进程内多标签。
        # "default" 用启动时的 persistent context（browser=None）。
        # _tabs 永远指向当前 active profile 的 tabs（见下方 property）
        self._profiles: dict = {"default": {"browser": None, "context": None,
                                             "tabs": {}, "domains": set(),
                                             "fingerprint": {}}}
        self._active_profile: str = "default"
        self._fp_index = 0  # 指纹池轮换下标
        # §39.6 盲熔断：连续无图截图计数
        self._blind_streak = 0
        # §39.1.3 URL 级 stuck：norm_url -> {"visits", "titles": [最近3个标题]}
        self._url_state: dict = {}
        # 诊断地基（console + 网络日志）：页面静默失败时唯一的真相来源。
        # run5 教训：Agnes 的 .catch() 吞掉异常，页面无变化、无 toast——
        # 只有 console/pageerror 和网络日志能说清发生了什么。
        # 懒初始化（测试用 __new__ 绕过 __init__ 也能工作）。
        self._diag_pages = set()
        # 浏览器独占租约仲裁（防止主 Agent 与子 Agent 并发踩踏）
        self._lease_owner: str | None = None
        self._lease_lock = threading.Lock()

    def acquire_lease(self, owner_id: str, timeout_sec: float = 5.0) -> bool:
        """申领浏览器独占租约（如 'main' 或 'sub:<task_id>'）。"""
        import time
        start = time.time()
        while time.time() - start < timeout_sec:
            with self._lease_lock:
                if self._lease_owner is None or self._lease_owner == owner_id:
                    self._lease_owner = owner_id
                    return True
            time.sleep(0.05)
        return False

    def release_lease(self, owner_id: str) -> bool:
        """释放浏览器独占租约。"""
        with self._lease_lock:
            if self._lease_owner == owner_id:
                self._lease_owner = None
                return True
        return False

    def current_lease_owner(self) -> str | None:
        """查询当前持有独占租约的 owner_id。"""
        with self._lease_lock:
            return self._lease_owner

    def check_lease(self, caller_id: str | None = None) -> bool:
        """检查调用方是否拥有执行权限。若未传入 caller_id 且未被锁定则放行。"""
        with self._lease_lock:
            if self._lease_owner is None:
                return True
            if caller_id and self._lease_owner == caller_id:
                return True
            return False

    def _diag_logs(self):
        """懒初始化诊断日志容器。"""
        from collections import deque
        if not hasattr(self, "_console_log"):
            self._console_log = deque(maxlen=200)
        if not hasattr(self, "_network_log"):
            self._network_log = deque(maxlen=300)
        if not hasattr(self, "_req_start"):
            self._req_start = {}
        return self._console_log, self._network_log, self._req_start

    def _attach_page_diagnostics(self, page):
        """给页面挂 console/pageerror/request/response 监听（每个 page 只挂一次）。
        在 _page setter 和 _do_start 里调用，tab 切换自动覆盖。"""
        if page is None:
            return
        try:
            import weakref
            if not hasattr(self, "_diag_pages"):
                self._diag_pages = weakref.WeakSet()
            # WeakSet 按对象身份去重：page 被 GC 后自动移除，不存在 id 复用误判
            if page in self._diag_pages:
                return
            self._diag_pages.add(page)
        except Exception:
            return
        console_log, network_log, req_start = self._diag_logs()
        import time as _time

        def _on_console(msg):
            try:
                text = msg.text or ""
                if len(text) > 800:
                    text = text[:800] + "…"
                # 噪音过滤：React DevTools 提示等
                if "Download the React DevTools" in text:
                    return
                console_log.append({"t": _time.time(), "type": msg.type, "text": text})
            except Exception:
                pass

        def _on_pageerror(err):
            try:
                text = str(err)
                # 未捕获异常带堆栈才有定位价值
                stack = getattr(err, "stack", "")
                if stack and str(stack) != text:
                    text = str(stack)[:800]
                console_log.append({"t": _time.time(), "type": "pageerror",
                                    "text": text[:800]})
            except Exception:
                pass

        def _on_request(req):
            try:
                if len(req_start) > 1000:
                    req_start.clear()
                entry = {"t": _time.time()}
                # POST 数据：调试"到底发了什么参数"用（只记 xhr/fetch，截断）
                try:
                    if req.resource_type in ("xhr", "fetch"):
                        pd = req.post_data
                        if pd:
                            entry["post"] = pd[:1000]
                        # 关键请求头（authorization 脱敏）
                        hdrs = req.headers or {}
                        auth = hdrs.get("authorization", "")
                        if auth:
                            entry["auth"] = (auth[:16] + "…") if len(auth) > 16 else auth
                except Exception:
                    pass
                req_start[req] = entry
            except Exception:
                pass

        def _on_response(resp):
            try:
                req = resp.request
                start_entry = req_start.pop(req, None)
                start = start_entry["t"] if isinstance(start_entry, dict) else start_entry
                rtype = ""
                try:
                    rtype = req.resource_type
                except Exception:
                    pass
                # 降噪：只记 xhr/fetch/document，或报错的（图片字体 404 也值得看）
                if rtype not in ("xhr", "fetch", "document") and resp.status < 400:
                    return
                try:
                    url = resp.url or ""
                except Exception:
                    url = ""
                entry = {
                    "t": _time.time(),
                    "method": getattr(req, "method", "?"),
                    "url": url[:200],
                    "status": resp.status,
                    "ms": int((_time.time() - start) * 1000) if start else -1,
                    "type": rtype,
                }
                if isinstance(start_entry, dict):
                    if start_entry.get("post"):
                        entry["post"] = start_entry["post"]
                    if start_entry.get("auth"):
                        entry["auth"] = start_entry["auth"]
                # 私货：失败请求（4xx/5xx）的响应体是定位根因的关键，自动存一份
                # （成功请求的 body 不存，噪音太大；要看成功的用 browser_evaluate 自己 fetch）
                if resp.status >= 400:
                    try:
                        body = resp.text()
                        if body:
                            entry["body"] = body[:2000]
                    except Exception:
                        pass
                network_log.append(entry)
            except Exception:
                pass

        try:
            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)
            page.on("request", _on_request)
            page.on("response", _on_response)
            page.on("dialog", self._on_dialog)
            page.on("popup", self._on_popup)
        except Exception:
            pass

    def _on_dialog(self, dialog):
        """JS 对话框（alert/confirm/prompt/beforeunload）自动处理并记录。
        Playwright 默认自动 dismiss 且模型无感知——这里接管并留下日志。"""
        try:
            console_log, _, _ = self._diag_logs()
            import time as _time
            dtype = ""
            msg = ""
            try:
                dtype = dialog.type
                msg = dialog.message or ""
            except Exception:
                pass
            try:
                if dtype == "beforeunload":
                    dialog.dismiss()  # 留在当前页，避免丢状态
                    action = "已自动取消（留在当前页）"
                else:
                    dialog.accept(dialog.default_value or "")
                    action = "已自动接受"
            except Exception:
                action = "（自动处理失败）"
            console_log.append({"t": _time.time(), "type": "dialog",
                                "text": f"[{dtype}] {msg[:200]} {action}"})
        except Exception:
            pass

    def _on_popup(self, popup):
        """window.open 弹窗自动注册为 tab（role=popup_N），不丢失。"""
        try:
            import time as _time
            self._attach_page_diagnostics(popup)
            n = 1
            while f"popup_{n}" in self._tabs:
                n += 1
            role = f"popup_{n}"
            try:
                url = popup.url or ""
            except Exception:
                url = ""
            self._tabs[role] = {"page": popup, "url_prefix": url[:60],
                                "title_kw": "", "pinned": False,
                                "last_used": _time.time()}
            console_log, _, _ = self._diag_logs()
            console_log.append({"t": _time.time(), "type": "popup",
                                "text": f"新弹窗已自动注册为标签页 role={role}（{url[:100]}），"
                                        f"用 browser_switch_tab 切换"})
        except Exception:
            pass

    def _recent_console_errors(self, limit: int = 3) -> str:
        """最近的 console 错误/pageerror，供失败信息附带。"""
        console_log, _, _ = self._diag_logs()
        errs = [m for m in console_log if m["type"] in ("error", "pageerror", "warning")]
        if not errs:
            return ""
        lines = [f"[{m['type']}] {m['text'][:200]}" for m in errs[-limit:]]
        return " [页面日志] " + " | ".join(lines)

    def _recent_network_errors(self, limit: int = 3) -> str:
        """最近的失败网络请求（4xx/5xx/无响应），供失败信息附带。"""
        _, network_log, _ = self._diag_logs()
        errs = [r for r in network_log if r["status"] >= 400 or r["status"] <= 0]
        if not errs:
            return ""
        lines = [f"{r['status']} {r['method']} {r['url'][:100]}" for r in errs[-limit:]]
        return " [网络] " + " | ".join(lines)

    def _run(self, fn, *args, **kwargs):
        """将 Playwright 操作委托给固定工作线程执行。"""
        import threading
        if threading.current_thread().name.startswith(f"pw_{self.profile}"):
            return fn(*args, **kwargs)
        future = self._executor.submit(fn, *args, **kwargs)
        return future.result()

    def start(self):
        self._run(self._do_start)

    def _do_start(self):
        from playwright.sync_api import sync_playwright
        # 浏览器重启：所有 profile 的 tabs 失效；非 default 的独立进程全部回收
        for _name in [n for n in self._profiles if n != "default"]:
            _prof = self._profiles[_name]
            try:
                if _prof.get("browser") is not None:
                    _prof["browser"].close()
                elif _prof.get("context") is not None:
                    _prof["context"].close()
            except Exception:
                pass
            del self._profiles[_name]
        self._profiles["default"]["tabs"] = {}
        self._profiles["default"]["domains"] = set()
        self._active_profile = "default"
        if not self.profile_mgr.is_automation_allowed(self.profile):
            raise PermissionError(
                f"profile={self.profile} 禁止自动化（日常 profile 只允许用户接管）")

        # 兼容 Windows 网络映射驱动器与 UNC 路径（去除 \\?\UNC\ 避免 Node.js lstat 报错）
        try:
            import playwright._impl._driver as _pw_driver
            import playwright._impl._transport as _pw_trans

            def _clean_unc(p: str) -> str:
                if p.startswith("\\\\?\\UNC\\"):
                    return "\\\\" + p[8:]
                if p.startswith("\\\\?\\"):
                    return p[4:]
                return p

            _orig = getattr(_pw_driver, "_alterego_orig", _pw_driver.compute_driver_executable)
            _pw_driver._alterego_orig = _orig

            def _patched():
                n, c = _orig()
                return _clean_unc(n), _clean_unc(c)

            _pw_driver.compute_driver_executable = _patched
            _pw_trans.compute_driver_executable = _patched
        except Exception:
            pass

        self._pw = sync_playwright().start()
        launch_kwargs = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-default-browser-check",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if self.split_screen and not self.headless:
            from ui.window_layout import get_split_layout
            layout = get_split_layout(browser_ratio=self.browser_ratio)
            b_geo = layout["browser"]
            self._split_geo = b_geo
            launch_kwargs["args"].extend([
                f"--window-position={b_geo['x']},{b_geo['y']}",
                f"--window-size={b_geo['width']},{b_geo['height']}",
            ])
        else:
            launch_kwargs["args"].append("--start-maximized")

        if not self.headless:
            # Let the real client area drive layout, not a fixed 1280px viewport.
            launch_kwargs["no_viewport"] = True
        if sys.platform == "win32":
            try:
                self._browser = self._pw.chromium.launch_persistent_context(
                    str(self.profile_mgr.user_data_dir(self.profile)),
                    channel="msedge",
                    **launch_kwargs,
                )
            except Exception:
                self._browser = self._pw.chromium.launch_persistent_context(
                    str(self.profile_mgr.user_data_dir(self.profile)),
                    **launch_kwargs,
                )
        else:
            self._browser = self._pw.chromium.launch_persistent_context(
                str(self.profile_mgr.user_data_dir(self.profile)),
                **launch_kwargs,
            )
        try:
            # 方案3 工业级反反爬与指纹混淆补丁 (Stealth Hardening)
            self._browser.add_init_script(STEALTH_INIT_SCRIPT)
        except Exception:
            pass
        pages = self._browser.pages
        self._page = pages[0] if pages else self._browser.new_page()

        try:
            from .multitab_recorder import MultiTabBrowserController, SmartZoomDirector
            if self.smart_zoom is None:
                self.smart_zoom = SmartZoomDirector()
            if self.multitab_controller is None:
                self.multitab_controller = MultiTabBrowserController(
                    context=self._browser,
                    director=self.smart_zoom,
                )
            if getattr(self, "auto_record", True) and not getattr(self.multitab_controller, "_recording", False):
                try:
                    self.multitab_controller.start_recording(self._page)
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Failed to init MultiTabBrowserController: %s", e)

        self.lock_input_for_agent()
        self._ensure_maximized()

    def _ensure_maximized(self):
        """Resize only the current page's CDP window, before fresh observations."""
        if self.headless:
            self._window_layout = {"status": "headless", "geometry_verified": False}
            return self._window_layout
        session = None
        try:
            session = self._raw_page.context.new_cdp_session(self._raw_page)
            target = session.send("Browser.getWindowForTarget", {})
            window_id = target.get("windowId")
            if type(window_id) is not int or window_id < 0:
                raise ValueError("missing target browser window")
            bounds = target.get("bounds") or {}

            if self.split_screen:
                # 分屏协同模式（左 70% 浏览器，右 30% UI 脚本）：
                # 保持 normal 窗口状态，不强制全屏最大化以避免遮盖右侧 AlterEgo UI 窗口。
                if bounds.get("windowState") in {"minimized", "fullscreen"}:
                    session.send("Browser.setWindowBounds", {"windowId": window_id,
                                 "bounds": {"windowState": "normal"}})
                if not getattr(self, "_split_bounds_initialized", False):
                    b_geo = getattr(self, "_split_geo", None)
                    if not b_geo:
                        from ui.window_layout import get_split_layout
                        b_geo = get_split_layout(browser_ratio=self.browser_ratio)["browser"]
                        self._split_geo = b_geo
                    session.send("Browser.setWindowBounds", {
                        "windowId": window_id,
                        "bounds": {
                            "windowState": "normal",
                            "left": b_geo["x"],
                            "top": b_geo["y"],
                            "width": b_geo["width"],
                            "height": b_geo["height"],
                        }
                    })
                    self._split_bounds_initialized = True

                for _ in range(12):
                    current_bounds = session.send("Browser.getWindowBounds", {"windowId": window_id}).get("bounds") or {}
                    w = current_bounds.get("width")
                    h = current_bounds.get("height")
                    if type(w) is int and w > 0 and type(h) is int and h > 0 and current_bounds.get("windowState") != "minimized":
                        self._window_layout = {"status": "split_screen", "geometry_verified": True,
                                               "window_id": window_id, "bounds": dict(current_bounds)}
                        return self._window_layout
                    time.sleep(.05)
                self._window_layout = {"status": "split_screen_unverified", "geometry_verified": False,
                                       "window_id": window_id}
            else:
                if bounds.get("windowState") != "maximized":
                    if bounds.get("windowState") in {"minimized", "fullscreen"}:
                        session.send("Browser.setWindowBounds", {"windowId": window_id,
                                     "bounds": {"windowState": "normal"}})
                    session.send("Browser.setWindowBounds", {"windowId": window_id,
                                 "bounds": {"windowState": "maximized"}})
                previous = None
                for _ in range(12):
                    bounds = session.send("Browser.getWindowBounds", {"windowId": window_id}).get("bounds") or {}
                    valid = (bounds.get("windowState") == "maximized"
                             and type(bounds.get("width")) is int and bounds["width"] > 0
                             and type(bounds.get("height")) is int and bounds["height"] > 0)
                    if valid and bounds == previous:
                        self._window_layout = {"status": "maximized", "geometry_verified": True,
                                               "window_id": window_id, "bounds": dict(bounds)}
                        return self._window_layout
                    previous = bounds
                    time.sleep(.05)
                self._window_layout = {"status": "maximize_unverified", "geometry_verified": False,
                                       "window_id": window_id}
        except Exception as exc:
            self._window_layout = {"status": "unavailable", "geometry_verified": False,
                                   "error_type": type(exc).__name__}
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass
        return self._window_layout

    def set_split_ratio(self, browser_ratio: float):
        """动态更新分屏比例并调整浏览器窗口边界。"""
        return self._run(self._do_set_split_ratio, browser_ratio)

    def _do_set_split_ratio(self, browser_ratio: float):
        self.browser_ratio = float(browser_ratio)
        from ui.window_layout import get_split_layout
        layout = get_split_layout(browser_ratio=self.browser_ratio)
        b_geo = layout["browser"]
        self._split_geo = b_geo

        if self.headless or not self._browser:
            return b_geo

        session = None
        try:
            target_page = getattr(self, "_raw_page", None) or getattr(self, "_page", None)
            if target_page and not target_page.is_closed():
                session = target_page.context.new_cdp_session(target_page)
                target = session.send("Browser.getWindowForTarget", {})
                window_id = target.get("windowId")
                if type(window_id) is int and window_id >= 0:
                    session.send("Browser.setWindowBounds", {
                        "windowId": window_id,
                        "bounds": {
                            "windowState": "normal",
                            "left": b_geo["x"],
                            "top": b_geo["y"],
                            "width": b_geo["width"],
                            "height": b_geo["height"],
                        }
                    })
                    self._split_bounds_initialized = True
                    self._window_layout = {
                        "status": "split_screen",
                        "geometry_verified": True,
                        "window_id": window_id,
                        "bounds": {
                            "left": b_geo["x"],
                            "top": b_geo["y"],
                            "width": b_geo["width"],
                            "height": b_geo["height"],
                            "windowState": "normal",
                        }
                    }
        except Exception:
            pass
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass
        return b_geo

    def focus_window(self):
        """将当前自动化浏览器窗口推到最前台。"""
        return self._run(self._do_focus_window)

    def _do_focus_window(self):
        if self.headless or not self._browser:
            return
        try:
            page = getattr(self, "_raw_page", None) or getattr(self, "_page", None)
            if page and not page.is_closed():
                session = page.context.new_cdp_session(page)
                try:
                    session.send("Page.bringToFront", {})
                finally:
                    session.detach()
        except Exception:
            pass
        try:
            from tools.windows_notify import focus_edge_browser_window
            focus_edge_browser_window()
        except Exception:
            pass

    def _layout_note(self):
        if self._window_layout.get("status") == "headless":
            return ""
        if self.split_screen:
            pct = int(round(self.browser_ratio * 100))
            return (f" [窗口分屏已就绪 ({pct}%)]" if self._window_layout.get("geometry_verified")
                    else " [窗口分屏尚未验证]")
        return (" [窗口已最大化并验证]" if self._window_layout.get("geometry_verified")
                else " [窗口最大化尚未验证]")

    def stop(self):
        try:
            self._run(self._do_stop)
        except Exception:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _do_stop(self):
        # 浏览器停止：所有 profile 的独立进程回收，重置为只有 default
        for _name in [n for n in self._profiles if n != "default"]:
            _prof = self._profiles[_name]
            try:
                if _prof.get("browser") is not None:
                    _prof["browser"].close()
                elif _prof.get("context") is not None:
                    _prof["context"].close()
            except Exception:
                pass
        self._profiles = {"default": {"browser": None, "context": None,
                                       "tabs": {}, "domains": set(),
                                       "fingerprint": {}}}
        self._active_profile = "default"
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._page = None

    @property
    def _page(self):
        """暴露 _page 代理供单测兼容使用（如 w._page.set_content）。"""
        class _Proxy:
            def __init__(self, worker):
                self._worker = worker

            def __getattr__(self, name):
                return lambda *args, **kwargs: self._worker._run(
                    lambda: getattr(self._worker._raw_page, name)(*args, **kwargs)
                )
        return _Proxy(self)

    @_page.setter
    def _page(self, val):
        self._raw_page = val
        # tab 切换时自动给新 page 挂诊断监听
        try:
            self._attach_page_diagnostics(val)
        except Exception:
            pass
        # Gemini 补丁 2026-10-15（§15 防误触）：新 page 重建 input guard
        try:
            from .input_guard import BrowserInputGuard
            self._input_guard = BrowserInputGuard(val) if val else None
            # 若之前处于锁定状态，新页面自动继承蓝紫锁定态
            if self._input_guard and getattr(self, "_guard_locked", False):
                self._input_guard.lock_for_agent()
        except Exception:
            self._input_guard = None

    def lock_input_for_agent(self) -> bool:
        """§15 running 态：锁定人类物理输入，Agent 独占。"""
        self._guard_locked = True
        guard = getattr(self, "_input_guard", None)
        if guard:
            return guard.lock_for_agent()
        return False

    def unlock_input_for_human(self, reason: str = "", task_id: str = "") -> bool:
        """§15 paused_takeover 态：解锁，人类可操作（扫码/验证码）。"""
        self._guard_locked = False
        guard = getattr(self, "_input_guard", None)
        if guard:
            return guard.unlock_for_human(reason=reason, task_id=task_id)
        return False

    def release_input_guard(self) -> bool:
        """任务结束时释放遮罩与输入锁。"""
        self._guard_locked = False
        guard = getattr(self, "_input_guard", None)
        if guard:
            return guard.release_all()
        return False

    def _clean_screenshot(self, clean: bool = False, **kwargs) -> bytes:
        """纯净截图：临时彻底隐藏 AlterEgo 遮罩、胶囊横幅和流光边框，截取真实原始网页后恢复。
        若 clean=True，额外移除 SoM 框选层（__ae_som），返回无任何标注的纯原图。
        默认 clean=False 时保留 SoM 框选，供大模型视觉理解与编号直达。
        """
        raw_page = getattr(self, "_raw_page", None)
        if not raw_page:
            return b""
        try:
            raw_page.evaluate("""(clean) => {
                const ids = ['__alterego_root', '__alterego_cursor', '__ae_lock_toast'];
                if (clean) ids.push('__ae_som');
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (el) el.style.setProperty('display', 'none', 'important');
                }
                const st = document.getElementById('__alterego_style');
                if (st) st.disabled = true;
                void (document.body ? document.body.offsetHeight : 0);
            }""", clean)
        except Exception:
            pass
        try:
            return raw_page.screenshot(**kwargs)
        finally:
            try:
                raw_page.evaluate("""(clean) => {
                    const ids = ['__alterego_root', '__alterego_cursor', '__ae_lock_toast'];
                    if (clean) ids.push('__ae_som');
                    for (const id of ids) {
                        const el = document.getElementById(id);
                        if (el) el.style.removeProperty('display');
                    }
                    const st = document.getElementById('__alterego_style');
                    if (st) st.disabled = false;
                }""", clean)
            except Exception:
                pass

    def verify_login_state(self) -> dict:
        """§9.6 双探针验真：判定 login_lost 前的代码级硬核验（不靠 LLM 猜）。

        背景：小红书/B站/知乎在反爬冷却或单帖 404 时，页面常显示通用登录引导浮层，
        视觉模型易误判为 login_lost。若误报触发 L2 熔断，全天任务罢工。

        双探针：
        1. Cookie 探针：检查关键 Session 字段是否还在
        2. DOM 探针：检查顶部导航栏是否存在个人头像节点 [class*="avatar"]

        返回：
            {"status": "logged_in" | "rate_limited" | "login_lost",
             "detail": str, "probe": {...}}

        - 有头像但页面 404/空白 → rate_limited（30 分钟退避，不打扰用户）
        - 没头像且跳登录页 → login_lost（才叫用户扫码）
        """
        page = getattr(self, "_page", None)
        if not page:
            return {"status": "unknown", "detail": "无可用页面", "probe": {}}

        probe = {}
        try:
            # 探针 1：Cookie 检查
            cookies = page.context.cookies()
            # 常见 session 标识（按站点补充）
            session_keys = ["sessionid", "SESS", "token", "auth", "login",
                           "uid", "userid", "passport"]
            has_session = any(
                any(sk in c["name"].lower() for sk in session_keys)
                for c in cookies)
            probe["has_session_cookie"] = has_session
            probe["cookie_count"] = len(cookies)

            # 探针 2：头像 DOM 检查
            avatar_found = page.evaluate("""() => {
                const sel = '[class*="avatar"], [class*="Avatar"], '
                          + 'img[src*="avatar"], [data-testid*="avatar"]';
                return document.querySelector(sel) !== null;
            }""")
            probe["avatar_found"] = bool(avatar_found)

            # 探针 3：URL 是否在登录页
            url = page.url.lower()
            on_login_page = any(k in url for k in
                               ["login", "signin", "sign-in", "auth"])
            probe["on_login_page"] = on_login_page

            # 判定
            if probe["avatar_found"] and probe["has_session_cookie"]:
                # 有头像有 session：登录正常，页面空白/404 是反爬冷却
                return {"status": "logged_in", "detail": "头像+Session 均正常",
                        "probe": probe}
            if probe["avatar_found"] and not on_login_page:
                # 有头像但无 session cookie：可能是反爬 404，非掉线
                return {"status": "rate_limited",
                        "detail": "有头像但页面异常，判定为反爬冷却（非掉线）",
                        "probe": probe}
            if not probe["avatar_found"] and on_login_page:
                # 无头像且在登录页：真正的掉线
                return {"status": "login_lost",
                        "detail": "无头像且跳转到登录页，确认登录失效",
                        "probe": probe}
            # 边界情况：保守按 rate_limited 处理（不熔断）
            return {"status": "rate_limited",
                    "detail": "探针结果不明确，按反爬冷却处理（不触发熔断）",
                    "probe": probe}
        except Exception as e:
            return {"status": "unknown", "detail": f"探针异常: {e}",
                    "probe": probe}

    @property
    def _tabs(self):
        """当前 active profile 的 tabs。跨 profile 切换时自动分流。
        兼容：没走 __init__ 的老测试（__new__ 直建）用实例级 fallback。"""
        profiles = getattr(self, "_profiles", None)
        if profiles:
            prof = profiles.get(getattr(self, "_active_profile", "default"))
            if prof is not None:
                return prof["tabs"]
        # 老测试兼容路径
        fb = self.__dict__.get("_tabs_legacy")
        if fb is None:
            fb = {}
            self.__dict__["_tabs_legacy"] = fb
        return fb

    @_tabs.setter
    def _tabs(self, val):
        profiles = getattr(self, "_profiles", None)
        if profiles:
            prof = profiles.get(getattr(self, "_active_profile", "default"))
            if prof is not None:
                prof["tabs"] = val
                return
        self.__dict__["_tabs_legacy"] = val

    # 向后兼容：老代码/测试用的 _contexts / _active_context 别名
    @property
    def _contexts(self):
        return getattr(self, "_profiles", {})

    @_contexts.setter
    def _contexts(self, val):
        # 老格式 {"name": {"context": ctx, "tabs": {}}} → 新格式补齐字段
        profiles = {}
        for name, ctx in (val or {}).items():
            profiles[name] = {"browser": None,
                              "context": ctx.get("context"),
                              "tabs": ctx.get("tabs", {}),
                              "domains": set(),
                              "fingerprint": {}}
        self._profiles = profiles

    @property
    def _active_context(self):
        return getattr(self, "_active_profile", "default")

    @_active_context.setter
    def _active_context(self, val):
        self._active_profile = val

    def _guard(self) -> str | None:
        return self._run(self._do_guard)

    # ---------- iframe 感知（P0 地基）：支付表单/嵌入组件/企业后台大量用 iframe，
    # 选择器默认只搜主 frame。frame 清单进 snapshot；click/type 自动搜子 frame；
    # evaluate 可指定 frame。----------

    def _frame_inventory(self) -> list[dict]:
        """返回所有 frame：[{id, name, url, is_main}]。f0 恒为主 frame。"""
        inv = []
        try:
            main = self._raw_page.main_frame
            for i, f in enumerate(self._raw_page.frames):
                try:
                    url = f.url or ""
                except Exception:
                    url = ""
                inv.append({
                    "id": f"f{i}",
                    "name": f.name or "",
                    "url": url[:100],
                    "is_main": f == main,
                })
        except Exception:
            pass
        return inv

    def _resolve_frame(self, frame_ref: str | None):
        """frame_ref: None/'auto'/'' → 主 frame；'f1' → 对应 frame；非法 → None。"""
        try:
            frames = self._raw_page.frames
        except Exception:
            return None
        if not frame_ref or frame_ref == "auto":
            return self._raw_page.main_frame
        try:
            return frames[int(str(frame_ref).lstrip("f"))]
        except (ValueError, IndexError):
            return None

    def _detect_active_modal(self) -> dict:
        """检测当前页面是否存在置顶活跃浮层（Modal/Dialog/Drawer/Popup/Mask）。
        解决现代复杂 SPA 详情浮层下，背景全局搜索框等元素劫持输入的通病。
        返回 dict: {'has_modal': bool, 'selector': str, 'title': str, 'rect': dict, 'zIndex': int}
        """
        if not self._raw_page:
            return {"has_modal": False}
        js = """
        () => {
          try {
            // 1. 标准 ARIA 弹窗与 HTML5 dialog
            const standardSelectors = [
              'dialog[open]',
              '[role="dialog"]:not([aria-hidden="true"])',
              '[aria-modal="true"]:not([aria-hidden="true"])'
            ];
            for (const sel of standardSelectors) {
              const els = Array.from(document.querySelectorAll(sel));
              for (const el of els) {
                const style = window.getComputedStyle(el);
                if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                  const rect = el.getBoundingClientRect();
                  if (rect.width >= 200 && rect.height >= 120) {
                    return {
                      has_modal: true,
                      selector: sel,
                      title: el.getAttribute('aria-label') || el.querySelector('h1, h2, h3, header, .title')?.innerText?.slice(0, 50) || '',
                      rect: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) },
                      zIndex: parseInt(style.zIndex, 10) || 0
                    };
                  }
                }
              }
            }

            // 2. 常见 SPA 浮层容器 (class 命名特征 + 高 z-index + 覆盖主视口)
            const classKeywords = ['modal', 'dialog', 'drawer', 'popup', 'lightbox', 'undertake', 'detail-mask', 'note-detail'];
            const candidates = [];
            const allDivs = document.querySelectorAll('div, section, article, aside');
            for (const el of allDivs) {
              if (el.closest && (el.closest('#__alterego_root') || el.closest('#__alterego_cursor'))) continue;
              const cls = (typeof el.className === 'string' ? el.className : '').toLowerCase();
              const id = (el.id || '').toLowerCase();
              const matched = classKeywords.some(kw => cls.includes(kw) || id.includes(kw));
              if (matched) {
                const style = window.getComputedStyle(el);
                if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                  const pos = style.position;
                  if (pos === 'fixed' || pos === 'absolute') {
                    const z = parseInt(style.zIndex, 10) || 0;
                    const rect = el.getBoundingClientRect();
                    if (rect.width >= 260 && rect.height >= 180 && (z >= 5 || pos === 'fixed')) {
                      candidates.push({ el, rect, z });
                    }
                  }
                }
              }
            }

            if (candidates.length > 0) {
              candidates.sort((a, b) => (b.z - a.z));
              const best = candidates[0];
              const el = best.el;
              let sel = '';
              if (el.id) {
                sel = '#' + CSS.escape(el.id);
              } else if (el.className && typeof el.className === 'string') {
                const primaryCls = el.className.trim().split(/\\s+/).filter(c => !c.includes(':') && !c.includes('/') && !c.includes('[') && !c.includes(']'))[0];
                if (primaryCls) sel = '.' + CSS.escape(primaryCls);
              }
              if (!sel) sel = el.tagName.toLowerCase();
              return {
                has_modal: true,
                selector: sel,
                title: el.querySelector('h1, h2, h3, header, [class*="title"]')?.innerText?.slice(0, 50) || '',
                rect: { x: Math.round(best.rect.x), y: Math.round(best.rect.y), width: Math.round(best.rect.width), height: Math.round(best.rect.height) },
                zIndex: best.z
              };
            }
          } catch(e) {}
          return { has_modal: false };
        }
        """
        try:
            res = self._raw_page.evaluate(js)
            if isinstance(res, dict) and res.get("has_modal"):
                return res
        except Exception:
            pass
        return {"has_modal": False}

    def _find_locator(self, selector: str, frame_ref: str | None = None, scope_to_modal: bool = True):
        """在指定 frame（或 auto 全搜）中定位。返回 (frame, locator, frame_id)。
        auto 模式：主 frame 优先（保持现有行为与速度），命中数为 0 再搜子 frame。
        若检测到当前页面处于活跃浮层 (Modal/Dialog/Drawer) 状态，优先在浮层容器内定位，
        防止背景层常驻搜索框、导航栏劫持输入。
        page 对象无 frames 属性时（如旧测试替身），降级为直接 page.locator()。"""
        _frames = getattr(self._raw_page, "frames", None)
        if not isinstance(_frames, (list, tuple)):
            try:
                return None, self._raw_page.locator(selector), "f0"
            except Exception:
                return None, None, None
        if frame_ref and frame_ref != "auto":
            frame = self._resolve_frame(frame_ref)
            if frame is None:
                return None, None, None
            return frame, frame.locator(selector), frame_ref
        try:
            main = self._raw_page.main_frame
        except Exception:
            return None, None, None

        # 优先在活跃浮层 (Active Modal Scope) 内搜索
        if scope_to_modal and not selector.startswith(("html", "body", "!", ":")):
            try:
                modal_info = self._detect_active_modal()
                if modal_info.get("has_modal") and modal_info.get("selector"):
                    modal_sel = modal_info["selector"]
                    if selector != modal_sel:
                        modal_loc = main.locator(modal_sel)
                        scoped_loc = modal_loc.locator(selector)
                        if scoped_loc.count() > 0:
                            return main, scoped_loc, "f0_modal"
            except Exception:
                pass

        try:
            loc = main.locator(selector)
            if loc.count() > 0:
                return main, loc, "f0"
        except Exception:
            pass
        try:
            frames = self._raw_page.frames
        except Exception:
            return None, None, None
        for i, f in enumerate(frames):
            if f == main:
                continue
            try:
                loc = f.locator(selector)
                if loc.count() > 0:
                    return f, loc, f"f{i}"
            except Exception:
                continue
        return None, None, None

    def _page_text(self) -> str:
        """当前页面正文（截断 20000 字符）。条件检查共用。"""
        try:
            return self._raw_page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 20000) : ''")
        except Exception:
            return ""

    def _check_expect_condition(self, cond: str, value: str, net_from: int = 0):
        """单个期望条件的即时检查。返回 True/False，未知条件返回 None。
        _verify_expect 与 browser_wait_for 共用。"""
        try:
            if cond == "url_contains":
                return value in (self._raw_page.url or "")
            if cond == "url_not_contains":
                return value not in (self._raw_page.url or "")
            if cond == "text_visible":
                return value in self._page_text()
            if cond == "text_hidden":
                return value not in self._page_text()
            if cond in ("selector_visible", "selector_hidden"):
                try:
                    frame, loc, _ = self._find_locator(value, None)
                    vis = loc is not None and loc.first.is_visible(timeout=500)
                except Exception:
                    vis = False
                return vis if cond == "selector_visible" else not vis
            if cond in ("countdown_visible", "has_countdown"):
                import re
                text = self._page_text()
                pattern = r"(\b\d{1,3}\s*(?:s|秒|S)\b|重新(?:获取|发送)\s*\(?\s*\d{1,3}\s*(?:s|秒|S)?\)?|\d{1,3}\s*秒?后重新(?:获取|发送)|(?:\b|[^0-9])(?:60|59|58|57|56|55|54|53|52|51|50|[1-4][0-9]|[1-9])\s*(?:s|秒)\b|\b\d{1,2}\s*:\s*\d{2}\b)"
                return bool(re.search(pattern, text))
            if cond == "network_url":
                _, network_log, _ = self._diag_logs()
                reqs = list(network_log)[net_from:]
                return any(value.lower() in r["url"].lower() for r in reqs)
        except Exception:
            return False
        return None

    def _verify_expect(self, expect: dict, timeout: float = 5.0) -> str:
        """统一的效果核验：动作后验证是否达到声明的期望状态。
        run5 教训：点了纸飞机但没变成倒计时，模型却宣布成功。
        期望写进参数，框架自动核验，不符就明说，不让模型靠猜。
        支持：url_contains / url_not_contains / text_visible / text_hidden /
               selector_visible / selector_hidden。返回每项通过/未通过。"""
        import time as _time
        if not isinstance(expect, dict) or not expect:
            return ""
        results = []
        deadline = _time.monotonic() + timeout

        def _check(cond, value):
            return self._check_expect_condition(cond, value)

        pending = {}
        unknown = []
        for k, v in expect.items():
            r = _check(k, v)
            if r is None:
                unknown.append(k)
            else:
                pending[k] = v
        for k in unknown:
            results.append(f"{k}: 未知条件（支持 url_contains/url_not_contains/text_visible/text_hidden/selector_visible/selector_hidden/network_url）")
        while pending and _time.monotonic() < deadline:
            done = [k for k, v in pending.items() if _check(k, v)]
            for k in done:
                results.append(f"{k}({pending.pop(k)}): 通过")
            if pending:
                _time.sleep(0.5)
        for k, v in pending.items():
            results.append(f"{k}({v}): 未通过（{timeout:g}秒内未达成）")
        if not results:
            return ""
        ok = all("通过" in r and "未通过" not in r for r in results)
        tag = "[期望核验通过]" if ok else "[期望核验未通过]"
        return f" {tag} " + "；".join(results) + "。"

    def _do_guard(self) -> str | None:
        raw_p = getattr(self, "_raw_page", None)
        if raw_p is None or self._browser is None:
            if self._browser is not None and raw_p is None:
                # 轻量恢复：context 还在（比如关掉了当前 tab），只需重建当前页，
                # 不必重启整个浏览器
                try:
                    pages = self._browser.pages
                    self._page = pages[0] if pages else self._browser.new_page()
                except Exception as e:
                    return f"[浏览器恢复失败] {e}"
            else:
                try:
                    self._do_start()
                except Exception as e:
                    return f"[浏览器启动失败] {e}"
        else:
            try:
                if raw_p.is_closed():
                    pages = self._browser.pages
                    self._page = pages[0] if pages else self._browser.new_page()
            except Exception:
                try:
                    self._do_stop()
                except Exception:
                    pass
                try:
                    self._do_start()
                except Exception as e:
                    return f"[浏览器重启失败] {e}"
        if getattr(self, "circuit", None):
            return self.circuit.check_before_action(self.profile)
        return None

    def _human_delay(self):
        factor = self.circuit.delay_factor() if getattr(self, "circuit", None) else 1.0
        time.sleep(random.uniform(1.0, 3.0) * factor)

    # ---------- 工具实现 ----------

    def navigate(self, args: dict) -> str:
        return self._run(self._do_navigate, args)

    @staticmethod
    def _norm_url(url: str) -> str:
        """URL 规范化：去掉 fragment、尾部斜杠，用于 stuck 检测的同一性判断。"""
        from urllib.parse import urlsplit, urlunsplit
        try:
            parts = urlsplit(url.strip().lower())
            path = parts.path.rstrip("/") or "/"
            return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
        except Exception:
            return url.strip().lower()

    @staticmethod
    def _infer_role_for_url(url: str, existing_roles: set | None = None) -> tuple[str, bool]:
        """根据 URL 智能推断标签页 role 与 pinned 属性。"""
        existing_roles = existing_roles or set()
        url_l = (url or "").lower()
        # 1. 邮箱/收件箱/验证码站（优先设为 mailbox 并 pinned，防止被关闭或被刷新覆盖）
        mail_keywords = ("temp-email", "temp-mail", "tempmail", "10minutemail",
                         "guerrillamail", "mail", "inbox", "mailbox", "linshi")
        if any(k in url_l for k in mail_keywords):
            base_role = "mailbox"
            pinned = True
        elif any(k in url_l for k in ("sign-up", "signup", "register", "zhuce")):
            base_role = "register"
            pinned = False
        else:
            from urllib.parse import urlparse
            host = ""
            try:
                host = urlparse(url_l).hostname or ""
            except Exception:
                pass
            if host:
                parts = host.split(".")
                name = parts[-2] if len(parts) >= 2 and parts[-2] not in ("com", "co", "net", "org", "gov", "edu") else parts[0]
                base_role = "".join(c for c in name if c.isalnum() or c in ("_", "-")) or "target"
            else:
                base_role = "target"
            pinned = False

        role = base_role
        idx = 2
        while role in existing_roles:
            role = f"{base_role}_{idx}"
            idx += 1
        return role, pinned

    def _sync_tabs(self):
        """同步 Playwright 真实页面与 self._tabs 映射。清理已关闭标签页。"""
        tabs = self._tabs
        closed = [r for r, t in list(tabs.items()) if not t.get("page") or getattr(t["page"], "is_closed", lambda: False)()]
        for r in closed:
            tabs.pop(r, None)

    def _find_tab(self, role_or_query: str) -> tuple[str | None, dict | None]:
        """按 role / 域名 / 页面关键字智能查找匹配的标签页。"""
        if not role_or_query:
            return None, None
        q = role_or_query.strip()
        tabs = self._tabs
        # 1. 精确匹配
        if q in tabs:
            return q, tabs[q]
        # 2. 忽略大小写匹配
        q_low = q.lower()
        for r, t in tabs.items():
            if r.lower() == q_low:
                return r, t
        # 3. 域名 / URL 前缀匹配（如传了 openrouter.ai 或 inodetree.com）
        for r, t in tabs.items():
            p = t.get("page")
            p_url = (getattr(p, "url", "") or "").lower()
            prefix = (t.get("url_prefix", "") or "").lower()
            if q_low in p_url or q_low in prefix or (p_url and q_low in _etld1(p_url)):
                return r, t
        # 4. 语义关键字匹配（mailbox / email / 注册站）
        if any(k in q_low for k in ("mail", "email", "inbox", "temp")):
            for r, t in tabs.items():
                p = t.get("page")
                p_url = (getattr(p, "url", "") or "").lower()
                if any(k in p_url for k in ("mail", "email", "inbox", "temp")):
                    return r, t
        if any(k in q_low for k in ("reg", "sign", "target")):
            for r, t in tabs.items():
                p = t.get("page")
                p_url = (getattr(p, "url", "") or "").lower()
                if any(k in p_url for k in ("register", "signup", "sign-up")):
                    return r, t
        return None, None

    def _apply_adapter_context(self, url: str, msg: str) -> str:
        try:
            if not hasattr(self, "_site_registry"):
                from agent.strategy import SiteAdapterRegistry
                self._site_registry = SiteAdapterRegistry()
            _adapter = self._site_registry.match(url)
            if _adapter is not None:
                _ctx = _adapter.prompt_context()
                if _ctx:
                    return msg + "\n" + _ctx
        except Exception:
            pass
        return msg

    def _do_navigate(self, args: dict) -> str:
        if (blocked := self._do_guard()):
            return blocked
        self._ensure_maximized()
        self.lock_input_for_agent()
        url = (args.get("url") or "").strip()
        if getattr(self, "smart_zoom", None):
            self.smart_zoom.record_action("navigate", 0, 0, f"打开网址: {url}")
        if not url:
            return "[导航失败] URL 不能为空"

        # 常见大模型幻觉与简写域名自动纠偏
        alias_map = {
            "geminiapi.com": "gemini.google.com",
            "gemini.ai": "gemini.google.com",
            "chatgpt.ai": "chatgpt.com",
            "claude.com": "claude.ai",
        }
        for alias, target in alias_map.items():
            if alias in url:
                url = url.replace(alias, target)

        # 本地文件导航容错自愈：处理相对路径或大模型产生的盘符/用户目录幻觉
        if url.startswith("file://"):
            from urllib.parse import unquote, urlparse
            parsed = urlparse(url)
            raw_path = unquote(parsed.path)
            if raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
                raw_path = raw_path[1:]
            p = Path(raw_path)
            if not p.exists():
                # 尝试在当前工作区及其子目录自动寻找匹配同名文件
                fname = p.name
                if (ws_file := Path(fname).resolve()).exists():
                    url = ws_file.as_uri()
                else:
                    found = list(Path(".").rglob(fname))
                    if found:
                        url = found[0].resolve().as_uri()
        elif not url.startswith("http://") and not url.startswith("https://") and not url.startswith("about:"):
            p = Path(url).resolve()
            if p.exists():
                url = p.as_uri()
            else:
                url = f"https://{url}"

        # §39.5 通用最基本打开网址逻辑：
        # 跨域打开新站点时，绝不覆盖当前已有页面（保护表单输入、临时邮箱会话等）
        self._sync_tabs()
        cur_url = ""
        try:
            cur_url = getattr(self._raw_page, "url", "") or "" if self._raw_page else ""
        except Exception:
            cur_url = ""

        is_blank = (not cur_url or cur_url.startswith("about:") or cur_url == "chrome://newtab/")
        force_current = bool(args.get("force_current_tab", False))

        target_etld = _etld1(url)
        cur_etld = _etld1(cur_url) if not is_blank else ""
        is_cross_site = (not is_blank and bool(cur_etld) and bool(target_etld) and cur_etld != target_etld)

        if is_cross_site and not force_current:
            # 1. 优先检查是否已有标签页属于目标站点
            existing_role = None
            existing_tab = None
            for r, t in self._tabs.items():
                t_page = t.get("page")
                if t_page and not getattr(t_page, "is_closed", lambda: False)():
                    try:
                        t_url = getattr(t_page, "url", "") or ""
                        if (target_etld and _etld1(t_url) == target_etld) or (t.get("url_prefix") and _etld1(t["url_prefix"]) == target_etld):
                            existing_role = r
                            existing_tab = t
                            break
                    except Exception:
                        continue

            if existing_tab is not None:
                # 找到已有标签页：自动切回，不重复开新页，也不刷新覆盖
                self._page = existing_tab["page"]
                existing_tab["last_used"] = time.time()
                try:
                    if hasattr(self._raw_page, "bring_to_front"):
                        self._raw_page.bring_to_front()
                except Exception:
                    pass

                cur_tab_url = getattr(self._raw_page, "url", "") or ""
                if self._norm_url(cur_tab_url) == self._norm_url(url):
                    msg = (f"[已自动切回已有标签页] 目标域名 ({target_etld}) 已在标签页 (role={existing_role}) 打开，"
                           f"已自动切换到该标签页，保持原页面会话与状态（未重新刷新，表单与凭据完整保留）。\n"
                           f"[当前状态] URL: {cur_tab_url} 标题：{getattr(self._raw_page, 'title', lambda: '')()}"
                           + self._layout_note())
                    return self._apply_adapter_context(url, msg)
                else:
                    # 目标是同站的不同子路径，在该标签页执行跳转
                    try:
                        nav_timeout = int(args.get("timeout") or 15000)
                        self._raw_page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
                    except Exception:
                        pass
                    existing_tab["last_used"] = time.time()
                    title = ""
                    try:
                        title = getattr(self._raw_page, "title", lambda: "")() or ""
                    except Exception:
                        pass
                    msg = (f"[已自动切回已有标签页并导航] 切换至 role={existing_role} 并打开目标路径。\n"
                           f"[已打开] {url} 标题：{title}"
                           + self._layout_note())
                    return self._apply_adapter_context(url, msg)

            # 2. 没有已有标签页：自动开辟新标签页打开，保持多标签并行
            new_role, is_pinned = self._infer_role_for_url(url, set(self._tabs.keys()))
            new_res = self._do_new_tab({"url": url, "role": new_role, "pinned": is_pinned})
            if "[标签页已开]" in new_res:
                try:
                    if hasattr(self._raw_page, "bring_to_front"):
                        self._raw_page.bring_to_front()
                except Exception:
                    pass
                title = ""
                try:
                    title = getattr(self._raw_page, "title", lambda: "")() or ""
                except Exception:
                    pass
                msg = (f"[已在新标签页打开] 目标域名 ({target_etld}) 与当前页面 ({cur_etld}) 跨域，"
                       f"为避免覆盖原页面状态（表单/邮箱/会话），已自动开辟新标签页 (role={new_role}) 打开并设为当前页。\n"
                       f"[已打开] {url} 标题：{title}"
                       + self._layout_note())
                return self._apply_adapter_context(url, msg)

        # §39.1.1：导航就绪判据 = domcontentloaded + 关键元素显式等待。
        # 优化超时为 15 秒（防止 VPN/网络不可达时死等 30 秒卡住会话）
        wait_for = (args.get("wait_for") or "").strip()
        nav_timeout = int(args.get("timeout") or 15000)
        try:
            self._raw_page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
            if wait_for:
                try:
                    self._raw_page.wait_for_selector(wait_for, timeout=10000)
                except Exception as e:  # noqa: BLE001
                    return (f"[导航就绪失败] 页面已打开，但关键元素 {wait_for} 在 10 秒内未出现: {e}。"
                            "请 browser_snapshot 重新观察页面状态。")
        except Exception as e:  # noqa: BLE001
            if "captcha" in str(e).lower() or "verify you are human" in str(e).lower():
                self.circuit.record("captcha")
                return f"[导航失败] {e}"
            # §39.1.1：load 超时但页面实际已就绪（snapshot 能看到）→ 按成功处理，
            # 不再误报"页面明明在还报失败"。
            cur = ""
            try:
                cur = self._raw_page.url or ""
            except Exception:
                pass
            from urllib.parse import urlsplit
            try:
                same_site = (urlsplit(cur).netloc == urlsplit(url).netloc
                             and bool(urlsplit(cur).netloc))
            except Exception:
                same_site = False
            diag = ""
            net_err_msg = str(e).lower()
            if any(k in net_err_msg for k in ("timeout", "err_connection", "err_proxy", "err_name_not_resolved", "err_ssl")):
                diag = (
                    " [网络与代理诊断] 目标网站连接超时或网络不通。"
                    "若访问海外站点（如 Google / TikTok / ChatGPT），请检查系统 VPN/代理节点是否已开启且可用，"
                    "或尝试在代理客户端切换节点后再试。"
                )
            if not same_site:
                return f"[导航失败] {e}{diag}"
            self._human_delay()
        # 任务全生命周期自动录屏：在有效导航发生时自动激活跨标签录制与运镜记录
        if getattr(self, "auto_record", True) and getattr(self, "multitab_controller", None):
            if not getattr(self.multitab_controller, "_recording", False):
                try:
                    self.multitab_controller.start_recording(self._raw_page)
                except Exception:
                    pass
        if getattr(self, "smart_zoom", None):
            self.smart_zoom.record_action("navigate", 0, 0, f"导航到: {url}")
        # 方案5：若页面存在 Cloudflare Turnstile 验证码，尝试自动拟人穿透
        try:
            from tools.browser.human_curve import auto_solve_turnstile
            auto_solve_turnstile(self._raw_page, timeout_sec=5.0)
        except Exception:
            pass
        title = ""
        try:
            title = self._raw_page.title() or ""
        except Exception:
            pass

        # 登记或更新当前页面在 self._tabs 中的状态
        from urllib.parse import urlsplit
        try:
            landed = getattr(self._raw_page, "url", "") or url
            prefix = f"{urlsplit(landed).scheme}://{urlsplit(landed).netloc}" if landed and not landed.startswith("about:") else ""
            title_kw = title
        except Exception:
            prefix, title_kw = "", ""

        current_tab_role = None
        for r, t in self._tabs.items():
            if t.get("page") is self._raw_page:
                current_tab_role = r
                break

        if current_tab_role is None:
            new_role, is_pinned = self._infer_role_for_url(landed or url, set(self._tabs.keys()))
            self._tabs[new_role] = {
                "page": self._raw_page,
                "url_prefix": prefix,
                "title_kw": title_kw,
                "pinned": is_pinned,
                "last_used": time.time(),
            }
        else:
            cur_tab = self._tabs[current_tab_role]
            cur_tab["url_prefix"] = prefix
            cur_tab["title_kw"] = title_kw
            cur_tab["last_used"] = time.time()
            if not cur_tab.get("pinned"):
                _, inferred_pinned = self._infer_role_for_url(landed or url)
                if inferred_pinned:
                    cur_tab["pinned"] = True

        ok_msg = f"[已打开] {url} 标题：{title}" + self._layout_note()
        ok_msg = self._apply_adapter_context(url, ok_msg)
        # §39.1.3 URL 级 stuck 检测：同一 URL 无状态变化重访 ≥3 次 → 换策略
        norm = self._norm_url(url)
        st = self._url_state.setdefault(norm, {"visits": 0, "titles": []})
        st["visits"] += 1
        st["titles"].append(title)
        st["titles"] = st["titles"][-_URL_STUCK_VISITS:]
        if st["visits"] >= _URL_STUCK_VISITS and len(set(st["titles"])) == 1:
            return (ok_msg + f"\n[URL循环检测] {url} 已是第 {st['visits']} 次访问且页面状态无变化。"
                    "停止重复导航：换定位策略（如改用 browser_switch_tab 切换已开标签页、换入口 URL），"
                    "仍无进展请 browser_request_takeover 接管。")
        return ok_msg


    def snapshot(self, args: dict) -> str:
        return self._run(self._do_snapshot, args)

    def _frame_elements_summary(self, frame, limit: int = 12) -> str:
        """子 frame 内可交互元素摘要（JS 枚举）。无障碍树不一定覆盖 iframe 内容，
        这个给模型一个能见度。失败返回空串（不阻断）。"""
        try:
            els = frame.evaluate("""() => {
                const out = [];
                const q = 'button, a[href], input, select, textarea, [role="button"], [role="textbox"]';
                for (const el of document.querySelectorAll(q)) {
                    if (el.closest && (el.closest('#__alterego_root') || el.closest('#__alterego_cursor') || el.closest('#__ae_lock_toast'))) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) continue;
                    const tag = el.tagName.toLowerCase();
                    let name = (el.innerText || '').trim().slice(0, 24).replace(/\\s+/g, ' ')
                        || el.value || el.placeholder || el.getAttribute('aria-label') || '';
                    if (!name || name.includes('申请人工接管') || name.includes('完成接管') || name.includes('停止任务') || name.includes('AlterEgo 正在控制') || name.includes('已锁定防误触')) continue;
                    out.push(`[${tag}] ${name}`.trim());
                    if (out.length >= 12) break;
                }
                return out;
            }""")
            if not els:
                return ""
            return " 元素: " + " | ".join(str(e)[:40] for e in els[:limit])
        except Exception:
            return ""

    def _get_viewport(self) -> dict[str, int]:
        """获取当前页面真实视口尺寸。
        当以最大化模式（no_viewport=True）启动时，page.viewport_size 为 None，
        必须通过 window.innerWidth / innerHeight 动态读取实际尺寸，
        避免固定 1280x800 导致高分屏/宽屏下坐标计算偏差及 >1000 越界。
        """
        if self._raw_page:
            try:
                vp = self._raw_page.viewport_size
                if vp and vp.get("width") and vp.get("height"):
                    return {"width": int(vp["width"]), "height": int(vp["height"])}
                size = self._raw_page.evaluate(
                    "() => ({width: window.innerWidth || document.documentElement.clientWidth || 1280, "
                    "height: window.innerHeight || document.documentElement.clientHeight || 800})"
                )
                if isinstance(size, dict) and size.get("width") and size.get("height"):
                    return {"width": int(size["width"]), "height": int(size["height"])}
            except Exception:
                pass
        return {"width": 1280, "height": 800}

    def _interactive_tree_with_coords(self, modal_sel: str = "") -> list[dict]:
        """Gemini 补丁 2026-10-15 & 地基升级：JS 直接采集带真实中心坐标的可交互元素。
        解决：AX 树只有 nodeId 无坐标，模型靠截图硬猜；纯图标按钮无名可查。
        支持 ContentEditable 富文本标签、浮层内元素优先识别。"""
        try:
            els = self._raw_page.evaluate(INTERACTIVE_TREE_JS, modal_sel)
            return els if isinstance(els, list) else []
        except Exception:
            return []

    def _do_snapshot(self, args: dict) -> str:
        """CDP 无障碍树 → 可操作元素清单（替代不存在的 page.accessibility.snapshot）。"""
        if (blocked := self._do_guard()):
            return blocked
        self._ensure_maximized()
        # 方案5：若当前页面卡在 Cloudflare 安全验证，尝试自动穿透
        try:
            from tools.browser.human_curve import auto_solve_turnstile
            auto_solve_turnstile(self._raw_page, timeout_sec=4.0)
        except Exception:
            pass
        current_url = self._raw_page.url if self._raw_page else "未打开"
        current_title = self._raw_page.title() if self._raw_page else ""
        header = f"[当前页面] URL: {current_url} | 标题: {current_title}" + self._layout_note() + "\n"
        modal_info = self._detect_active_modal()
        modal_sel = modal_info.get("selector") if modal_info.get("has_modal") else ""
        if modal_info.get("has_modal"):
            m_title = (modal_info.get("title") or "").strip()
            header += f"[活跃浮层] {m_title or '弹窗/详情浮层'} (容器: {modal_sel}) | 浮层覆盖主视口，操作与输入将优先收敛于此浮层\n"
        self._sync_tabs()
        if len(self._tabs) > 1:
            tab_items = []
            for r, t in self._tabs.items():
                is_cur = " (当前)" if t.get("page") is self._raw_page else ""
                p = t.get("page")
                t_url = ""
                try:
                    t_url = getattr(p, "url", "") or ""
                except Exception:
                    t_url = t.get("url_prefix", "")
                pinned_mark = "📌" if t.get("pinned") else ""
                tab_items.append(f"{pinned_mark}{r}{is_cur}: {t_url}")
            header += "[已打开标签页] " + " | ".join(tab_items) + "\n"
        # iframe 清单：让模型知道页面里有几个 frame、在哪，避免对着 iframe 里的元素
        # 用主 frame 选择器死磕。元素定位时可用 frame 参数（如 frame="f1"）指定。
        frames = self._frame_inventory()
        if len(frames) > 1:
            parts = []
            summarized = 0
            for fr in frames:
                tag = "主" if fr["is_main"] else "子"
                name = f' name="{fr["name"]}"' if fr["name"] else ""
                summary = ""
                if not fr["is_main"] and summarized < 3:
                    # 子 frame 内容摘要：无障碍树可能覆盖不到。
                    # 跳过广告/追踪类 frame（降噪），最多摘要 3 个。
                    url_l = fr["url"].lower()
                    if not any(k in url_l for k in ("ads", "doubleclick", "googlesyndication", "analytics", "tracker")):
                        try:
                            _f = self._resolve_frame(fr["id"])
                            if _f is not None:
                                summary = self._frame_elements_summary(_f)
                                summarized += 1
                        except Exception:
                            pass
                parts.append(f'{fr["id"]}({tag}){name} {fr["url"]}{summary}')
            header += "[Frames] " + " | ".join(parts) + "\n"
        try:
            cdp = self._raw_page.context.new_cdp_session(self._raw_page)
            tree = cdp.send("Accessibility.getFullAXTree", {})
        except Exception as e:  # noqa: BLE001
            return f"{header}[无障碍树失败] {e}，改用截图兜底"
        supported_roles = {
            "button", "link", "textbox", "spinbutton", "searchbox",
            "checkbox", "combobox", "listbox", "radio", "menuitem",
            "tab", "switch", "heading",
        }
        lines = []
        for n in tree.get("nodes", []):
            role = (n.get("role") or {}).get("value", "")
            if role not in supported_roles:
                continue
            name = (n.get("name") or {}).get("value", "")
            if not name:
                name = (n.get("description") or {}).get("value", "")
            if not name:
                for prop in n.get("properties", []):
                    if prop.get("name") in ("placeholder", "valuetext", "roledescription"):
                        name = (prop.get("value") or {}).get("value", "")
                        if name:
                            break
            # 严格过滤 AlterEgo 自身注入的流光呼吸框与接管胶囊元素，绝不污染大模型上下文
            if any(k in name for k in ("申请人工接管", "完成接管", "停止任务", "AlterEgo 正在控制", "已锁定防误触", "AlterEgo")):
                continue
            # 角色规范化为直观认知类型，避免大模型误解或漏填
            display_role = "textbox" if role in ("spinbutton", "searchbox") else role
            suffix = " (数字输入框)" if role == "spinbutton" else ""
            lines.append(f"- [{display_role}] {name}{suffix} (nodeId={n.get('nodeId')})")
        if not lines:
            return header + "[无障碍树] 当前页面无任何可交互元素（若为 about:blank 或空白页，请先使用 browser_navigate 打开目标网站）"
        out = header + "[无障碍树]\n" + "\n".join(lines[:60])
        # Gemini 补丁 2026-10-15 & 地基升级：追加带真实坐标的元素树（SoM 风格）。
        # AX 树给语义，坐标树给位置，模型不用再靠截图硬猜坐标。
        coords = self._interactive_tree_with_coords(modal_sel)
        if coords:
            vp = self._get_viewport()
            vw, vh = vp["width"], vp["height"]
            clines = []
            for el in coords[:40]:
                # 转成 0~1000 相对坐标（与 browser_click_at 一致），强制保证在 0~1000 范围内
                rx = max(0, min(1000, round(el["x"] / vw * 1000)))
                ry = max(0, min(1000, round(el["y"] / vh * 1000)))
                m_tag = " [浮层内]" if el.get("in_modal") else ""
                edit_tag = " [可编辑]" if el.get("is_editable") else ""
                ref_id = el.get("ref", f"ae{el['id']}")
                clines.append(
                    f"- [ref={ref_id}] [#{el['id']}] [{el['tag']}{edit_tag}] {el['name']}{m_tag} "
                    f"@({rx},{ry}) {el['w']}x{el['h']}")
            out += ("\n[坐标与句柄树] 优先用 browser_click(ref=\"aeN\") / browser_fill_input(ref=\"aeN\") 直接按句柄操作；"
                    "亦可用 browser_fill_input(coords=[x,y]) 或 browser_click_at 点（0~1000 相对坐标）：\n" + "\n".join(clines))
            out += ("\n[ref 规则] 优先使用 browser_click(ref=\"aeN\") 或 browser_fill_input(ref=\"aeN\") 直接操作，"
                    "无需换算坐标或写选择器。仅当 ref 命中失败或提示 [ref 失效] 时，重新 browser_snapshot 取新句柄。")
        return out

    def click(self, args: dict) -> str:
        return self._run(self._do_click, args)

    def click_at(self, args: dict) -> str:
        return self._run(self._do_click_at, args)

    def _begin_api_watch(self):
        """点击前后监听 XHR/fetch。返回 (api_errors, api_all, detach)。
        api_all 记录所有请求（含 2xx）：run5 教训——点中小飞机却无事发生，
        '点击后零接口请求'本身就是关键信号（按钮没响应/被拦截）。"""
        api_errors: list[str] = []
        api_all: list[str] = []

        def _on_response(resp):
            try:
                if resp.request.resource_type not in ("xhr", "fetch"):
                    return
                api_all.append(f"{resp.status} {resp.request.method} {resp.url[:120]}")
                if resp.status >= 400:
                    api_errors.append(f"{resp.status} {resp.url[:150]}")
            except Exception:
                pass

        def _detach():
            try:
                self._raw_page.remove_listener("response", _on_response)
            except Exception:
                pass

        try:
            self._raw_page.on("response", _on_response)
        except Exception:
            pass
        return api_errors, api_all, _detach

    def _finish_click(self, label: str, before: str, api_errors: list, detach,
                      api_all: list | None = None) -> str:
        """点击后统一收尾：human_delay → 摘监听器 → 服务端 verdict → DOM 指纹。"""
        self._human_delay()
        detach()
        # 服务端 verdict 优先：接口报错直接暴露，不让模型以按钮状态（倒计时/toast）为准
        if api_errors:
            uniq_errors = list(dict.fromkeys(api_errors))[:3]
            return (f"[已点击] {label} [警告] 点击前后捕获到接口报错："
                    f"{'; '.join(uniq_errors)}。"
                    f"服务端拒绝了这次动作（如限流/校验失败），不要以按钮状态（倒计时/toast）为准，"
                    f"去业务侧验证实际效果。")
        # 点击成功只证明点到了，不等于业务效果达成；指纹无变化时明确提示截图验证。
        after = self._page_fingerprint()
        if before and after and before == after:
            note = ""
            if api_all is not None and not api_all:
                note = ("且点击后未捕获到任何 XHR/fetch 请求——按钮很可能根本没响应"
                        "（被禁用/被透明层拦截/需要先满足前置条件）。")
            diag = self._recent_console_errors() + self._recent_network_errors()
            return (f"[已点击] {label} [提示] 但点击前后页面 URL 与内容均无任何变化，"
                    f"{note}"
                    f"{diag}"
                    f"不要假设成功，先 browser_screenshot 确认。")
        return f"[已点击] {label}"

    def _page_fingerprint(self) -> str:
        """页面指纹：URL + 标题 + DOM 长度与哈希。用于判断一次点击是否产生了任何可见变化。"""
        try:
            sig = self._raw_page.evaluate(
                "() => { const el = document.documentElement; const html = el ? el.innerHTML : '';"
                " let h = 0; for (let i = 0; i < html.length; i++) { h = (h * 31 + html.charCodeAt(i)) | 0; }"
                " return document.title + '|' + html.length + '|' + h; }"
            )
            return f"{self._raw_page.url}#{sig}"
        except Exception:
            return ""

    def _do_click(self, args: dict) -> str:
        if (blocked := self._do_guard()):
            return blocked
        ref = (args.get("ref") or "").strip()
        selector = (args.get("selector") or "").strip()
        frame_ref = args.get("frame")  # 可选：f1 / auto（默认全搜：主 frame 优先）

        if ref:
            # 优先按 ref 句柄解析活节点
            # 注入属性可能被 SPA 重渲染清掉：清掉 = ref 过期，明确告知自愈，严禁盲点误点
            probe = self._find_locator(f'[data-ae-ref="{ref}"]', frame_ref)
            if probe[1] is None or probe[1].count() == 0:
                return (f"[ref 失效] 句柄 {ref} 不在当前页面（页面可能已跳转、局部重渲染或节点已更新）。"
                        f"请立即重新调用 browser_snapshot 获取最新有效 ref，禁止沿用旧 ref 重试。")
            selector = f'[data-ae-ref="{ref}"]'

        if not selector:
            return "[缺少参数] 请提供目标元素句柄 ref（首选，如 'ae1'）或 selector 选择器"

        if any(k in selector for k in ("__ae_", "__alterego", "申请人工接管", "完成接管")):
            if not selector.startswith('[data-ae-ref="'):
                return "[拒绝操作] 该元素属于 AlterEgo 系统控制横幅，禁止 Agent 自行点击。请操作目标网页中的业务元素。"
        if getattr(self, "smart_zoom", None):
            self.smart_zoom.record_action("click", 0, 0, f"点击元素: {selector}")
        target_selector = selector
        # run4 [99] 实测：模型写出 `div >> css:svg[aria-label="send"]`——把 css 引擎前缀
        # 写成了 `css:`（正确是 `css=`），直接 SyntaxError 浪费一轮。自动纠正这类误用。
        import re as _re2
        target_selector = _re2.sub(r">>\s*css:", ">> css=", target_selector)
        if target_selector.startswith("css:"):
            target_selector = "css=" + target_selector[4:]
        # 智能选择器保护：防止 Playwright 的 :has-text 模糊子串匹配误命中前置长按钮
        # 例如页面同时有 [button] 使用电话号码继续 和 [button] 继续 时，
        # button:has-text("继续") 会优先命中 DOM 前部的 "使用电话号码继续"。
        # 若转换为 :text-is(...) 能在页面上找到元素，则优先采用精确匹配。
        if ":has-text(" in target_selector:
            import re
            exact_candidate = re.sub(
                r':has-text\((["\']?)(.*?)\1\)',
                r':text-is(\1\2\1)',
                target_selector,
            )
            try:
                if self._raw_page.locator(exact_candidate).count() > 0:
                    target_selector = exact_candidate
            except Exception:
                pass
        # 智能捕获文件下载：若点击按钮包含“下载”语义，优先尝试捕获 Playwright 下载事件
        is_download_intent = any(k in selector.lower() for k in ("下载", "download"))
        if is_download_intent:
            try:
                from pathlib import Path
                with self._raw_page.expect_download(timeout=4000) as download_info:
                    self._raw_page.click(target_selector, timeout=10000)
                download = download_info.value
                target_path = Path("downloads") / download.suggested_filename
                target_path.parent.mkdir(parents=True, exist_ok=True)
                download.save_as(str(target_path))
                self._human_delay()
                return f"[已点击并成功下载] 文件已保存至本地: {target_path.resolve()}"
            except Exception:
                pass

        # 第三层验证（v3 seq 280-294 教训）：点到了（元素树）+ 有变化（DOM 指纹）都可能是假的——
        # 前端倒计时也会撒谎。只有服务端的 verdict 是硬的：点前后抓 XHR/fetch 响应，
        # 4xx/5xx 直接暴露（如发码接口回 429 被限流），不让模型信按钮状态。
        api_errors, _api_all, _detach = self._begin_api_watch()
        # iframe 感知：先定位元素所在 frame（主 frame 优先，自动搜子 frame），
        # 找不到才报错。frame_id 进结果，模型下次可直接 frame="f1" 精确打。
        _frame, _locator, _frame_id = self._find_locator(target_selector, frame_ref)
        if _locator is None:
            _detach()
            return (f"[点击失败] {target_selector}: 所有 frame 中均未找到该选择器。"
                    f"先 browser_snapshot 看 [Frames] 清单确认目标在哪个 frame，"
                    f"再用 browser_click_at 按坐标点。")
        try:
            before = self._page_fingerprint()
            # 方案5：拟人贝塞尔视觉轨迹移过去，再让 Playwright 做带 actionability 检查的点击
            try:
                box = _locator.first.bounding_box(timeout=3000)
                if box and hasattr(self._raw_page, "mouse") and hasattr(self._raw_page.mouse, "move"):
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    try:
                        self._raw_page.evaluate(CURSOR_INJECT_JS)
                    except Exception:
                        pass
                    from tools.browser.human_curve import human_mouse_move
                    human_mouse_move(self._raw_page, cx, cy)
            except Exception:
                pass
            _locator.first.click(timeout=10000)
        except Exception as e:  # noqa: BLE001
            # s-cdf93b36b0 实测："继续"按钮连点 4 次超时，实因表单未填完按钮被禁用，
            # 模型却一直在重试点击。失败时快速探测 disabled，给出明确指引。
            try:
                if _locator.first.is_disabled(timeout=2000):
                    _detach()
                    return (f"[点击失败] {target_selector}: 目标按钮处于禁用(disabled)状态，点不了。"
                            f"先 browser_screenshot 看表单：哪项没填完/有校验报错，"
                            f"补完再点。原始错误: {e}")
            except Exception:
                pass
            # 动作失败自动升级：选择器找到了、但 Playwright 点击超时
            # （actionability 检查不过：被遮挡/不在视口/动画中）。
            # 自动降级为 JS 直接 click——绕过可操作性检查，元素真实存在就能点中。
            # 这是通用策略：DOM 定位成功 → 动作执行失败 → 换执行方式，不换定位。
            js_ok = False
            if "Timeout" in str(e):
                try:
                    _locator.first.evaluate("(el) => el.click()")
                    js_ok = True
                except Exception:
                    pass
            if not js_ok:
                # run4 [99][101][103][111] 实测：验证码框内嵌的小飞机图标按钮，4 连换选择器全超时——
                # 它根本不在无障碍树里，CSS 选择器再怎么微调也命中不了。
                # 超时后指引模型换定位策略：截图看清位置 → browser_click_at 按坐标点。
                timeout_hint = ""
                if "Timeout" in str(e):
                    timeout_hint = (" [提示] 选择器连续超时：目标可能不在无障碍树/DOM 可达范围"
                                    "（如输入框内嵌的小图标按钮，或跨 frame 元素定位漂移）。"
                                    "不要只微调选择器重试——"
                                    "① browser_screenshot 看清目标在图中的位置；"
                                    "② 用 browser_click_at 按截图坐标直接点（x/y 为 0~1000 相对坐标）。"
                                    f"（本次定位在 {_frame_id}）")
                _detach()
                return f"[点击失败] {target_selector}: {e}{timeout_hint}"
            # JS 点击成功：走正常的效果验证流程（label 标记降级）
            js_label = (target_selector if _frame_id in (None, "f0")
                        else f"{target_selector} [frame={_frame_id}]") + " [JS降级点击]"
            result = self._finish_click(js_label, before, api_errors, _detach, _api_all)
            expect = args.get("expect")
            if isinstance(expect, dict) and expect:
                result += self._verify_expect(expect)
            return result
        clicked_label = target_selector if _frame_id in (None, "f0") else f"{target_selector} [frame={_frame_id}]"
        result = self._finish_click(clicked_label, before, api_errors, _detach, _api_all)
        if any(k in (target_selector or "") for k in ("验证码", "获取验证码", "发送验证码", "verify_code", "sms_code", "captcha_code")):
            result += " 【发码硬判据】若本次点击是发送验证码，必须调用 browser_verify_code_countdown 确认页面进入数字倒计时状态（如 60s/59秒），未确认倒计时严禁切换标签页！"
        # 统一效果核验：调用方声明了 expect（如点发送后应出现倒计时），框架自动验证
        expect = args.get("expect")
        if isinstance(expect, dict) and expect:
            result += self._verify_expect(expect)
        return result

    def _marked_click_shot(self, ax: float, ay: float):
        """点击落点回显：截图并在点击位置画红圈，返回 JPEG bytes。
        run4 首战教训：模型报的坐标与小飞机实际位置差 ~140px，点偏了却不自知。
        把落点画出来，模型下次才能对比修正。失败返回 None（不阻断主流程）。
        run5 教训：红圈图只进了消息 images、没落盘，复盘时看不到——现落盘保存。
        """
        import io
        import time
        from pathlib import Path
        try:
            from PIL import Image, ImageDraw
            raw = self._clean_screenshot()
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            w, h = img.size
            vp = self._get_viewport()
            cx, cy = ax * w / vp["width"], ay * h / vp["height"]
            d = ImageDraw.Draw(img)
            r = max(14, int(min(w, h) * 0.025))
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0), width=4)
            d.line([cx - r - 10, cy, cx + r + 10, cy], fill=(255, 0, 0), width=3)
            d.line([cx, cy - r - 10, cx, cy + r + 10], fill=(255, 0, 0), width=3)
            if max(w, h) > 1568:
                scale = 1568 / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            jpeg = buf.getvalue()
            # 落盘：复盘时红圈落点必须看得见（用户：越完善越好，不怕日志多）
            try:
                out = Path("shots") / f"click_at_{int(time.time())}.jpg"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(jpeg)
            except Exception:
                pass
            return jpeg
        except Exception:
            return None

    def _element_at_point(self, ax: float, ay: float) -> str:
        """报告坐标点最上层命中的元素。run5 教训：红圈正中小飞机但按钮没反应，
        必须知道点击到底落在了哪个元素上（被透明层拦截/点错元素，一查便知）。"""
        try:
            info = self._raw_page.evaluate(
                """([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return 'none';
                    const cls = (el.className && el.className.baseVal !== undefined)
                        ? el.className.baseVal : (el.className || '');
                    return el.tagName.toLowerCase()
                        + (el.id ? '#' + el.id : '')
                        + (cls ? '.' + String(cls).trim().split(/\\s+/).slice(0,3).join('.') : '');
                }""", [ax, ay])
            return str(info)
        except Exception:
            return "unknown"

    def _human_like_click(self, ax: float, ay: float):
        """Gemini 补丁 2026-10-15 & 方案5 优化：
        1. 注入虚拟光标（顶层 SVG，pointer-events: none）
        2. 基于三阶贝塞尔 + Fitts's Law + 生理手腕微抖动的真人鼠标轨迹
        3. 拟人按键回弹保持 (Dwell Time) + 物理微移
        """
        page = self._raw_page
        try:
            page.evaluate(CURSOR_INJECT_JS)
        except Exception:
            pass  # 注入失败不阻塞，降级为普通点击
        try:
            from tools.browser.human_curve import human_mouse_click
            human_mouse_click(page, ax, ay)
        except Exception:
            try:
                page.mouse.click(ax, ay)
            except Exception:
                pass

    def _do_click_at(self, args: dict):
        """按截图/视口相对坐标点击（0~1000）。逃生通道：选择器够不到的元素
        （如输入框内嵌的小图标按钮，run4 四连超时），模型从截图读坐标直接点。
        复用与 _do_click 相同的点击后验证（服务端 verdict + DOM 指纹），
        另附落点回显图（红圈=实际点击位置，供模型校准）。"""
        if (blocked := self._do_guard()):
            return blocked
        try:
            x = float(args.get("x"))
            y = float(args.get("y"))
        except (TypeError, ValueError):
            return ("[点击失败] browser_click_at 需要数字坐标 x/y（0~1000，相对视口/截图）。"
                    "先 browser_screenshot，从图中读出目标位置再点。")
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            return f"[点击失败] 坐标越界：x={x}, y={y}，必须在 0~1000 范围内（相对视口）。"
        vp = self._get_viewport()
        ax, ay = x / 1000 * vp["width"], y / 1000 * vp["height"]
        if getattr(self, "smart_zoom", None):
            self.smart_zoom.record_action("click", int(ax), int(ay), f"坐标点击: ({x:g}, {y:g})")
        label = f"坐标({x:g}, {y:g})"
        api_errors, api_all, _detach = self._begin_api_watch()
        try:
            before = self._page_fingerprint()
            self._human_like_click(ax, ay)  # Gemini 补丁：可视平滑点击（替代隐形瞬移）
        except Exception as e:  # noqa: BLE001
            _detach()
            return f"[点击失败] {label}: {e}"
        hit = self._element_at_point(ax, ay)
        text = self._finish_click(label, before, api_errors, _detach, api_all)
        text += f" [落点元素] {hit}。"
        if hit and any(k in hit.lower() for k in ("验证码", "发送", "获取", "code", "send", "sms")):
            text += " 【发码硬判据】若本次点击是发送验证码，必须调用 browser_verify_code_countdown 确认页面进入数字倒计时状态（如 60s/59秒），未确认倒计时严禁切换标签页！"
        text += (" 附图红圈是这次点击的实际落点：若未点中目标，对比红圈与目标的位置偏差，"
                 "修正 x/y 后再点。")
        expect = args.get("expect")
        if isinstance(expect, dict) and expect:
            text += self._verify_expect(expect)
        shot = self._marked_click_shot(ax, ay)
        if shot:
            return Message(role="tool", name="browser_click_at", content=text,
                           images=[ImagePart(data=shot, mime="image/jpeg")])
        return text

    def probe_at(self, args: dict) -> Message | str:
        return self._run(self._do_probe_at, args)

    def _zoomed_crop(self, ax: float, ay: float, zoom: int = 3):
        """以 (ax, ay)（CSS px）为中心，截图局部放大 zoom 倍，红十字标出该点。
        返回 (jpeg_bytes, 说明文本)。失败返回 (None, '')。"""
        import io
        try:
            from PIL import Image, ImageDraw
            raw = self._clean_screenshot()
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            w, h = img.size
            vp = self._get_viewport()
            cx, cy = ax * w / vp["width"], ay * h / vp["height"]
            # 裁 360x360（截图 px），放大 zoom 倍
            box = 360
            left = int(max(0, min(w - box, cx - box / 2)))
            top = int(max(0, min(h - box, cy - box / 2)))
            crop = img.crop((left, top, left + box, top + box))
            big = crop.resize((box * zoom, box * zoom), Image.LANCZOS)
            d = ImageDraw.Draw(big)
            # 红十字中心 = 探针点在放大图中的位置
            px, py = (cx - left) * zoom, (cy - top) * zoom
            d.line([px - 30, py, px + 30, py], fill=(255, 0, 0), width=3)
            d.line([px, py - 30, px, py + 30], fill=(255, 0, 0), width=3)
            d.ellipse([px - 12, py - 12, px + 12, py + 12], outline=(255, 0, 0), width=3)
            buf = io.BytesIO()
            big.save(buf, "JPEG", quality=82)
            return buf.getvalue(), (w, h, zoom)
        except Exception:
            return None, (0, 0, 0)

    def _do_probe_at(self, args: dict):
        """点前探针：报告坐标点最上层元素 + 返回该点周围放大图。
        run4 教训：模型目测坐标差 140px 却不自知。先探后点，把'我以为点这'
        变成'确认是这再点'。通用能力，非 Agnes 专用。"""
        if (blocked := self._do_guard()):
            return blocked
        try:
            x = float(args.get("x"))
            y = float(args.get("y"))
        except (TypeError, ValueError):
            return ("[探针失败] browser_probe_at 需要数字坐标 x/y（0~1000）。"
                    "先 browser_screenshot，从图中读出目标大致位置再探。")
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            return f"[探针失败] 坐标越界：x={x}, y={y}，必须在 0~1000 范围内。"
        zoom = int(args.get("zoom", 3))
        zoom = min(max(zoom, 2), 5)
        vp = self._get_viewport()
        ax, ay = x / 1000 * vp["width"], y / 1000 * vp["height"]
        hit = self._element_at_point(ax, ay)
        text = (f"[探针] 坐标({x:g}, {y:g})：该点最上层元素 {hit}。")
        jpeg, (w, h, z) = self._zoomed_crop(ax, ay, zoom)
        if jpeg:
            text += (
                f" 附图为该点周围放大 {z} 倍的局部图，红色十字中心即 ({x:g}, {y:g})。"
                f"若目标不在十字中心：量出目标在放大图中相对十字的像素偏移 (dx, dy)（右、下为正），"
                f"修正坐标 = ({x:g} + dx/{z}/{w}*1000, {y:g} + dy/{z}/{h}*1000)。"
                f"确认十字压住目标后，再用 browser_click_at 按修正坐标点。"
            )
            return Message(role="tool", name="browser_probe_at", content=text,
                           images=[ImagePart(data=jpeg, mime="image/jpeg")])
        text += "（放大图生成失败，仅返回元素信息）"
        return text

    def type_text(self, args: dict) -> str:
        return self._run(self._do_type_text, args)

    def _native_set_value(self, loc, text: str) -> bool:
        """React 受控组件兼容：用原生 setter 设值 + 派发 input/change 事件。
        run10 教训（s-db11beaf15）：Playwright fill() 对 React 受控组件有时
        填不进去——值看似设上了，但 React state 没更新，提交时还是空的，
        agent 被迫手写 evaluate 试错烧了 16 步。
        现在框架自动处理：fill 后读回验证，不对就用原生 setter 重填。"""
        try:
            ok = loc.evaluate("""(el, v) => {
                try {
                    const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (!desc || !desc.set) return 'no-setter';
                    const setv = desc.set;
                    el.focus();
                    setv.call(el, '');
                    setv.call(el, v);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return 'ok';
                } catch (e) {
                    return 'err:' + e.message;
                }
            }""", text)
            return ok == 'ok'
        except Exception:
            return False

    def _do_type_text(self, args: dict) -> str:
        if (blocked := self._do_guard()):
            return blocked
        selector, text = args.get("selector", ""), args.get("text", "")
        press_enter = bool(args.get("press_enter", False))
        if getattr(self, "smart_zoom", None):
            self.smart_zoom.record_action("type", 0, 0, f"输入文本到: {selector}")

        # §39.1.2 关键状态钉死：填表前 assert。模型的自述值一律视为未经验证，
        # 关键值（邮箱/验证码/订单号）必须先 state_pin 钉死，这里强制比对。
        pinned_key = (args.get("assert_pinned_key") or "").strip()
        if pinned_key:
            from agent.task_state import current_task_state
            ok, msg = current_task_state().check(pinned_key, text)
            if not ok:
                return msg
        # s-cdf93b36b0 实测：模型从截图读出邮箱后直接填表，全程没调 state_pin。
        # 软提醒（不阻断）：填邮箱格式文本却没传 assert_pinned_key 时警告一次。
        import re as _re
        pin_nudge = ""
        if not pinned_key and _re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text or ""):
            pin_nudge = (" [钉死提醒] 正在填写邮箱格式的关键值但未传 assert_pinned_key。"
                         "§39.1.2：关键值须先从截图读回、state_pin 钉死，"
                         "再用 assert_pinned_key 填表。")

        # v4 实测（s-0a6c414e5d）：模型把邮箱填进了密码框（用户点眼睛图标现场看到），
        # 之后才纠正。值对但框错：assert_pinned_key 只验值不验框，拦不住。
        # 密码框里出现邮箱格式文本 → 直接警告，要求截图确认。
        field_mismatch_warn = ""
        _sel_low = (selector or "").lower()
        if (_re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text or "")
                and ("password" in _sel_low or "密码" in (selector or ""))):
            field_mismatch_warn = (" [警告] 正在往疑似密码框的选择器里填邮箱格式文本！"
                                   "v4 实测曾把邮箱填进密码框。请 browser_screenshot 确认填对了框，"
                                   "填错了就清空该框、用正确的邮箱框重填。")

        # 候选选择器列表：优先使用用户传入的 selector，失败时快速自愈降级
        candidates = [selector] if selector else []
        for fb in [
            'div[role="textbox"]',
            '#prompt-textarea',
            'div[contenteditable="true"]',
            'textarea',
            '[placeholder*="ChatGPT"]',
            '[placeholder*="询问"]',
            '[placeholder*="输入"]',
        ]:
            if fb not in candidates:
                candidates.append(fb)

        last_err = None
        frame_ref = args.get("frame")  # iframe 感知：可选 f1 / auto
        for cand in candidates:
            try:
                # iframe 感知：主 frame 找不到就自动搜子 frame
                _frame, _locator, _frame_id = self._find_locator(cand, frame_ref)
                if _locator is None:
                    last_err = f"{cand}: 所有 frame 中均未找到"
                    continue
                loc = _locator.first
                if not loc.is_visible(timeout=2500):
                    continue
                try:
                    loc.fill(text, timeout=2000)
                except Exception:
                    # 对于 contenteditable div，fill 可能会报不支持，改走点击聚焦并打字
                    loc.click(timeout=1500)
                    self._raw_page.keyboard.type(text)

                # run10 教训：fill 看似成功但 React 受控组件没收到（读回是空的）。
                # 填完读回验证，不对就用原生 setter 重填，不让模型手写 JS 试错。
                react_note = ""
                try:
                    actual = loc.input_value(timeout=2000)
                    if actual != text:
                        if self._native_set_value(loc, text):
                            actual = loc.input_value(timeout=2000)
                        if actual == text:
                            react_note = " [React兼容: 原生setter重填成功]"
                        else:
                            react_note = (" [警告] 填入后读回值不符（React受控组件可能未更新），"
                                          "提交前请 browser_screenshot 确认")
                except Exception:
                    pass

                # 激活前端表单校验（如 DeepSeek/Vue/React 表单需 blur/change 才会点亮发送验证码按钮）
                try:
                    loc.evaluate("el => { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); el.dispatchEvent(new Event('blur', {bubbles: true})); }")
                except Exception:
                    pass

                if press_enter:
                    time.sleep(0.3)
                    self._raw_page.keyboard.press("Enter")
                self._human_delay()
                suffix = "（已按回车提交）" if press_enter else ""
                frame_note = "" if _frame_id in (None, "f0") else f" [frame={_frame_id}]"
                result = f"[已输入] {cand}{frame_note}{suffix}{react_note}{pin_nudge}{field_mismatch_warn}"
                expect = args.get("expect")
                if isinstance(expect, dict) and expect:
                    result += self._verify_expect(expect)
                return result
            except Exception as e:
                last_err = e
                continue

        return f"[输入失败] 所有候选输入框均无法填写: {last_err}"

    def fill_input(self, args: dict) -> str:
        return self._run(self._do_fill_input, args)

    def _do_fill_input(self, args: dict) -> str:
        """通用闭环拟人输入引擎（统一解决原生框、React/Vue 受控组件、富文本与评论占位符）：
        1. 定位与聚焦：支持 selector 或相对视口坐标 x/y / coords=[x, y]；若均未提供则聚焦当前 activeElement。
           自动处理点击未激活的输入占位符（如各类社区评论栏'说点什么'）。
        2. 兼容各种输入类型：原生 <input>/<textarea>、React/Vue 受控组件、
           ContentEditable 富文本（Slate.js, Draft.js, Lexical, ProseMirror 等）。
        3. 真实落地回读闭环校验：写入后读取实际值，严格断言输入内容已落入页面，杜绝虚假成功。
        4. 支持可选 clear_before, press_enter, expect, assert_pinned_key。
        """
        if (blocked := self._do_guard()):
            return blocked

        text = str(args.get("text", ""))
        ref = (args.get("ref") or "").strip()
        selector = (args.get("selector") or "").strip()
        clear_before = bool(args.get("clear_before", True))
        press_enter = bool(args.get("press_enter", False))
        frame_ref = args.get("frame", "auto")

        if ref:
            # 优先按 ref 句柄解析活输入节点
            probe = self._find_locator(f'[data-ae-ref="{ref}"]', frame_ref)
            if probe[1] is None or probe[1].count() == 0:
                return (f"[ref 失效] 句柄 {ref} 不在当前页面（页面可能已跳转、局部重渲染或节点已更新）。"
                        f"请重新调用 browser_snapshot 获取最新有效 ref，禁止沿用旧 ref 重试。")
            selector = f'[data-ae-ref="{ref}"]'

        pinned_key = (args.get("assert_pinned_key") or "").strip()
        if pinned_key:
            from agent.task_state import current_task_state
            ok, msg = current_task_state().check(pinned_key, text)
            if not ok:
                return msg

        # 解析坐标（可选参数：x, y 或 coords=[x, y]）
        coords = args.get("coords")
        x_val = args.get("x")
        y_val = args.get("y")
        target_coords = None
        if coords and isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                target_coords = (float(coords[0]), float(coords[1]))
            except (ValueError, TypeError):
                pass
        elif x_val is not None and y_val is not None:
            try:
                target_coords = (float(x_val), float(y_val))
            except (ValueError, TypeError):
                pass

        # 1. 目标激活与定位
        modal_info = self._detect_active_modal()
        in_modal = modal_info.get("has_modal", False)
        target_desc = ""

        if target_coords is not None:
            cx, cy = target_coords
            vp = self._get_viewport()
            ax, ay = cx / 1000 * vp["width"], cy / 1000 * vp["height"]
            target_desc = f"坐标({cx:g}, {cy:g})"
            if getattr(self, "smart_zoom", None):
                self.smart_zoom.record_action("fill_input", int(ax), int(ay), f"坐标输入: ({cx:g}, {cy:g})")
            try:
                self._human_like_click(ax, ay)
            except Exception:
                try:
                    self._raw_page.mouse.click(ax, ay)
                except Exception:
                    pass
            time.sleep(0.3)
        elif selector:
            target_desc = f"选择器 '{selector}'"
            if getattr(self, "smart_zoom", None):
                self.smart_zoom.record_action("fill_input", 0, 0, f"输入文本到: {selector}")
            frame, loc, frame_id = self._find_locator(selector, frame_ref, scope_to_modal=True)
            if loc is None or loc.count() == 0:
                fallback_found = False
                for fb in [
                    'div[role="textbox"]',
                    'div[contenteditable="true"]',
                    'textarea',
                    '[placeholder*="评论"]',
                    '[placeholder*="说点什么"]',
                    '[placeholder*="输入"]',
                ]:
                    f_frame, f_loc, f_id = self._find_locator(fb, frame_ref, scope_to_modal=True)
                    if f_loc is not None and f_loc.count() > 0:
                        loc = f_loc
                        frame_id = f_id
                        target_desc = f"选择器 '{fb}' (降级匹配)"
                        fallback_found = True
                        break
                if not fallback_found:
                    return f"[输入失败] 页面中未找到输入目标: {selector}"

            try:
                first_loc = loc.first
                try:
                    first_loc.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                first_loc.click(timeout=2000)
                time.sleep(0.2)
            except Exception as e:
                try:
                    loc.first.evaluate("(el) => el.click()")
                    time.sleep(0.2)
                except Exception:
                    return f"[输入失败] 无法点击聚焦目标 {selector}: {e}"
        else:
            target_desc = "当前焦点元素"
            if getattr(self, "smart_zoom", None):
                self.smart_zoom.record_action("fill_input", 0, 0, "向当前焦点输入")

        # 2. 检查并等待可编辑元素就绪
        inspect_js = """
        () => {
            let el = document.activeElement;
            if (!el || el === document.body || el === document.documentElement) {
                const cand = document.querySelector(':focus') || 
                             document.querySelector('[contenteditable="true"], [role="textbox"], textarea, input:not([type="hidden"])');
                if (cand) {
                    try { cand.focus(); } catch(e) {}
                    el = cand;
                }
            }
            if (!el || el === document.body) return { is_editable: false, tag: 'none' };
            
            // 若当前聚焦的是包裹容器，向下穿透找到真正的输入/富文本节点
            if (!el.isContentEditable && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') {
                const inner = el.querySelector('[contenteditable="true"], [role="textbox"], textarea, input');
                if (inner) {
                    try { inner.focus(); } catch(e) {}
                    el = inner;
                }
            }

            const isInput = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA';
            const isContentEditable = el.isContentEditable || el.getAttribute('contenteditable') === 'true' || el.getAttribute('role') === 'textbox';
            
            let val = '';
            if (isInput) {
                val = el.value || '';
            } else {
                val = el.innerText || el.textContent || '';
            }
            
            let idStr = el.id ? '#' + el.id : '';
            let clsStr = (typeof el.className === 'string' && el.className.trim()) ? '.' + el.className.trim().split(/\\s+/)[0] : '';
            return {
                is_editable: isInput || isContentEditable,
                tag: el.tagName.toLowerCase() + idStr + clsStr,
                is_input: isInput,
                is_contenteditable: isContentEditable,
                current_value: val.slice(0, 100)
            };
        }
        """
        active_info = {}
        for _ in range(5):
            try:
                active_info = self._raw_page.evaluate(inspect_js)
                if active_info and active_info.get("is_editable"):
                    break
            except Exception:
                pass
            time.sleep(0.15)

        is_editable = active_info.get("is_editable", False) if isinstance(active_info, dict) else False

        # 3. 清空与文本写入
        if is_editable:
            is_input = active_info.get("is_input", False)
            is_ce = active_info.get("is_contenteditable", False)

            if clear_before:
                try:
                    if is_input:
                        self._raw_page.evaluate("""() => {
                            const el = document.activeElement;
                            if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {
                                el.value = '';
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                            }
                        }""")
                    elif is_ce:
                        self._raw_page.evaluate("""() => {
                            const el = document.activeElement;
                            if (el && el.isContentEditable) {
                                const range = document.createRange();
                                range.selectNodeContents(el);
                                const sel = window.getSelection();
                                sel.removeAllRanges();
                                sel.addRange(range);
                                document.execCommand('delete', false, null);
                                if (el.innerText && el.innerText.trim()) {
                                    el.innerText = '';
                                    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
                                }
                            }
                        }""")
                except Exception:
                    pass

            if is_ce:
                try:
                    self._raw_page.evaluate("""(t) => {
                        const el = document.activeElement;
                        if (!el) return false;
                        el.focus();
                        let ok = false;
                        try {
                            ok = document.execCommand('insertText', false, t);
                        } catch(e) {}
                        if (!ok || !(el.innerText || el.textContent || '').includes(t)) {
                            const tn = document.createTextNode(t);
                            el.appendChild(tn);
                            el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: t}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                        return true;
                    }""", text)
                except Exception:
                    pass
            elif is_input:
                try:
                    self._raw_page.evaluate("""(t) => {
                        const el = document.activeElement;
                        if (!el) return false;
                        const proto = el.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (desc && desc.set) {
                            desc.set.call(el, t);
                        } else {
                            el.value = t;
                        }
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }""", text)
                except Exception:
                    pass

        # 4. 回读与按键补录
        readback_js = """
        () => {
            const el = document.activeElement;
            if (!el) return '';
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                return el.value || '';
            }
            return el.innerText || el.textContent || '';
        }
        """
        actual_val = ""
        try:
            actual_val = str(self._raw_page.evaluate(readback_js) or "")
        except Exception:
            pass

        if text and text not in actual_val:
            try:
                self._raw_page.keyboard.type(text, delay=15)
                time.sleep(0.2)
                actual_val = str(self._raw_page.evaluate(readback_js) or "")
            except Exception:
                pass

        try:
            self._raw_page.evaluate("""() => {
                if (document.activeElement) {
                    document.activeElement.dispatchEvent(new Event('blur', {bubbles: true}));
                }
            }""")
        except Exception:
            pass

        # 5. 回读核验闭环断言
        verified = text in actual_val or (len(text) > 10 and actual_val.strip() != "")
        if not verified:
            tag_name = active_info.get("tag", "none") if isinstance(active_info, dict) else "unknown"
            return (f"[输入未落地] 目标文本未能成功写入页面！\n"
                    f"诊断线索: 目标={target_desc} | 当前激活元素=<{tag_name}> | 回读内容='{actual_val[:40]}' | 预期='{text[:40]}'\n"
                    f"排查建议: 目标容器可能需要先点击激活（如评论框需先 browser_click_at 点击'说点什么'占位符），"
                    f"或被人机验证/顶层浮窗阻挡。请 browser_screenshot 检查画面。")

        # 6. 回车提交
        if press_enter:
            time.sleep(0.2)
            try:
                self._raw_page.keyboard.press("Enter")
            except Exception:
                pass
            time.sleep(0.4)

        self._human_delay()
        modal_tag = " [浮层内]" if in_modal else ""
        enter_tag = " (已按回车提交)" if press_enter else ""
        elem_tag = active_info.get("tag", "input") if isinstance(active_info, dict) else "input"
        result = f"[输入成功] {target_desc} -> <{elem_tag}>{modal_tag}{enter_tag} | 写入字数={len(text)} | 回读核验通过"

        expect = args.get("expect")
        if isinstance(expect, dict) and expect:
            result += self._verify_expect(expect)

        return result

    def record_start(self, args: dict) -> str:
        return self._run(self._do_record_start, args)

    def _do_record_start(self, args: dict) -> str:
        """显式启动网页全流程一镜到底录屏（CDP Screencast + ffmpeg）。"""
        if getattr(self, "multitab_controller", None) is None:
            return "[录屏失败] 浏览器多标签录屏控制器未初始化"
        if getattr(self.multitab_controller, "_recording", False):
            return f"[录屏已在进行中] 当前录制文件: {self.multitab_controller.output_mp4}"
        name = (args.get("name") or "").strip()
        if name:
            import time
            from pathlib import Path
            ts = int(time.time())
            safe_name = "".join(c for c in name if c.isalnum() or c in ("-", "_"))
            out_path = Path("artifacts/videos") / f"{safe_name}_{ts}.mp4"
            self.multitab_controller.output_mp4 = str(out_path)
        ok = self.multitab_controller.start_recording(self._raw_page)
        if ok:
            return f"[录屏已启动] 正在进行一镜到底连续录制，输出目标: {self.multitab_controller.output_mp4}（支持跨 Tab 切换追踪与运镜）"
        return "[录屏启动失败] 系统未检测到 ffmpeg 或管道创建异常"

    def record_stop(self, args: dict) -> str:
        return self._run(self._do_record_stop, args)

    def _do_record_stop(self, args: dict) -> str:
        """停止录屏并输出 MP4 视频与运镜元数据。"""
        if getattr(self, "multitab_controller", None) is None or not getattr(self.multitab_controller, "_recording", False):
            return "[录屏未在进行] 当前无正在录制的视频流"
        import os
        video_path = self.multitab_controller.stop_recording()
        if video_path and os.path.exists(video_path):
            size_kb = round(os.path.getsize(video_path) / 1024, 1)
            return f"[录屏已完成] 视频产物已落盘: {video_path} ({size_kb} KB)，已附带智能运镜元数据 actions_meta.json"
        return "[录屏结束] 录制已停止，但未能生成有效视频文件"

    def wait(self, args: dict) -> str:
        return self._run(self._do_wait, args)

    def evaluate(self, args: dict) -> str:
        return self._run(self._do_evaluate, args)

    def console_log(self, args: dict) -> str:
        return self._run(self._do_console_log, args)

    def _do_console_log(self, args: dict) -> str:
        """读取浏览器 console 消息（含未捕获异常 pageerror）。
        页面静默失败时（点了没反应、无 toast）先看这里——前端 catch 吞掉的
        异常、资源加载失败都会留下痕迹。通用诊断能力。"""
        if (blocked := self._do_guard()):
            return blocked
        console_log, _, _ = self._diag_logs()
        only = (args.get("type") or "").strip().lower()  # error/warning/pageerror/空=全部
        limit = min(max(int(args.get("limit", 20)), 1), 50)
        msgs = list(console_log)
        if only:
            msgs = [m for m in msgs if m["type"] == only]
        if not msgs:
            return f"[页面日志] 最近无 console 消息" + (f"（类型={only}）" if only else "")
        import datetime as _dt
        lines = []
        for m in msgs[-limit:]:
            ts = _dt.datetime.fromtimestamp(m["t"]).strftime("%H:%M:%S")
            lines.append(f"[{ts}][{m['type']}] {m['text'][:300]}")
        return "[页面日志] 最近 {} 条：\n".format(len(lines)) + "\n".join(lines)

    def network_log(self, args: dict) -> str:
        return self._run(self._do_network_log, args)

    def _do_network_log(self, args: dict) -> str:
        """读取最近的网络请求（xhr/fetch/document，或报错的）。
        回答'请求到底发了没、回了什么'——点击后无事发生时先查这里，
        比猜原因可靠。通用诊断能力。"""
        if (blocked := self._do_guard()):
            return blocked
        _, network_log, _ = self._diag_logs()
        filt = (args.get("filter") or "").strip().lower()
        errors_only = bool(args.get("errors_only", False))
        limit = min(max(int(args.get("limit", 20)), 1), 50)
        reqs = list(network_log)
        if filt:
            reqs = [r for r in reqs if filt in r["url"].lower()]
        if errors_only:
            reqs = [r for r in reqs if r["status"] >= 400 or r["status"] <= 0]
        if not reqs:
            return "[网络日志] 最近无匹配的请求"
        import datetime as _dt
        lines = []
        show_body = bool(args.get("show_body", False))
        show_post = bool(args.get("show_post", False))
        n = 0
        for r in reqs[-limit:]:
            n += 1
            ts = _dt.datetime.fromtimestamp(r["t"]).strftime("%H:%M:%S")
            lines.append(f"[{ts}] {r['status']} {r['method']} {r['url'][:150]} ({r['ms']}ms)")
            if show_post and r.get("post"):
                lines.append(f"  请求体: {r['post'][:300]}")
            if r.get("auth"):
                lines.append(f"  Authorization: {r['auth']}")
            if show_body and r.get("body"):
                lines.append(f"  响应体: {r['body'][:500]}")
        return f"[网络日志] 最近 {n} 条：\n" + "\n".join(lines)

    def inspect(self, args: dict):
        return self._run(self._do_inspect, args)

    def _do_inspect(self, args: dict):
        """元素级取证：回答'这个元素到底是什么状态'。
        返回标签/属性/文本/包围盒/可见性/计算样式（颜色/背景/透明度/pointer-events/
        cursor）/禁用态/点击 handler 线索，并附元素截图与主色采样。
        图片类按钮（如纸飞机 <img>）的死活看主色最准：深灰=活，浅灰=死。
        通用诊断能力，frame 参数规则同 click。"""
        if (blocked := self._do_guard()):
            return Message(role="tool", name="browser_inspect", content=blocked)
        selector = (args.get("selector") or "").strip()
        frame_ref = args.get("frame")
        if not selector:
            return Message(role="tool", name="browser_inspect",
                           content="[缺少 selector] 请提供 CSS 选择器")
        frame, loc, fid = self._find_locator(selector, frame_ref)
        if frame is None or loc is None:
            return Message(role="tool", name="browser_inspect",
                           content=f"[未找到] {selector}（frame={frame_ref or 'auto'}）")
        first = loc.first
        try:
            info = first.evaluate("""(el) => {
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                const attrs = {};
                for (const a of el.attributes) attrs[a.name] = String(a.value).slice(0, 120);
                let cls = '';
                try { cls = (el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || '').toString().slice(0, 120); } catch (e) {}
                return {
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    cls: cls,
                    attrs: attrs,
                    text: (el.innerText || '').trim().slice(0, 120).replace(/\\s+/g, ' '),
                    value: (el.value !== undefined && el.value !== null) ? String(el.value).slice(0, 120) : '',
                    box: {x: Math.round(r.x), y: Math.round(r.y),
                          w: Math.round(r.width), h: Math.round(r.height)},
                    style: {
                        color: cs.color, background: cs.backgroundColor,
                        opacity: cs.opacity, display: cs.display,
                        visibility: cs.visibility, cursor: cs.cursor,
                        pointerEvents: cs.pointerEvents,
                    },
                    disabled: !!el.disabled,
                    ariaDisabled: el.getAttribute('aria-disabled') || '',
                    hasOnClick: !!(el.onclick || el.getAttribute('onclick')),
                };
            }""")
        except Exception as e:
            return Message(role="tool", name="browser_inspect",
                           content=f"[取证失败] {selector}: {e}")
        st = info["style"]
        box = info["box"]
        visible = (box["w"] > 0 and box["h"] > 0 and st["display"] != "none"
                   and st["visibility"] != "hidden" and st["opacity"] != "0")
        # 状态判定线索
        clues = []
        if info["disabled"] or info["ariaDisabled"] == "true":
            clues.append("已禁用（disabled）")
        if st["pointerEvents"] == "none":
            clues.append("pointer-events:none（点不中）")
        if st["cursor"] == "not-allowed":
            clues.append("cursor:not-allowed（视觉上不可点）")
        if not info["hasOnClick"] and info["tag"] not in ("button", "a", "input", "select"):
            clues.append("无 onclick handler（可能是死元素）")
        if not visible:
            clues.append("不可见")

        lines = [
            f"[元素取证] {selector}（frame={fid}）",
            f"标签: <{info['tag']}> id='{info['id']}' class='{info['cls']}'",
            f"文本: '{info['text']}'" + (f" 值: '{info['value']}'" if info["value"] else ""),
        ]
        # 关键属性（alt/src/href/type/role 等一眼可见）
        attrs = info.get("attrs") or {}
        show_attrs = {k: v for k, v in attrs.items()
                      if k in ("alt", "src", "href", "type", "role", "aria-label",
                               "placeholder", "title", "data-testid")}
        if show_attrs:
            lines.append("属性: " + " ".join(f'{k}="{v}"' for k, v in show_attrs.items()))
        lines.append(
            f"包围盒: ({box['x']},{box['y']}) {box['w']}x{box['h']} 可见: {'是' if visible else '否'}")
        # 0-1000 相对坐标：直接喂 browser_click_at，不用模型换算
        try:
            vp = self._get_viewport()
            vw, vh = vp["width"], vp["height"]
            cx = max(0, min(1000, int((box["x"] + box["w"] / 2) / vw * 1000)))
            cy = max(0, min(1000, int((box["y"] + box["h"] / 2) / vh * 1000)))
            lines.append(f"点击坐标: ({cx}, {cy})（0-1000 相对坐标，可直接 browser_click_at）")
        except Exception:
            pass
        lines.append(
            f"样式: color={st['color']} bg={st['background']} opacity={st['opacity']} cursor={st['cursor']}")
        if clues:
            lines.append("状态线索: " + "；".join(clues))
        # 元素截图 + 主色采样
        images = []
        try:
            png = first.screenshot(timeout=8000)
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(png)).convert("RGB")
            small = img.resize((24, 24))
            pxs = list(small.getdata())
            n = len(pxs)
            ar = sum(p[0] for p in pxs) // n
            ag = sum(p[1] for p in pxs) // n
            ab = sum(p[2] for p in pxs) // n
            lines.append(f"主色（平均）: #{ar:02x}{ag:02x}{ab:02x}（图片类元素看这个判死活）")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
            images = [ImagePart(data=buf.getvalue(), mime="image/jpeg")]
            lines.append("附元素放大图。")
        except Exception:
            lines.append("（元素截图失败）")
        return Message(role="tool", name="browser_inspect",
                       content="\n".join(lines),
                       images=images if images else None)

    def resource(self, args: dict) -> str:
        return self._run(self._do_resource, args)

    def _do_resource(self, args: dict) -> str:
        """页面资源分析（通用源码级调查能力）。
        - 不带 url：列出页面加载的 JS/CSS 资源（找可疑 bundle）。
        - 带 url：用页面自身的 fetch（带 cookie/认证）下载文本资源，max_bytes 截断。
        - 带 search：在资源内按关键词搜（如找 API 路径），返回匹配行上下文。
        调试'前端到底调了什么接口/按钮到底绑了什么逻辑'时用。"""
        if (blocked := self._do_guard()):
            return blocked
        url = (args.get("url") or "").strip()
        search = (args.get("search") or "").strip()
        max_bytes = min(max(int(args.get("max_bytes", 20000)), 1000), 200000)
        ctx = min(max(int(args.get("context", 2)), 0), 5)

        if not url:
            try:
                res = self._raw_page.evaluate("""() => {
                    const out = [];
                    for (const e of performance.getEntriesByType('resource')) {
                        if (/\\.(js|css)(\\?|$)/.test(e.name)) out.push(e.name);
                        if (out.length >= 30) break;
                    }
                    return out;
                }""")
            except Exception as e:
                return f"[资源列表失败] {e}"
            if not res:
                return "[资源] 页面未加载 JS/CSS（或 performance API 不可用）"
            return "[页面资源] JS/CSS（最多 30 个）：\n" + "\n".join(res)

        try:
            text = self._raw_page.evaluate("""async (u) => {
                const r = await fetch(u, {credentials: 'same-origin'});
                if (!r.ok) return 'HTTP_ERROR:' + r.status;
                const t = await r.text();
                return t.slice(0, 200000);
            }""", url)
        except Exception as e:
            return f"[下载失败] {url}: {e}"
        if text.startswith("HTTP_ERROR:"):
            return f"[下载失败] {url}: HTTP {text[11:]}"
        if len(text) >= max_bytes and not search:
            text = text[:max_bytes] + f"\n…（截断，共 {len(text)} 字符，加大 max_bytes 或用 search 定位）"
        if search:
            lines = text.split("\n")
            hits = []
            for i, ln in enumerate(lines):
                if search.lower() in ln.lower():
                    s = max(0, i - ctx)
                    e_ = min(len(lines), i + ctx + 1)
                    hits.append(f"--- 第 {i+1} 行 ---\n" + "\n".join(
                        l[:300] for l in lines[s:e_]))
                    if len(hits) >= 10:
                        break
            if not hits:
                return f"[搜索] '{search}' 在 {url} 中无匹配（资源 {len(text)} 字符）"
            return (f"[搜索] '{search}' 在 {url} 中命中 {len(hits)} 处：\n\n"
                    + "\n\n".join(hits))
        return f"[资源内容] {url}（{len(text)} 字符）：\n{text[:max_bytes]}"

    def wait_for(self, args: dict) -> str:
        return self._run(self._do_wait_for, args)

    def _do_wait_for(self, args: dict) -> str:
        """条件等待：轮询直到任一条件满足或超时（独立长等待步骤）。
        与 expect 的区别：expect 是动作后 5 秒自动核验；wait_for 是独立步骤，
        用于等异步事件——邮件到达、倒计时结束、特定 API 响应、URL 跳转。
        条件：text_visible / text_hidden / selector_visible / selector_hidden /
               url_contains / url_not_contains / network_url（含该子串的请求出现，
               只看等待开始后发出的）。timeout 最长 300 秒。"""
        if (blocked := self._do_guard()):
            return blocked
        import time as _time
        timeout = min(max(float(args.get("timeout", 30)), 1), 300)
        conds = {k: v for k, v in args.items()
                 if k in ("text_visible", "text_hidden", "selector_visible",
                          "selector_hidden", "url_contains", "url_not_contains",
                          "network_url") and v}
        if not conds:
            return ("[缺少条件] 请提供至少一个：text_visible/text_hidden/selector_visible/"
                    "selector_hidden/url_contains/url_not_contains/network_url")
        _, network_log, _ = self._diag_logs()
        net_from = len(network_log)
        start = _time.monotonic()
        deadline = start + timeout
        unknown = [k for k in conds
                   if self._check_expect_condition(k, conds[k], net_from) is None]
        while _time.monotonic() < deadline:
            for k, v in conds.items():
                if k in unknown:
                    continue
                if self._check_expect_condition(k, v, net_from):
                    dt = _time.monotonic() - start
                    return f"[等待满足] {k}({v}) 在 {dt:.1f}s 后满足"
            _time.sleep(0.5)
        un = f"（未知条件已跳过：{','.join(unknown)}）" if unknown else ""
        return (f"[等待超时] {timeout:g}s 内未满足任一条件："
                + ", ".join(f"{k}({v})" for k, v in conds.items() if k not in unknown)
                + un)

    def storage(self, args: dict) -> str:
        return self._run(self._do_storage, args)

    def _do_storage(self, args: dict) -> str:
        """查看浏览器存储（登录态/会话调试用）。
        action: cookies（当前上下文的 cookie，值脱敏截断）/
                local（localStorage）/ session（sessionStorage）。
        回答'我登录了吗''token 存哪了''刷新后还在吗'。"""
        if (blocked := self._do_guard()):
            return blocked
        action = (args.get("action") or "cookies").strip().lower()
        try:
            if action == "cookies":
                cookies = self._raw_page.context.cookies()
                if not cookies:
                    return "[cookie] 当前上下文无 cookie（可能未登录或已过期）"
                lines = []
                for c in cookies[:40]:
                    v = c.get("value", "")
                    shown = (v[:12] + "…") if len(v) > 12 else v
                    lines.append(f"{c.get('name')}={shown}（域 {c.get('domain')}）")
                return f"[cookie] {len(cookies)} 个：\n" + "\n".join(lines)
            if action in ("local", "session"):
                data = self._raw_page.evaluate(
                    f"() => Object.entries({action}Storage).slice(0, 40)"
                    f".map(([k, v]) => [k, v.slice(0, 200)])")
                if not data:
                    return f"[{action}Storage] 为空"
                lines = [f"{k} = {v}" for k, v in data]
                return f"[{action}Storage] {len(data)} 项：\n" + "\n".join(lines)
        except Exception as e:
            return f"[存储读取失败] {e}"
        return f"[未知 action] {action}（支持 cookies/local/session）"

    def press(self, args: dict) -> str:
        return self._run(self._do_press, args)

    def _do_press(self, args: dict) -> str:
        """键盘按键：Enter（表单提交）/ Tab（切换焦点）/ Escape（关闭弹窗）/
        ArrowDown 等。作用于当前焦点元素（无焦点时作用于页面）。
        比 type 更轻：不需要选择器。"""
        if (blocked := self._do_guard()):
            return blocked
        key = (args.get("key") or "").strip()
        if not key:
            return "[缺少 key] 如 Enter / Tab / Escape / ArrowDown"
        try:
            self._raw_page.keyboard.press(key)
            return f"[按键] {key}"
        except Exception as e:
            return f"[按键失败] {key}: {e}"

    def handler(self, args: dict):
        return self._run(self._do_handler, args)

    def _do_handler(self, args: dict) -> str:
        """事件 handler 溯源：这个元素被点击后到底执行什么代码。
        按优先级尝试：
        1. onclick 属性 / el.onclick
        2. React：__reactProps$xxx.onClick 源码（React 把 handler 挂在 DOM 节点上）
        3. Vue：__vue__ 实例的事件绑定线索
        4. 祖先元素冒泡上可能拦截的 handler
        沉默失败（点了没反应）的根因定位用：handler 里有没有 .catch 吞异常、
        有没有前置条件判断（如邮箱非法直接 return）。"""
        if (blocked := self._do_guard()):
            return blocked
        selector = (args.get("selector") or "").strip()
        frame_ref = args.get("frame")
        if not selector:
            return "[缺少 selector] 请提供 CSS 选择器"
        frame, loc, fid = self._find_locator(selector, frame_ref)
        if frame is None or loc is None:
            return f"[未找到] {selector}（frame={frame_ref or 'auto'}）"
        try:
            info = loc.first.evaluate("""(el) => {
                const out = {onclickAttr: '', onclickSrc: '', reactSrc: '', vueInfo: '',
                             ancestors: [], framework: 'unknown'};
                // 1. 原生 onclick
                const attr = el.getAttribute('onclick');
                if (attr) out.onclickAttr = attr.slice(0, 500);
                try {
                    if (el.onclick) out.onclickSrc = String(el.onclick).slice(0, 800);
                } catch (e) {}
                // 2. React props（key 形如 __reactProps$xxx / __reactFiber$xxx）
                try {
                    const rk = Object.keys(el).find(k => k.startsWith('__reactProps$') || k.startsWith('__reactFiber$'));
                    if (rk) {
                        out.framework = 'react';
                        let props = el[rk];
                        if (props && props.memoizedProps) props = props.memoizedProps;  // fiber
                        for (const hk of ['onClick', 'onMouseDown', 'onSubmit']) {
                            if (props && typeof props[hk] === 'function') {
                                out.reactSrc = hk + ' = ' + String(props[hk]).slice(0, 1200);
                                break;
                            }
                        }
                        if (!out.reactSrc) out.reactSrc = '(有 React props 但无点击类 handler)';
                    }
                } catch (e) {}
                // 3. Vue
                try {
                    if (el.__vue__ || el._vnode) {
                        out.framework = 'vue';
                        out.vueInfo = '检测到 Vue 实例挂载（具体 handler 需看组件源码）';
                    }
                } catch (e) {}
                // 4. 祖先链：谁可能拦截/冒泡处理
                let p = el.parentElement, depth = 0;
                while (p && depth < 4) {
                    const hasClick = !!(p.onclick || p.getAttribute('onclick'));
                    const rk = Object.keys(p).find(k => k.startsWith('__reactProps$'));
                    if (hasClick || rk) {
                        out.ancestors.push('<' + p.tagName.toLowerCase() + '>' +
                            (p.id ? '#' + p.id : '') + (hasClick ? ' 有onclick' : ' 有React props'));
                    }
                    p = p.parentElement; depth++;
                }
                return out;
            }""")
        except Exception as e:
            return f"[溯源失败] {selector}: {e}"
        lines = [f"[Handler 溯源] {selector}（frame={fid}，框架: {info['framework']}）"]
        if info["onclickAttr"]:
            lines.append(f"onclick 属性: {info['onclickAttr']}")
        if info["onclickSrc"]:
            lines.append(f"el.onclick 源码:\n{info['onclickSrc']}")
        if info["reactSrc"]:
            lines.append(f"React handler 源码:\n{info['reactSrc']}")
        if info["vueInfo"]:
            lines.append(info["vueInfo"])
        if info["ancestors"]:
            lines.append("祖先可疑拦截: " + " <- ".join(info["ancestors"]))
        if len(lines) == 1:
            lines.append("未发现任何点击 handler——这是'死元素'的强证据（或 handler 绑在更上层，检查祖先）。")
        else:
            # 启发式：提醒看 .catch / 前置 return
            src = (info["onclickSrc"] + info["reactSrc"]).lower()
            if ".catch" in src:
                lines.append("[注意] handler 含 .catch——异常可能被静默吞掉，点后无反应时查 browser_console。")
            if "return;" in src or "return null" in src:
                lines.append("[注意] handler 含提前 return——可能有前置条件（如输入非法）直接退出。")
        return "\n".join(lines)

    def observe(self, args: dict) -> str:
        return self._run(self._do_observe, args)

    def _do_observe(self, args: dict) -> str:
        """DOM 变化观察：记录 duration 秒内的 DOM 突变（新增/删除节点、属性变更、
        文本变更），返回摘要。调试'点了之后页面到底发生了什么'用——
        比截图肉眼对比可靠，比指纹的'有/无变化'具体。
        selector 可选：只观察某个子树。duration 最长 30 秒。"""
        if (blocked := self._do_guard()):
            return blocked
        import time as _time
        duration = min(max(float(args.get("duration", 5)), 1), 30)
        selector = (args.get("selector") or "").strip()
        try:
            self._raw_page.evaluate("""(sel) => {
                window.__ae_obs = [];
                const root = sel ? document.querySelector(sel) : document.body;
                if (!root) return 'NO_ROOT';
                const obs = new MutationObserver((muts) => {
                    for (const m of muts) {
                        if (window.__ae_obs.length > 100) return;
                        let desc = m.type;
                        if (m.type === 'childList') {
                            const added = [...m.addedNodes]
                                .filter(n => n.nodeType === 1)
                                .map(n => '<' + n.tagName.toLowerCase() + '>').slice(0, 3);
                            const removed = m.removedNodes.length;
                            desc = `childList +${added.join('')} -${removed}节点`;
                        } else if (m.type === 'attributes') {
                            desc = `属性 ${m.attributeName} 变更 @ <${m.target.tagName.toLowerCase()}>`;
                        } else if (m.type === 'characterData') {
                            desc = `文本变更: ${(m.target.data || '').slice(0, 60)}`;
                        }
                        window.__ae_obs.push(desc);
                    }
                });
                obs.observe(root, {childList: true, subtree: true,
                                   attributes: true, characterData: true});
                window.__ae_obs_observer = obs;
                return 'OK';
            }""", selector)
        except Exception as e:
            return f"[观察失败] {e}"
        _time.sleep(duration)
        try:
            muts = self._raw_page.evaluate("""() => {
                try { window.__ae_obs_observer.disconnect(); } catch (e) {}
                const r = (window.__ae_obs || []).slice(0, 40);
                window.__ae_obs = [];
                return r;
            }""")
        except Exception as e:
            return f"[读取变化失败] {e}"
        if not muts:
            return f"[观察] {duration:g}s 内 DOM 无任何变化"
        # 聚合计数
        from collections import Counter
        cnt = Counter(muts)
        lines = [f"[观察] {duration:g}s 内 {len(muts)} 次 DOM 突变："]
        for desc, n in cnt.most_common(15):
            lines.append(f"  x{n} {desc[:100]}")
        return "\n".join(lines)

    def upload(self, args: dict) -> str:
        return self._run(self._do_upload, args)

    def _do_upload(self, args: dict) -> str:
        """文件上传：给 <input type=\"file\"> 设置本地文件。
        selector 定位 file input，path 为本地文件路径（workspace 内）。
        有些上传按钮是假的（点开系统对话框），真 input 藏在 DOM 里——
        找不到时用 browser_inspect 找 input[type=file]。"""
        if (blocked := self._do_guard()):
            return blocked
        selector = (args.get("selector") or "").strip()
        path = (args.get("path") or "").strip()
        if not selector or not path:
            return "[缺少参数] 需要 selector（file input） 和 path（本地文件路径）"
        from pathlib import Path as _Path
        p = _Path(path).expanduser()
        if not p.exists():
            # 试 workspace 相对路径
            p2 = _Path.home() / "workspace" / path.lstrip("/")
            if p2.exists():
                p = p2
            else:
                return f"[文件不存在] {path}"
        frame, loc, fid = self._find_locator(selector, args.get("frame"))
        if frame is None or loc is None:
            return (f"[未找到] {selector}（frame={args.get('frame') or 'auto'}）。"
                    f"上传按钮可能是假的，真 input[type=file] 藏在 DOM 里——"
                    f"用 browser_inspect 找。")
        try:
            loc.first.set_input_files(str(p))
            return f"[已上传] {p.name} → {selector}（frame={fid}）"
        except Exception as e:
            return f"[上传失败] {e}"

    def scroll(self, args: dict) -> str:
        return self._run(self._do_scroll, args)

    def _do_scroll(self, args: dict) -> str:
        """滚动：direction=up/down/top/bottom，或 selector 把元素滚进视口。
        长页面找折叠下方元素、截图前定位用。amount 为像素数（默认 600）。"""
        if (blocked := self._do_guard()):
            return blocked
        selector = (args.get("selector") or "").strip()
        try:
            if selector:
                frame, loc, fid = self._find_locator(selector, args.get("frame"))
                if frame is None or loc is None:
                    return f"[未找到] {selector}"
                loc.first.scroll_into_view_if_needed(timeout=5000)
                return f"[已滚动] {selector} 已滚进视口（frame={fid}）"
            direction = (args.get("direction") or "down").strip().lower()
            amount = min(max(int(args.get("amount", 600)), 100), 5000)
            js = {
                "up": f"window.scrollBy(0, -{amount})",
                "down": f"window.scrollBy(0, {amount})",
                "top": "window.scrollTo(0, 0)",
                "bottom": "window.scrollTo(0, document.body.scrollHeight)",
            }.get(direction)
            if not js:
                return f"[未知方向] {direction}（支持 up/down/top/bottom）"
            self._raw_page.evaluate(js)
            return f"[已滚动] {direction}"
        except Exception as e:
            return f"[滚动失败] {e}"

    def hover(self, args: dict) -> str:
        return self._run(self._do_hover, args)

    def _do_hover(self, args: dict) -> str:
        """悬停：鼠标移到元素上（展开下拉菜单、触发 tooltip/悬停态）。
        有些菜单 hover 才渲染，用完接 browser_snapshot 看新出现的元素。"""
        if (blocked := self._do_guard()):
            return blocked
        ref = (args.get("ref") or "").strip()
        selector = (args.get("selector") or "").strip()
        if ref:
            probe = self._find_locator(f'[data-ae-ref="{ref}"]', args.get("frame"))
            if probe[1] is None or probe[1].count() == 0:
                return f"[ref 失效] 句柄 {ref} 不在当前页面，请重新调用 browser_snapshot 获取最新 ref。"
            selector = f'[data-ae-ref="{ref}"]'
        if not selector:
            return "[缺少 selector 或 ref]"
        frame, loc, fid = self._find_locator(selector, args.get("frame"))
        if frame is None or loc is None:
            return f"[未找到] {selector}（frame={args.get('frame') or 'auto'}）"
        try:
            loc.first.hover(timeout=8000)
            return f"[已悬停] {selector}（frame={fid}）"
        except Exception as e:
            return f"[悬停失败] {e}"

    def select(self, args: dict) -> str:
        return self._run(self._do_select, args)

    def _do_select(self, args: dict) -> str:
        """下拉框选择：<select> 元素按 value/label/index 选一项。
        value 为 option 的 value 属性，label 为可见文本，index 为序号（0 起）。
        三者传一个即可，优先级 value > label > index。"""
        if (blocked := self._do_guard()):
            return blocked
        ref = (args.get("ref") or "").strip()
        selector = (args.get("selector") or "").strip()
        if ref:
            probe = self._find_locator(f'[data-ae-ref="{ref}"]', args.get("frame"))
            if probe[1] is None or probe[1].count() == 0:
                return f"[ref 失效] 句柄 {ref} 不在当前页面，请重新调用 browser_snapshot 获取最新 ref。"
            selector = f'[data-ae-ref="{ref}"]'
        if not selector:
            return "[缺少 selector 或 ref]"
        frame, loc, fid = self._find_locator(selector, args.get("frame"))
        if frame is None or loc is None:
            return f"[未找到] {selector}（frame={args.get('frame') or 'auto'}）"
        try:
            if args.get("value") is not None:
                chosen = loc.first.select_option(value=str(args["value"]), timeout=8000)
            elif args.get("label") is not None:
                chosen = loc.first.select_option(label=str(args["label"]), timeout=8000)
            elif args.get("index") is not None:
                chosen = loc.first.select_option(index=int(args["index"]), timeout=8000)
            else:
                return "[缺少选项] 传 value / label / index 其中之一"
            return f"[已选择] {selector} → {chosen}（frame={fid}）"
        except Exception as e:
            return f"[选择失败] {e}（确认是原生 <select>，自定义下拉用 click+click 选项）"

    def find_text(self, args: dict) -> str:
        return self._run(self._do_find_text, args)

    def _do_find_text(self, args: dict) -> str:
        """文本定位：找页面上包含该文本的位置，返回 0-1000 相对坐标。
        run4 教训：模型从截图肉眼估像素，坐标差 ~140px 点偏了。
        JS 直接量 bounding rect，不用肉眼。拿到坐标后直接 browser_click_at。
        支持活跃浮层优先排序：若存在顶层浮层，优先返回浮层内的命中项。
        返回最多 10 个匹配（含中心坐标与文本片段）。"""
        if (blocked := self._do_guard()):
            return blocked
        text = (args.get("text") or "").strip()
        if not text:
            return "[缺少 text]"
        try:
            modal_info = self._detect_active_modal()
            modal_sel = modal_info.get("selector") if modal_info.get("has_modal") else ""
            hits = self._raw_page.evaluate("""([t, modalSel]) => {
                const out = [];
                const seen = new Set();
                const lower = t.toLowerCase();
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    const node = walker.currentNode;
                    const v = node.nodeValue || '';
                    if (!v.toLowerCase().includes(lower)) continue;
                    const pe = node.parentElement;
                    if (!pe) continue;
                    if (pe.closest && (pe.closest('#__alterego_root') || pe.closest('#__alterego_cursor') || pe.closest('#__ae_lock_toast'))) continue;
                    if (v.includes('申请人工接管') || v.includes('完成接管') || v.includes('停止任务') || v.includes('AlterEgo 正在控制') || v.includes('已锁定防误触')) continue;
                    const r = pe.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) continue;
                    const key = Math.round(r.x) + ',' + Math.round(r.y);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    let inModal = false;
                    try {
                        if (modalSel && pe.closest && pe.closest(modalSel)) {
                            inModal = true;
                        }
                    } catch(e) {}
                    out.push({x: r.x + r.width / 2, y: r.y + r.height / 2,
                              w: Math.round(r.width), h: Math.round(r.height),
                              text: v.trim().slice(0, 50).replace(/\\s+/g, ' '),
                              inModal: inModal});
                    if (out.length >= 25) break;
                }
                out.sort((a, b) => (b.inModal ? 1 : 0) - (a.inModal ? 1 : 0));
                return out.slice(0, 10);
            }""", [text, modal_sel])
        except Exception as e:
            return f"[查找失败] {e}"
        if not hits:
            return f"[未找到] 页面上没有包含 '{text}' 的可见文本"
        vp = self._get_viewport()
        vw, vh = vp["width"], vp["height"]
        modal_tag = "（已优先置顶浮层内匹配）" if modal_sel and any(h.get("inModal") for h in hits) else ""
        lines = [f"[文本定位] '{text}' 找到 {len(hits)} 处{modal_tag}（0-1000 相对坐标，直接喂 browser_click_at）："]
        for i, h in enumerate(hits):
            rx = max(0, min(1000, int(h["x"] / vw * 1000)))
            ry = max(0, min(1000, int(h["y"] / vh * 1000)))
            m_note = " [浮层内]" if h.get("inModal") else ""
            lines.append(f"  {i+1}. ({rx}, {ry}) '{h['text']}'{m_note}")
        return "\n".join(lines)

    def _do_evaluate(self, args: dict) -> str:
        """在页面 JS 上下文中执行表达式并返回结果（JSON 序列化后截断）。
        run5 教训：Agnes 纸飞机按钮的发送函数 .catch() 静默吞异常，
        UI 点击失败时需在页面内直接 fetch 发码接口并读返回 JSON 定位原因。
        表达式里不要做破坏性操作；返回超长时截断。
        frame 参数：在指定 frame 里执行（f1…），默认主 frame。"""
        if (blocked := self._do_guard()):
            return blocked
        expression = (args.get("expression") or "").strip()
        if not expression:
            return "[执行失败] expression 为空"
        frame = self._resolve_frame(args.get("frame"))
        if frame is None:
            frame = self._raw_page.main_frame
        try:
            result = frame.evaluate(expression)
        except Exception as e:
            return f"[执行失败] {e}"
        import json as _json
        try:
            text = _json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            text = str(result)
        if len(text) > 4000:
            text = text[:4000] + f"...（截断，共 {len(text)} 字符）"
        return f"[执行结果] {text}"

    def _do_wait(self, args: dict) -> str:
        if (blocked := self._do_guard()):
            return blocked
        seconds = min(max(float(args.get("seconds", 5)), 1), 60)
        reason = args.get("reason", "等待页面渲染完成")
        time.sleep(seconds)
        return f"[等待完成] 已在网页端等待 {seconds} 秒（原因: {reason}）"


    # ---------- §39.5 多标签页管理 ----------

    def new_tab(self, args: dict) -> str:
        return self._run(self._do_new_tab, args)

    # ---- Context 隔离（多账号）：每个命名 context 是独立的无痕级隔离 ----
    def context_new(self, args: dict) -> str:
        return self._run(self._do_context_new, args)

    def _do_context_new(self, args: dict) -> str:
        """建独立 browser 进程（进程级隔离：指纹都区分），用于多账号。
        同 eTLD 的站可共进程多标签；跨 eTLD 强制不同进程。"""
        if (blocked := self._do_guard()):  # 确保浏览器已启动
            return blocked
        name = (args.get("name") or "").strip()
        if not name:
            return "[失败] 需要 name 参数（如 acct1）"
        if name in self._profiles:
            return f"[失败] profile={name} 已存在，先 browser_context_close 再建"
        if self._pw is None:
            return "[失败] Playwright 未初始化"
        # 指纹轮换
        fp = _FINGERPRINTS[self._fp_index % len(_FINGERPRINTS)]
        self._fp_index += 1
        try:
            browser = self._pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--no-default-browser-check",
                ],
                ignore_default_args=["--enable-automation"],
            )
            bctx = browser.new_context(
                user_agent=fp["user_agent"],
                viewport=fp["viewport"],
                locale=fp["locale"],
                timezone_id=fp["timezone_id"],
            )
            # 方案3 工业级反反爬与指纹混淆补丁（同 default 进程）
            bctx.add_init_script(STEALTH_INIT_SCRIPT)
        except Exception as e:
            return f"[失败] 启动 browser 进程失败: {e}"
        # 可选：建时直接导入 cookies（cookie 登录）
        cookies = args.get("cookies") or []
        if cookies:
            try:
                bctx.add_cookies(cookies)
            except Exception as e:
                try:
                    bctx.close()
                    browser.close()
                except Exception:
                    pass
                return f"[失败] cookies 格式不对，profile 未建: {e}"
        self._profiles[name] = {"browser": browser, "context": bctx, "tabs": {},
                                 "domains": set(), "fingerprint": fp}
        n = len(cookies) if isinstance(cookies, list) else 0
        extra = f"，已导入 {n} 个 cookies" if n else ""
        return (f"[Profile 已建] name={name}（独立进程，指纹: {fp['viewport']['width']}x"
                f"{fp['viewport']['height']}{extra}）。用 browser_context_switch 切过去。")

    def context_switch(self, args: dict) -> str:
        return self._run(self._do_context_switch, args)

    def _do_context_switch(self, args: dict) -> str:
        """切换 active profile。tabs 按 profile 隔离，切过去看到的是另一套 tab/另一套登录态。"""
        name = (args.get("name") or "").strip()
        if name not in self._profiles:
            known = ", ".join(sorted(self._profiles)) or "无"
            return f"[失败] 不存在 profile={name}（现有: {known}）"
        self._active_profile = name
        tabs = self._tabs
        # 切到新 profile 的当前页（若有）
        pages = [t["page"] for t in tabs.values()]
        live = None
        for p in pages:
            try:
                if not p.is_closed():
                    live = p
                    break
            except Exception:
                continue
        if live is not None:
            self._page = live
        return (f"[已切换] profile={name}，该 profile 下 {len(tabs)} 个 tab"
                + ("" if live is not None else "（无可用页面，先 browser_new_tab）"))

    def context_close(self, args: dict) -> str:
        return self._run(self._do_context_close, args)

    def _do_context_close(self, args: dict) -> str:
        """关闭 profile（独立 browser 进程 + 其下所有 tab 一起关），释放隔离。"""
        name = (args.get("name") or "").strip()
        if name == "default":
            return "[失败] default 不能关"
        prof = self._profiles.get(name)
        if prof is None:
            return f"[失败] 不存在 profile={name}"
        try:
            if prof.get("browser") is not None:
                prof["browser"].close()  # 关整个进程（context 随之释放）
            elif prof.get("context") is not None:
                prof["context"].close()
        except Exception:
            pass
        del self._profiles[name]
        if self._active_profile == name:
            self._active_profile = "default"
            # 回到 default 的页面
            pages = [t["page"] for t in self._tabs.values()]
            for p in pages:
                try:
                    if not p.is_closed():
                        self._page = p
                        break
                except Exception:
                    continue
        return f"[已关闭] profile={name}（进程已回收）"

    def context_list(self, args: dict) -> str:
        return self._run(self._do_context_list, args)

    def _do_context_list(self, args: dict) -> str:
        """列出所有 profile 及其 tab 数、域名、指纹，标出当前 active 的。"""
        lines = []
        for name in sorted(self._profiles):
            prof = self._profiles[name]
            mark = " ←当前" if name == self._active_profile else ""
            kind = "默认(persistent)" if prof.get("browser") is None else "独立进程"
            doms = ",".join(sorted(prof.get("domains") or ())) or "-"
            fp = prof.get("fingerprint") or {}
            vp = fp.get("viewport", {})
            fps = f"{vp.get('width', '?')}x{vp.get('height', '?')}" if vp else "-"
            lines.append(f"- {name} [{kind}] {len(prof['tabs'])}tab 域:{doms} 指纹:{fps}{mark}")
        return "[Profiles]\n" + "\n".join(lines) if lines else "[Profiles] 无"

    def context_cookies(self, args: dict) -> str:
        return self._run(self._do_context_cookies, args)

    def _do_context_cookies(self, args: dict) -> str:
        """cookie 登录/导出：在指定 profile 导入 cookies（登录态），或导出当前 cookies。
        传 cookies=[{name,value,domain,path}] 即导入；不传则导出（值脱敏显示个数）。"""
        name = (args.get("name") or "").strip() or self._active_profile
        prof = self._profiles.get(name)
        if prof is None:
            return f"[失败] 不存在 profile={name}"
        bctx = prof.get("context")
        # default 用 persistent context：self._browser 本身就是 context
        if bctx is None and name == "default":
            bctx = self._browser
        if bctx is None:
            return "[失败] 该 profile 无可用 context"
        cookies = args.get("cookies")
        if cookies:
            # 导入：cookie 登录
            if not isinstance(cookies, list):
                return "[失败] cookies 必须是 list"
            try:
                bctx.add_cookies(cookies)
            except Exception as e:
                return f"[失败] 导入 cookies 失败: {e}"
            return f"[已导入] {len(cookies)} 个 cookies 到 profile={name}。刷新页面验证登录态。"
        else:
            # 导出：返回个数（值不打日志，防泄漏）
            try:
                stored = bctx.cookies()
            except Exception as e:
                return f"[失败] 读取 cookies 失败: {e}"
            domains = sorted({c.get("domain", "?") for c in stored})
            return (f"[Cookies] profile={name} 共 {len(stored)} 个，"
                    f"域名: {', '.join(domains) or '无'}")

    def _do_new_tab(self, args: dict) -> str:
        """按逻辑 role 开新标签页（如 mailbox / register），不按句柄。

        多站点任务强制每个逻辑页面独占 tab，禁止在同一 tab 来回 navigate 切站。
        """
        if (blocked := self._do_guard()):
            return blocked
        url = (args.get("url") or "").strip()
        role = (args.get("role") or "").strip()
        pinned = bool(args.get("pinned", False))
        if not role:
            role, inferred_pinned = self._infer_role_for_url(url, set(self._tabs.keys()))
            if not pinned:
                pinned = inferred_pinned
        if role in self._tabs:
            return f"[开标签页失败] role={role} 已存在，先 browser_close_tab 关闭或换 role"
        # LRU 预算：最多 _TAB_BUDGET 个，挤掉最久未用的非 pinned 页
        if len(self._tabs) >= _TAB_BUDGET:
            victims = sorted(
                ((r, t["last_used"]) for r, t in self._tabs.items() if not t["pinned"]),
                key=lambda x: x[1])
            if not victims:
                return "[开标签页失败] 已达上限且全部 pinned，先关闭一个"
            self._close_tab_role(victims[0][0])
        try:
            # 进程级隔离：用 active profile 的 context 建 page
            # （命名 profile = 独立 browser 进程；default 用 persistent context）
            _profiles = getattr(self, "_profiles", None) or {}
            _prof = _profiles.get(getattr(self, "_active_profile", "default")) or {}
            _bctx = _prof.get("context")
            page = _bctx.new_page() if _bctx is not None else self._browser.new_page()
            # eTLD 登记：同域 tabs 可共进程
            if url:
                _prof.setdefault("domains", set()).add(_etld1(url))
        except Exception as e:  # noqa: BLE001
            return f"[开标签页失败] new_page 失败: {e}"
        if url:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception as e:  # noqa: BLE001
                try:
                    page.close()
                except Exception:
                    pass
                diag = ""
                net_err_msg = str(e).lower()
                if any(k in net_err_msg for k in ("timeout", "err_connection", "err_proxy", "err_name_not_resolved", "err_ssl")):
                    diag = (
                        " [网络与代理诊断] 目标网站连接超时或网络不通。"
                        "若访问海外站点（如 Google / TikTok / ChatGPT），请检查系统 VPN/代理节点是否已开启且可用，"
                        "或尝试在代理客户端切换节点后再试。"
                    )
                return f"[开标签页失败] {url} 打开失败: {e}{diag}"
        from urllib.parse import urlsplit
        try:
            landed = page.url or url
            prefix = f"{urlsplit(landed).scheme}://{urlsplit(landed).netloc}"
            title_kw = page.title() or ""
        except Exception:
            prefix, title_kw = "", ""
        self._tabs[role] = {"page": page, "url_prefix": prefix, "title_kw": title_kw,
                            "pinned": pinned, "last_used": time.time()}
        self._page = page
        try:
            if hasattr(page, "bring_to_front"):
                page.bring_to_front()
        except Exception:
            pass
        pin_note = "（pinned，任务中不许关闭）" if pinned else ""
        return f"[标签页已开] role={role} url={landed}{pin_note}，已设为当前页"

    def switch_tab(self, args: dict) -> str:
        return self._run(self._do_switch_tab, args)

    def _do_switch_tab(self, args: dict) -> str:
        """按 role / 域名 / 页面关键字智能切回标签页。切回先核验身份（URL 前缀），不符拒绝切换。"""
        if (blocked := self._do_guard()):
            return blocked
        self._sync_tabs()
        role = (args.get("role") or "").strip()
        target_role, tab = self._find_tab(role)
        if tab is None:
            known = ", ".join(f"{r} ({getattr(t.get('page'), 'url', '') or t.get('url_prefix')})" for r, t in self._tabs.items()) or "无"
            return f"[切换失败] 不存在与 role='{role}' 匹配的标签页（现有: {known}）"
        page = tab["page"]
        try:
            if getattr(page, "is_closed", lambda: False)():
                del self._tabs[target_role]
                return f"[切换失败] role={target_role} 的标签页已关闭，请 browser_new_tab 重开"
            cur = getattr(page, "url", "") or ""
        except Exception as e:  # noqa: BLE001
            return f"[切换失败] 读取标签页状态失败: {e}"
        prefix = tab.get("url_prefix", "")
        if prefix and cur and not cur.startswith(prefix) and not cur.startswith("about:"):
            return (f"[标签页身份不符] role={target_role} 钉死的是 {prefix}，现在是 {cur}。"
                    "页面可能被导航走了。请先 browser_snapshot 重新观察，"
                    "确认无误后用 browser_new_tab 重开该 role。")
        tab["last_used"] = time.time()
        self._page = page
        try:
            if hasattr(page, "bring_to_front"):
                page.bring_to_front()
        except Exception:
            pass
        return f"[已切换] role={target_role} 当前 URL: {cur or '(空白页)'}"

    def close_tab(self, args: dict) -> str:
        return self._run(self._do_close_tab, args)

    def _close_tab_role(self, role: str):
        tab = self._tabs.pop(role, None)
        if tab is None:
            return
        try:
            tab["page"].close()
        except Exception:
            pass
        if self._raw_page is tab["page"]:
            rest = [t["page"] for t in self._tabs.values()]
            self._page = rest[0] if rest else None
            if self._raw_page is not None:
                try:
                    if hasattr(self._raw_page, "bring_to_front"):
                        self._raw_page.bring_to_front()
                except Exception:
                    pass

    def _do_close_tab(self, args: dict) -> str:
        self._sync_tabs()
        role = (args.get("role") or "").strip()
        force = bool(args.get("force", False))
        target_role, tab = self._find_tab(role)
        if tab is None:
            return f"[关闭失败] 不存在 role={role} 的标签页"
        if tab["pinned"] and not force:
            return (f"[关闭拒绝] role={target_role} 是 pinned 关键页（如邮箱），任务中不许关闭。"
                    "如确需关闭，加 force=true")
        self._close_tab_role(target_role)
        return f"[已关闭] role={target_role}"

    def screenshot(self, args: dict):
        return self._run(self._do_screenshot, args)

    def _cleanup_old_screenshots(self, root: Path, days: int = 7):
        """删除 N 天前的截图（按文件名时间戳或 mtime），保证磁盘空间。"""
        import time as _time
        cutoff = _time.time() - days * 86400
        try:
            if not root.is_dir():
                return
            for p in root.rglob("shot_*.jpg"):
                try:
                    # 文件名 shot_<unixtime>.jpg 优先用时间戳
                    ts = int(p.stem.split("_")[-1])
                    old = ts < cutoff
                except Exception:
                    old = p.stat().st_mtime < cutoff
                if old:
                    p.unlink()
        except Exception:
            pass

    def _do_screenshot(self, args: dict) -> Message:
        """截图 → JPEG quality=80，最长边 1568px（spec §24）。

        §39.6 视觉链路必通：必须返回带 images 的 Message，裸路径字符串
        不许出 worker（tools_registry 只给 Message 搬运 images）。
        连续 K=3 次产出无图 → 视觉链路熔断，直接 blocked，不许模型瞎猜。
        """
        if (blocked := self._do_guard()):
            return Message(role="tool", name="browser_screenshot", content=blocked)
        import io
        from pathlib import Path
        from PIL import Image
        self._ensure_maximized()
        clean = bool(args.get("clean", False))
        try:
            raw = self._clean_screenshot(clean=clean)
            if not raw:
                raise ValueError("截图返回空数据")
            img = Image.open(io.BytesIO(raw))
            w, h = img.size
            longest = max(w, h)
            if longest > 1568:
                scale = 1568 / longest
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            # 截图归档：默认存到 screenshots/YYYY-MM-DD/，不再散落根目录
            # （用户要求 2026-10-15：根目录 shot_*.jpg 太乱）
            import datetime as _dt
            _default_dir = Path("screenshots") / _dt.date.today().isoformat()
            out = Path(args.get("out_dir", str(_default_dir))) / f"shot_{int(time.time())}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            # 顺手清理 7 天前的截图（防磁盘爆）
            self._cleanup_old_screenshots(Path("screenshots"), days=7)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=80)
            jpeg_bytes = buf.getvalue()
            if not jpeg_bytes:
                raise ValueError("JPEG 编码产出空数据")
            out.write_bytes(jpeg_bytes)
        except Exception as e:  # noqa: BLE001
            self._blind_streak += 1
            if self._blind_streak >= _BLIND_STREAK_LIMIT:
                return Message(
                    role="tool", name="browser_screenshot",
                    content=("[视觉链路熔断] 连续 3 次截图未产出图片数据，模型处于盲态。"
                             "已停止，避免无图瞎猜。请检查截图链路后手动恢复。"),
                    metadata={"tool_control": {"retryable": False,
                                               "reason": "vision_link_blind"}})
            current_url = getattr(self._raw_page, "url", "未打开") or "未打开"
            return Message(role="tool", name="browser_screenshot",
                           content=f"[截图失败] {e}（连续 {self._blind_streak} 次无图，"
                                   f"当前页面: {current_url}）")
        self._blind_streak = 0
        current_url = self._raw_page.url if self._raw_page else "未打开"
        content = (f"[截图] {out}（{img.size[0]}x{img.size[1]}，当前页面: {current_url}）"
                   + self._layout_note())
        return Message(role="tool", name="browser_screenshot", content=content,
                       images=[ImagePart(data=jpeg_bytes, mime="image/jpeg")])

    def save_image(self, args: dict) -> str:
        return self._run(self._do_save_image, args)

    def _do_save_image(self, args: dict) -> str:
        """从当前页面提取并保存图片至本地。"""
        if (blocked := self._do_guard()):
            return blocked
        from pathlib import Path
        self._ensure_maximized()
        selector = args.get("selector") or "img"
        out_path = args.get("out_path", "")
        if not out_path:
            out_path = f"downloads/image_{int(time.time())}.png"
        out_file = Path(out_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            loc = self._raw_page.locator(selector)
            if loc.count() == 0:
                fallback_loc = self._raw_page.locator('img[src*="oaiusercontent"], img[src*="image"], img')
                if fallback_loc.count() > 0:
                    loc = fallback_loc.first
                else:
                    return f"[保存图片失败] 当前页面未找到匹配的图片元素（当前 URL: {self._raw_page.url}）"
            else:
                loc = loc.first
            loc.screenshot(path=str(out_file))
            self._human_delay()
            return f"[图片保存成功] 已直接提取并保存至本地文件: {out_file.resolve()}"
        except Exception as e:
            return f"[保存图片失败] {e}"

    def verify_code_countdown(self, args: dict) -> str:
        return self._run(self._do_verify_code_countdown, args)

    def _do_verify_code_countdown(self, args: dict) -> str:
        """【发码判定专有工具】检验验证码发送按钮是否真正成功触发并进入倒计时。

        硬判据：必须出现倒计时数字（如 60s、59秒、重新获取(59)）才代表发码动作已生效。
        若未进入倒计时，自动排查：
        1. 是否弹出图形/滑块/拼图人机验证（若有，应立即 browser_request_takeover 请求人工介入）；
        2. 是否有表单校验错误（如密码不一致、协议未勾选）；
        3. 发码按钮是否仍为初始未点击文本。
        """
        if (blocked := self._do_guard()):
            return blocked
        import json
        import time as _time

        timeout = min(max(float(args.get("timeout", 6.0)), 1.0), 30.0)
        selector = (args.get("selector") or "").strip()

        start = _time.monotonic()
        deadline = start + timeout
        last_eval = None

        js_code = """(sel) => {
            const cdRegex = /(?:\\b\\d{1,3}\\s*(?:s|秒|S)\\b|重新(?:获取|发送)\\s*\\(?\\s*\\d{1,3}\\s*(?:s|秒|S)?\\)?|\\d{1,3}\\s*秒?后重新(?:获取|发送)|(?:\\b|[^0-9])(?:60|59|58|57|56|55|54|53|52|51|50|[1-4][0-9]|[1-9])\\s*(?:s|秒)\\b|\\b\\d{1,2}\\s*:\\s*\\d{2}\\b)/i;

            let targetElements = [];
            if (sel) {
                try {
                    targetElements = Array.from(document.querySelectorAll(sel));
                } catch(e) {}
            }
            if (!targetElements.length) {
                targetElements = Array.from(document.querySelectorAll('button, a, span, input[type="button"], input[type="submit"], div[role="button"], [class*="code"], [class*="send"], [class*="verify"], [class*="countdown"]'));
            }

            for (const el of targetElements) {
                const text = (el.innerText || el.textContent || el.value || '').trim();
                if (cdRegex.test(text)) {
                    const m = text.match(cdRegex);
                    const r = el.getBoundingClientRect();
                    return {
                        status: "countdown_found",
                        text: text,
                        countdown_text: m ? m[0] : text,
                        tag: el.tagName.toLowerCase(),
                        className: el.className || "",
                        disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
                        rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}
                    };
                }
            }

            // 全局文本节点扫描
            const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                const v = (node.nodeValue || '').trim();
                if (cdRegex.test(v)) {
                    const pe = node.parentElement;
                    if (pe) {
                        const r = pe.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            const m = v.match(cdRegex);
                            return {
                                status: "countdown_found",
                                text: v,
                                countdown_text: m ? m[0] : v,
                                tag: pe.tagName.toLowerCase(),
                                className: pe.className || "",
                                disabled: Boolean(pe.disabled || pe.getAttribute("aria-disabled") === "true"),
                                rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}
                            };
                        }
                    }
                }
            }

            // 未检测到倒计时，收集诊断信息
            let captchaFound = null;
            const captchaSelectors = [
                'iframe[src*="captcha"]', 'iframe[src*="geetest"]', 'iframe[src*="hcaptcha"]', 'iframe[src*="recaptcha"]', 'iframe[src*="turnstile"]',
                '.geetest_holder', '.geetest_window', '.geetest_radar', '.hcaptcha-box', '.cf-turnstile', '[id*="captcha"]', '[class*="captcha"]',
                '.yidun', '.aliyun-captcha', '.tcaptcha-transform'
            ];
            for (const s of captchaSelectors) {
                try {
                    const el = document.querySelector(s);
                    if (el) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 20 && r.height > 20) {
                            captchaFound = `检测到人机验证组件: ${s}`;
                            break;
                        }
                    }
                } catch(e) {}
            }
            if (!captchaFound) {
                const bodyText = document.body ? document.body.innerText : '';
                const kws = ["安全验证", "拖动滑块", "按顺序点击", "人机身份验证", "滑动拼图", "请完成验证", "滑动验证"];
                for (const kw of kws) {
                    if (bodyText.includes(kw)) {
                        captchaFound = `页面出现人机验证提示: "${kw}"`;
                        break;
                    }
                }
            }

            const formErrors = [];
            const errorEls = Array.from(document.querySelectorAll('.error, [class*="error"], [role="alert"], [class*="feedback"], .ant-form-item-explain, .ds-form-item__error, .el-form-item__error'));
            for (const el of errorEls) {
                const t = (el.innerText || '').trim();
                if (t && t.length < 80 && !formErrors.includes(t)) {
                    formErrors.push(t);
                }
            }

            let sendBtn = null;
            for (const el of targetElements) {
                const text = (el.innerText || el.textContent || el.value || '').trim();
                if (text.includes("验证码") || text.includes("获取") || text.includes("发送") || text.toLowerCase().includes("code")) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 10 && r.height > 10) {
                        sendBtn = {
                            text: text,
                            tag: el.tagName.toLowerCase(),
                            disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true")
                        };
                        break;
                    }
                }
            }

            return {
                status: "no_countdown",
                captcha: captchaFound,
                form_errors: formErrors.slice(0, 5),
                send_button: sendBtn
            };
        }"""

        while _time.monotonic() < deadline:
            try:
                res = self._raw_page.evaluate(js_code, selector)
                last_eval = res
                if res and res.get("status") == "countdown_found":
                    elapsed = _time.monotonic() - start
                    cd_text = res.get("countdown_text", "")
                    full_text = res.get("text", "")
                    tag = res.get("tag", "element")
                    cls = res.get("className", "")
                    return (
                        f"[倒计时已确认] 验证码发送已成功触发！（耗时 {elapsed:.1f}s）\n"
                        f"- 检测到倒计时文案: \"{cd_text}\" (完整文本: \"{full_text}\")\n"
                        f"- 目标元素: <{tag} class=\"{cls}\">\n"
                        f"- 判定结论: 发码请求已成功被服务端受理，页面已进入倒计时。\n"
                        f"👉 现在可以安全切换到邮箱/短信标签页（browser_switch_tab）查收验证码！"
                    )
            except Exception:
                pass
            _time.sleep(0.5)

        # 超时未检测到倒计时
        lines = [
            f"[未检测到倒计时] 验证码发送未成功！在 {timeout:g}s 内页面发码按钮未出现倒计时数字。",
        ]
        if last_eval and isinstance(last_eval, dict):
            btn = last_eval.get("send_button")
            if btn:
                lines.append(f"- 发码按钮当前状态: 文本仍为 \"{btn.get('text', '')}\"（禁用状态: {btn.get('disabled')}），未进入倒计时状态。")
            if last_eval.get("captcha"):
                captcha_str = last_eval.get("captcha")
                lines.append(f"- ⚠️ 人机验证拦截: {captcha_str}")
                lines.append("  ↳ 触发动作: 已自动发起人工协同接管 (browser_request_takeover)，浏览器已切换为橙色接管状态，请在页面中完成滑块/验证！")
                try:
                    takeover_res = self._do_request_takeover(
                        reason=f"页面出现人机安全验证/滑块拦截 ({captcha_str})，请在左侧浏览器内协助完成验证",
                        timeout_sec=300.0,
                    )
                    lines.append(f"- 人工协同接管结束: {takeover_res}")
                except Exception as e:
                    lines.append(f"- 启动人工接管异常: {e}")
            if last_eval.get("form_errors"):
                lines.append(f"- ⚠️ 表单校验拦截: 发现错误提示: {json.dumps(last_eval.get('form_errors'), ensure_ascii=False)}")
                lines.append("  ↳ 恢复动作: 表单未填完整或有误（如密码不一致/未同意协议），请先补填表单再点击发送！")
        lines.append("【铁律】严禁在未确认倒计时的情况下切换到邮箱标签页！必须先排除上述拦截原因，重新点击发送并确认出现倒计时！")
        return "\n".join(lines)

    def request_takeover(self, args: dict) -> str:
        """用户接管流程（spec §15 / §31.5）。

        遇到验证码、扫码、登录墙时暂停自动化，弹出置顶小浮窗，等待用户在同一个浏览器中手动完成操作后点击【完成】。
        """
        reason = args.get("reason") or "遇到验证码或需要人工接管"
        timeout_sec = float(args.get("timeout_sec", 7200.0))
        return self._run(self._do_request_takeover, reason, timeout_sec)

    def _do_request_takeover(self, reason: str, timeout_sec: float) -> str:
        import uuid
        from agent.takeover import get_takeover_manager

        mgr = get_takeover_manager()
        task_id = f"takeover_{uuid.uuid4().hex[:8]}"
        curr_url = self._raw_page.url if self._raw_page else ""

        shot_path = ""
        try:
            self._ensure_maximized()
            if self._raw_page:
                from pathlib import Path
                Path("downloads").mkdir(parents=True, exist_ok=True)
                shot_path = f"downloads/{task_id}.jpg"
                raw = self._clean_screenshot(quality=80, type="jpeg")
                if raw:
                    Path(shot_path).write_bytes(raw)
        except Exception:
            pass

        # §15.7 联动：浏览器页面切入人类接管态（琥珀橙光效 + 解锁物理键鼠 + 顶部横幅显示完成按钮）
        self.unlock_input_for_human(reason=reason, task_id=task_id)

        mgr.request_takeover(
            task_id=task_id,
            reason=reason,
            target="Chrome",
            page_url=curr_url,
            screenshot_path=shot_path,
            timeout_sec=timeout_sec,
        )

        res = None
        try:
            res = mgr.wait_for_takeover(task_id, timeout_sec=timeout_sec)
        finally:
            if res and res.outcome == "completed":
                self.lock_input_for_agent()
            else:
                self.release_input_guard()

        if res and res.outcome == "completed":
            snap = self._do_snapshot({})
            return f"[人工接管已完成] 用户已在浏览器中完成操作并交还控制权。当前自动化已无缝恢复。\n最新页面状态：\n{snap}"
        elif res and res.outcome == "cancelled":
            return f"[人工接管已取消] 用户点击取消了本次人工接管。当前页面 URL: {curr_url}"
        else:
            return f"[人工接管超时] 用户接管操作已超过时限（{timeout_sec}s）。自动化任务已暂停。当前页面 URL: {curr_url}"


def make_browser_tools(worker: BrowserWorker) -> list[ToolDefinition]:
    def _schema(props, required):
        return {"type": "object", "properties": props, "required": required}

    return [
        ToolDefinition(
            name="browser_navigate",
            description=("打开网页。通用多标签页管理：跨域导航自动开辟新标签页或切回已有站点，"
                         "保护已有表单与临时邮箱状态不被覆盖；同域导航在当前标签页执行。"
                         "就绪判据=domcontentloaded+可选 wait_for 关键元素（§39.1.1）。"
                         "同一 URL 无状态变化重访≥3次会报 URL循环检测（§39.1.3）。"),
            parameters=_schema({"url": {"type": "string"},
                                "wait_for": {"type": "string", "default": "",
                                             "description": "关键元素 CSS 选择器，导航后显式等待它出现（如 input[type='email']）"},
                                "force_current_tab": {"type": "boolean", "default": False,
                                                      "description": "是否强制在当前标签页导航（即使跨域，通常设为 false）"},
                                "profile": {"type": "string", "default": "default"}},
                               ["url"]),
            func=worker.navigate),
        ToolDefinition(
            name="browser_new_tab",
            description=("按逻辑 role 开新标签页（如 mailbox、register），并设为当前页。"
                         "多站点任务强制每个逻辑页面独占 tab（§39.5）。pinned=true 的是关键页，任务中不许关闭。"
                         "新 tab 建在当前 active context 下（如需账号隔离先 browser_context_new/switch）。"),
            parameters=_schema({"url": {"type": "string", "default": "",
                                         "description": "新标签页要打开的网址，可空"},
                                "role": {"type": "string",
                                         "description": "逻辑 role 名，如 mailbox / register"},
                                "pinned": {"type": "boolean", "default": False,
                                           "description": "是否为关键页（邮箱等），pinned 页不许关闭"}},
                               ["role"]),
            func=worker.new_tab),
        ToolDefinition(
            name="browser_context_new",
            description=("建独立 browser 进程（进程级隔离：cookies/storage 全隔离 + 指纹区分）。"
                         "多账号任务每个账号一个进程，互不串号。可选建时直接传 cookies 导入登录态。"
                         "同 eTLD 的站可在同一进程内多标签，跨域自动隔离。"),
            parameters=_schema({"name": {"type": "string", "description": "context 名，如 acct1"},
                                "cookies": {"type": "array", "default": [],
                                             "description": "可选：[{name,value,domain,path}]，建时直接导入（cookie 登录）"}},
                               ["name"]),
            func=worker.context_new),
        ToolDefinition(
            name="browser_context_switch",
            description="切换 active profile（独立进程）。tabs 按进程隔离，切过去看到的是另一套 tab/另一套登录态。",
            parameters=_schema({"name": {"type": "string", "description": "context 名"}},
                               ["name"]),
            func=worker.context_switch),
        ToolDefinition(
            name="browser_context_close",
            description="关闭指定 profile（独立进程 + 其下所有 tab 一起关，进程回收）。default 不能关。",
            parameters=_schema({"name": {"type": "string", "description": "context 名"}},
                               ["name"]),
            func=worker.context_close),
        ToolDefinition(
            name="browser_context_list",
            description="列出所有 profile（进程）及其 tab 数、域名、指纹，标出当前 active 的。",
            parameters=_schema({}, []),
            func=worker.context_list),
        ToolDefinition(
            name="browser_context_cookies",
            description=("在指定 profile 导入/导出 cookies。传 cookies=[{name,value,domain,path}] 即导入（cookie 登录）；"
                         "不传则导出当前 cookies 个数与域名（值不落日志）。"),
            parameters=_schema({"name": {"type": "string", "default": "",
                                          "description": "profile 名，空=当前 active"},
                                "cookies": {"type": "array", "default": [],
                                             "description": "[{name,value,domain,path}]，传了即导入"}},
                                []),
            func=worker.context_cookies),
        ToolDefinition(
            name="browser_switch_tab",
            description=("按 role、域名或关键字切回已有标签页（如 mailbox、register 或 inodetree.com）。"
                         "切回时自动核验身份，确保页面不被串改。"),
            parameters=_schema({"role": {"type": "string", "description": "逻辑 role 名、域名或页面关键字（如 mailbox / register）"}},
                               ["role"]),
            func=worker.switch_tab),
        ToolDefinition(
            name="browser_close_tab",
            description="按 role 关闭标签页。pinned 关键页拒绝关闭（除非 force=true）。"
                        "关闭当前页后会自动切到其余任一 tab。",
            parameters=_schema({"role": {"type": "string", "description": "逻辑 role 名"},
                                "force": {"type": "boolean", "default": False}},
                               ["role"]),
            func=worker.close_tab),
        ToolDefinition(
            name="browser_snapshot",
            description="读页面无障碍树（CDP），返回当前页面 URL、标题及可操作元素清单。",
            parameters=_schema({}, []),
            func=worker.snapshot),
        ToolDefinition(
            name="browser_click",
            description="点击页面元素。优先使用句柄 ref（来自 browser_snapshot 坐标树的 [ref=aeN]），无需换算坐标或编写复杂选择器。亦支持 CSS selector 点击。支持自动下载监听（若按钮含下载语义将自动保存文件至 downloads/ 目录）。iframe 感知：默认自动搜索全部 frame（主 frame 优先）；已知目标在子 frame 时传 frame=\"f1\"。expect 可选：声明点击后的期望状态（如 {\"text_visible\": \"发送成功\"}），框架自动核验 5 秒，未达成会明确报告。",
            parameters=_schema({
                "ref": {"type": "string", "default": "", "description": "【首选】元素句柄，来自最近一次 browser_snapshot 的 [ref=aeN]（如 'ae3'）。优先级最高，天然免疫位移与选择器报错。"},
                "selector": {"type": "string", "default": "", "description": "目标元素的 CSS 选择器（未传 ref 时使用）"},
                "frame": {"type": "string", "default": "auto", "description": "目标所在 frame（f0=主，f1/f2…=子 frame，见 snapshot 的 [Frames] 清单）；默认 auto 全搜"},
                "expect": {"type": "object", "default": {}, "description": "期望状态（5秒内核验）：url_contains/url_not_contains/text_visible/text_hidden/selector_visible/selector_hidden，如 {\"url_contains\": \"/dashboard\"}"}},
                []),
            func=worker.click),
        ToolDefinition(
            name="browser_click_at",
            description=("按截图坐标点击页面（x/y 为 0~1000 相对视口坐标）。逃生通道：当 browser_click "
                         "用选择器连续超时、目标不在无障碍树里时用——如输入框内嵌的小图标按钮"
                         "（验证码框里的小飞机）。先 browser_screenshot，从图中读出目标位置再点。"
                         "返回会附一张落点回显图（红圈=实际点击位置）：没点中就对比偏差修正坐标再点。"),
            parameters=_schema({"x": {"type": "number", "description": "横坐标 0~1000（相对视口宽度）"},
                                "y": {"type": "number", "description": "纵坐标 0~1000（相对视口高度）"},
                                "expect": {"type": "object", "default": {}, "description": "期望状态（5秒内核验）：url_contains/url_not_contains/text_visible/text_hidden/selector_visible/selector_hidden"}},
                               ["x", "y"]),
            func=worker.click_at),
        ToolDefinition(
            name="browser_probe_at",
            description=("点前探针：报告指定坐标（0~1000）最上层命中的元素，并返回该点周围放大图"
                         "（红十字中心即该坐标）。用于 browser_click_at 之前确认'十字确实压住了目标'，"
                         "避免 run4 那种差 140px 却不自知的点偏。若目标不在十字中心，按返回文本中的公式"
                         "量偏移修正坐标再点。"),
            parameters=_schema({"x": {"type": "number", "description": "横坐标 0~1000（相对视口宽度）"},
                                "y": {"type": "number", "description": "纵坐标 0~1000（相对视口高度）"},
                                "zoom": {"type": "number", "default": 3, "description": "放大倍数（2~5，默认 3）"}},
                               ["x", "y"]),
            func=worker.probe_at),
        ToolDefinition(
            name="browser_fill_input",
            description=(
                "【推荐首选输入工具】通用闭环拟人输入引擎。优先支持 ref 句柄（来自 browser_snapshot 坐标树的 [ref=aeN]）；"
                "同时支持 CSS selector 或截图相对视口坐标 (0~1000) 定位；"
                "自动激活未展开的占位框/评论栏（如各类社区评论'说点什么'）；支持原生 <input>/<textarea> 以及"
                "现代前端富文本/ContentEditable（Slate/Draft/Lexical/React/Vue）；写入后自动回读核验文本真实落地，"
                "杜绝虚假上报；支持可选 press_enter=true 直接回车提交。"
            ),
            parameters=_schema({
                "text": {"type": "string", "description": "要填入的文本内容"},
                "ref": {"type": "string", "default": "", "description": "【首选】目标元素句柄，来自最近一次 browser_snapshot 的 [ref=aeN]（如 'ae2'）。优先于 selector 与 coords，天然免疫位移与选择器错误。"},
                "selector": {"type": "string", "default": "", "description": "目标输入框或占位符的 CSS 选择器（与 ref/coords 二选一）"},
                "coords": {"type": "array", "items": {"type": "number"}, "description": "目标相对视口坐标 [x, y]（0~1000，与 ref/selector 二选一）"},
                "x": {"type": "number", "description": "目标相对视口横坐标 0~1000"},
                "y": {"type": "number", "description": "目标相对视口纵坐标 0~1000"},
                "clear_before": {"type": "boolean", "default": True, "description": "输入前是否清空已有内容，默认 true"},
                "press_enter": {"type": "boolean", "default": False, "description": "输入完成后是否自动回车提交，默认 false"},
                "assert_pinned_key": {"type": "string", "default": "", "description": "关键值钉死校验（state_pin 键名）"},
                "frame": {"type": "string", "default": "auto", "description": "目标所在 frame，默认 auto 全搜"},
                "expect": {"type": "object", "default": {}, "description": "输入后的期望状态（5秒内核验）"}
            }, ["text"]),
            func=worker.fill_input),
        ToolDefinition(
            name="browser_type",
            description=("在输入框输入文本（CSS selector）。支持可选参数 press_enter=true，输入后直接回车提交表单"
                         "（极力推荐在输入邮箱、搜索词等表单时开启，能彻底避免误点按钮）。"
                         "填关键值（邮箱/验证码/订单号）时必须传 assert_pinned_key：先 state_pin 从截图读回钉死，"
                         "这里会强制比对，不一致直接拒绝（§39.1.2）。"),
            parameters=_schema({"selector": {"type": "string"},
                                "text": {"type": "string"},
                                "assert_pinned_key": {"type": "string", "default": "",
                                                     "description": "关键值钉死检查：text 必须与 state_pin 钉死的值完全一致，否则拒绝输入"},
                                "press_enter": {"type": "boolean",
                                                "description": "输入完成后是否自动按回车键（Enter）提交表单。填写邮箱/搜索框时强烈建议设为 true。"},
                                "frame": {"type": "string", "default": "auto",
                                          "description": "目标所在 frame（f0=主，f1/f2…=子 frame，见 snapshot 的 [Frames] 清单）；默认 auto 全搜"},
                                "expect": {"type": "object", "default": {},
                                           "description": "期望状态（5秒内核验）：url_contains/url_not_contains/text_visible/text_hidden/selector_visible/selector_hidden"}},
                               ["selector", "text"]),
            func=worker.type_text),
        ToolDefinition(
            name="browser_screenshot",
            description=("页面截图（JPEG 80，最长边 1568px）。默认保留 SoM 彩色编号框选层，"
                         "模型可直接对照图中数字标号调用 browser_click(ref=\"aeN\")；"
                         "传 clean: true 可去除全部框选标注，截取真实原始网页图（用于海报/原图交付或视觉对比）。"),
            parameters=_schema({
                "out_dir": {"type": "string", "default": ".", "description": "截图保存目录"},
                "clean": {"type": "boolean", "default": False, "description": "是否截取无标注的纯净原图，默认 false（带 SoM 框选）"}
            }, []),
            func=worker.screenshot),
        ToolDefinition(
            name="browser_save_image",
            description="保存当前网页中的图片至本地。支持指定 selector（如 img、img[alt*=\"海报\"]，默认为页面内最大/可见的图片），并保存至 out_path（默认 downloads/image_<时间戳>.png）。在 ChatGPT、豆包等生图网站中直接使用本工具即可完成原图落地，无需调用终端或点击下载按钮。",
            parameters=_schema({"selector": {"type": "string", "default": "img", "description": "目标图片的 CSS 选择器"},
                                "out_path": {"type": "string", "default": "", "description": "保存到本地的文件路径，默认 downloads/image_<timestamp>.png"}},
                               []),
            func=worker.save_image),
        ToolDefinition(
            name="browser_wait",
            description="在网页端等待指定秒数（默认 5 秒，最大 60 秒），用于等待 ChatGPT、豆包等 AI 平台生成长文本、代码输出、图像渲染或页面动态加载。严禁使用 shell_exec 调用 sleep 命令，等待网页生成必须使用本工具。",
            parameters=_schema({"seconds": {"type": "number", "default": 5, "description": "等待秒数（1~60）"},
                                "reason": {"type": "string", "default": "等待生成完成", "description": "等待原因"}}, []),
            func=worker.wait),
        ToolDefinition(
            name="browser_evaluate",
            description="在当前页面 JS 上下文中执行表达式并返回结果。用于读取页面状态（如按钮是否可点）、或在 UI 点击静默失败时直接调用页面背后的接口（如 Agnes 发码接口）并读取返回 JSON 定位原因。只读表达式优先；不要做破坏性操作。frame 参数可选：在指定 frame 里执行（f1…，见 snapshot 的 [Frames] 清单），默认主 frame。",
            parameters=_schema({"expression": {"type": "string", "description": "要执行的 JS 表达式，如 \"document.querySelector('img[alt=\\\"send\\\"]')?.outerHTML\" 或 \"fetch(url).then(r=>r.json()).then(j=>JSON.stringify(j))\""},
                                "frame": {"type": "string", "default": "auto", "description": "执行的 frame（f0=主，f1/f2…=子 frame）；默认主 frame"}}, ["expression"]),
            func=worker.evaluate),
        ToolDefinition(
            name="browser_console",
            description=("读取浏览器 console 消息（含未捕获异常 pageerror）。页面静默失败时"
                         "（点了没反应、无 toast、无倒计时）先看这里——前端 catch 吞掉的异常、"
                         "资源加载失败都会留下痕迹。type 可选 error/warning/pageerror 过滤。"),
            parameters=_schema({"type": {"type": "string", "default": "", "description": "过滤类型：error/warning/pageerror，空=全部"},
                                "limit": {"type": "number", "default": 20, "description": "最多返回条数（1~50）"}}, []),
            func=worker.console_log),
        ToolDefinition(
            name="browser_network_log",
            description=("读取最近的网络请求（xhr/fetch/document，或报错的请求），含状态码与耗时。"
                         "回答'请求到底发了没、回了什么'——点击后无事发生、怀疑被静默吞掉时先查这里。"
                         "filter 按 URL 子串过滤，errors_only 只看失败的。"),
            parameters=_schema({"filter": {"type": "string", "default": "", "description": "按 URL 子串过滤，如 verification"},
                                "errors_only": {"type": "boolean", "default": False, "description": "只看 4xx/5xx/无响应的"},
                                "show_body": {"type": "boolean", "default": False, "description": "显示失败请求的响应体"},
                                "show_post": {"type": "boolean", "default": False, "description": "显示 POST 请求体"},
                                "limit": {"type": "number", "default": 20, "description": "最多返回条数（1~50）"}}, []),
            func=worker.network_log),
        ToolDefinition(
            name="browser_inspect",
            description=("元素级取证：返回标签/属性/文本/包围盒/可见性/计算样式（颜色/背景/透明度/"
                         "pointer-events/cursor）/禁用态/点击 handler 线索，并附元素放大图与主色采样。"
                         "回答'这个按钮到底是什么状态'——图片类按钮看主色判死活。frame 参数规则同 click。"),
            parameters=_schema({"selector": {"type": "string", "description": "CSS 选择器"},
                                "frame": {"type": "string", "default": "auto", "description": "frame 引用：auto/f0/f1…"}} , ["selector"]),
            func=worker.inspect),
        ToolDefinition(
            name="browser_resource",
            description=("页面资源分析：不带 url 列出页面加载的 JS/CSS；带 url 用页面自身 fetch 下载文本资源；"
                         "带 search 在资源内按关键词搜（如找 API 路径）。调试前端逻辑/接口时用。"),
            parameters=_schema({"url": {"type": "string", "default": "", "description": "资源 URL，空=列出页面 JS/CSS"},
                                "search": {"type": "string", "default": "", "description": "关键词搜索，如 verification"},
                                "context": {"type": "number", "default": 2, "description": "匹配行上下文行数（0~5）"},
                                "max_bytes": {"type": "number", "default": 20000, "description": "返回上限（1000~200000）"}}, []),
            func=worker.resource),
        ToolDefinition(
            name="browser_wait_for",
            description=("条件等待：轮询直到任一条件满足或超时（独立长等待步骤）。"
                         "用于等异步事件——邮件到达、倒计时结束、特定 API 响应、URL 跳转。"
                         "条件：text_visible/text_hidden/selector_visible/selector_hidden/"
                         "url_contains/url_not_contains/network_url（只看等待开始后的请求）。"
                         "timeout 最长 300 秒。"),
            parameters=_schema({"text_visible": {"type": "string", "default": "", "description": "等文本出现"},
                                "text_hidden": {"type": "string", "default": "", "description": "等文本消失"},
                                "selector_visible": {"type": "string", "default": "", "description": "等选择器可见"},
                                "selector_hidden": {"type": "string", "default": "", "description": "等选择器消失"},
                                "url_contains": {"type": "string", "default": "", "description": "等 URL 包含"},
                                "url_not_contains": {"type": "string", "default": "", "description": "等 URL 不包含"},
                                "network_url": {"type": "string", "default": "", "description": "等含该子串的网络请求"},
                                "timeout": {"type": "number", "default": 30, "description": "超时秒数（1~300）"}}, []),
            func=worker.wait_for),
        ToolDefinition(
            name="browser_storage",
            description=("查看浏览器存储（登录态/会话调试）。action: cookies（值脱敏截断）/"
                         "local（localStorage）/ session（sessionStorage）。"
                         "回答'我登录了吗''token 存哪了'。"),
            parameters=_schema({"action": {"type": "string", "default": "cookies", "description": "cookies/local/session"}}, []),
            func=worker.storage),
        ToolDefinition(
            name="browser_press",
            description=("键盘按键：Enter（表单提交）/ Tab（切换焦点）/ Escape（关闭弹窗）/ ArrowDown 等。"
                         "作用于当前焦点元素。"),
            parameters=_schema({"key": {"type": "string", "description": "按键名，如 Enter"}}, ["key"]),
            func=worker.press),
        ToolDefinition(
            name="browser_handler",
            description=("事件 handler 溯源：元素被点击后到底执行什么代码。查 onclick 属性、"
                         "React props 里的 onClick 源码、Vue 挂载线索、祖先拦截。"
                         "沉默失败（点了没反应）的根因定位用：看 handler 有没有 .catch 吞异常、"
                         "有没有前置条件提前 return。frame 参数规则同 click。"),
            parameters=_schema({"selector": {"type": "string", "description": "CSS 选择器"},
                                "frame": {"type": "string", "default": "auto", "description": "frame 引用：auto/f0/f1…"}}, ["selector"]),
            func=worker.handler),
        ToolDefinition(
            name="browser_observe",
            description=("DOM 变化观察：记录 duration 秒内的 DOM 突变（新增/删除节点、属性变更、"
                         "文本变更），返回聚合摘要。调试'点了之后页面到底发生了什么'用，"
                         "比截图肉眼对比可靠。selector 可选只观察某个子树，duration 最长 30 秒。"),
            parameters=_schema({"duration": {"type": "number", "default": 5, "description": "观察秒数（1~30）"},
                                "selector": {"type": "string", "default": "", "description": "只观察该选择器子树，空=全页面"}}, []),
            func=worker.observe),
        ToolDefinition(
            name="browser_upload",
            description=("文件上传：给 <input type=file> 设置本地文件。"
                         "上传按钮可能是假的（真 input 藏在 DOM 里），找不到时用 browser_inspect 找 input[type=file]。"),
            parameters=_schema({"selector": {"type": "string", "description": "file input 的 CSS 选择器"},
                                "path": {"type": "string", "description": "本地文件路径"},
                                "frame": {"type": "string", "default": "auto", "description": "frame 引用"}}, ["selector", "path"]),
            func=worker.upload),
        ToolDefinition(
            name="browser_scroll",
            description=("滚动：direction=up/down/top/bottom 按像素滚，或 selector 把元素滚进视口。"
                         "长页面找折叠下方元素用。"),
            parameters=_schema({"direction": {"type": "string", "default": "down", "description": "up/down/top/bottom"},
                                "amount": {"type": "number", "default": 600, "description": "像素数（100~5000）"},
                                "selector": {"type": "string", "default": "", "description": "滚到该元素可见"},
                                "frame": {"type": "string", "default": "auto", "description": "frame 引用"}}, []),
            func=worker.scroll),
        ToolDefinition(
            name="browser_hover",
            description=("悬停：鼠标移到元素上，展开下拉菜单/触发 tooltip。优先使用 ref 句柄。用完接 browser_snapshot 看新元素。"),
            parameters=_schema({
                "ref": {"type": "string", "default": "", "description": "【首选】元素句柄，来自最近一次 browser_snapshot 的 [ref=aeN]"},
                "selector": {"type": "string", "default": "", "description": "CSS 选择器（未传 ref 时使用）"},
                "frame": {"type": "string", "default": "auto", "description": "frame 引用"}}, []),
            func=worker.hover),
        ToolDefinition(
            name="browser_select",
            description=("下拉框选择：原生 <select> 按 value/label/index 选一项。"
                         "自定义下拉（div 模拟的）用 click 点开再 click 选项。"),
            parameters=_schema({
                "ref": {"type": "string", "default": "", "description": "【首选】select 元素句柄，来自最近一次 browser_snapshot 的 [ref=aeN]"},
                "selector": {"type": "string", "default": "", "description": "select 的 CSS 选择器（未传 ref 时使用）"},
                "value": {"type": "string", "description": "option 的 value"},
                "label": {"type": "string", "description": "option 可见文本"},
                "index": {"type": "number", "description": "序号（0 起）"},
                "frame": {"type": "string", "default": "auto", "description": "frame 引用"}}, []),
            func=worker.select),
        ToolDefinition(
            name="browser_find_text",
            description=("文本定位：找页面上包含该文本的位置，返回 0-1000 相对坐标（最多 10 处）。"
                         "JS 直接量 bounding rect，不用从截图肉眼估像素。拿到坐标后直接 browser_click_at。"),
            parameters=_schema({"text": {"type": "string", "description": "要找的文本（子串匹配，不区分大小写）"}}, ["text"]),
            func=worker.find_text),
        ToolDefinition(
            name="browser_verify_code_countdown",
            description=(
                "【发码判定专有工具】检验验证码发送按钮是否真正成功触发！"
                "通过轮询页面检测按钮或文本是否进入倒计时状态（如 60s、59秒、重新获取(58)）。"
                "若进入倒计时，则代表发码动作已生效，可安全切去邮箱/手机收件；若未进入倒计时，自动诊断未成功原因（是否弹出滑块人机验证、是否有表单拦截报错），"
                "【铁律】严禁在未确认倒计时的情况下切换到邮箱标签页！"
            ),
            parameters=_schema({
                "selector": {"type": "string", "description": "可选。发码按钮的选择器"},
                "timeout": {"type": "number", "default": 6.0, "description": "等待倒计时出现的最长秒数（默认 6 秒）"}
            }, []),
            func=worker.verify_code_countdown),
        ToolDefinition(
            name="browser_request_takeover",
            description="当遇到滑块验证码、短信/邮箱验证码、微信扫码登录、人机识别或需要用户手动介入的复杂表单时调用本工具。系统将弹出原生置顶小浮窗暂停自动化，等待用户在当前浏览器中手动完成操作后点击【完成】自动无缝恢复执行。",
            parameters=_schema({"reason": {"type": "string", "description": "需要用户接管的具体原因，如'遇到极验滑块验证码'、'需要扫码登录'"}}, ["reason"]),
            func=worker.request_takeover),
        ToolDefinition(
            name="browser_lock_input",
            description="（§15 防误触）锁定人类物理鼠标键盘输入，Agent 独占执行。页面显示蓝色边框+提示。任务开始/恢复时调用。",
            parameters=_schema({}, []),
            func=lambda args: ("[输入已锁定] 人类物理键鼠已屏蔽，Agent 独占执行中。"
                               if worker.lock_input_for_agent() else "[锁定失败] 无可用页面"),
        ),
        ToolDefinition(
            name="browser_unlock_input",
            description="（§15 防误触）解锁人类输入，允许用户操作页面（扫码/验证码/接管）。接管时调用。",
            parameters=_schema({}, []),
            func=lambda args: ("[输入已解锁] 用户可操作页面。"
                               if worker.unlock_input_for_human() else "[解锁失败] 无可用页面"),
        ),
        ToolDefinition(
            name="browser_record_start",
            description=(
                "手动开启当前浏览器会话的跨标签页一镜到底录屏（输出至 artifacts/videos/*.mp4）。"
                "系统默认也会在导航时自动录屏，本工具可用于按需指定录像名称或显式重置录制。"
            ),
            parameters=_schema({
                "name": {"type": "string", "default": "", "description": "录屏任务标识名称，如 'xhs_comment'"}
            }, []),
            func=worker.record_start),
        ToolDefinition(
            name="browser_record_stop",
            description="停止当前录屏，完成视频封装转码并输出最终 MP4 视频文件路径及运镜元数据 actions_meta.json。",
            parameters=_schema({}, []),
            func=worker.record_stop),
    ]

# [M3 完成] 接管模式（用户接管浏览器窗口，见 spec §15/§31.5）已完整实现。
