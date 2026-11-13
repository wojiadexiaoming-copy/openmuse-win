"use strict";
/* P6 后回归：持久化错误消息只渲染一次（SSE 先到 / HTTP 先到 / 断网本地兜底）。
 * 用 jsdom 跑真实 index.html + app.js，只 mock 网络与 EventSource。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "ui", "index.html"), "utf8");
const APP = fs.readFileSync(path.join(ROOT, "ui", "static", "app.js"), "utf8");

const PERSISTED = {
  id: "msg-runtime-error", role: "agent", content: "Task failed",
  type: "assistant_final", status: "error",
};

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function boot(order, networkFailure) {
  const dom = new JSDOM(HTML, {
    url: "http://127.0.0.1:5000/?token=T&desktop=1",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  window.matchMedia = window.matchMedia || (() => ({
    matches: false, addListener() {}, removeListener() {},
    addEventListener() {}, removeEventListener() {},
  }));
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.scrollTo = function () {};

  let source;
  class EventSourceMock {
    constructor() { this.handlers = {}; source = this; }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() {}
  }
  window.EventSource = EventSourceMock;
  const emit = () => source.handlers.message({ data: JSON.stringify(PERSISTED) });

  let chatCalls = 0;
  window.fetch = async (u, init) => {
    const url = String(u);
    const method = (init && init.method) || "GET";
    if (method === "POST" && url.includes("/api/chat")) {
      chatCalls++;
      if (networkFailure) throw new Error("offline");
      if (order === "sse-first") emit();
      return jsonResponse({ error: "Task failed", message: PERSISTED }, 500);
    }
    if (url.includes("/api/state/snapshot")) {
      return jsonResponse({
        agent_state: { state: "idle", detail: "就绪" }, activity: [], approvals: [],
        active_takeover: null, plan: [],
        status: { current_model: "auto", token_usage: { input: 0, output: 0, known_requests: 0, unknown_requests: 0 } },
      });
    }
    if (url.includes("/api/sessions")) return jsonResponse([]);
    if (url.includes("/api/tasks/running")) return jsonResponse([]);
    if (url.includes("/api/models")) {
      return jsonResponse({ current: "auto", models: [{ id: "auto", name: "EchoBird", has_key: true }], roles: {} });
    }
    if (url.includes("/api/avatar/themes")) return jsonResponse({ themes: [] });
    if (url.includes("/api/approvals/history")) return jsonResponse([]);
    return jsonResponse({ ok: true });
  };

  const errors = [];
  window.addEventListener("error", (e) => errors.push(String(e.message)));
  window.addEventListener("unhandledrejection", (e) => errors.push("reject: " + String(e.reason)));
  window.eval(APP);
  return { window, emit, errors, chatCalls: () => chatCalls };
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function run(order, networkFailure) {
  const env = boot(order, networkFailure);
  await wait(180);   // DOMContentLoaded → restore()

  const doc = env.window.document;
  const input = doc.getElementById("composer-input");
  input.value = "test request";
  env.window.__alterego.sendMessage();
  await wait(140);   // /api/chat 结算

  if (order === "http-first" && !networkFailure) env.emit();  // 迟到的 SSE 不得重复渲染
  await wait(80);

  const msgs = [...doc.querySelectorAll("#view-chat .msg")];
  const persisted = doc.querySelectorAll("#msg-msg-runtime-error");
  if (networkFailure) {
    assert.equal(msgs.length, 2, "network failure keeps a local fallback message");
    assert.equal(persisted.length, 0, "network failure must not fake a persisted id");
    assert.ok(msgs[1].textContent.includes("请求失败"), "fallback text: " + msgs[1].textContent);
  } else {
    assert.equal(persisted.length, 1, `${order} must render one persisted error`);
    assert.equal(msgs.length, 2, "one user message and one error");
    assert.ok(msgs[1].textContent.includes("Task failed"));
  }
  assert.equal(doc.querySelectorAll("#view-chat .thinking").length, 0,
    "thinking placeholder must be cleared");
  assert.equal(input.disabled, false);
  input.value = "next";
  input.dispatchEvent(new env.window.Event("input", { bubbles: true }));
  assert.equal(doc.getElementById("btn-send").disabled, false, "composer must be usable again");
  assert.equal(env.errors.length, 0, env.errors.join("; "));
}

(async () => {
  await run("sse-first", false);
  await run("http-first", false);
  await run("http-first", true);
  console.log("UI persisted error delivery: 3 scenarios passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
