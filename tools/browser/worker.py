"""浏览器工具：navigate / click / type / snapshot。

感知链（spec §9）：CDP 无障碍树优先 → Playwright locator → 截图兜底。

[实测结论 2026-10-11]：Playwright Python 1.63 没有 page.accessibility.snapshot()
（spec 曾标 [待验证]，现确认不存在）。无障碍树走 CDP
Accessibility.getFullAXTree（已在 Chromium 1243 上实测可用）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
import logging
import os
from pathlib import Path
import random
import sys
import threading
import time

from agent.tools_registry import ToolDefinition
from agent.models import ImagePart, Message


logger = logging.getLogger("alterego.browser")


class BrowserCallTimeout(RuntimeError):
    """Playwright/CDP 调用超过等待上限：工作线程已卡死，不能再无限阻塞。"""


DEFAULT_CALL_TIMEOUT_SEC = 120.0


def _configured_call_timeout() -> float:
    """单次 Playwright 调用的等待上限；可用环境变量覆盖。"""
    try:
        value = float(os.environ.get("ALTEREGO_BROWSER_CALL_TIMEOUT_SEC", ""))
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else DEFAULT_CALL_TIMEOUT_SEC


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
([modalSel, holdMs]) => {
  // ① 清旧：清理上次快照残留的旧 ref 属性与 SoM 框选层，保证编号属于当前最新快照
  try {
    document.querySelectorAll('[data-ae-ref]').forEach(el => el.removeAttribute('data-ae-ref'));
    if (window.__ae_som_mo) { window.__ae_som_mo.disconnect(); }
    if (window.__ae_som_timer) { clearTimeout(window.__ae_som_timer); window.__ae_som_timer = null; }
    document.getElementById('__ae_som')?.remove();
  } catch(e) {}

  // ①b 框选层样式（一次性注入）：淡入上浮 + 柔和光晕，尊重系统减少动效偏好
  try {
    if (!document.getElementById('__ae_som_css')) {
      const st = document.createElement('style');
      st.id = '__ae_som_css';
      st.textContent =
        '@keyframes aeSomIn{from{opacity:0;transform:translateY(2px) scale(.985)}to{opacity:1;transform:none}}' +
        '#__ae_som .ae-som-box,#__ae_som .ae-som-badge{animation:aeSomIn .16s cubic-bezier(.22,.61,.36,1) both}' +
        '@media (prefers-reduced-motion:reduce){#__ae_som .ae-som-box,#__ae_som .ae-som-badge{animation:none}}';
      (document.head || document.documentElement).appendChild(st);
    }
  } catch(e) {}

  const C = { click: '#2563eb', input: '#f59e0b', modal: '#a855f7' }; // 蓝/橙/紫
  const EXCLUDE = '#__alterego_root, #__alterego_cursor, #__ae_lock_toast, #__ae_som';

  let layer = null;
  try {
    layer = document.createElement('div');
    layer.id = '__ae_som';
    layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483646;opacity:1;will-change:opacity;transition:opacity .45s ease;';
    document.documentElement.appendChild(layer);
  } catch(e) {}

  const inModalOf = (el) => {
    try { return !!(modalSel && el.closest && el.closest(modalSel)); } catch(e) { return false; }
  };
  const paint = (rect, color, num) => {
    if (!layer) return;
    try {
      const delay = Math.min((num - 1) * 5, 180);
      const box = document.createElement('div');
      box.className = 'ae-som-box';
      box.style.cssText =
        'position:fixed;left:' + (rect.left - 2) + 'px;top:' + (rect.top - 2) + 'px;' +
        'width:' + (rect.width + 4) + 'px;height:' + (rect.height + 4) + 'px;' +
        'border:1.5px solid ' + color + 'e6;border-radius:6px;' +
        'background:' + color + '0f;box-shadow:0 0 0 1px ' + color + '1f,0 2px 10px -2px ' + color + '40;' +
        'pointer-events:none;box-sizing:border-box;animation-delay:' + delay + 'ms;';
      const badge = document.createElement('div');
      badge.className = 'ae-som-badge';
      badge.textContent = num;
      badge.style.cssText =
        'position:fixed;left:' + (rect.left - 2) + 'px;top:' + Math.max(0, rect.top - 18) + 'px;' +
        'background:' + color + ';color:#ffffff;font:600 10px/16px ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;' +
        'letter-spacing:.02em;padding:0 5px;border-radius:5px;white-space:nowrap;pointer-events:none;' +
        'box-shadow:0 1px 3px rgba(15,23,42,.35),0 0 0 1px rgba(255,255,255,.18) inset;' +
        'z-index:2147483647;animation-delay:' + delay + 'ms;';
      layer.append(box, badge);
    } catch(e) {}
  };

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
    
    const isFileInput = el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'file';
    const inputType = el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text').toLowerCase() : '';
    const isTextInput = el.tagName === 'INPUT'
      && ['text', 'search', 'email', 'url', 'tel', 'password', 'number'].indexOf(inputType) >= 0;
    const isEditable = isTextInput || el.tagName === 'TEXTAREA' || el.isContentEditable || el.getAttribute('role') === 'textbox' || el.getAttribute('contenteditable') === 'true';
    const inModal = inModalOf(el);

    const refStr = 'ae' + id;
    try {
      el.setAttribute('data-ae-ref', refStr);
    } catch(e) {}

    // ② 同源绘制 SoM 视觉标记框与编号徽章
    paint(rect, inModal ? C.modal : (isEditable ? C.input : C.click), id);

    results.push({
      ref: refStr,
      id: id++,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || (isEditable ? 'textbox' : ''),
      is_editable: isEditable,
      in_modal: inModal,
      name: text || (isFileInput ? '[文件上传]' : (isEditable ? '[输入框]' : '[纯图标/无文本]')),
      x: Math.round(rect.left + rect.width / 2),
      y: Math.round(rect.top + rect.height / 2),
      w: Math.round(rect.width),
      h: Math.round(rect.height)
    });
  }

  // ②b 纯图标控件兜底（P0-2）：无文字、SVG/IMG 图标、指针手型、邻近输入框/表单的小热区。
  // 发送/提交/纸飞机/评论箭头这类控件常是 div>svg 的自绘热区，不在主选择器里，
  // 模型只能靠坐标猜（抖音评论 2026-10-27：文案已填、找不到发送按钮）。
  try {
    const editableRects = [];
    document.querySelectorAll('input, textarea, [contenteditable="true"], [contenteditable=""]').forEach(e => {
      const r = e.getBoundingClientRect();
      if (r.width >= 4 && r.height >= 4) editableRects.push(r);
    });
    const nearEditable = (r) => {
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      for (const e of editableRects) {
        if (cx >= e.left - 320 && cx <= e.right + 320 && cy >= e.top - 320 && cy <= e.bottom + 320) return true;
      }
      return false;
    };
    const seen = new Set();
    const icons = Array.from(document.querySelectorAll('svg, img')).slice(0, 500);
    for (const ic of icons) {
      if (ic.closest && (ic.closest('#__alterego_root') || ic.closest('#__alterego_cursor') || ic.closest('#__ae_lock_toast') || ic.closest('#__ae_som'))) continue;
      const ir = ic.getBoundingClientRect();
      if (ir.width < 8 || ir.height < 8 || ir.width > 80 || ir.height > 80) continue;
      // 先从父级向上找真实可点热区（指针手型 / onclick / role=button / tabindex）；
      // cursor 是继承属性，svg 自己也会继承到手型，优先取祖先的热区矩形更贴近实际点击范围
      let node = ic.parentElement, target = null, covered = false;
      for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
        if (node.hasAttribute && node.hasAttribute('data-ae-ref')) { covered = true; break; } // 主通道已收录
        let cursor = '';
        try { cursor = window.getComputedStyle(node).cursor; } catch(e) {}
        if (cursor === 'pointer' || node.hasAttribute('onclick') || node.getAttribute('role') === 'button' || node.hasAttribute('tabindex')) { target = node; break; }
      }
      if (!target && !covered) {
        let iconCursor = '';
        try { iconCursor = window.getComputedStyle(ic).cursor; } catch(e) {}
        if (iconCursor === 'pointer' || ic.hasAttribute('onclick') || ic.getAttribute('role') === 'button') target = ic;
      }
      if (!target || seen.has(target)) continue;
      const rect = target.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8 || rect.width > 120 || rect.height > 120) continue;
      if (rect.bottom < 0 || rect.top > window.innerHeight) continue;
      const st = window.getComputedStyle(target);
      if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0' || st.pointerEvents === 'none') continue;
      if (((target.innerText || '').trim()).length > 0) continue; // 有文字的应由主通道覆盖
      if (!(nearEditable(rect) || (target.closest && target.closest('form')))) continue;
      seen.add(target);
      const label = (target.getAttribute('aria-label') || target.getAttribute('title') || ic.getAttribute('aria-label') || '').trim().slice(0, 40);
      const isModal = inModalOf(target);
      const refStr = 'ae' + id;
      try { target.setAttribute('data-ae-ref', refStr); } catch(e) {}
      paint(rect, isModal ? C.modal : C.click, id);
      results.push({
        ref: refStr,
        id: id++,
        tag: target.tagName.toLowerCase(),
        role: target.getAttribute('role') || 'icon',
        is_editable: false,
        in_modal: isModal,
        icon_only: true,
        name: label ? ('[图标按钮] ' + label) : '[图标按钮]',
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2),
        w: Math.round(rect.width),
        h: Math.round(rect.height)
      });
    }
  } catch(e) {}

  // ③ 瞬态显示：停留约 1.8s 后整层淡出（层保留在 DOM，可按需 pulse 回显）
  // 常驻编号框会挡住用户看页面（2026-10-28 携程登录页反馈），只用来看位置。
  try {
    const HOLD = (typeof holdMs === 'number' && holdMs > 0) ? holdMs : 1800;
    const hideLayer = () => {
      try {
        if (!layer || !layer.isConnected) return;
        layer.style.transition = 'opacity .45s ease, visibility 0s linear .45s';
        layer.style.opacity = '0';
        layer.style.visibility = 'hidden';
      } catch(e) {}
    };
    if (layer) {
      clearTimeout(window.__ae_som_timer);
      window.__ae_som_timer = setTimeout(hideLayer, HOLD);
    }
    window.__ae_som_pulse = (ms) => {
      try {
        if (!layer || !layer.isConnected) return false;
        clearTimeout(window.__ae_som_timer);
        layer.style.transition = 'none';
        layer.style.visibility = 'visible';
        layer.style.opacity = '1';
        void layer.offsetHeight;
        const hold = (typeof ms === 'number' && ms > 0) ? ms : HOLD;
        window.__ae_som_timer = setTimeout(hideLayer, hold);
        return true;
      } catch(e) { return false; }
    };
    window.__ae_som_off = () => {
      try { window.__ae_som_mo?.disconnect(); } catch(e) {}
      try { clearTimeout(window.__ae_som_timer); window.__ae_som_timer = null; } catch(e) {}
      document.getElementById('__ae_som')?.remove();
    };
  } catch(e) {}

  // 浮层内元素优先置顶；同等条件下图标按钮先列，确保发送/提交键不被 40 行截断
  results.sort((a, b) => {
    const am = a.in_modal ? 0 : 1, bm = b.in_modal ? 0 : 1;
    if (am !== bm) return am - bm;
    const ai = a.icon_only ? 0 : 1, bi = b.icon_only ? 0 : 1;
    if (ai !== bi) return ai - bi;
    return a.id - b.id;
  });
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

# 可编辑目标只认文本类控件：hidden/file/checkbox/radio/button 等不是输入目标。
# 2026-10-28 抖音评论事故：浮层内唯一 "input" 是隐藏的 input[type=file]（选图上传），
# fill_input 把它当成评论框，点击/聚焦全部落空。
_TEXT_EDITABLE_CSS = (
    'textarea:not([disabled]):not([readonly]):visible, '
    'input[type="text"]:not([disabled]):not([readonly]):visible, '
    'input[type="search"]:not([disabled]):not([readonly]):visible, '
    'input[type="email"]:not([disabled]):not([readonly]):visible, '
    'input[type="url"]:not([disabled]):not([readonly]):visible, '
    'input[type="tel"]:not([disabled]):not([readonly]):visible, '
    'input[type="password"]:not([disabled]):not([readonly]):visible, '
    'input[type="number"]:not([disabled]):not([readonly]):visible, '
    'input:not([type]):not([disabled]):not([readonly]):visible, '
    '[contenteditable="true"]:visible, '
    '[role="textbox"]:visible'
)

# §39.17 fill_input 目标完整性：显式目标打 data-ae-fill-target 标记，写入/回读只认这个节点。
# 历史事故：评论框点击未聚焦，全局兜底挑了 DOM 里第一个输入框（顶部搜索框），
# 评论写进了搜索框，回读却报「输入成功」。
_FILL_PICK_JS = """
        const describe = (n) => {
            if (!n) return 'none';
            const idStr = n.id ? '#' + n.id : '';
            let clsStr = '';
            if (typeof n.className === 'string' && n.className.trim()) {
                clsStr = '.' + n.className.trim().split(/\\s+/)[0];
            }
            return (n.tagName || '').toLowerCase() + idStr + clsStr;
        };
        const TEXT_TYPES = ['text', 'search', 'email', 'url', 'tel', 'password', 'number'];
        const usable = (n) => {
            if (!n || n.disabled || n.readOnly) return false;
            if (n.getAttribute('aria-hidden') === 'true') return false;
            let st = null;
            try { st = window.getComputedStyle(n); } catch(e) {}
            if (st && (st.display === 'none' || st.visibility === 'hidden')) return false;
            if (typeof n.getBoundingClientRect === 'function') {
                const r = n.getBoundingClientRect();
                if (r.width <= 0 && r.height <= 0) return false;
            }
            return true;
        };
        const textInput = (n) => {
            if (!n || n.tagName !== 'INPUT') return false;
            const t = (n.getAttribute('type') || 'text').toLowerCase();
            return TEXT_TYPES.indexOf(t) >= 0;
        };
        const editable = (n) => {
            if (!n || !usable(n)) return false;
            if (n.tagName === 'TEXTAREA' || textInput(n)) return true;
            return !!(n.isContentEditable || n.getAttribute('contenteditable') === 'true'
                || n.getAttribute('role') === 'textbox');
        };
        const descend = (n) => {
            if (!n) return null;
            if (editable(n)) return n;
            const cands = n.querySelectorAll('[contenteditable="true"], [contenteditable=""], [role="textbox"], textarea, input');
            for (const c of cands) {
                if (editable(c)) return c;
            }
            return null;
        };
        const mark = (node) => {
            if (!editable(node)) {
                return {ok: false, reason: 'not_editable', tag: describe(node)};
            }
            document.querySelectorAll('[data-ae-fill-target]').forEach(n => n.removeAttribute('data-ae-fill-target'));
            node.setAttribute('data-ae-fill-target', '1');
            try { node.scrollIntoView({block: 'center'}); } catch(e) {}
            try { node.focus({preventScroll: true}); } catch(e) {}
            const active = document.activeElement;
            const isInput = node.tagName === 'INPUT' || node.tagName === 'TEXTAREA';
            return {
                ok: true,
                is_input: isInput,
                is_contenteditable: !isInput,
                tag: describe(node),
                focused: active === node || node.contains(active),
                active_tag: describe(active),
            };
        };
"""

_FILL_MARK_JS = """(el) => { /*ae-fill-mark*/
""" + _FILL_PICK_JS + """
        return mark(descend(el));
    }"""

_FILL_ACTIVE_MARK_JS = """() => { /*ae-fill-mark*/
""" + _FILL_PICK_JS + """
        const el = document.activeElement;
        if (!el || el === document.body || el === document.documentElement) {
            return {ok: false, reason: 'no_focus', tag: 'none'};
        }
        return mark(descend(el));
    }"""

_FILL_POINT_MARK_JS = """([x, y]) => { /*ae-fill-mark*/
""" + _FILL_PICK_JS + """
        const hit = document.elementFromPoint(x, y);
        let cur = hit, node = null;
        while (cur && cur !== document.body && cur !== document.documentElement && !node) {
            node = descend(cur);
            cur = cur.parentElement;
        }
        if (node) return mark(node);
        return {ok: false, reason: 'not_editable', tag: hit ? describe(hit) : 'none'};
    }"""

# §39.23 懒挂载编辑器激活：评论区常是占位容器，真实编辑器要等一次完整指针事件链
# （pointerover/enter → mousedown → pointerup/mouseup → click）才由 React 挂载。
# Playwright locator.click 在隐藏 input[type=file] 抢占落点时可能只触发部分事件，
# 导致 contenteditable 始终不出现（2026-10-28 抖音评论事故）。
_ACTIVATE_CHAIN_JS = """
        const describe = (n) => {
            if (!n) return 'none';
            const idStr = n.id ? '#' + n.id : '';
            let clsStr = '';
            if (typeof n.className === 'string' && n.className.trim()) {
                clsStr = '.' + n.className.trim().split(/\\s+/)[0];
            }
            return (n.tagName || '').toLowerCase() + idStr + clsStr;
        };
        const dispatchChain = (node, x, y) => {
            const P = window.PointerEvent;
            const fire = (type, Ctor, buttons) => {
                try {
                    node.dispatchEvent(new Ctor(type, {
                        bubbles: true, cancelable: true, composed: true,
                        clientX: x, clientY: y, button: 0, buttons: buttons,
                        pointerId: 1, pointerType: 'mouse', isPrimary: true,
                    }));
                } catch(e) {}
            };
            if (P) { fire('pointerover', P, 0); fire('pointerenter', P, 0); }
            fire('mouseover', MouseEvent, 0);
            fire('mouseenter', MouseEvent, 0);
            if (P) fire('pointerdown', P, 1);
            fire('mousedown', MouseEvent, 1);
            if (P) fire('pointerup', P, 0);
            fire('mouseup', MouseEvent, 0);
            fire('click', MouseEvent, 0);
        };
