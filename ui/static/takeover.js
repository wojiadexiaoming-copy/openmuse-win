/* AlterEgo 接管浮窗 · 逻辑
 * 由原生小窗口（pywebview 无边框置顶）加载 takeover.html?task_id=xxx&target=Chrome
 * 后端约定见 api.md。 */
"use strict";

const API = (window.location.origin && window.location.origin !== "null" && !window.location.origin.startsWith("file:"))
  ? window.location.origin
  : "http://localhost:5000";
const params = new URLSearchParams(location.search);
const TASK_ID = params.get("task_id") || "demo";
const TARGET = params.get("target") || "Chrome";

const titleEl = document.getElementById("takeover-title");
const timerEl = document.getElementById("takeover-timer");
const barEl = document.getElementById("bar");
const start = Date.now();
let finished = false;

titleEl.textContent = `影仔 接管中：${TARGET}`;

/* 计时器 */
setInterval(() => {
  const s = Math.floor((Date.now() - start) / 1000);
  timerEl.textContent =
    `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  if (s > 600) timerEl.classList.add("warn");  // 超过10分钟标黄提醒
}, 1000);

/* 完成 / 取消 */
document.getElementById("btn-done").onclick = () => finish("done");
document.getElementById("btn-cancel").onclick = () => finish("cancel");

async function finish(kind) {
  if (finished) return;
  finished = true;
  const path = kind === "done"
    ? `/api/takeover/${TASK_ID}/done`
    : `/api/takeover/${TASK_ID}/cancel`;
  try {
    await fetch(API + path, { method: "POST" });
  } catch (e) {
    // 后端连不上也照关窗口，不卡死用户（fail safe）
    console.warn("takeover api unreachable:", e.message);
  }
  // 给原生壳一个关闭信号：优先调 pywebview bridge，降级直接关
  if (window.pywebview && window.pywebview.api && window.pywebview.api.close) {
    window.pywebview.api.close();
  } else {
    barEl.innerHTML = `<div class="title" style="width:100%;text-align:center">${
      kind === "done" ? "✓ 已交还给影仔" : "已取消接管"}</div>`;
    barEl.classList.add("done");
    setTimeout(() => window.close(), 1200);
  }
}

/* 兜底：如果后端主动结束了接管（如超时），轮询状态后自动关窗 */
setInterval(async () => {
  if (finished) return;
  try {
    const r = await fetch(`${API}/api/takeover/${TASK_ID}/status`);
    const s = await r.json();
    if (s.status !== "active") finish(s.status === "completed" ? "done" : "cancel");
  } catch (e) { /* 后端未启动时静默，纯演示 */ }
}, 5000);

/* 全局快捷键备用：Ctrl+Shift+Alt+D = 完成（由原生壳注册，页面内仅做提示） */
document.addEventListener("keydown", e => {
  if (e.ctrlKey && e.shiftKey && e.altKey && e.key.toLowerCase() === "d") finish("done");
});
