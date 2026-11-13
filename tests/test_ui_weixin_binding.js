"use strict";
/* v1.5.29：设置弹窗「微信」标签页回归——真实 index.html + app.js，只 mock 网络与 SSE。
 * 覆盖：标签切换拉状态、授权过期文案、生成二维码（图片地址带 token）、
 *       weixin_login_progress=confirmed 后徽标与二维码区切到已连接。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "ui", "index.html"), "utf8");
const APP = fs.readFileSync(path.join(ROOT, "ui", "static", "app.js"), "utf8");

const state = {
  status: { connected: false, token_expired: true, user_id: "", has_active_qr: false, qr_image_exists: false },
  loginCalls: 0,
};
const sseHandlers = {};

function jsonResponse(body) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const dom = new JSDOM(HTML, {
  url: "http://127.0.0.1:5000/?token=T&desktop=1",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const { window } = dom;
window.matchMedia = window.matchMedia || (() => ({
  matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
}));
window.HTMLElement.prototype.scrollIntoView = function () {};
window.scrollTo = function () {};
window.fetch = (u, init) => {
  const url = String(u);
  const method = (init && init.method) || "GET";
  if (url.includes("/api/weixin/status")) return Promise.resolve(jsonResponse(state.status));
  if (url.includes("/api/weixin/login") && method === "POST") {
    state.loginCalls += 1;
    state.status = { connected: false, token_expired: false, user_id: "", has_active_qr: true, qr_image_exists: true };
    return Promise.resolve(jsonResponse({
      status: "waiting_scan", qrcode: "q1",
      qrcode_url: "https://weixin.example/q", qr_image_path: "/tmp/weixin_qr.png",
    }));
  }
  if (url.includes("/api/models")) {
    return Promise.resolve(jsonResponse({ current: "auto", models: [], roles: {} }));
  }
  return Promise.resolve(jsonResponse({}));
};
window.EventSource = class {
  constructor() {}
  addEventListener(name, fn) { sseHandlers[name] = fn; }
  close() {}
};
window.eval(APP);

const doc = window.document;
const $ = (s) => doc.querySelector(s);
const click = (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
const tick = (ms = 40) => new Promise((r) => setTimeout(r, ms));

(async () => {
  await tick(160);

  click($("#btn-settings"));
  await tick();
  const tab = $('[data-set-tab="weixin"]');
  assert.ok(tab, "设置弹窗必须有微信标签");
  click(tab);
  await tick(80);
  assert.ok(!$('[data-set-panel="weixin"]').hidden, "微信面板应可见");
  assert.match($("#weixin-status-text").textContent, /72 小时/, $("#weixin-status-text").textContent);
  assert.match($("#weixin-badge").textContent, /授权已过期/, $("#weixin-badge").textContent);

  state.status = {
    connected: false, token_expired: false, credential_missing: true,
    user_id: "", has_active_qr: false, qr_image_exists: false,
  };
  click(tab);
  await tick(60);
  assert.match($("#weixin-status-text").textContent, /没有微信授权凭证/, $("#weixin-status-text").textContent);
  assert.match($("#weixin-badge").textContent, /未登录/, $("#weixin-badge").textContent);

  click($("#btn-weixin-qr"));
  await tick(80);
  assert.equal(state.loginCalls, 1, "应调用 /api/weixin/login");
  const img = $("#weixin-qr-box img");
  assert.ok(img, "生成后必须出现二维码图片");
  const src = img.getAttribute("src");
  assert.ok(src.includes("/api/weixin/qr_img?t="), src);
  assert.ok(src.includes("token=T"), "二维码 URL 必须带 token: " + src);

  state.status = { connected: true, token_expired: false, user_id: "wx_u1", has_active_qr: false, qr_image_exists: true };
  assert.ok(sseHandlers["weixin_login_progress"], "必须监听 weixin_login_progress");
  sseHandlers["weixin_login_progress"]({ data: JSON.stringify({ status: "confirmed", user_id: "wx_u1" }) });
  await tick(80);
  assert.match($("#weixin-badge").textContent, /已连接/, $("#weixin-badge").textContent);
  assert.match($("#weixin-qr-box").textContent, /已绑定/, $("#weixin-qr-box").textContent);

  state.status = {
    connected: true, token_expired: false, user_id: "wx_u1", has_active_qr: false,
    qr_image_exists: true, expires_soon: true, remaining_hours: 3.5,
  };
  click(tab);
  await tick(60);
  assert.match($("#weixin-badge").textContent, /即将到期/, $("#weixin-badge").textContent);
  assert.match($("#weixin-status-text").textContent, /3\.5 小时/, $("#weixin-status-text").textContent);

  state.status = {
    connected: true, token_expired: false, user_id: "wx_u1", has_active_qr: false,
    qr_image_exists: false,
    last_error: "getupdates 返回非 JSON 响应: '<html>'", last_error_source: "poll",
  };
  click(tab);
  await tick(80);
  assert.match($("#weixin-badge").textContent, /通道异常/, $("#weixin-badge").textContent);
  assert.match($("#weixin-status-text").textContent, /getupdates/, $("#weixin-status-text").textContent);

  process.stdout.write("weixin settings OK\n");
})().catch((e) => {
  console.error(e && e.stack || e);
  process.exit(1);
});