"""

_ACTIVATE_NODE_JS = """(el) => { /*ae-activate*/
""" + _ACTIVATE_CHAIN_JS + """
        if (!el || !el.getBoundingClientRect) return {ok: false, reason: 'no_node'};
        try { el.scrollIntoView({block: 'center'}); } catch(e) {}
        const r = el.getBoundingClientRect();
        if (!(r.width > 0 && r.height > 0)) return {ok: false, reason: 'no_box', tag: describe(el)};
        const x = r.x + r.width / 2, y = r.y + r.height / 2;
        let hit = document.elementFromPoint(x, y);
        if (!hit || (hit !== el && !el.contains(hit))) hit = el;
        dispatchChain(hit, x, y);
        return {ok: true, tag: describe(el), hit: describe(hit), x: x, y: y};
    }"""

_ACTIVATE_POINT_JS = """([x, y]) => { /*ae-activate*/
""" + _ACTIVATE_CHAIN_JS + """
        let hit = document.elementFromPoint(x, y);
        if (hit && hit.closest && hit.closest('#__alterego_root, #__alterego_cursor, #__ae_som, #__ae_lock_toast')) {
            hit = document.elementFromPoint(x, y + 1) || null;
        }
        if (!hit) return {ok: false, reason: 'no_hit'};
        dispatchChain(hit, x, y);
        return {ok: true, tag: describe(hit), hit: describe(hit), x: x, y: y};
    }"""

_ACTIVE_TEXT_EDITABLE_JS = """() => { /*ae-active-editable*/
        const active = document.activeElement;
        if (!active || active === document.body || active === document.documentElement) return {found: false};
        const tag = (active.tagName || '').toLowerCase();
        const type = (active.getAttribute('type') || 'text').toLowerCase();
        const ok = tag === 'textarea'
            || (tag === 'input' && ['text', 'search', 'email', 'url', 'tel', 'password', 'number'].indexOf(type) >= 0)
            || active.isContentEditable || active.getAttribute('contenteditable') === 'true'
            || active.getAttribute('role') === 'textbox';
        return {found: !!ok, tag: tag};
    }"""

_FILL_STATE_JS = """() => { /*ae-fill-state*/
        const el = document.querySelector('[data-ae-fill-target]');
        if (!el) return {found: false};
        const describe = (n) => {
            if (!n) return 'none';
            const idStr = n.id ? '#' + n.id : '';
            let clsStr = '';
            if (typeof n.className === 'string' && n.className.trim()) {
                clsStr = '.' + n.className.trim().split(/\\s+/)[0];
            }
            return (n.tagName || '').toLowerCase() + idStr + clsStr;
        };
        const active = document.activeElement;
        const isInput = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA';
        const isCE = el.isContentEditable || el.getAttribute('contenteditable') === 'true'
            || el.getAttribute('role') === 'textbox';
        let val = '';
        try {
            val = isInput ? (el.value || '') : (el.innerText || el.textContent || '');
        } catch(e) {}
        return {
            found: true,
            focused: active === el || el.contains(active),
            tag: describe(el),
            active_tag: describe(active),
            is_input: isInput,
            is_contenteditable: isCE && !isInput,
            current_value: val.slice(0, 20000),
        };
    }"""

_EDITOR_STATE_JS = """() => { /*ae-editor-state*/
        const el = document.querySelector('[data-ae-fill-target]');
        if (!el) return {found: false};
        const isInput = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA';
        if (isInput) return {found: true, is_input: true, state_gated: false};
        const draftRoot = el.closest('.DraftEditor-root');
        const isDraft = !!draftRoot || !!el.closest('[class*="DraftEditor"]');
        const isProseMirror = !!el.closest('[class*="ProseMirror"]');
        // Draft.js 的占位符在编辑器 root 下、content 之外，scope 必须取 root 才看得见
        const scope = draftRoot || el.closest('[class*="ProseMirror"]') || el;
        const dataBlocks = el.querySelectorAll('[data-block]').length;
        const filledKids = Array.from(el.children || []).filter(ch => {
            try { return (ch.innerText || ch.textContent || '').trim().length > 0; } catch(e) { return false; }
        }).length;
        const blocks = dataBlocks || filledKids;
        const hasContents = !!el.querySelector('[data-contents]');
        const state_gated = isDraft || isProseMirror || dataBlocks > 0 || hasContents;
        let placeholderVisible = false, placeholderText = '';
        try {
            const nodes = scope.querySelectorAll('[data-placeholder], [class*="placeholder" i]');
            for (const n of nodes) {
                const r = n.getBoundingClientRect();
                const st = window.getComputedStyle(n);
                if (r.width > 0 && r.height > 0 && st.display !== 'none'
                    && st.visibility !== 'hidden' && parseFloat(st.opacity || '1') > 0) {
                    placeholderVisible = true;
                    placeholderText = (n.innerText || n.getAttribute('data-placeholder') || '').trim().slice(0, 60);
                    break;
                }
            }
        } catch(e) {}
        return {
            found: true,
            is_input: false,
            state_gated: state_gated,
            blocks: blocks,
            placeholder_visible: placeholderVisible,
            placeholder_text: placeholderText,
            text: String(el.innerText || el.textContent || '').slice(0, 20000),
        };
    }"""

_REACT_PROBE_JS = """(el) => { /*ae-react-probe*/
        const out = {ok: false, framework: '', tag: '', text: '', disabled: false,
                     chain: [], handlers: [], handler_nodes: [], hook_owner: '', hooks: []};
        if (!el) return out;
        out.ok = true;
        try { out.tag = (el.tagName || '').toLowerCase(); } catch (e) {}
        try { out.text = String(el.innerText || el.value || '').trim().slice(0, 60); } catch (e) {}
        try { out.disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true'); } catch (e) {}
        const own = Object.getOwnPropertyNames(el);
        // React detection
        const fiberKey = own.find(k => k.startsWith('__reactFiber$'))
                      || own.find(k => k.startsWith('__reactInternalInstance$'));
        const propsKey = own.find(k => k.startsWith('__reactProps$'));
        const props = propsKey ? el[propsKey] : null;
        // Vue detection
        const vueKey = own.find(k => k === '__vue__' || k === '__vueParentComponent');
        const vueInst = vueKey ? el[vueKey] : null;
        if (!fiberKey && !props && !vueKey) return out;
        // Determine framework
        if (fiberKey || props) {
            out.framework = 'react';
        } else if (vueKey) {
            out.framework = 'vue';
        }
        const label = (node) => {
            if (!node || !node.type) return '?';
            if (typeof node.type === 'string') return '<' + node.type + '>';
            return node.type.displayName || node.type.name || 'anon';
        };
        const handlersOf = (p) => p ? Object.keys(p).filter(
            k => /^on[A-Z]/.test(k) && typeof p[k] === 'function') : [];
        // React path
        if (out.framework === 'react') {
            out.handlers = handlersOf(props).slice(0, 8);
            let f = fiberKey ? el[fiberKey] : null;
            let guard = 0;
            while (f && guard++ < 32) {
                const name = label(f);
                if (name !== '?' && out.chain.length < 8) out.chain.push(name);
                const hs = handlersOf(f.memoizedProps);
                if (hs.length && out.handler_nodes.length < 4) {
                    out.handler_nodes.push(name + ':' + hs.slice(0, 3).join('/'));
                }
                f = f.return;
            }
            let g = fiberKey ? el[fiberKey] : null;
            guard = 0;
            while (g && guard++ < 32 && typeof g.type !== 'function') g = g.return;
            if (g && typeof g.type === 'function') {
                out.hook_owner = label(g);
                let h = g.memoizedState, i = 0;
                while (h && i < 10) {
                    let v = h.memoizedState, r, raw = false;
                    const t = typeof v;
                    if (v === null || t === 'string' || t === 'number' || t === 'boolean') {
                        r = (t === 'string' && v.length > 40) ? v.slice(0, 40) + '…' : v;
                    } else if (Array.isArray(v)) {
                        r = '[' + v.length + ']'; raw = true;
                    } else if (t === 'object') {
                        const ks = Object.keys(v);
                        if (ks.includes('create') || ks.includes('destroy')) { r = '(effect)'; raw = true; }
                        else if (ks.includes('current')) { r = '(ref)'; raw = true; }
                        else { r = '{' + ks.slice(0, 5).join(',') + '}'; raw = true; }
                    } else {
                        r = t;
                    }
                    let sv;
                    try { sv = raw ? String(r) : JSON.stringify(r); } catch (e) { sv = String(r); }
                    out.hooks.push(i + '=' + sv);
                    h = h.next; i++;
                }
            }
        }
        // Vue path
        else if (out.framework === 'vue') {
            // Vue 3: __vueParentComponent has .type (component def) and .props
            // Vue 2: __vue__ has .$options, .$props, .$emit
            const comp = vueInst.type ? vueInst : (vueInst.$options ? vueInst : null);
            if (comp) {
                const name = comp.type?.name || comp.type?.displayName || comp.$options?.name || 'anon';
                out.chain.push(name);
                out.hook_owner = name;
                // Vue props
                const vprops = comp.type?.props || comp.$options?.props || {};
                const propKeys = Object.keys(vprops).slice(0, 8);
                if (propKeys.length) out.handlers.push('props:' + propKeys.join(','));
                // Vue emits/events
                const emits = comp.type?.emits || comp.$options?.emits || [];
                if (Array.isArray(emits) && emits.length) {
                    out.handlers.push('emits:' + emits.slice(0, 5).join(','));
                }
            }
        }
        return out;
    }"""

_FILL_RESET_JS = """() => { /*ae-fill-reset*/
        document.querySelectorAll('[data-ae-fill-target]').forEach(el => el.removeAttribute('data-ae-fill-target'));
        return true;
    }"""

_FILL_CLEAR_JS = """(isInput) => { /*ae-fill-clear*/
        const el = document.querySelector('[data-ae-fill-target]');
        if (!el) return false;
        if (isInput) {
            el.value = '';
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        } else {
            try {
                const range = document.createRange();
                range.selectNodeContents(el);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand('delete', false, null);
            } catch(e) {}
            if (el.innerText && el.innerText.trim()) {
                el.innerText = '';
                el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
            }
        }
        return true;
    }"""

_FILL_WRITE_INPUT_JS = """(t) => { /*ae-fill-write-input*/
        const el = document.querySelector('[data-ae-fill-target]');
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
    }"""

_FILL_WRITE_CE_JS = """(t) => { /*ae-fill-write-ce*/
        const el = document.querySelector('[data-ae-fill-target]');
        if (!el) return false;
        try { el.focus(); } catch(e) {}
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
    }"""

_FILL_FOCUS_JS = """() => { /*ae-fill-focus*/
        const el = document.querySelector('[data-ae-fill-target]');
        if (!el) return false;
        try { el.focus({preventScroll: false}); } catch(e) {}
        return true;
    }"""

_FILL_BLUR_JS = """() => { /*ae-fill-blur*/
        const el = document.querySelector('[data-ae-fill-target]');
        if (el) el.dispatchEvent(new Event('blur', {bubbles: true}));
        return true;
    }"""

_FILL_IN_MODAL_JS = """(sel) => { /*ae-fill-in-modal*/
        try {
            const el = document.querySelector('[data-ae-fill-target]');
            const modal = sel ? document.querySelector(sel) : null;
            return !!(el && modal && (modal === el || modal.contains(el)));
        } catch(e) { return false; }
    }"""


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
        # 工具调用超时看护：卡死标记 + 自动重启内核（避免一次 CDP 挂起拖死整轮）
        self._wedged: dict | None = None
        self._wedged_lock = threading.Lock()
        self._recover_lock = threading.Lock()
        self._recover_history: list[float] = []
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
        # v2.6 重定向熔断：按 (netloc, 请求路径) 累计"请求列表页却落到别处"次数
        self._redirect_hist: dict = {}
        # 诊断地基（console + 网络日志）：页面静默失败时唯一的真相来源。
        # run5 教训：Agnes 的 .catch() 吞掉异常，页面无变化、无 toast——
        # 只有 console/pageerror 和网络日志能说清发生了什么。
        # 懒初始化（测试用 __new__ 绕过 __init__ 也能工作）。
        self._diag_pages = set()
        # 浏览器独占租约仲裁（防止主 Agent 与子 Agent 并发踩踏）
        self._lease_owner: str | None = None
        self._lease_lock = threading.Lock()
        # §10.21 通道升级（C1 树 → C2 视觉 → C3 OS 键鼠 → C4 人工）：
        # key = "通道|目标|意图" -> {"fails": 连续无进展次数, "forced": 强制升级到的通道}
        self._channel_progress: dict = {}

    def acquire_lease(self, owner_id: str, timeout_sec: float = 300.0) -> bool:
        """申领浏览器独占租约（如 'main' 或 'sub:<task_id>'），默认排队 5 分钟（§5.9）。"""
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

    # 自动重启频控：10 分钟内最多重启 2 次，防止死循环重启
    MAX_AUTO_RECOVERIES = 2
    RECOVER_WINDOW_SEC = 600.0
    # 页面内发起的接管：单次浏览器调用最多暂停等待这么久，避免子 Agent 的 420s 看门狗误杀；
    # 超时不解绑请求，下一次浏览器调用会继续等（用户随时可点【完成接管，继续任务】）。
    PAGE_TAKEOVER_WAIT_SEC = 300.0

    def _lock_attr(self, name: str) -> threading.Lock:
        """测试用 __new__ 绕过 __init__ 时按需补锁。"""
        lock = getattr(self, name, None)
        if lock is None:
            lock = threading.Lock()
            setattr(self, name, lock)
        return lock

    def _mark_wedged(self, fn, timeout: float):
        tool = getattr(fn, "__name__", "browser_operation")
        if tool.startswith("_do_"):
            tool = tool[4:]
        with self._lock_attr("_wedged_lock"):
            self._wedged = {"since": time.time(), "tool": tool, "timeout": timeout}
        logger.warning("[browser] %s 超过 %gs 未返回，标记内核卡死", tool, timeout)

    def _recover_if_wedged(self) -> str:
        """上次调用超时后，在下一次浏览器调用前重启内核；返回给模型的可见说明。"""
        with self._lock_attr("_wedged_lock"):
            wedged = getattr(self, "_wedged", None)
        if not wedged:
            return ""
        with self._lock_attr("_recover_lock"):
            now = time.time()
            history = [t for t in getattr(self, "_recover_history", [])
                       if now - t < self.RECOVER_WINDOW_SEC]
            if len(history) >= self.MAX_AUTO_RECOVERIES:
                self._recover_history = history
                raise BrowserCallTimeout(
                    f"[浏览器内核反复卡死] 最近 {int(self.RECOVER_WINDOW_SEC / 60)} 分钟已自动重启 "
                    f"{len(history)} 次仍再次卡死，已停止自动恢复。"
                    "请向用户说明浏览器自动化当前不可用，需要人工接管或重启应用后再试。")
            history.append(now)
            self._recover_history = history
            logger.warning("[browser] 检测到内核卡死（%s 超时约 %.0fs），自动重启",
                           wedged.get("tool"), wedged.get("timeout") or 0)
            self._restart_kernel()
            with self._lock_attr("_wedged_lock"):
                self._wedged = None
        timeout = float(wedged.get("timeout") or 0.0)
        since = float(wedged.get("since") or time.time())
        # since 是"检测到超时"的时刻，真实等待还要加上超时上限本身
        waited = int(timeout + max(0.0, time.time() - since))
        limit = f"（等待上限 {int(timeout)} 秒）" if timeout else ""
        return (f"[浏览器内核已自动重启] 上次 {wedged.get('tool')} 调用卡死超时{limit}，"
                f"累计等待约 {waited} 秒；"
                "标签页与登录态可能已丢失，请重新打开目标页并截图确认页面状态。")

    def _restart_kernel(self):
        """丢弃卡死的工作线程与驱动进程，换新执行器重新启动浏览器。"""
        old_executor = getattr(self, "_executor", None)
        self._kill_playwright_driver()
        if old_executor is not None:
            # 不等待卡死线程退出：恢复动作不能被它二次拖死
            try:
                old_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"pw_{getattr(self, 'profile', 'default')}")
        self._pw = None
        self._browser = None
        self._raw_page = None
        for prof in (getattr(self, "_profiles", {}) or {}).values():
            try:
                prof["browser"] = None
                prof["context"] = None
                prof["tabs"] = {}
            except Exception:
                continue
        future = self._executor.submit(self._do_start)
        try:
            future.result(timeout=_configured_call_timeout())
        except FutureTimeoutError:
            raise BrowserCallTimeout(
                "[浏览器内核重启失败] 新内核启动超过等待上限仍未就绪。"
                "请告知用户浏览器自动化暂不可用，建议人工接管或重启应用。") from None
        except Exception as exc:
            raise BrowserCallTimeout(
                f"[浏览器内核重启失败] {type(exc).__name__}: {exc}。"
                "请告知用户浏览器自动化暂不可用，建议人工接管或重启应用。") from None

    def _kill_playwright_driver(self):
        """强杀 Playwright 驱动及其 Chromium 子进程，让卡死的调用尽快抛错。"""
        pw = getattr(self, "_pw", None)
        pid = None
        try:
            pid = int(pw._connection._transport._proc.pid)
        except Exception:
            pid = None
        try:
            import psutil
            if not pid:
                me = psutil.Process()
                for child in me.children(recursive=True):
                    try:
                        cmd = " ".join(child.cmdline()).lower()
                    except Exception:
                        continue
                    if "playwright" in cmd and "run-driver" in cmd:
                        pid = child.pid
                        break
            if not pid:
                return
            proc = psutil.Process(pid)
            victims = proc.children(recursive=True) + [proc]
            for item in victims:
                try:
                    item.terminate()
                except Exception:
                    pass
            _, alive = psutil.wait_procs(victims, timeout=3)
            for item in alive:
                try:
                    item.kill()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("[browser] 回收驱动进程失败: %s", type(exc).__name__)

    def _run(self, fn, *args, _timeout: float | None = None, **kwargs):
        """将 Playwright 操作委托给固定工作线程执行，并给等待设上限。"""
        import threading
        thread_prefix = f"pw_{getattr(self, 'profile', 'default')}"
        if threading.current_thread().name.startswith(thread_prefix):
            return fn(*args, **kwargs)
        caller_id = getattr(threading.current_thread(), "_lease_caller_id", None)
        if hasattr(self, "check_lease") and not self.check_lease(caller_id):
            owner = self.current_lease_owner() or "未知"
            raise RuntimeError(
                f"浏览器租约冲突：当前由 {owner} 独占（单写者）。"
                "请先 poll_subagent 或等待其结束，不要取消健康子任务抢浏览器。")
        recovered_note = self._recover_if_wedged()
        timeout = _timeout if (_timeout and _timeout > 0) else _configured_call_timeout()
        future = self._executor.submit(self._call_with_page_takeover, timeout, fn, *args, **kwargs)
        try:
            result = future.result(timeout=timeout)
        except FutureTimeoutError:
            self._mark_wedged(fn, timeout)
            tool = getattr(fn, "__name__", "browser_operation")
            if tool.startswith("_do_"):
                tool = tool[4:]
            raise BrowserCallTimeout(
                f"[工具超时] {tool} 超过 {timeout:g} 秒未返回，已停止等待。"
                "Playwright/CDP 调用被阻塞（页面假死、驱动失联或原生弹窗占用）。"
                "不要重试同一操作；下一次 browser_* 调用会自动重启浏览器内核，"
                "重启后先用 browser_navigate 打开目标页并截图确认状态。") from None
        if recovered_note:
            if isinstance(result, str):
                result = recovered_note + "\n" + result
            elif isinstance(result, Message):
                result.content = recovered_note + "\n" + (result.content or "")
        return result

    # ---------- §39.24 页内接管：泵式等待与空闲派发 ----------

    def _call_with_page_takeover(self, timeout: float, fn, *args, **kwargs):
        """在浏览器工作线程上执行工具，并处理用户从网页横幅发起的接管。

        失效场景（2026-10-28 豆包事故）：用户在 Agent 页面点【申请人工接管】，
        绑定回调只在有 Playwright 调用在飞时才被投递；若没人消费，请求会一直挂着，
        且 Agent 会继续操作页面与用户抢输入。这里在每次浏览器调用前后各检查一次：
        调用前发现请求就先暂停，调用期间投递的请求在调用结束后消费。
        暂停预算必须留在 _run 的等待上限之内，否则会被误判成内核卡死并触发自动重启。
        """
        if getattr(fn, "__name__", "") in ("_do_stop", "_do_start"):
            return fn(*args, **kwargs)  # 生命周期调用不能被用户接管暂停拖住
        deadline = time.monotonic() + max(1.0, timeout - 15.0)
        self._flush_page_bindings()
        pause_note = self._page_takeover_pause(deadline)
        if pause_note:
            return pause_note
        result = fn(*args, **kwargs)
        self._flush_page_bindings()
        pause_note = self._page_takeover_pause(deadline)
        if pause_note and isinstance(result, str):
            return pause_note + "\n" + result
        return result

    def _flush_page_bindings(self) -> None:
        """让 Playwright 事件循环转一圈，把网页横幅里排队的按钮回调投递到 Python。"""
        if getattr(self, "_input_guard", None) is None:
            return
        page = getattr(self, "_raw_page", None)
        if page is None:
            return
        try:
            page.wait_for_timeout(20)
        except Exception:
            pass

    def _page_takeover_pause(self, deadline: float | None = None) -> str:
        """消费页内接管请求；返回非空表示自动化必须暂停（内容给模型看）。"""
        guard = getattr(self, "_input_guard", None)
        if guard is None:
            return ""
        tid = guard.peek_page_request()
        if not tid:
            return ""
        budget = self.PAGE_TAKEOVER_WAIT_SEC
        if deadline is not None:
            budget = min(budget, deadline - time.monotonic())
        if budget <= 0:
            return ""
        from agent.takeover import get_takeover_manager
        mgr = get_takeover_manager()
        res = self._pump_wait_takeover(mgr, tid, budget, resolve_on_timeout=False)
        if res.outcome == "completed":
            guard.take_page_request()
            self.lock_input_for_agent()
            return ""
        if res.outcome == "cancelled":
            guard.take_page_request()
            self.release_input_guard()
            return ("[人工接管已取消] 用户在网页横幅内取消了接管，"
                    "请停止当前浏览器自动化并向用户确认下一步。")
        return ("[用户正在接管中] 用户已在网页横幅申请人工接管，自动化已暂停；"
                "不要继续操作页面，等待用户点击【完成接管，继续任务】后再继续。")

    def _pump_wait_takeover(self, mgr, task_id: str, timeout_sec: float,
                            *, resolve_on_timeout: bool = True):
        """泵式等待接管结果：保持 Playwright 事件循环运转，页内按钮点击才能投递进 Python。

        §39.24：旧实现直接 threading.Event.wait，等待期间没有任何 Playwright 调用，
        expose_binding 回调被排队到超时之后，用户点【✓ 完成接管】永远没反应。
        """
        from agent.takeover import TakeoverResult

        deadline = time.monotonic() + max(0.0, timeout_sec)
        page = getattr(self, "_raw_page", None)
        while True:
            res = mgr.poll(task_id)
            if res is not None:
                return res
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if resolve_on_timeout:
                    return mgr.resolve(task_id, "timeout", "用户接管超时")
                return TakeoverResult(task_id=task_id, outcome="timeout",
                                      summary="等待用户接管超时（请求仍有效）")
            step = min(0.25, remaining)
            pumped = False
            try:
                if page is not None and not page.is_closed():
                    page.wait_for_timeout(step * 1000.0)
                    pumped = True
            except Exception:
                pumped = False
            if not pumped:
                time.sleep(step)

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
        # 演示与运行加固：主动清理陈旧的 Chromium 单例锁，防止崩溃遗留锁导致启动失败
        def _clean_stale_profile_locks(pdir):
            for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lf = Path(pdir) / lock_name
                if lf.exists():
                    try:
                        lf.unlink(missing_ok=True)
                    except Exception:
                        pass

        pdir = Path(self.profile_mgr.user_data_dir(self.profile))
        _clean_stale_profile_locks(pdir)

        if sys.platform == "win32":
            try:
                self._browser = self._pw.chromium.launch_persistent_context(
                    str(pdir),
                    channel="msedge",
                    **launch_kwargs,
                )
            except Exception as e_edge:
                logger.warning("[browser] Edge persistent context 启动受阻: %s，尝试 Chrome 回退", e_edge)
                try:
                    self._browser = self._pw.chromium.launch_persistent_context(
                        str(pdir),
                        channel="chrome",
                        **launch_kwargs,
                    )
                except Exception as e_chrome:
                    logger.warning("[browser] Chrome 启动受阻: %s，尝试内置 Chromium 回退", e_chrome)
                    try:
                        self._browser = self._pw.chromium.launch_persistent_context(
                            str(pdir),
                            **launch_kwargs,
                        )
                    except Exception as e_chrom:
                        # 终极演示容灾：若宿主机有 Edge 正占用该 profile，立即启用独立临时目录，保证路演 100% 成功拉起
                        logger.warning("[browser] Profile 目录被占用: %s，立即切换至独立应急 Profile", e_chrom)
                        fallback_dir = pdir.parent / f"{pdir.name}_session_{int(time.time())}"
                        fallback_dir.mkdir(parents=True, exist_ok=True)
                        self._browser = self._pw.chromium.launch_persistent_context(
                            str(fallback_dir),
                            channel="msedge",
                            **launch_kwargs,
                        )
        else:
            try:
                self._browser = self._pw.chromium.launch_persistent_context(
                    str(pdir),
                    **launch_kwargs,
                )
            except Exception as e_posix:
                fallback_dir = pdir.parent / f"{pdir.name}_session_{int(time.time())}"
                fallback_dir.mkdir(parents=True, exist_ok=True)
                self._browser = self._pw.chromium.launch_persistent_context(
                    str(fallback_dir),
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
            if getattr(self, "_wedged", None):
                # 卡死状态下不能再排队等旧线程，直接回收驱动
                self._kill_playwright_driver()
            else:
                self._run(self._do_stop)
        except Exception:
            self._kill_playwright_driver()
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
            self._input_guard = (BrowserInputGuard(val, on_mode_change=self._on_guard_mode_change)
                                 if val else None)
            # 若之前处于锁定状态，新页面自动继承蓝紫锁定态
            if self._input_guard and getattr(self, "_guard_locked", False):
                self.lock_input_for_agent()
        except Exception:
            self._input_guard = None

    def _on_guard_mode_change(self, mode: str) -> None:
        """页内按钮切换接管态时同步锁标记（纯内存；禁止任何 Playwright 调用）。"""
        self._guard_locked = mode == "AGENT_ACTIVE"

    def lock_input_for_agent(self) -> bool:
        """§15 running 态：锁定人类物理输入，Agent 独占。"""
        guard = getattr(self, "_input_guard", None)
        ok = guard.lock_for_agent() if guard else False
        self._guard_locked = bool(ok)
        return ok

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

    def _do_lock_input_tool(self) -> str:
        """browser_lock_input 工具体：必须经 _run 在 Playwright 线程执行。"""
        if not getattr(self, "_input_guard", None):
            return "[锁定失败] 无可用页面"
        if self.lock_input_for_agent():
            return ("[DOM 防误触已启用] 网页内容区已拦截物理键鼠输入，"
                    "顶部保留人工接管交互条。")
        return "[锁定失败] 页面未就绪或遮罩注入被拒，请先打开目标页面后重试"

    def _do_unlock_input_tool(self) -> str:
        """browser_unlock_input 工具体：必须经 _run 在 Playwright 线程执行。"""
        if not getattr(self, "_input_guard", None):
            return "[解锁失败] 无可用页面"
        if self.unlock_input_for_human():
            return "[DOM 防误触已解除] 用户可自由交互操作页面。"
        return "[解锁失败] 页面未就绪或遮罩注入被拒"

    def _clean_screenshot(self, clean: bool = False, **kwargs) -> bytes:
        """纯净截图：临时彻底隐藏 AlterEgo 遮罩、胶囊横幅和流光边框，截取真实原始网页后恢复。
        若 clean=True，额外移除 SoM 框选层（__ae_som），返回无任何标注的纯原图。
        默认 clean=False 时保留 SoM 框选，供大模型视觉理解与编号直达；框选层平时自动淡出
        （v1.5.26），截图前用 __ae_som_pulse() 瞬时回显，保证画面里带编号。
        """
        raw_page = getattr(self, "_raw_page", None)
        if not raw_page:
            return b""
        if not clean:
            try:
                raw_page.evaluate(
                    "() => { try { return window.__ae_som_pulse ? window.__ae_som_pulse(1200) : false; }"
                    " catch (e) { return false; } }")
            except Exception:
                pass
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
                if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0'
                    && style.pointerEvents !== 'none') {
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

            // 2. 常见 SPA 浮层容器（class 命名特征 + 位置/层级 + 真实可交互）
            // 收紧：portal/popover/toast/menu 等常驻容器不是模态浮层；候选还必须
            // 有真实内容、可接收指针事件，且在自身中心点确实是最上层元素
            // （否则只是被遮挡/透明的背景壳，曾把 #im-entry-xxx-popup-portal 误判为浮层）。
            const classKeywords = ['modal', 'dialog', 'drawer', 'popup', 'lightbox', 'undertake', 'detail-mask', 'note-detail'];
            const classExcludes = ['portal', 'popover', 'dropdown', 'toast', 'tooltip', 'notice', 'notification', 'menu', 'im-entry'];
            const candidates = [];
            const allDivs = document.querySelectorAll('div, section, article, aside');
            for (const el of allDivs) {
              if (el.closest && (el.closest('#__alterego_root') || el.closest('#__alterego_cursor'))) continue;
              const cls = (typeof el.className === 'string' ? el.className : '').toLowerCase();
              const id = (el.id || '').toLowerCase();
              if (classExcludes.some(kw => cls.includes(kw) || id.includes(kw))) continue;
              const matched = classKeywords.some(kw => cls.includes(kw) || id.includes(kw));
              if (!matched) continue;
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
              if (style.pointerEvents === 'none') continue;
              const pos = style.position;
              if (pos !== 'fixed' && pos !== 'absolute') continue;
              const z = parseInt(style.zIndex, 10) || 0;
              const rect = el.getBoundingClientRect();
              if (!(rect.width >= 260 && rect.height >= 180 && (z >= 5 || pos === 'fixed'))) continue;
              const text = (el.innerText || '').trim();
              const hasContent = text.length >= 2 || !!el.querySelector(
                'input, textarea, button, a, img, [contenteditable="true"], [role="button"], [role="textbox"]');
              if (!hasContent) continue;
              const top = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
              if (!top || !(top === el || el.contains(top))) continue;
              candidates.push({ el, rect, z });
            }

            if (candidates.length > 0) {
              candidates.sort((a, b) => (b.z - a.z));
              for (const best of candidates) {
                const el = best.el;
                let sel = '';
                if (el.id) {
                  sel = '#' + CSS.escape(el.id);
                } else if (el.className && typeof el.className === 'string') {
                  const primaryCls = el.className.trim().split(/\\s+/).filter(c => !c.includes(':') && !c.includes('/') && !c.includes('[') && !c.includes(']'))[0];
                  if (primaryCls) sel = '.' + CSS.escape(primaryCls);
                }
                // 没有稳定的 id/class 就不能可靠限定浮层作用域：不虚构 tag 选择器
                if (!sel) continue;
                return {
                  has_modal: true,
                  selector: sel,
                  title: el.querySelector('h1, h2, h3, header, [class*="title"]')?.innerText?.slice(0, 50) || '',
                  rect: { x: Math.round(best.rect.x), y: Math.round(best.rect.y), width: Math.round(best.rect.width), height: Math.round(best.rect.height) },
                  zIndex: best.z
                };
              }
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

    @staticmethod
    def _react_probe_text(state: dict | None, limit: int = 420) -> str:
        """把组件探针结果压成一行可读摘要（给模型看，也给闸门自动附带）。"""
        if not isinstance(state, dict) or not state.get("ok"):
            return ""
        framework = state.get("framework") or ""
        if not framework:
            return "组件探针：目标不是 React/Vue 节点（或该页未使用已知框架）。"
        parts = []
        chain = state.get("chain") or []
        if chain:
            parts.append("组件链=" + " < ".join(chain[:6]))
        handlers = state.get("handlers") or []
        if handlers:
            parts.append("元素事件=" + "/".join(handlers))
        else:
            parts.append("元素事件=无（handler 不在该节点上）")
        hnodes = state.get("handler_nodes") or []
        if hnodes:
            parts.append("事件绑定=" + "；".join(hnodes))
        owner = state.get("hook_owner") or ""
        if owner:
            hooks = "、".join(state.get("hooks") or []) or "无"
            parts.append(f"{owner} hooks: {hooks}")
        if state.get("disabled"):
            parts.append("disabled=true")
        text = f"{framework.upper()} 探针：" + "；".join(parts)
        return text[:limit]

    def _react_probe_el(self, locator) -> dict | None:
        """读取目标元素的 React fiber/props 状态。元素级取证，不依赖选择器重定位。"""
        if locator is None:
            return None
        try:
            return locator.first.evaluate(_REACT_PROBE_JS)
        except Exception:
            return None

    def _react_probe_at(self, ax: float, ay: float) -> dict | None:
        """按 CSS 像素坐标探针：坐标点击路径拿不到 locator 时用。"""
        try:
            js = ("(pt) => { const el = document.elementFromPoint(pt[0], pt[1]);"
                  " return el ? (" + _REACT_PROBE_JS + ")(el) : null; }")
            return self._raw_page.evaluate(js, [ax, ay])
        except Exception:
            return None

    def _submit_gate_hint(self, before_fp: str, api_all: list | None = None,
                          probe: dict | None = None) -> str:
        """提交闸门：点中了提交类按钮、URL 却没变。
        旧版一律归因"前端校验静默拦截 + 联想框没选中"，对 AJAX 评论/发布是纯误导——
        这些按钮本来就不跳转。现在按点击期间抓到的 XHR/fetch 分流：
        - 零请求：handler 没执行或被前置校验静默拦截（点了个寂寞），必须换证据/换路径；
        - 有请求：AJAX 提交成功与否看业务结果，URL 不是判据。"""
        try:
            url_now = str(self._raw_page.url or "")
        except Exception:
            return ""
        url_before = (before_fp or "").split("#")[0]
        if not url_before or url_now != url_before:
            return ""
        detail = self._react_probe_text(probe)
        detail_note = f" {detail}" if detail else ""
        if api_all is None:
            return (" [提交闸门·URL未变] 该点击没有引起 URL 变化。AJAX 提交本来就不跳转，"
                    "URL 不是提交成功的判据；先用 browser_network_log 确认请求到底发没发，"
                    "再去业务侧核验结果，不要重复点击。")
        reqs = list(api_all or [])
        if not reqs:
            return (" [提交闸门·零请求] 点击后没有捕获到任何 XHR/fetch 请求：提交 handler 没有被触发，"
                    "或被页面前置校验静默拦截（DOM 有字 ≠ 组件状态有效），点了等于没点。"
                    f"{detail_note}"
                    " 不要重复点击同一按钮：① 用 browser_react_probe 核对事件绑定与组件状态"
                    "（文本/提交中/禁用标志）；② 换等价提交路径（Enter/Ctrl+Enter/表单 submit）；"
                    "③ 若提交标志疑似卡死，重新加载页面后重新输入再提交；"
                    "④ 若是联想输入框只写了显示值，先 browser_fill_combo 真正选中候选。")
        sample = "；".join(r[:80] for r in reqs[:3])
        return (f" [提交闸门·URL未变] 点击后已发出 {len(reqs)} 条 XHR/fetch（{sample}），"
                "URL 未变属正常——AJAX 提交本来就不跳转，URL 不是判据。"
                "去业务侧核验（列表新增/计数变化/成功提示/接口响应），不要重复点击。")

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
    def _norm_query(query: str) -> str:
        """查询参数规范化：排序 + 统一编码，使同义变体命中同一个计数器。"""
        from urllib.parse import parse_qsl, urlencode
        try:
            pairs = parse_qsl(query or "", keep_blank_values=True)
            pairs.sort()
            return urlencode(pairs)
        except Exception:
            return query or ""

    @staticmethod
    def _redirect_feedback(requested: str, landed: str, hist: dict) -> str:
        """v2.7 修复：导航返回前的落地对比（纯函数，hist 为调用方持有的计数器）。

        背景（2026-10-22 复盘）：站点把深链静默重定向回首页，工具回报"已打开"；
        且旧实现只用 (netloc, path) 作键，某个查询参数变体成功落地会清空同路径
        所有变体的计数，坏 URL 形态可以无限重试绕过熔断。

        现在按两个维度分别累计：
        - exact: (netloc, path, 规范化查询参数) —— 同一 URL 形态
        - path:  (netloc, path)                  —— 同路径下所有变体
        成功落地只清自己的 exact 键与该 path 键，不碰其它形态的累计。
        """
        from urllib.parse import urlsplit
        try:
            r, l = urlsplit(requested or ""), urlsplit(landed or "")
        except Exception:
            return ""
        if not r.netloc or not l.netloc or r.netloc != l.netloc:
            return ""  # 跨域跳转多为登录/正常流程，不干预
        rp = (r.path or "/").rstrip("/") or "/"
        lp = (l.path or "/").rstrip("/") or "/"
        exact_key = ("exact", r.netloc, rp, BrowserWorker._norm_query(r.query))
        path_key = ("path", r.netloc, rp)
        if rp == lp:
            hist.pop(exact_key, None)  # 最终落到所求路径，只重置本形态
            hist.pop(path_key, None)
            return ""
        exact_n = int(hist.get(exact_key, 0)) + 1
        path_n = int(hist.get(path_key, 0)) + 1
        hist[exact_key] = exact_n
        hist[path_key] = path_n
        note = (f"\n[URL重定向] 请求路径 {rp} 实际落在 {lp}（站点静默重定向，"
                f"\"[已打开]\"≠\"打开的是你要的页\"，请先核对当前页面再操作）")
        if exact_n >= 2 or path_n >= 2:
            note += (f"\n[重复重定向熔断·铁律17] 该 URL 形态已累计 {exact_n} 次被重定向"
                     f"（同路径累计 {path_n} 次）。禁止再次直接拼接同类 URL 重试；"
                     "改用其它入口（站点首页表单/站内搜索/点击链接），"
                     "或 browser_request_takeover 人工接管。")
        return note

    def _landing_note(self, requested: str) -> str:
        """导航成功路径统一调用：读取真实落地 URL 并生成重定向反馈。"""
        try:
            landed = getattr(self._raw_page, "url", "") or ""
        except Exception:
            landed = ""
        hist = getattr(self, "_redirect_hist", None)
        if hist is None:
            hist = self._redirect_hist = {}
        return self._redirect_feedback(requested, landed, hist)

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

    def _check_url_security(self, url: str) -> str | None:
        """SEC-05: 导航安全与 SSRF/元数据/调试端口/敏感文件拦截。"""
        from urllib.parse import urlparse, unquote
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()

        # 1. 协议白名单 (空 scheme 在补齐前不直接拦截)
        if scheme and scheme not in ("http", "https", "about", "file"):
            return f"[导航拦截] 不受支持的协议: '{scheme}'，仅支持 http, https, file, about"

        # 2. 本地文件 file:// 路径检查
        if scheme == "file":
            raw_path = unquote(parsed.path)
            if raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
                raw_path = raw_path[1:]
            path_str = raw_path.replace("\\", "/").lower()
            sensitive_patterns = (
                "system32", "etc/passwd", "etc/shadow", ".ssh", "id_rsa", ".env", "vault/llm_api_key.bin"
            )
            if any(pat in path_str for pat in sensitive_patterns):
                return "[导航拦截] 禁止通过本地文件协议访问系统或密钥敏感路径"
            return None

        # 3. HTTP / HTTPS 检查
        if scheme in ("http", "https"):
            host = (parsed.hostname or "").lower()
            port = parsed.port

            # 阻止云元数据服务 (AWS, GCP, Azure 等)
            if host in ("169.254.169.254", "metadata.google.internal", "instance-data") or host.startswith("169.254."):
                return "[域名不在白名单] 目标包含云元数据敏感地址 (169.254.x.x)，禁止访问"

            # 阻止 Chrome / Edge CDP 远程调试端口 9222
            if port == 9222 or ":9222" in url:
                return "[导航拦截] 禁止访问浏览器远程调试接口 (CDP 9222)"

            # 如果显式配置了允许域名白名单 (tools.browser.allowed_domains)
            allowed = getattr(self, "allowed_domains", None)
            if allowed and isinstance(allowed, (list, tuple, set)) and len(allowed) > 0:
                allowed_set = {d.lower().strip() for d in allowed if d.strip()}
                if host not in allowed_set and not any(host.endswith("." + d) for d in allowed_set):
                    return f"[域名不在白名单] 域名 '{host}' 未在允许访问列表内"

        return None

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

        if (sec_err := self._check_url_security(url)):
            return sec_err

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

        if (sec_err := self._check_url_security(url)):
            return sec_err

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
                    except Exception as e:
                        if "err_too_many_redirects" in str(e).lower() or "too many redirects" in str(e).lower():
                            return "[重定向次数超限] 目标页面发生多次重定向循环，已终止导航"
                        pass
                    existing_tab["last_used"] = time.time()
                    title = ""
                    try:
                        title = getattr(self._raw_page, "title", lambda: "")() or ""
                    except Exception:
                        pass
                    msg = (f"[已自动切回已有标签页并导航] 切换至 role={existing_role} 并打开目标路径。\n"
                           f"[已打开] {url} 标题：{title}"
                           + self._layout_note() + self._landing_note(url))
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
                       + self._layout_note() + self._landing_note(url))
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
            if "err_too_many_redirects" in str(e).lower() or "too many redirects" in str(e).lower():
                return "[重定向次数超限] 目标页面发生多次重定向循环，已主动终止导航"
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
        # 实走：导航若发生在非前台标签页，用户窗口仍停在旧页（如 about:blank），
        # 表现为"标签打开了但没切过去、看不到执行"。导航成功后统一置顶。
        try:
            if hasattr(self._raw_page, "bring_to_front"):
                self._raw_page.bring_to_front()
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

        ok_msg = f"[已打开] {url} 标题：{title}" + self._layout_note() + self._landing_note(url)
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


    def _visible_text_digest(self, max_lines: int = 25, max_chars: int = 1500) -> str:
        """DOM 可见文本摘要：AX 树被自绘组件屏蔽时的兜底感知。
        只取叶子级可见文本，过滤 AlterEgo 自身注入的浮层，避免污染上下文。"""
        try:
            rows = self._raw_page.evaluate(
                "x=>{const o=[];const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);"
                "let n;while((n=w.nextNode())){const t=(n.textContent||'').replace(/\\s+/g,' ').trim();"
                "if(t.length<2)continue;const e=n.parentElement;if(!e)continue;"
                "const st=getComputedStyle(e);if(st.display==='none'||st.visibility==='hidden')continue;"
                "const r=e.getBoundingClientRect();if(r.width<2||r.height<2)continue;"
                "if(/AlterEgo|申请人工接管|完成接管|停止任务|已锁定防误触/.test(t))continue;"
                "o.push(t);if(o.length>=80)break;}return o;}")
        except Exception:
            return ""
        seen, picked, total = set(), [], 0
        for t in rows or []:
            if t in seen:
                continue
            seen.add(t)
            if total + len(t) > max_chars:
                break
            picked.append(t)
            total += len(t) + 1
            if len(picked) >= max_lines:
                break
        if not picked:
            return ""
        return ("\n[页面可见文本摘要] AX 树节点过少（页面可能是自绘/虚拟列表），"
                "以下是从 DOM 直接提取的可见文本，据此判断页面是否真的有内容：\n"
                + "\n".join(f"  · {t}" for t in picked))

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
            els = self._raw_page.evaluate(INTERACTIVE_TREE_JS, [modal_sel, 1800])
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
            digest = self._visible_text_digest()
            if digest:
                return (header + "[无障碍树] 当前页面无可交互元素，但 DOM 里存在可见文本——"
                        "说明该页被前端自绘/虚拟列表屏蔽了 AX 树，页面并不是空白。" + digest)
            return header + "[无障碍树] 当前页面无任何可交互元素（若为 about:blank 或空白页，请先使用 browser_navigate 打开目标网站）"
        out = header + "[无障碍树]\n" + "\n".join(lines[:60])
        # 实走：列表页（如携程航班列表）主内容由自绘组件渲染，AX 树只剩页脚链接，
        # 模型据此误判"页面空白"→反复截图/导航直到超时。节点过少时补 DOM 文本摘要。
        if len(lines) < 6:
            digest = self._visible_text_digest()
            if digest:
                out += digest
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
                    "无需换算坐标或写选择器。标 [图标按钮] 的是无文字图标控件（发送/提交/评论箭头等），"
                    "已可直接按 ref 点击，不要再用坐标猜。仅当 ref 命中失败或提示 [ref 失效] 时，重新 browser_snapshot 取新句柄。")
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

    # ==== §10.21 通道升级：C1 树/ref → C2 视觉/坐标 → C3 OS 键鼠 → C4 人工 ====
    # 设计见 docs/浏览器通道升级与无进展判据.md
    _CHANNEL_LABEL = {1: "C1(树/ref)", 2: "C2(视觉/坐标)", 3: "C3(OS键鼠)", 4: "C4(人工)"}
    _CHANNEL_HINT = {
        2: "browser_screenshot 看清目标 → browser_click_at(x,y) 按坐标点击 → 坐标输入",
        3: ("真实鼠标点击聚焦后先真实逐键输入（browser_fill_input 对富文本会逐键打字 / browser_type），"
            "仍为空再 browser_paste（系统剪贴板 + 真实 Ctrl+V）；两个原语都失败才算 C3 失败"),
        4: "停止重试，向用户说明已尝试的通道与卡点，交人工接管",
    }
    _C3_PRIMITIVES = ("type", "paste")
    _C3_PRIMITIVE_HINT = {
        "type": "真实逐键输入：browser_fill_input 重新绑定目标后输入（Draft.js/ProseMirror 认 isTrusted 按键）",
        "paste": "系统剪贴板 + 真实 Ctrl+V：browser_paste",
    }
    _CHANNEL_LOCK_TTL = 120.0  # 锁定后 120 秒半开放行一次，避免同一 ref 被永久锁死

    def _channel_state(self) -> dict:
        """测试可能用 __new__ 绕过 __init__，这里懒初始化。"""
        st = getattr(self, "_channel_progress", None)
        if st is None:
            st = {}
            self._channel_progress = st
        return st

    @staticmethod
    def _channel_key(channel: int, target: str, intent: str) -> str:
        return f"{channel}|{target}|{intent}"

    def _channel_guard(self, channel: int, target: str, intent: str, variant: str = "") -> str:
        """同一目标在同一层连续无进展后，禁止继续在同层重试（强制换通道）。
        C3 内部再分原语（type=真实逐键 / paste=系统剪贴板粘贴）：一个原语失败后
        先换另一个原语，两个都失败才升级 C4（§39.20）。"""
        rec = self._channel_state().get(self._channel_key(channel, target, intent)) or {}
        nxt = int(rec.get("forced") or 0)
        if nxt and channel < nxt:
            forced_at = float(rec.get("forced_at") or 0)
            if forced_at and time.time() - forced_at > self._CHANNEL_LOCK_TTL:
                # 半开放行：目标可能已被别的通道改动，允许一次探测；再失败立即重新锁定
                rec["forced"] = 0
                rec["fails"] = 1
                rec["variants_failed"] = []
                rec["next_variant"] = ""
                return ""
            label = self._CHANNEL_LABEL.get(channel, f"C{channel}")
            target_label = self._CHANNEL_LABEL.get(nxt, f"C{nxt}")
            hint = self._CHANNEL_HINT.get(nxt, "下一通道")
            return (f"[通道锁定 → {target_label}] 目标 {target} 在 {label} 已连续 2 次无进展，"
                    f"本轮禁止再用 {label} 类操作。请改用 {target_label}：{hint}。"
                    f"若已在 {target_label} 成功操作并确认目标变化，请重新 browser_snapshot 取新 ref 再试；"
                    f"同一目标锁定 {int(self._CHANNEL_LOCK_TTL)} 秒后半开放行一次。")
        if channel == 3 and variant:
            need = str(rec.get("next_variant") or "")
            if need and variant != need:
                tried_at = float(rec.get("variant_forced_at") or 0)
                if tried_at and time.time() - tried_at > self._CHANNEL_LOCK_TTL:
                    rec["next_variant"] = ""
                    return ""
                hint = self._C3_PRIMITIVE_HINT.get(need, need)
                return (f"[通道锁定 → C3/{need}] 目标 {target} 的 C3/{variant} 原语近期无进展，"
                        f"本轮请改用 C3/{need}：{hint}。"
                        f"两个原语都失败后才升级 C4(人工)；{int(self._CHANNEL_LOCK_TTL)} 秒后半开放行一次。")
        return ""

    def _channel_note(self, channel: int, target: str, intent: str, progressed: bool,
                      variant: str = "") -> str:
        """记录一次结果：有进展清零；连续 2 次无进展强制升级到下一通道。
        C3 按原语分别记：一个原语失败先要求换另一个，两个都失败才锁到 C4。"""
        key = self._channel_key(channel, target, intent)
        rec = self._channel_state().setdefault(key, {"fails": 0, "forced": 0})
        label = self._CHANNEL_LABEL.get(channel, f"C{channel}")
        tag = f"{label}/{variant}" if (channel == 3 and variant) else label
        if progressed:
            rec["fails"] = 0
            rec["forced"] = 0
            rec["variants_failed"] = []
            rec["next_variant"] = ""
            return f"[通道={tag} 有进展]"
        if channel == 3 and variant:
            failed = list(rec.get("variants_failed") or [])
            if variant not in failed:
                failed.append(variant)
            rec["variants_failed"] = failed
            remaining = [v for v in self._C3_PRIMITIVES if v not in failed]
            if remaining:
                rec["next_variant"] = remaining[0]
                rec["variant_forced_at"] = time.time()
                rec["fails"] = 0
                hint = self._C3_PRIMITIVE_HINT.get(remaining[0], remaining[0])
                return (f"[通道={tag} 尝试=1/1 无进展 → 下次改用 {label}/{remaining[0]}]"
                        f"（{hint}）")
            rec["forced"] = 4
            rec["forced_at"] = time.time()
            rec["fails"] = 0
            rec["next_variant"] = ""
            nxt_label = self._CHANNEL_LABEL.get(4, "C4")
            return f"[通道={tag} 原语 type+paste 均未落地 → 下一轮强制 {nxt_label}]"
        rec["fails"] = int(rec.get("fails") or 0) + 1
        if rec["fails"] >= 2:
            nxt = min(channel + 1, 4)
            rec["forced"] = nxt
            rec["forced_at"] = time.time()
            rec["fails"] = 0
            nxt_label = self._CHANNEL_LABEL.get(nxt, f"C{nxt}")
            return f"[通道={label} 尝试=2/2 无进展 → 下一轮强制 {nxt_label}]"
        return f"[通道={label} 尝试={rec['fails']}/2 无进展]"

    def _target_signature(self, loc) -> str:
        """目标态：value + aria-expanded + 可见文本，用于判断目标本身是否变化。"""
        try:
            st = loc.first.evaluate(
                "(el) => [el.value || '', el.getAttribute('aria-expanded') || '',"
                " (el.innerText || '').trim().slice(0, 120)].join('|')")
            return str(st or "")
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
        # §10.21 通道门控：该目标若已在 C1 连续 2 次无进展，禁止再用 ref/选择器重试
        target_key = ref or target_selector
        if (blocked := self._channel_guard(1, target_key, "click")):
            return blocked
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
            note = self._channel_note(1, target_key, "click", False)
            return (f"{note} [点击失败] {target_selector}: 所有 frame 中均未找到该选择器。"
                    f"先 browser_snapshot 看 [Frames] 清单确认目标在哪个 frame，"
                    f"再用 browser_click_at 按坐标点。")
        _pages_before = self._pages_snapshot()
        _adopt_note = ""
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
            _adopt_note = self._adopt_new_page(_pages_before)
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
                    _adopt_note = self._adopt_new_page(_pages_before)
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
            result = self._channel_note(1, target_key, "click",
                                        self._page_fingerprint() != before) + " " + result
            result += _adopt_note
            expect = args.get("expect")
            if isinstance(expect, dict) and expect:
                _v = self._verify_expect(expect)
                result += _v
                if "未通过" in _v:
                    result += self._submit_gate_hint(
                        before, _api_all, self._react_probe_el(_locator))
            return result
        clicked_label = target_selector if _frame_id in (None, "f0") else f"{target_selector} [frame={_frame_id}]"
        result = self._finish_click(clicked_label, before, api_errors, _detach, _api_all)
        result = self._channel_note(1, target_key, "click",
                                    self._page_fingerprint() != before) + " " + result
        result += _adopt_note
        if any(k in (target_selector or "") for k in ("验证码", "获取验证码", "发送验证码", "verify_code", "sms_code", "captcha_code")):
            result += " 【发码硬判据】若本次点击是发送验证码，必须调用 browser_verify_code_countdown 确认页面进入数字倒计时状态（如 60s/59秒），未确认倒计时严禁切换标签页！"
        # 统一效果核验：调用方声明了 expect（如点发送后应出现倒计时），框架自动验证
        expect = args.get("expect")
        if isinstance(expect, dict) and expect:
            _v = self._verify_expect(expect)
            result += _v
            if "未通过" in _v:
                result += self._submit_gate_hint(
                    before, _api_all, self._react_probe_el(_locator))
        return result

    def _pages_snapshot(self):
        """当前 context 的页面列表快照（用于点击后识别站点自开的新标签页）。"""
        try:
            return list(self._raw_page.context.pages)
        except Exception:
            return []

    def _adopt_new_page(self, before_pages) -> str:
        """点击触发 target=_blank / window.open 时，把新页设为当前页并置顶。
        实走：站点把结果开在新标签页，工具却继续在旧页上核验，
        表现为"标签打开了但没切过去、执行结果看不到、expect 永远不通过"。"""
        try:
            pages = list(self._raw_page.context.pages)
        except Exception:
            return ""
        fresh = [p for p in pages if p not in before_pages and not p.is_closed()]
        if not fresh:
            return ""
        page = fresh[-1]
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        self._page = page
        try:
            if hasattr(page, "bring_to_front"):
                page.bring_to_front()
        except Exception:
            pass
        try:
            self._sync_tabs()
        except Exception:
            pass
        try:
            url = getattr(page, "url", "") or ""
        except Exception:
            url = ""
        return f" [新标签页] 站点自行打开了新页面，已自动切换并置顶: {url}"

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
        _pages_before = self._pages_snapshot()
        try:
            before = self._page_fingerprint()
            self._human_like_click(ax, ay)  # Gemini 补丁：可视平滑点击（替代隐形瞬移）
        except Exception as e:  # noqa: BLE001
            _detach()
            return f"[点击失败] {label}: {e}"
        _adopt_note = self._adopt_new_page(_pages_before)
        hit = self._element_at_point(ax, ay)
        text = self._finish_click(label, before, api_errors, _detach, api_all)
        text += _adopt_note
        text += f" [落点元素] {hit}。"
        if hit and any(k in hit.lower() for k in ("验证码", "发送", "获取", "code", "send", "sms")):
            text += " 【发码硬判据】若本次点击是发送验证码，必须调用 browser_verify_code_countdown 确认页面进入数字倒计时状态（如 60s/59秒），未确认倒计时严禁切换标签页！"
        text += (" 附图红圈是这次点击的实际落点：若未点中目标，对比红圈与目标的位置偏差，"
                 "修正 x/y 后再点。")
        expect = args.get("expect")
        if isinstance(expect, dict) and expect:
            _v = self._verify_expect(expect)
            text += _v
            if "未通过" in _v:
                text += self._submit_gate_hint(
                    before, api_all, self._react_probe_at(ax, ay))
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

    @staticmethod
    def _set_system_clipboard(text: str) -> bool:
        """把文本写入「操作系统剪贴板」——OS 级输入通道（L5）的第一步。
        优先级：tkinter（标准库，Windows 上写入即取得剪贴板所有权，销毁后仍保留）
        → pyperclip → PowerShell Set-Clipboard。"""
        try:
            import tkinter
            _r = tkinter.Tk()
            _r.withdraw()
            _r.clipboard_clear()
            _r.clipboard_append(text)
            _r.update()
            _r.destroy()
            return True
        except Exception:
            pass
        try:
            import pyperclip  # type: ignore
            pyperclip.copy(text)
            return True
        except Exception:
            pass
        try:
            import subprocess
            subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                           input=text.encode("utf-8"), timeout=8)
            return True
        except Exception:
            return False

    def _os_paste(self, text: str, clear_before: bool = True) -> bool:
        """OS 级输入通道（L5）：系统剪贴板 + 真实 Ctrl+V 键事件。
        合成 input 事件与 DOM 赋值都会被 state 门控编辑器（Draft.js/brt/ProseMirror/
        受控组件）的 React state 忽略；而浏览器原生的 Ctrl+V 粘贴是 isTrusted 行为，
        能真正驱动 state 更新（发布按钮点亮、占位符隐藏）。"""
        import time as _t
        page = self._raw_page
        if not text or not self._set_system_clipboard(text):
            return False
        try:
            if clear_before:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                _t.sleep(0.05)
            page.keyboard.press("Control+V")
            _t.sleep(0.2)
            return True
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

    def _clear_fill_marker(self, frame=None):
        try:
            (frame or self._raw_page).evaluate(_FILL_RESET_JS)
        except Exception:
            pass

    def _mark_fill_target(self, loc) -> dict:
        try:
            res = loc.first.evaluate(_FILL_MARK_JS)
            return res if isinstance(res, dict) else {}
        except Exception:
            return {}

    def _mark_active_fill_target(self, frame=None) -> dict:
        try:
            res = (frame or self._raw_page).evaluate(_FILL_ACTIVE_MARK_JS)
            return res if isinstance(res, dict) else {}
        except Exception:
            return {}

    def _mark_point_fill_target(self, frame, ax: float, ay: float) -> dict:
        try:
            res = (frame or self._raw_page).evaluate(_FILL_POINT_MARK_JS, [ax, ay])
            return res if isinstance(res, dict) else {}
        except Exception:
            return {}

    def _fill_target_state(self, frame=None) -> dict:
        try:
            res = (frame or self._raw_page).evaluate(_FILL_STATE_JS)
            return res if isinstance(res, dict) else {}
        except Exception:
            return {}

    def _editor_state(self, frame=None) -> dict:
        """读取 state 门控编辑器的自身状态（Draft.js/ProseMirror），不认 DOM 假文字。"""
        try:
            res = (frame or self._raw_page).evaluate(_EDITOR_STATE_JS)
            return res if isinstance(res, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _editor_landed(state: dict, text: str) -> bool:
        """富文本落地判据：文字必须在编辑器内，且 state 门控编辑器已生成内容块。

        DOM 有字但 data-block=0（Draft.js 假草稿）一律不算落地——这正是
        「回读核验通过」却发不出评论的根因（§39.20）。
        """
        norm = lambda s: "".join(str(s or "").split())  # noqa: E731
        if not isinstance(state, dict) or not state.get("found"):
            return False
        target, probe = norm(text), norm(state.get("text"))
        if not target:
            return not probe
        if target not in probe:
            return False
        if state.get("state_gated") and int(state.get("blocks") or 0) <= 0:
            return False
        if state.get("state_gated") and state.get("placeholder_visible"):
            return False
        return True

    def _focus_fill_target(self, frame=None, loc=None) -> dict:
        """确保焦点真正落在标记节点上：先查状态，必要时真实点击/JS focus 后复读。"""
        state = self._fill_target_state(frame)
        if state.get("focused"):
            return state
        if loc is not None:
            try:
                loc.first.click(timeout=1500)
                time.sleep(0.15)
                state = self._fill_target_state(frame)
                if state.get("focused"):
                    return state
            except Exception:
                pass
        try:
            (frame or self._raw_page).evaluate(_FILL_FOCUS_JS)
            time.sleep(0.1)
        except Exception:
            pass
        return self._fill_target_state(frame)

    def _try_activate_editor(self, frame=None, loc=None, ax=None, ay=None) -> dict:
        """给懒挂载编辑器补一次完整事件链并短暂等待焦点落地（§39.23）。

        只作用于本次显式目标（loc）或明确落点（ax, ay），绝不改填页面其它输入框。
        返回 {dispatched, focused, info, trusted_click}。
        """
        target = frame or self._raw_page
        info = {}
        if loc is not None:
            try:
                info = loc.first.evaluate(_ACTIVATE_NODE_JS) or {}
            except Exception:
                info = {}
        if not info.get("ok") and ax is not None and ay is not None:
            try:
                info = target.evaluate(_ACTIVATE_POINT_JS, [ax, ay]) or {}
            except Exception:
                info = {}
        if not info.get("ok"):
            return {"dispatched": False, "focused": False, "info": info}

        def _focused() -> bool:
            try:
                probe = target.evaluate(_ACTIVE_TEXT_EDITABLE_JS) or {}
            except Exception:
                probe = {}
            return bool(probe.get("found"))

        trusted_clicked = False
        deadline = time.time() + 1.2
        while time.time() < deadline:
            if _focused():
                return {"dispatched": True, "focused": True, "info": info,
                        "trusted_click": trusted_clicked}
            if not trusted_clicked and time.time() > deadline - 0.7:
                px, py = info.get("x"), info.get("y")
                if isinstance(px, (int, float)) and isinstance(py, (int, float)):
                    try:
                        self._raw_page.mouse.click(px, py)
                    except Exception:
                        pass
                trusted_clicked = True
            time.sleep(0.1)
        return {"dispatched": True, "focused": _focused(), "info": info,
                "trusted_click": trusted_clicked}

    def _single_editable_in_modal(self, frame_ref, modal_info):
        """浮层内可编辑节点恰好唯一时返回 (frame, locator)，否则 None（0 个含糊，多个更含糊）。"""
        if not isinstance(modal_info, dict) or not modal_info.get("has_modal"):
            return None
        modal_sel = (modal_info.get("selector") or "").strip()
        if not modal_sel:
            return None
        frame = self._resolve_frame(frame_ref)
        if frame is None:
            return None
        try:
            inner = frame.locator(modal_sel).locator(_TEXT_EDITABLE_CSS)
            if inner.count() != 1:
                return None
        except Exception:
            return None
        return frame, inner

    def fill_input(self, args: dict) -> str:
        return self._run(self._do_fill_input, args)

    def _do_fill_input(self, args: dict) -> str:
        """通用闭环拟人输入引擎（统一解决原生框、React/Vue 受控组件、富文本与评论占位符）：
        1. 目标完整性（§39.17）：ref > selector > 坐标。显式目标优先，坐标仅在其缺席时生效；
           命中的节点打 data-ae-fill-target 标记，写入/回读只认该节点。目标失焦时宁可失败，
           也不改填页面上"第一个输入框"（背景搜索框劫持评论输入的根因）。
        2. 兼容原生 <input>/<textarea>、受控组件与 ContentEditable 富文本。
        3. 真实落地回读闭环：空白归一化后的严格包含校验，不做"长度够就算成功"的假阳性放行。
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

        # §10.21 通道门控：同一目标在 C1 连续 2 次无进展后，禁止再用 ref/选择器重试
        _ck = (ref or selector or
               (str(args.get("coords")) if args.get("coords")
                else f"{args.get('x')},{args.get('y')}"))
        if (blocked := self._channel_guard(1, _ck, "input")):
            return blocked

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

        modal_info = self._detect_active_modal()
        frame = None
        target_loc = None
        mark: dict = {}
        state: dict = {}
        target_desc = ""

        # 1. 目标绑定：显式 ref/selector 优先，坐标仅在无显式目标时生效
        if selector:
            target_desc = f"选择器 '{selector}'"
            coord_note = ""
            if target_coords is not None:
                coord_note = f" [已忽略坐标({target_coords[0]:g}, {target_coords[1]:g})：显式目标优先]"
            if getattr(self, "smart_zoom", None):
                self.smart_zoom.record_action("fill_input", 0, 0, f"输入文本到: {selector}")
            frame, loc, _frame_id = self._find_locator(selector, frame_ref, scope_to_modal=True)
            if loc is None or loc.count() == 0:
                return (f"[输入失败] 页面中未找到输入目标: {selector}{coord_note}\n"
                        f"排查建议: 目标可能已变化或尚未展开；请重新 browser_snapshot 获取最新 ref/选择器后再试。"
                        f"严禁改填页面上的其它输入框（历史事故：评论写进了顶部搜索框）。")
            self._clear_fill_marker(frame)
            # 评论栏/占位入口常需一次真实点击才展开编辑器；点击后 DOM 可能重渲染
            try:
                loc.first.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            try:
                loc.first.click(timeout=2000)
                time.sleep(0.2)
            except Exception:
                pass
            re_frame, re_loc, _re_id = self._find_locator(selector, frame_ref, scope_to_modal=True)
            if re_loc is not None and re_loc.count() > 0:
                frame = re_frame or frame
                loc = re_loc
            target_loc = loc
            modal_info = self._detect_active_modal()
            mark = self._mark_fill_target(loc)
            if not mark.get("ok"):
                # 占位容器点击后编辑器仍未挂载：补完整指针事件链，再重新定位（§39.23）
                act = self._try_activate_editor(frame, loc)
                if act.get("dispatched"):
                    target_desc += " [原生事件链激活]"
                    re_frame, re_loc, _re_id = self._find_locator(selector, frame_ref, scope_to_modal=True)
                    if re_loc is not None and re_loc.count() > 0:
                        frame = re_frame or frame
                        loc = re_loc
                        target_loc = loc
                        mark = self._mark_fill_target(loc)
                    if not mark.get("ok"):
                        info = act.get("info") or {}
                        if isinstance(info.get("x"), (int, float)) and isinstance(info.get("y"), (int, float)):
                            mark = self._mark_point_fill_target(frame, info["x"], info["y"])
                            if mark.get("ok"):
                                target_loc = None
                if not mark.get("ok"):
                    alt = self._single_editable_in_modal(frame_ref, modal_info)
                    if alt is None:
                        return (f"[输入失败] 目标 {selector} 命中 <{mark.get('tag', 'unknown')}>，"
                                f"其内部没有 input/textarea/contenteditable，无法输入。{coord_note}\n"
                                f"排查建议: 该容器可能只是占位入口；请 browser_screenshot 确认编辑器是否已展开，"
                                f"再 browser_snapshot 取真实输入框的 ref。禁止改填其它输入框。")
                    frame, alt_loc = alt
                    mark = self._mark_fill_target(alt_loc)
                    if not mark.get("ok"):
                        return (f"[输入失败] 浮层内唯一候选仍不可编辑: <{mark.get('tag', 'unknown')}>")
                    target_loc = alt_loc
                    target_desc += " [浮层内替代目标]"
            state = self._focus_fill_target(frame, target_loc)
            if not state.get("found") or not state.get("focused"):
                return (f"[输入失败] 目标 {selector} 未能获得焦点"
                        f"（目标 <{mark.get('tag', 'unknown')}>，当前焦点 <{state.get('active_tag', 'unknown')}>）。"
                        f"为避免把文本写进错误控件，本次已中止。{coord_note}\n"
                        f"排查建议: 检查是否有遮挡浮层/人机验证；可先 browser_click 目标容器，"
                        f"或 browser_screenshot 后重试。")
            target_desc += coord_note
        elif target_coords is not None:
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
            self._clear_fill_marker(None)
            mark = self._mark_point_fill_target(None, ax, ay)
            if not mark.get("ok"):
                act = self._try_activate_editor(None, ax=ax, ay=ay)
                if act.get("dispatched"):
                    target_desc += " [原生事件链激活]"
                    mark = self._mark_point_fill_target(None, ax, ay)
                if not mark.get("ok"):
                    modal_info = self._detect_active_modal()
                    alt = self._single_editable_in_modal(frame_ref, modal_info)
                    if alt is None:
                        return (f"[输入失败] 坐标({cx:g}, {cy:g}) 落点 <{mark.get('tag', 'none')}> 不是可编辑元素。\n"
                                f"排查建议: 先用 browser_probe_at 确认落点，或先点击展开评论框再输入；"
                                f"禁止改用其它输入框。")
                    frame, alt_loc = alt
                    mark = self._mark_fill_target(alt_loc)
                    if not mark.get("ok"):
                        return (f"[输入失败] 浮层内唯一候选仍不可编辑: <{mark.get('tag', 'unknown')}>")
                    target_loc = alt_loc
                    target_desc += " [浮层内替代目标]"
            state = self._focus_fill_target(None, target_loc)
            if not state.get("found") or not state.get("focused"):
                return (f"[输入失败] 坐标点击后焦点未落在可编辑目标"
                        f"（目标 <{mark.get('tag', 'unknown')}>，当前焦点 <{state.get('active_tag', 'unknown')}>）。"
                        f"为避免误填，本次已中止。排查建议: browser_screenshot 确认落点后再试。")
        else:
            target_desc = "当前焦点元素"
            if getattr(self, "smart_zoom", None):
                self.smart_zoom.record_action("fill_input", 0, 0, "向当前焦点输入")
            self._clear_fill_marker(None)
            mark = self._mark_active_fill_target(None)
            if not mark.get("ok"):
                return (f"[输入失败] 当前没有可编辑的焦点元素（焦点: <{mark.get('tag', 'none')}>）。\n"
                        f"排查建议: 先 browser_click / browser_click_at 点击目标输入框，"
                        f"或用 selector/ref 明确指定输入目标。")
            state = self._focus_fill_target(None, None)
            if not state.get("focused"):
                return (f"[输入失败] 焦点元素 <{mark.get('tag', 'unknown')}> 未能保持焦点"
                        f"（当前焦点 <{state.get('active_tag', 'unknown')}>）。")

        write_frame = frame or self._raw_page
        is_input = bool(state.get("is_input")) or bool(mark.get("is_input"))
        is_ce = bool(state.get("is_contenteditable")) or bool(mark.get("is_contenteditable"))
        editor_state = self._editor_state(frame) if is_ce else {}
        state_gated = bool(editor_state.get("state_gated"))
        ce_evidence = ""
        success_note = ""

        def _readback() -> dict:
            return self._fill_target_state(frame)

        def _landed(val: str) -> bool:
            norm = lambda s: " ".join(str(s or "").split())  # noqa: E731
            return norm(text) in norm(val)

        if is_ce and state_gated:
            # state 门控富文本（Draft.js/ProseMirror）：DOM 赋值只会造出"假草稿"，
            # 编辑器 React state 不认——必须走真实输入原语（§39.20）。
            # 原语顺序：默认 type → paste；上一轮某原语已失败时，先换另一个原语。
            rec3 = self._channel_state().get(self._channel_key(3, _ck, "input")) or {}
            if int(rec3.get("forced") or 0) >= 4:
                blocked = self._channel_guard(3, _ck, "input", variant="type")
                if blocked:
                    return blocked
                rec3 = {}
            order = ["type", "paste"]
            if str(rec3.get("next_variant") or "") == "paste":
                order = ["paste", "type"]
            filled, c3_note = False, ""
            for primitive in order:
                if clear_before:
                    try:
                        self._raw_page.keyboard.press("Control+A")
                        self._raw_page.keyboard.press("Backspace")
                        time.sleep(0.05)
                    except Exception:
                        pass
                if not _readback().get("focused"):
                    self._focus_fill_target(frame, target_loc)
                try:
                    if primitive == "type":
                        self._raw_page.keyboard.type(text, delay=15)
                        time.sleep(0.2)
                        editor_state = self._editor_state(frame)
                        filled = self._editor_landed(editor_state, text)
                    elif self._os_paste(text, clear_before=False):
                        time.sleep(0.25)
                        editor_state = self._editor_state(frame)
                        filled = self._editor_landed(editor_state, text)
                except Exception:
                    filled = False
                c3_note = self._channel_note(3, _ck, "input", filled, variant=primitive)
                if filled:
                    break
            state = _readback()
            actual_val = str(editor_state.get("text") or "")
            if not filled:
                tag_name = state.get("tag") or mark.get("tag") or "unknown"
                return (f"{c3_note} [输入未落地] {target_desc} -> <{tag_name}> 是 state 门控富文本编辑器，"
                        f"文字没有进入编辑器状态（DOM 回读='{actual_val[:60]}'，"
                        f"data-block={int(editor_state.get('blocks') or 0)}，"
                        f"占位符{'仍在' if editor_state.get('placeholder_visible') else '已隐藏'}）。"
                        f"真实逐键与系统粘贴都未落地，禁止宣称输入成功。\n"
                        f"排查建议: 先 browser_screenshot 核对画面与焦点；再按 ref → browser_click → "
                        f"browser_fill_input 重新绑定编辑器；仍失败再 browser_request_takeover 请人工确认"
                        f"（不要连续重试同一原语）。")
            success_note = c3_note
            ce_evidence = (f" | 编辑器状态: data-block={int(editor_state.get('blocks') or 0)}、"
                           f"占位符{'仍在' if editor_state.get('placeholder_visible') else '已隐藏'}")
        else:
            # 2. 清空与文本写入（只作用于标记节点，不认全局 activeElement）
            if clear_before:
                try:
                    write_frame.evaluate(_FILL_CLEAR_JS, is_input)
                except Exception:
                    pass
            try:
                write_frame.evaluate(_FILL_WRITE_INPUT_JS if is_input else _FILL_WRITE_CE_JS, text)
            except Exception:
                pass

            state = _readback()
            actual_val = str(state.get("current_value", "") or "")

            if text and not _landed(actual_val):
                if not _readback().get("focused"):
                    self._focus_fill_target(frame, target_loc)
                try:
                    self._raw_page.keyboard.type(text, delay=15)
                    time.sleep(0.2)
                except Exception:
                    pass
                state = _readback()
                actual_val = str(state.get("current_value", "") or "")

            # L5 兜底升级：DOM 赋值 + 合成事件 + 真实逐键都拿不下时（典型：Draft.js/brt
            # 这类 state 门控编辑器，DOM 有字但 React state 恒空），转 OS 级粘贴——
            # 写系统剪贴板 + 真实 Ctrl+V（isTrusted，能驱动 state 更新）。
            if text and not _landed(actual_val):
                if not _readback().get("focused"):
                    self._focus_fill_target(frame, target_loc)
                if self._os_paste(text, clear_before=True):
                    time.sleep(0.25)
                    state = _readback()
                    actual_val = str(state.get("current_value", "") or "")

            try:
                write_frame.evaluate(_FILL_BLUR_JS)
            except Exception:
                pass

            # 3. 回读核验闭环断言：空白归一化后的严格包含，无长度豁免
            if not _landed(actual_val):
                tag_name = state.get("tag") or mark.get("tag") or "unknown"
                focus_note = ("仍聚焦" if state.get("focused")
                              else f"已失焦（当前焦点 <{state.get('active_tag', 'unknown')}>）")
                note = self._channel_note(1, _ck, "input", False)
                return (f"{note} [输入未落地] 目标文本未能成功写入页面！\n"
                        f"诊断线索: 目标={target_desc} | 目标元素=<{tag_name}>（{focus_note}） | "
                        f"回读内容='{actual_val[:60]}' | 预期='{text[:60]}'\n"
                        f"排查建议: 目标容器可能需要先点击激活（如评论框需先点击'说点什么'占位符），"
                        f"或被人机验证/顶层浮窗阻挡。请 browser_screenshot 检查画面。")

        # 4. 回车提交（确保焦点仍在目标上，避免 Enter 提交到别处）
        enter_note = ""
        if press_enter:
            if not _readback().get("focused"):
                self._focus_fill_target(frame, target_loc)
            time.sleep(0.2)
            focus_before = {}
            try:
                focus_before = self._raw_page.evaluate(self._FOCUS_TEXT_JS) or {}
            except Exception:
                focus_before = {}
            if not isinstance(focus_before, dict):
                focus_before = {}
            try:
                self._raw_page.keyboard.press("Enter")
            except Exception:
                pass
            time.sleep(0.4)
            if focus_before.get("editable"):
                time.sleep(0.3)
                try:
                    focus_after = self._raw_page.evaluate(self._FOCUS_TEXT_JS) or {}
                except Exception:
                    focus_after = {}
                if not isinstance(focus_after, dict):
                    focus_after = {}
                enter_note = " " + self._enter_submit_verdict(focus_before, focus_after)

        self._human_delay()
        modal_tag = ""
        if isinstance(modal_info, dict) and modal_info.get("has_modal") and modal_info.get("selector"):
            try:
                in_modal_now = write_frame.evaluate(_FILL_IN_MODAL_JS, modal_info["selector"])
            except Exception:
                in_modal_now = False
            if in_modal_now is True:
                modal_tag = " [浮层内]"
        enter_tag = " (已按回车)" if press_enter else ""
        elem_tag = state.get("tag") or mark.get("tag") or "input"
        note = success_note or self._channel_note(1, _ck, "input", True)
        result = (f"{note} [输入成功] {target_desc} -> <{elem_tag}>{modal_tag}{enter_tag} | "
                  f"写入字数={len(text)} | 回读核验通过{ce_evidence}{enter_note}")

        expect = args.get("expect")
        if isinstance(expect, dict) and expect:
            result += self._verify_expect(expect)

        return result

    def fill_combo(self, args: dict) -> str:
        return self._run(self._do_fill_combo, args)

    def _do_fill_combo(self, args: dict) -> str:
        """联想下拉框（autocomplete combobox）输入 + 选中（L6 原语）。
        解决携程出发地/12306/淘宝这类"输入框 + 联想候选列表"控件：
        只往 DOM 写字，城市码/选项值从未提交，提交时仍用旧值（表现为"填不上/搜不到"）。
        必须真实逐键输入触发联想请求，再点击候选列表项才算落地。
        步骤：真实点击聚焦 → 清空 → 真实逐键输入 → 等候选列表 → 点选 → 回读核验。"""
        if (blocked := self._do_guard()):
            return blocked
        import time as _t
        text = str(args.get("text", "") or "")
        option = str(args.get("option", "") or "").strip()
        if not text:
            return "[缺少 text] 需要输入到联想框的查询词（如城市名/拼音）"
        ref = (args.get("ref") or "").strip()
        selector = (args.get("selector") or "").strip()
        frame_ref = args.get("frame", "auto")
        try:
            timeout = min(max(float(args.get("timeout", 3.0) or 3.0), 0.5), 10.0)
        except (TypeError, ValueError):
            timeout = 3.0
        if ref:
            probe = self._find_locator(f'[data-ae-ref="{ref}"]', frame_ref)
            if probe[1] is None or probe[1].count() == 0:
                return (f"[ref 失效] 句柄 {ref} 不在当前页面（可能已跳转或局部重渲染）。"
                        f"请重新调用 browser_snapshot 获取最新 ref，禁止沿用旧 ref。")
            selector = f'[data-ae-ref="{ref}"]'
        if not selector:
            return "[缺少 selector 或 ref] 请指定联想输入框（携程出发地等）"
        # §10.21 通道门控：同一目标在 C1 连续 2 次无进展后，禁止复用 ref 重试
        target_key = ref or selector
        if (blocked := self._channel_guard(1, target_key, "input")):
            return blocked
        frame, loc, fid = self._find_locator(selector, frame_ref, scope_to_modal=True)
        if loc is None or loc.count() == 0:
            return f"[未找到] {selector}（frame={frame_ref}）"
        try:
            loc.first.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        # 1. 真实点击聚焦（联想逻辑只认真实键鼠事件，合成 click 不一定唤醒下拉）
        box = None
        try:
            box = loc.first.bounding_box()
        except Exception:
            box = None
        clicked = False
        if box:
            try:
                self._human_like_click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                clicked = True
            except Exception:
                clicked = False
        if not clicked:
            try:
                loc.first.click(timeout=2000)
                clicked = True
            except Exception:
                pass
        if not clicked:
            return f"[输入失败] 无法点击聚焦目标 {selector}"
        _t.sleep(0.2)
        # 2. 清空 + 真实逐键输入（CDP 键事件，才会触发联想请求）
        try:
            self._raw_page.keyboard.press("Control+A")
            self._raw_page.keyboard.press("Backspace")
            self._raw_page.keyboard.type(text, delay=90)
        except Exception as e:
            return f"[输入失败] 键入射入失败: {e}"

        def _read_box_value() -> str:
            try:
                return str(loc.first.evaluate(
                    "(el) => { const a = document.activeElement;"
                    " const t = (a && a !== document.body && (a.isContentEditable ||"
                    " ['INPUT','TEXTAREA'].includes(a.tagName))) ? a : el;"
                    " return t.isContentEditable ? (t.innerText || '') : (t.value || ''); }"
                ) or "").strip()
            except Exception:
                try:
                    return str(loc.first.input_value(timeout=800) or "").strip()
                except Exception:
                    return ""

        # 键击可能没落进目标框（焦点被浮层/内层控件抢走）。先回读，再逐级兜底。
        typed_value = _read_box_value()
        used_fallback = False
        if not typed_value:
            try:
                loc.first.focus()
                loc.first.fill(text, timeout=2000)
            except Exception:
                pass
            typed_value = _read_box_value()
        if not typed_value:
            try:
                loc.first.evaluate(
                    """(el, t) => {
                        el.focus();
                        if (el.isContentEditable) { el.innerText = t; }
                        else {
                            const proto = el.tagName === 'TEXTAREA'
                                ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                            Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, t);
                        }
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return el.isContentEditable ? (el.innerText || '') : (el.value || '');
                    }""", text)
            except Exception:
                pass
            typed_value = _read_box_value()
            used_fallback = True
        if not typed_value:
            _t.sleep(0.4)
            typed_value = _read_box_value()
        if not typed_value:
            return (f"[输入未落地] 真实键入与 DOM 兜底后，{selector} 的回读值仍为空。"
                    f"输入焦点可能被浮层/iframe 抢走，或该控件并非可编辑输入框。"
                    f"本次不计入通道失败。下一步：browser_screenshot 确认控件位置 → "
                    f"browser_click_at(x,y) 真实点击聚焦 → browser_paste/browser_type 输入；"
                    f"若控件在 iframe 内，先用 browser_snapshot 确认 frame 后再试。")
        if used_fallback:
            # DOM 写入不产生真实键事件，联想请求可能已错过；给框架一点反应时间
            _t.sleep(0.3)
        try:
            url_before = str(self._raw_page.url or "")
        except Exception:
            url_before = ""

        # 3. 等候选列表（命中查询词的优先）
        def _poll(wait_sec: float):
            _cands, _hits = [], []
            dl = _t.time() + wait_sec
            while _t.time() < dl:
                try:
                    _cands = self._combo_options(text, box)
                except Exception:
                    _cands = []
                _hits = [c for c in _cands if c.get("hit")]
                if _hits:
                    break
                _t.sleep(0.15)
            return _cands, _hits

        cands, hits = _poll(timeout)
        # L3 加档（15:10 实走）：首轮逐键输入太快时联想请求可能没被触发，
        # 表现为“无候选”但输入框已被改写。放慢到逐字 150ms 再等一轮。
        if not cands:
            try:
                self._raw_page.keyboard.press("Control+A")
                self._raw_page.keyboard.press("Backspace")
                for _ch in text:
                    self._raw_page.keyboard.type(_ch, delay=150)
                    _t.sleep(0.05)
                cands, hits = _poll(max(timeout, 5.0))
            except Exception:
                pass
        if not cands:
            note = self._channel_note(1, target_key, "input", False)
            fallback_note = "（值为 DOM 兜底写入，联想请求可能未触发）" if used_fallback else ""
            return (f"{note} [无候选] 输入 '{text}' 后（含逐字慢输加档）仍未捕获联想候选列表{fallback_note}。"
                    f"可能：该框不是联想控件、联想请求被网络/风控拦截、或候选不在可识别容器内。"
                    f"注意：该输入框已被改写——若后续点提交按钮页面毫无反应，"
                    f"先怀疑内部选中态失效（DOM 显示值 ≠ 表单有效值），"
                    f"请 browser_screenshot 确认下拉形态，或改用 browser_click_at 走视觉通道。")
        # 4. 选候选：语义候选优先；精确 option > 含查询词；无匹配则禁止盲选
        semantic = [c for c in cands if c.get("semantic")]
        pick = None
        if option:
            exact = [c for c in semantic if c["text"].strip() == option]
            pick = exact[0] if exact else next(
                (c for c in cands if option.lower() in c["text"].lower()), None)
        if pick is None and hits:
            sem_hits = [c for c in hits if c.get("semantic")]
            pick = (sem_hits or hits)[0]
        if pick is None:
            if semantic:
                pick = semantic[0]
            else:
                note = self._channel_note(1, target_key, "input", False)
                return (f"{note} [无匹配候选] 输入 '{text}' 后捕获到 {len(cands)} 个候选，"
                        f"但均无下拉选项语义（role=option / listbox），疑似抓到了页面推荐位或导航链接。"
                        f"已拒绝盲选，避免写进错误的框。请 browser_screenshot 确认下拉是否真的展开。")
        try:
            self._human_like_click(pick["x"], pick["y"])
        except Exception as e:
            note = self._channel_note(1, target_key, "input", False)
            return f"{note} [选中失败] 点击候选 '{pick['text']}' 出错: {e}"
        _t.sleep(0.35)
        # 5. 提交态核验：值被替换 / 候选收起 / URL 变化——三者任一才算真提交
        try:
            after = str(loc.first.input_value(timeout=1500) or "")
        except Exception:
            after = ""
        try:
            cands_after = self._combo_options(text, box) if box else []
        except Exception:
            cands_after = []
        try:
            url_after = str(self._raw_page.url or "")
        except Exception:
            url_after = ""
        committed = bool(after.strip()) and after.strip() != text.strip()
        if not committed and cands and not cands_after:
            committed = True
        if not committed and url_after != url_before:
            committed = True
        note = self._channel_note(1, target_key, "input", committed)
        self._human_delay()
        frame_note = "" if fid in (None, "f0") else f" [frame={fid}]"
        how = ("精确匹配" if option and option.lower() in pick["text"].lower()
               else "含查询词" if pick.get("hit") else "语义兜底")
        say = (f"{note} [联想{'已提交' if committed else '未提交'}] {selector}{frame_note} "
               f"输入 '{text}' → 选中 '{pick['text']}'({how})")
        if not committed:
            say += (f" | 提交态未达成（回读值='{after[:40]}'，候选余 {len(cands_after)} 个）: "
                    f"选中项可能不是该控件的真实下拉项。不要重复同一 ref，"
                    f"先 browser_screenshot 看清下拉结构，或改用坐标点击。")
        elif after.strip() != text.strip():
            say += f" | 提交值='{after[:40]}'（已替换为选中项）"
        else:
            say += f" | 候选已收起（值仍为查询词 '{after[:40]}'，按收起判为已提交）"
        expect = args.get("expect")
        if isinstance(expect, dict) and expect:
            say += self._verify_expect(expect)
        return say

    def _combo_options(self, query: str, box: dict | None) -> list[dict]:
        """采集联想候选项（通用规则，不含站点特判）。
        收紧判据（§10.21）：候选必须是输入框正下方的下拉项，且不得是链接/营销位。
        - 排除 <a> / 含 href / 位于 <a> 内（热门航线推荐位就是这种）
        - 排除含价格符号或"起/更多/热门/推荐/特价"等营销词的文本
        - 必须与输入框水平对齐、垂直距离 ≤ 420px、宽度 ≥ 输入框的 35%
        - 语义候选（role=option 或在 [role=listbox] 内）优先"""
        js = """([q, ix, iy, iw, ih]) => {
            const ql = (q || '').toLowerCase();
            const bad = /(￥|¥|起\\s*$|起价|更多|热门|推荐|特价|折扣|直达|往返|单程)/;
            const okr = (el) => {
                if (['INPUT', 'TEXTAREA', 'BODY', 'HTML', 'SCRIPT', 'STYLE'].includes(el.tagName)) return null;
                // 排除 Agent 自身的 SoM/光标/提示浮层，避免把编号徽标当成候选
                if (el.closest('#__ae_som,#__alterego_root,#__alterego_cursor,#__ae_lock_toast')) return null;
                if (el.tagName === 'A' || el.hasAttribute('href') || el.closest('a')) return null;
                const r = el.getBoundingClientRect();
                if (r.width < 3 || r.height < 3) return null;
                const st = getComputedStyle(el);
                if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) < 0.05) return null;
                return r;
            };
            const sel = '[role="option"],[role="listitem"],[role="menuitem"],li,' +
                        '[role="listbox"] > *,[class*="option"],[class*="Option"],' +
                        '[class*="suggest"],[class*="Suggest"],[class*="dropdown"] [class*="item"],' +
                        '[class*="dropdown"] [class*="Item"],dd';
            // 横向对齐（15:50 实走修正）：候选行可能比输入框宽，
            // 用"覆盖输入框中点"或"左缘与输入框左对齐"判定，不再用候选中心点比对。
            const hAlign = (r) => (r.left <= ix + iw / 2 && r.right >= ix + iw / 2) ||
                                  Math.abs(r.left - ix) <= 24;
            const vOk = (r) => r.top >= iy + ih - 2 && r.top <= iy + ih + 420;
            const minW = Math.min(Math.max(iw * 0.35, 60), 240);
            const out = [], seen = new Set();
            const push = (el, semantic) => {
                let txt = (el.innerText || el.textContent || '').trim();
                if (!txt || txt.length > 60) return;
                txt = txt.replace(/\\s+/g, ' ');
                if (bad.test(txt)) return;
                const r = okr(el);
                if (!r) return;
                if (r.width < minW) return;
                if (!vOk(r) || !hAlign(r)) return;
                const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                const isHit = !!ql && txt.toLowerCase().includes(ql);
                const k = Math.round(cx) + ',' + Math.round(cy);
                if (seen.has(k)) return;
                seen.add(k);
                out.push({text: txt, x: cx, y: cy, hit: isHit, semantic: semantic});
            };
            for (const el of document.querySelectorAll(sel)) {
                const semantic = el.getAttribute('role') === 'option' ||
                                 el.getAttribute('role') === 'menuitem' ||
                                 !!el.closest('[role="listbox"],[role="menu"]');
                push(el, semantic);
            }
            // 几何兜底（15:50 实走）：主选择器没有命中查询词的候选时全页扫描——
            // 携程等站点的下拉行是无 role/无语义类名的裸 div，
            // 只能靠"输入框正下方 + 横向对齐 + 含查询词"这组通用几何信号识别。
            // 注意不能用 !out.length：主选择器可能只捞到"搜索历史/推荐位"等非命中行，
            // 有 hit 才算真的采到了下拉。
            if (!out.some(c => c.hit) && ql) {
                for (const el of document.querySelectorAll('body *')) {
                    const t = (el.innerText || '');
                    if (!t || t.length > 60 || !t.toLowerCase().includes(ql)) continue;
                    if (el.children.length > 3) continue;
                    push(el, false);
                }
            }
            out.sort((a, b) => (b.semantic - a.semantic) || (b.hit - a.hit) || (a.y - b.y));
            return out.slice(0, 20);
        }"""
        b = box or {}
        try:
            res = self._raw_page.evaluate(
                js, [query, b.get("x", 0), b.get("y", 0),
                     b.get("width", 0), b.get("height", 0)])
        except Exception:
            return []
        return res if isinstance(res, list) else []

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

    def react_probe(self, args: dict) -> str:
        return self._run(self._do_react_probe, args)

    def _do_react_probe(self, args: dict) -> str:
        """组件内部状态取证：回答"这个按钮的 handler 到底绑没绑上、组件状态是否有效"。
        支持 React（fiber/props/hooks）和 Vue（__vue__/__vueParentComponent）。
        点击后零请求时，DOM 层面的 onclick 字段可能是空的（React 走合成事件，Vue 走 $emit），
        只有框架内部字段才看得见事件绑定与组件状态。通用诊断能力。"""
        if (blocked := self._do_guard()):
            return blocked
        ref = (args.get("ref") or "").strip()
        selector = (args.get("selector") or "").strip()
        frame_ref = args.get("frame")
        if ref:
            selector = f'[data-ae-ref="{ref}"]'
        if not selector:
            return "[缺少参数] 提供 ref（快照句柄）或 selector（CSS 选择器）"
        frame, loc, fid = self._find_locator(selector, frame_ref)
        if loc is None:
            return (f"[未找到] {selector}（frame={frame_ref or 'auto'}）。"
                    "先 browser_snapshot 确认节点仍在，再取 ref；节点已重挂载时旧 ref 会失效。")
        state = self._react_probe_el(loc)
        if not state or not state.get("ok"):
            return f"[探针失败] {selector}: 无法读取节点状态"
        framework = state.get("framework") or ""
        if not framework:
            return (f"[组件探针] {selector}（frame={fid}）：目标不是 React/Vue 节点（或页面未用已知框架）。"
                    "点击无响应时改用 browser_inspect 看禁用态/遮挡/pointer-events。")
        chain = state.get("chain") or []
        handlers = state.get("handlers") or []
        hnodes = state.get("handler_nodes") or []
        lines = [
            f"[{framework.upper()} 探针] {selector}（frame={fid}）",
            f"标签: <{state.get('tag', '')}> 文本: '{state.get('text', '')}'",
            "组件链: " + (" < ".join(chain) if chain else "（未读到函数组件）"),
            "元素事件属性: " + ("/".join(handlers) if handlers else "无"),
        ]
        if hnodes:
            lines.append("事件绑定层: " + "；".join(hnodes))
        owner = state.get("hook_owner")
        if owner:
            hooks = state.get("hooks") or []
            lines.append(f"最近函数组件: {owner}")
            lines.append("hook 状态: " + ("、".join(hooks) if hooks else "无"))
        lines.append(f"disabled: {'true' if state.get('disabled') else 'false'}")
        lines.append("判读：提交被静默拦截时，要么文本 state 为空，要么提交中/禁用布尔卡在 true。"
                     "确认卡死后重新加载页面、重新输入再提交；不要在同层连点。")
        return "\n".join(lines)

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
        action: cookies（当前上下文的 cookie，值脱敏截断；可传 name 过滤）/
                local（localStorage）/ session（sessionStorage）。
        回答'我登录了吗''token 存哪了''刷新后还在吗'。
        cookie 清单只是线索，不能单独作为登录态判据。"""
        if (blocked := self._do_guard()):
            return blocked
        action = (args.get("action") or "cookies").strip().lower()
        try:
            if action == "cookies":
                cookies = self._raw_page.context.cookies()
                if not cookies:
                    return ("[cookie] 当前上下文无 cookie（多为新会话或已全部过期）。"
                            "单凭 cookie 缺失不能判定“未登录”，请以页面 UI（头像/退出登录）或业务接口为准。")
                name_filter = (args.get("name") or "").strip().lower()
                if name_filter:
                    cookies = [c for c in cookies
                               if name_filter in str(c.get("name", "")).lower()]
                    if not cookies:
                        return (f"[cookie] 无名称含 '{name_filter}' 的 cookie。"
                                f"HttpOnly cookie 不出现在 document.cookie 里；"
                                f"此结果不能作为“未登录”的判据，请以页面 UI/业务接口为准。")
                lines = []
                for c in cookies[:40]:
                    v = c.get("value", "")
                    shown = (v[:12] + "…") if len(v) > 12 else v
                    ho = " [HttpOnly]" if c.get("httpOnly") else ""
                    lines.append(f"{c.get('name')}={shown}（域 {c.get('domain')}{ho}）")
                head = f"[cookie] {len(cookies)} 个"
                if len(cookies) > 40:
                    head += f"（仅列出前 40 个，共 {len(cookies)} 个；可传 name 过滤）"
                return (f"{head}：\n" + "\n".join(lines) +
                        "\n提示：HttpOnly cookie 不出现在 document.cookie 里；"
                        "cookie 清单只是线索，登录态以页面 UI（头像/退出登录）或业务接口为准。")
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

    _FOCUS_TEXT_JS = """() => {
        const el = document.activeElement;
        if (!el || el === document.body) return {editable: false, tag: '', text: ''};
        const host = (el.closest && el.closest('[contenteditable="true"]')) || el;
        return {
            editable: host.isContentEditable === true,
            tag: host.tagName ? host.tagName.toLowerCase() : '',
            text: String(host.innerText || host.textContent || '').slice(0, 800)
        };
    }"""

    @staticmethod
    def _enter_submit_verdict(before: dict, after: dict) -> str:
        """富文本内 Enter 的提交判据（v1.5.21）：只换行不算发出。"""
        norm = lambda s: "".join(str(s or "").split())  # noqa: E731
        b, a = norm(before.get("text", "")), norm(after.get("text", ""))
        tip = ("下一步：不要再按 Enter；用 browser_snapshot 找显式发送控件（发送/评论/纸飞机图标，"
               "表现为 [图标按钮] 行），按 ref → browser_find_text → browser_probe_at → browser_click_at "
               "通道点击；点完等 3~5 秒回读编辑器是否清空、列表是否出现新内容。")
        if a == b:
            return ("[Enter 未提交] 焦点在富文本编辑器（" + (before.get("tag") or "contenteditable") + "），"
                    "按 Enter 后内容没有变化——Draft.js/ProseMirror 等编辑器里 Enter 通常只换行，不发送。\n" + tip)
        if not a:
            return ("（编辑器内容已清空，疑似已提交；仍需 browser_evaluate 回读列表/新内容确认，不得直接宣称成功）")
        return ("（编辑器仍有内容且已变化，可能只是换行或部分提交；必须回读列表确认是否真的发出，"
                "未发出则找显式发送控件，不要重复按 Enter）")

    def _do_press(self, args: dict) -> str:
        """键盘按键：Enter（表单提交）/ Tab（切换焦点）/ Escape（关闭弹窗）/
        ArrowDown 等。作用于当前焦点元素（无焦点时作用于页面）。
        比 type 更轻：不需要选择器。
        Enter 在 contenteditable/Draft.js 里只换行：v1.5.21 起回读前后内容并显式判定是否提交。"""
        if (blocked := self._do_guard()):
            return blocked
        key = (args.get("key") or "").strip()
        if not key:
            return "[缺少 key] 如 Enter / Tab / Escape / ArrowDown"
        is_enter = key.lower() in ("enter", "return", "numpadenter")
        before = {}
        if is_enter:
            try:
                before = self._raw_page.evaluate(self._FOCUS_TEXT_JS) or {}
            except Exception:
                before = {}
            if not isinstance(before, dict):
                before = {}
        try:
            self._raw_page.keyboard.press(key)
        except Exception as e:
            return f"[按键失败] {key}: {e}"
        if is_enter and before.get("editable"):
            time.sleep(0.6)
            try:
                after = self._raw_page.evaluate(self._FOCUS_TEXT_JS) or {}
            except Exception:
                after = {}
            if not isinstance(after, dict):
                after = {}
            return f"[按键] {key} {self._enter_submit_verdict(before, after)}"
        return f"[按键] {key}"

    def paste(self, args: dict) -> str:
        return self._run(self._do_paste, args)

    def _do_paste(self, args: dict) -> str:
        """OS 级粘贴（L5 原语）：写系统剪贴板 → 发真实 Ctrl+V。
        用于 state 门控编辑器（Draft.js/brt/ProseMirror/React 受控组件）与普通输入框：
        合成 input 事件与 DOM 赋值会被 state 忽略，只有 isTrusted 的真实粘贴事件
        才能驱动 state 更新（发布按钮点亮/占位符消失）。"""
        if (blocked := self._do_guard()):
            return blocked
        import time as _t
        text = str(args.get("text", "") or "")
        from_clip = bool(args.get("from_clipboard", False))
        ref = (args.get("ref") or "").strip()
        selector = (args.get("selector") or "").strip()
        frame_ref = args.get("frame", "auto")
        clear_before = bool(args.get("clear_before", True))
        press_enter = bool(args.get("press_enter", False))
        if ref:
            probe = self._find_locator(f'[data-ae-ref="{ref}"]', frame_ref)
            if probe[1] is None or probe[1].count() == 0:
                return (f"[ref 失效] 句柄 {ref} 不在当前页面（可能已跳转或局部重渲染）。"
                        f"请重新调用 browser_snapshot 获取最新 ref。")
            selector = f'[data-ae-ref="{ref}"]'
        # §10.21 通道门控（C3/paste）：同一目标 paste 原语无进展时先要求改 type 原语
        target_key = ref or selector or "activeElement"
        if (blocked := self._channel_guard(3, target_key, "input", variant="paste")):
            return blocked
        # 1. 聚焦目标
        fid = None
        frame = None
        mark: dict = {}
        if selector:
            frame, loc, fid = self._find_locator(selector, frame_ref, scope_to_modal=True)
            if loc is None or loc.count() == 0:
                return f"[未找到] {selector}（frame={frame_ref}）"
            try:
                loc.first.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            try:
                loc.first.click(timeout=2000)
            except Exception:
                try:
                    loc.first.evaluate("(el) => el.focus()")
                except Exception:
                    return f"[输入失败] 无法聚焦目标 {selector}"
            _t.sleep(0.2)
            self._clear_fill_marker(frame)
            re_frame, re_loc, re_fid = self._find_locator(selector, frame_ref, scope_to_modal=True)
            if re_loc is not None and re_loc.count() > 0:
                frame, loc, fid = re_frame or frame, re_loc, re_fid or fid
            mark = self._mark_fill_target(loc)
        else:
            mark = self._mark_active_fill_target(None)
        # 2. 写剪贴板（除非显式要求沿用现有剪贴板内容）
        if not from_clip:
            if not text:
                return "[缺少 text] 传 text，或设 from_clipboard=true 沿用系统剪贴板现有内容"
            if not self._set_system_clipboard(text):
                return "[剪贴板失败] 无法写入系统剪贴板（tkinter / pyperclip / PowerShell 均不可用）"
        # 3. 真实 Ctrl+V（isTrusted 键事件）
        try:
            if clear_before:
                self._raw_page.keyboard.press("Control+A")
                self._raw_page.keyboard.press("Backspace")
                _t.sleep(0.05)
            self._raw_page.keyboard.press("Control+V")
            _t.sleep(0.25)
        except Exception as e:
            self._channel_note(3, target_key, "input", False, variant="paste")
            return f"[粘贴失败] Ctrl+V 键事件失败: {e}"
        readback_js = """() => {
            const el = document.activeElement;
            if (!el) return '';
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el.value || '';
            return el.innerText || el.textContent || '';
        }"""
        try:
            after = str(self._raw_page.evaluate(readback_js) or "")
        except Exception:
            after = ""
        submit_note = ""
        if press_enter:
            focus_before = {}
            if self._raw_page is not None:
                try:
                    focus_before = self._raw_page.evaluate(self._FOCUS_TEXT_JS) or {}
                except Exception:
                    focus_before = {}
                if not isinstance(focus_before, dict):
                    focus_before = {}
            try:
                self._raw_page.keyboard.press("Enter")
                _t.sleep(0.3)
            except Exception:
                pass
            if focus_before.get("editable"):
                _t.sleep(0.3)
                try:
                    focus_after = self._raw_page.evaluate(self._FOCUS_TEXT_JS) or {}
                except Exception:
                    focus_after = {}
                if not isinstance(focus_after, dict):
                    focus_after = {}
                submit_note = " " + self._enter_submit_verdict(focus_before, focus_after)
        self._human_delay()
        frame_note = "" if fid in (None, "f0") else f" [frame={fid}]"
        editor_state = self._editor_state(frame) if mark.get("is_contenteditable") else {}
        state_gated = bool(editor_state.get("state_gated"))
        editor_evidence = ""
        if from_clip:
            verdict = f"已粘贴系统剪贴板现有内容 | 回读摘录='{after[:40]}'"
            progressed = bool(after.strip())
            if state_gated:
                progressed = bool(str(editor_state.get("text") or "").strip())
        elif state_gated:
            landed = self._editor_landed(editor_state, text)
            editor_evidence = (f" | 编辑器状态: data-block={int(editor_state.get('blocks') or 0)}、"
                               f"占位符{'仍在' if editor_state.get('placeholder_visible') else '已隐藏'}")
            if landed:
                verdict = f"回读核验通过（写入字数={len(text)}）"
            else:
                verdict = (f"回读未落地：编辑器 state 未接受文本"
                           f"（DOM 回读='{after[:40]}'，data-block={int(editor_state.get('blocks') or 0)}，"
                           f"占位符{'仍在' if editor_state.get('placeholder_visible') else '已隐藏'}），"
                           f"禁止宣称粘贴成功")
            progressed = bool(landed)
        elif text and text in after:
            verdict = f"回读核验通过（写入字数={len(text)}）"
            progressed = True
        else:
            verdict = ("回读未含目标文本——若目标是富文本编辑器属正常（值渲染在别处），"
                       "请 browser_screenshot 确认；若确未落地，检查目标是否被人机验证浮层阻挡或未真正聚焦")
            progressed = False
        res = f"[已粘贴] {selector or '当前焦点'}{frame_note} | {verdict}{editor_evidence}{submit_note}"
        res = self._channel_note(3, target_key, "input", progressed, variant="paste") + " " + res
        expect = args.get("expect")
        if isinstance(expect, dict) and expect:
            res += self._verify_expect(expect)
        return res

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

    _EVALUATE_HISTORY_TTL = 120.0
    _EVALUATE_MAX_HISTORY = 64
    _EXPECT_KEY_ALIASES = {
        "from": {"from", "fromstation", "origin", "departure", "startstation",
                 "start", "出发", "出发站", "始发站"},
        "to": {"to", "tostation", "destination", "arrival", "endstation",
               "end", "到达", "到达站", "终点站"},
        "date": {"date", "traindate", "startdate", "departdate", "traveldate",
                 "日期", "出发日期"},
    }

    def _evaluate_state(self) -> dict:
        st = getattr(self, "_evaluate_history", None)
        if st is None:
            st = {}
            self._evaluate_history = st
        return st

    @staticmethod
    def _evaluate_fingerprint(expression: str) -> str:
        import hashlib
        return hashlib.sha1(" ".join(expression.split()).encode("utf-8", "ignore")).hexdigest()[:16]

    @staticmethod
    def _parse_evaluate_data(result):
        """尽量把 evaluate 返回的 JSON 字符串再解析一层，供一致性校验。"""
        import json as _json
        data = result
        for _ in range(2):
            if isinstance(data, str):
                stripped = data.strip()
                if not stripped or stripped[0] not in "[{":
                    break
                try:
                    data = _json.loads(stripped)
                    continue
                except Exception:
                    break
            break
        return data

    @classmethod
    def _evaluate_record_count(cls, data, depth: int = 0):
        """结构化结果里的“记录条数”：数组长度，或对象内最大数组/常见容器长度。"""
        if depth > 4:
            return None
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            counts = [len(v) for v in data.values() if isinstance(v, list)]
            if counts:
                return max(counts)
            for key in ("data", "result", "list", "rows", "items", "trains", "flights"):
                if key in data:
                    found = cls._evaluate_record_count(data[key], depth + 1)
                    if found is not None:
                        return found
        return None

    @staticmethod
    def _norm_key(key) -> str:
        import re
        return re.sub(r"[\s_\-]", "", str(key)).casefold()

    @staticmethod
    def _norm_value(value) -> str:
        import re
        return re.sub(r"[\s\-_/]", "", str(value)).casefold()

    def _validate_expect(self, expect: dict, data) -> tuple[list[str], bool]:
        """按 expect{from,to,date} 校验抓取记录；返回 (不匹配列表, 是否真正校验过)。"""
        if not isinstance(expect, dict) or not expect:
            return [], False
        aliases = self._EXPECT_KEY_ALIASES
        mismatches: list[str] = []
        checked = False
        seen = 0

        def walk(node, depth=0):
            nonlocal checked, seen
            if depth > 6 or seen > 500:
                return
            if isinstance(node, dict):
                seen += 1
                kind_values = {}
                for kind, names in aliases.items():
                    vals = [v for k, v in node.items()
                            if self._norm_key(k) in names and isinstance(v, (str, int, float))]
                    if vals:
                        kind_values[kind] = vals
                if kind_values:
                    checked = True
                    for kind, expected in expect.items():
                        if kind not in kind_values or expected in (None, ""):
                            continue
                        target = self._norm_value(expected)
                        if not any(self._norm_value(v) == target
                                   or target in self._norm_value(v)
                                   for v in kind_values[kind]):
                            mismatches.append(
                                f"{kind} 期望 '{expected}'，实际 '{kind_values[kind][0]}'")
                for value in node.values():
                    walk(value, depth + 1)
            elif isinstance(node, list):
                for item in node[:200]:
                    walk(item, depth + 1)

        walk(data)
        return mismatches[:8], checked

    @staticmethod
    def _evaluate_looks_failed(text: str) -> str:
        import re
        m = re.search(r"(?:TypeError|ReferenceError|SyntaxError):[^\"\\n]{0,120}"
                      r"|Failed to fetch"
                      r'|"err"\s*:\s*(?!\s*(?:null|false|0)\b)[^,}\n]{0,120}', text or "")
        return m.group(0)[:180] if m else ""

    def _evaluate_circuit(self, fingerprint: str) -> str:
        rec = self._evaluate_state().get(fingerprint) or {}
        until = float(rec.get("open_until") or 0)
        if until <= time.time():
            return ""
        reason = rec.get("open_reason") or "重复失败"
        return (f"[抓取未核验] [重复抓取熔断] 同一表达式在 {int(self._EVALUATE_HISTORY_TTL)} 秒内"
                f"已触发“{reason}”，暂不再执行。不要原样重试；"
                f"请改用 browser_snapshot 读取页面可见结果，或修改表达式/查询条件后重新取证。")

    def _evaluate_record(self, fingerprint: str, text: str, count, failed: str = "") -> str:
        state = self._evaluate_state()
        now = time.time()
        rec = state.get(fingerprint)
        if rec is None or now - float(rec.get("at") or 0) > self._EVALUATE_HISTORY_TTL:
            rec = {"at": now, "count": count, "fails": 0, "inconsistent": 0}
            state[fingerprint] = rec
            if len(state) > self._EVALUATE_MAX_HISTORY:
                for key in sorted(state, key=lambda k: state[k].get("at") or 0)[:-self._EVALUATE_MAX_HISTORY]:
                    state.pop(key, None)
        note = ""
        if failed:
            rec["fails"] = int(rec.get("fails") or 0) + 1
            rec["at"] = now
            if rec["fails"] >= 2:
                rec["open_until"] = now + self._EVALUATE_HISTORY_TTL
                rec["open_reason"] = "连续抓取失败"
            note = (f"[抓取未核验] 页面内取数失败：{failed}。"
                    "失败结果不能作为事实依据；请先确认接口/登录态，或改用页面可见结果。")
        else:
            rec["fails"] = 0
            prev_count = rec.get("count")
            if count is not None and prev_count is not None and count != prev_count:
                rec["inconsistent"] = int(rec.get("inconsistent") or 0) + 1
                if rec["inconsistent"] >= 2:
                    rec["open_until"] = now + self._EVALUATE_HISTORY_TTL
                    rec["open_reason"] = "同一查询返回条数反复变化"
                note = (f"[数据不一致] 同一表达式前次返回 {prev_count} 条，本次返回 {count} 条，"
                        "结果不稳定。不要直接采用本次数据；请先 browser_snapshot 核对页面实际结果，"
                        "或补充 expect(from/to/date) 后重新取证。")
            rec["count"] = count if count is not None else prev_count
            rec["at"] = now
        return note

    def _do_evaluate(self, args: dict) -> str:
        """在页面 JS 上下文中执行表达式并返回结果（JSON 序列化后截断）。
        run5 教训：Agnes 纸飞机按钮的发送函数 .catch() 静默吞异常，
        UI 点击失败时需在页面内直接 fetch 发码接口并读返回 JSON 定位原因。
        表达式里不要做破坏性操作；返回超长时截断。
        frame 参数：在指定 frame 里执行（f1…），默认主 frame。
        expect 可选：查询条件（from/to/date），用于校验抓取数据与查询是否一致。"""
        if (blocked := self._do_guard()):
            return blocked
        expression = (args.get("expression") or "").strip()
        if not expression:
            return "[执行失败] expression 为空"
        fingerprint = self._evaluate_fingerprint(expression)
        if (opened := self._evaluate_circuit(fingerprint)):
            return opened
        frame = self._resolve_frame(args.get("frame"))
        if frame is None:
            frame = self._raw_page.main_frame
        try:
            result = frame.evaluate(expression)
        except Exception as e:
            note = self._evaluate_record(fingerprint, str(e), None, failed=str(e))
            return f"[执行失败] {e}\n{note}"
        import json as _json
        try:
            text = _json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            text = str(result)
        if len(text) > 4000:
            text = text[:4000] + f"...（截断，共 {len(text)} 字符）"
        data = self._parse_evaluate_data(result if len(text) <= 4000 else text)
        failed = self._evaluate_looks_failed(text)
        count = None if failed else self._evaluate_record_count(data)
        record_note = self._evaluate_record(fingerprint, text, count, failed=failed)
        expect = args.get("expect")
        if not failed and isinstance(expect, dict) and expect:
            mismatches, checked = self._validate_expect(expect, data)
            if mismatches:
                record_note = (f"[数据不一致] 抓取结果与查询条件不符：{'；'.join(mismatches)}。"
                               "不要直接采用该数据；请确认字段语义或换查询后重新取证。"
                               f"\n原始结果：{text[:1200]}")
            elif checked:
                record_note = (record_note + " [抓取已核验] from/to/date 与查询条件一致。").strip()
        body = f"[执行结果] {text}"
        return f"{record_note}\n{body}" if record_note else body

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
            names = [str(c.get("name", "")) for c in stored]
            shown_names = ", ".join(names[:20]) + ("…" if len(names) > 20 else "")
            http_only = sum(1 for c in stored if c.get("httpOnly"))
            return (f"[Cookies] profile={name} 共 {len(stored)} 个（HttpOnly {http_only} 个），"
                    f"域名: {', '.join(domains) or '无'}\n"
                    f"名称: {shown_names or '无'}\n"
                    f"提示：cookie 数量/名称只是线索，不能单凭它判定登录态；"
                    f"以页面 UI（头像/退出登录）或业务接口为准。")

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
        landing_note = self._landing_note(url) if url else ""
        return f"[标签页已开] role={role} url={landed}{pin_note}，已设为当前页{landing_note}"

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
        verify_selector = (args.get("verify_selector") or "").strip()
        verify_js = (args.get("verify_js") or "").strip()
        # 人工接管是合法长等待：等待上限必须大于用户操作时限
        return self._run(self._do_request_takeover, reason, timeout_sec,
                         _timeout=timeout_sec + 120.0,
                         verify_selector=verify_selector, verify_js=verify_js)

    def _verify_takeover(self, verify_selector: str, verify_js: str) -> tuple[bool, str]:
        """接管完成后的可选核验：selector 必须存在且可见 / JS 表达式必须返回真值。

        2026-10-28 抖音事故：用户点了【完成】不代表操作真的做完了，旧实现无条件回
        「已完成」并注入快照，子 Agent 继续发评论才发现编辑器仍是空的。
        """
        ok = True
        notes = []
        if verify_selector:
            try:
                _frame, loc, _fid = self._find_locator(verify_selector, "auto")
                seen = bool(loc is not None and loc.count() > 0
                            and loc.first.is_visible(timeout=2000))
            except Exception:
                seen = False
            notes.append(f"选择器 '{verify_selector}' {'可见' if seen else '未找到或不可见'}")
            ok = ok and seen
        if verify_js:
            try:
                val = self._raw_page.evaluate(verify_js)
                err = ""
            except Exception as exc:
                val, err = None, str(exc)[:160]
            notes.append(f"核验脚本返回 {json.dumps(val, ensure_ascii=False, default=str)[:200]}"
                         + (f"（执行失败: {err}）" if err else ""))
            ok = ok and bool(val) and not err
        return ok, "；".join(notes)

    def _do_request_takeover(self, reason: str, timeout_sec: float,
                             verify_selector: str = "", verify_js: str = "") -> str:
        import uuid
        from agent.takeover import get_takeover_manager

        mgr = get_takeover_manager()
        task_id = f"takeover_{uuid.uuid4().hex[:8]}"
        curr_url = self._raw_page.url if self._raw_page else ""

        shot_path = ""
        try:
            self._ensure_maximized()
            # §39.18：接管要让用户直接用真实浏览器操作，先把浏览器推到最前台，
            # 不能让 AlterEgo 自己的窗口占据焦点。
            self._do_focus_window()
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
            # §39.24：泵式等待，保持 Playwright 事件循环在转，页内【✓ 完成接管】才能送达
            res = self._pump_wait_takeover(mgr, task_id, timeout_sec)
        finally:
            if res and res.outcome == "completed":
                self.lock_input_for_agent()
            else:
                self.release_input_guard()

        if res and res.outcome == "completed":
            if verify_selector or verify_js:
                verified, evidence = self._verify_takeover(verify_selector, verify_js)
                if not verified:
                    return (f"[接管完成但核验未通过] 用户已交还控制权，但页面未达到预期状态：{evidence}。\n"
                            f"当前页面 URL: {self._raw_page.url if self._raw_page else curr_url}\n"
                            f"禁止假设用户已经做完：先 browser_snapshot 核对实际状态；若操作确实未完成，"
                            f"继续执行剩余步骤，或再次 browser_request_takeover 并给出可核验的目标。")
                return (f"[人工接管已完成] 用户已在浏览器中完成操作并交还控制权，核验通过：{evidence}。"
                        f"当前自动化已无缝恢复。\n最新页面状态：\n{self._do_snapshot({})}")
            snap = self._do_snapshot({})
            return (f"[人工接管已完成] 用户已在浏览器中完成操作并交还控制权。当前自动化已无缝恢复。"
                    f"\n最新页面状态：\n{snap}")
        elif res and res.outcome == "cancelled":
            return f"[人工接管已取消] 用户点击取消了本次人工接管。当前页面 URL: {curr_url}"
        else:
            return f"[人工接管超时] 用户接管操作已超过时限（{timeout_sec}s）。自动化任务已暂停。当前页面 URL: {curr_url}"


def make_browser_tools(worker: BrowserWorker) -> list[ToolDefinition]:
    def _schema(props, required):
        return {"type": "object", "properties": props, "required": required}

    tools = [
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
                "selector": {"type": "string", "default": "", "description": "目标输入框或占位符的 CSS 选择器（与 ref 二选一）。与 coords 同时给出时以 selector 为准，坐标被忽略；目标不可编辑或无法聚焦时诚实失败，不会改填其它输入框。"},
                "coords": {"type": "array", "items": {"type": "number"}, "description": "目标相对视口坐标 [x, y]（0~1000）。仅在无 ref/selector 时生效；落点不是可编辑元素时诚实失败。"},
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
            name="browser_fill_combo",
            description=(
                "【联想下拉框专用】autocomplete combobox 输入 + 选中（L6）。"
                "用于携程出发地/12306/淘宝这类'输入框 + 联想候选列表'控件——"
                "只往框里写字，城市码/选项值不会提交，提交时仍用旧值（表现为'填不上/搜不到'）。"
                "本工具真实点击聚焦 → 逐键输入触发联想 → 等候选列表出现 → 点击候选落地。"
                "option 可指定要选中的候选可见文本（如 '北京 (BJS)'），留空则自动选含查询词的第一项。"
            ),
            parameters=_schema({
                "text": {"type": "string", "description": "输入到联想框的查询词（如城市名/拼音，如 '天津'）"},
                "option": {"type": "string", "default": "",
                           "description": "要选中的候选可见文本（如 '天津 (TSN)'）；留空=自动选含查询词的第一项"},
                "ref": {"type": "string", "default": "", "description": "【首选】联想输入框句柄，来自 browser_snapshot 的 [ref=aeN]"},
                "selector": {"type": "string", "default": "", "description": "联想输入框 CSS 选择器（未传 ref 时用）"},
                "timeout": {"type": "number", "default": 3.0, "description": "等待候选列表出现的最长秒数（0.5~10）"},
                "frame": {"type": "string", "default": "auto", "description": "目标所在 frame，默认 auto 全搜"},
                "expect": {"type": "object", "default": {}, "description": "选中后的期望状态（5秒内核验）"}
            }, ["text"]),
            func=worker.fill_combo),
        ToolDefinition(
            name="browser_paste",
            description=(
                "【OS 级输入终极通道 L5】写系统剪贴板 → 发真实 Ctrl+V。"
                "当 browser_fill_input / browser_type 对 state 门控编辑器"
                "（Draft.js/brt/ProseMirror/React 受控组件）失效时用——DOM 有字但 React state "
                "不接收，合成事件与 DOM 赋值都无效，只有 isTrusted 的真实粘贴事件能驱动 state "
                "更新（发布按钮点亮/占位符消失）。可传 text 现写现贴，或设 from_clipboard=true "
                "沿用系统剪贴板现有内容。"
            ),
            parameters=_schema({
                "text": {"type": "string", "default": "", "description": "要粘贴的文本（from_clipboard=true 时可空）"},
                "from_clipboard": {"type": "boolean", "default": False,
                                   "description": "true=沿用系统剪贴板现有内容（不覆盖剪贴板），默认 false 用 text"},
                "ref": {"type": "string", "default": "", "description": "【首选】目标元素句柄，来自 browser_snapshot 的 [ref=aeN]"},
                "selector": {"type": "string", "default": "", "description": "目标元素 CSS 选择器（未传 ref 时用）"},
                "clear_before": {"type": "boolean", "default": True, "description": "粘贴前是否清空已有内容（Ctrl+A + Backspace），默认 true"},
                "press_enter": {"type": "boolean", "default": False, "description": "粘贴后是否回车提交，默认 false"},
                "frame": {"type": "string", "default": "auto", "description": "目标所在 frame，默认 auto 全搜"},
                "expect": {"type": "object", "default": {}, "description": "粘贴后的期望状态（5秒内核验）"}
            }, []),
            func=worker.paste),
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
            description=("在当前页面 JS 上下文中执行表达式并返回结果。用于读取页面状态（如按钮是否可点）、"
                         "或在 UI 点击静默失败时直接调用页面背后的接口并读取返回 JSON 定位原因。"
                         "只读表达式优先；不要做破坏性操作。查询车次/航班/酒店等结构化数据时，"
                         "请传 expect（from/to/date），系统会校验返回记录是否与查询条件一致；"
                         "同一表达式重复失败或返回条数反复变化会被熔断，需改用页面可见结果。"
                         "frame 参数可选：在指定 frame 里执行（f1…，见 snapshot 的 [Frames] 清单），默认主 frame。"),
            parameters=_schema({"expression": {"type": "string", "description": "要执行的 JS 表达式，如 \"document.querySelector('img[alt=\\\"send\\\"]')?.outerHTML\" 或 \"fetch(url).then(r=>r.json()).then(j=>JSON.stringify(j))\""},
                                "frame": {"type": "string", "default": "auto", "description": "执行的 frame（f0=主，f1/f2…=子 frame）；默认主 frame"},
                                "expect": {"type": "object", "description": "可选查询条件，用于校验抓取结果：{\"from\":\"天津\",\"to\":\"深圳\",\"date\":\"2026-10-22\"}"}}, ["expression"]),
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
            name="browser_react_probe",
            description=("React 内部状态取证：读取目标元素的 fiber 组件链、props 事件绑定（onClick 等）"
                         "与最近函数组件的 hook 状态（文本/布尔标志）。点击后零请求时，"
                         "用来区分'handler 没绑上/根本没执行'与'前置校验或提交标志卡死拦截'。"
                         "支持 ref（快照句柄，首选）或 selector。"),
            parameters=_schema({"ref": {"type": "string", "default": "", "description": "快照中的元素句柄，如 ae74"},
                                "selector": {"type": "string", "default": "", "description": "CSS 选择器（与 ref 二选一）"},
                                "frame": {"type": "string", "default": "auto", "description": "frame 引用：auto/f0/f1…"}}, []),
            func=worker.react_probe),
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
            description=("查看浏览器存储（登录态/会话调试）。action: cookies（值脱敏截断，可传 name 过滤）/"
                         "local（localStorage）/ session（sessionStorage）。"
                         "注意：cookie 清单只是线索，不能单凭它判定“未登录”（HttpOnly cookie 不参与页面读取），"
                         "登录态以页面 UI（头像/退出登录）或业务接口为准。"),
            parameters=_schema({"action": {"type": "string", "default": "cookies", "description": "cookies/local/session"},
                                "name": {"type": "string", "default": "", "description": "cookie 名称过滤（子串，仅 action=cookies 生效）"}}, []),
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
            parameters=_schema({
                "reason": {"type": "string", "description": "需要用户接管的具体原因，如'遇到极验滑块验证码'、'需要扫码登录'"},
                "timeout_sec": {"type": "number", "default": 7200,
                                "description": "等待用户操作的最长秒数（默认 7200）"},
                "verify_selector": {"type": "string", "default": "",
                                    "description": "可选。用户点【完成】后必须存在且可见的选择器（如 '.comment-item'）；不满足会返回 [接管完成但核验未通过]"},
                "verify_js": {"type": "string", "default": "",
                              "description": "可选。用户点【完成】后求值为真才算完成的 JS 表达式（如 document.querySelector('.comment-item') !== null）"},
            }, ["reason"]),
            func=worker.request_takeover),
        ToolDefinition(
            name="browser_lock_input",
            description="（§15 防误触）启用 DOM 层防误触遮罩，拦截网页内容区的物理键鼠输入，同时放行顶部人工接管条。Agent 自动化执行时使用。",
            parameters=_schema({}, []),
            # 工具函数运行在 asyncio 线程池，不能直接碰 Playwright，必须编组到 pw_* 工作线程
            func=lambda args: worker._run(worker._do_lock_input_tool),
        ),
        ToolDefinition(
            name="browser_unlock_input",
            description="（§15 防误触）解除 DOM 层防误触遮罩，允许用户直接交互操作网页。进入人工接管状态时调用。",
            parameters=_schema({}, []),
            func=lambda args: worker._run(worker._do_unlock_input_tool),
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

    readonly_tools = {
        "browser_snapshot", "browser_screenshot", "browser_console",
        "browser_network_log", "browser_inspect", "browser_resource",
        "browser_react_probe",
        "browser_storage", "browser_observe", "browser_find_text",
        "browser_probe_at", "browser_context_list", "browser_verify_code_countdown",
    }
    for t in tools:
        t.parallel_safe = False
        t.is_readonly = t.name in readonly_tools

    return tools

# [M3 完成] 接管模式（用户接管浏览器窗口，见 spec §15/§31.5）已完整实现。
