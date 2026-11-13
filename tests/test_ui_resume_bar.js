"use strict";
/* v1.5.18 重启后未完成任务提示条：显示、继续、不再提醒与跨会话隐藏。
 * 用 jsdom 跑真实 index.html + app.js，只 mock 网络与 EventSource。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "ui", "index.html"), "utf8");
const APP = fs.readFileSync(path.join(ROOT, "ui", "static", "app.js"), "utf8");

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function boot(options) {
  const opts = options || {};
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

  class EventSourceMock {
    constructor() { this.handlers = {}; }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() {}
  }
  window.EventSource = EventSourceMock;

  const calls = { chat: [], dismiss: 0, pending: 0 };
  window.fetch = async (u, init) => {
    const url = String(u);
    const method = (init && init.method) || "GET";
    if (url.includes("/api/pending_resume")) {
      calls.pending++;
      if (!opts.pending) return jsonResponse({ has_pending: false });
      return jsonResponse({ has_pending: true, task: opts.pending });
    }
    if (method === "POST" && url.includes("/api/dismiss_resume")) {
      calls.dismiss++;
      return jsonResponse({ ok: true, persisted: true });
    }
    if (method === "POST" && url.includes("/api/chat")) {
      calls.chat.push(JSON.parse(init.body || "{}"));
      return jsonResponse({ ok: true });
    }
    if (url.includes("/api/state/snapshot")) {
      return jsonResponse({
        agent_state: { state: "idle", detail: "就绪" }, activity: [], approvals: [],
        active_takeover: null, plan: [],
        status: { current_model: "auto", token_usage: { input: 0, output: 0, known_requests: 0, unknown_requests: 0 } },
      });
    }
    if (url.match(/\/api\/sessions\/[^/]+\/messages/)) return jsonResponse([]);
    if (url.includes("/api/sessions")) {
      return jsonResponse(opts.sessions || [{ id: "s-1", title: "会话一" }]);
    }
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
  return { window, errors, calls };
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const PENDING = {
  session_id: "s-1",
  task_desc: "注册 Today AI 账号并完成手机验证",
  task_status: "waiting_user",
  task_origin_id: "uid-1",
};

async function scenarioShowsPending() {
  const env = boot({ pending: PENDING });
  await wait(260);
  const doc = env.window.document;
  const bar = doc.getElementById("resume-bar");
  assert.equal(bar.hidden, false, "pending task must show the resume bar");
  assert.ok(doc.getElementById("resume-bar-title").textContent.includes("等待你补充"),
    "title must expose the honest status: " + doc.getElementById("resume-bar-title").textContent);
  assert.ok(doc.getElementById("resume-bar-desc").textContent.includes("注册 Today AI"),
    "desc must show the task text");
  assert.equal(env.errors.length, 0, env.errors.join("; "));
}

async function scenarioDismissPersists() {
  const env = boot({ pending: PENDING });
  await wait(260);
  const doc = env.window.document;
  doc.getElementById("resume-bar-dismiss").click();
  await wait(80);
  assert.equal(doc.getElementById("resume-bar").hidden, true, "dismiss hides the bar");
  assert.equal(env.calls.dismiss, 1, "dismiss must persist to the backend");
  assert.equal(env.errors.length, 0, env.errors.join("; "));
}

async function scenarioContinueSendsMessage() {
  const env = boot({ pending: PENDING });
  await wait(260);
  const doc = env.window.document;
  doc.getElementById("resume-bar-continue").click();
  await wait(120);
  assert.equal(doc.getElementById("resume-bar").hidden, true, "continue hides the bar");
  assert.equal(env.calls.chat.length, 1, "continue must start one turn");
  assert.equal(env.calls.chat[0].content, "继续", "continue must send the resume keyword");
  assert.equal(env.calls.chat[0].session_id, "s-1", "continue must stay in the pending session");
  assert.equal(env.errors.length, 0, env.errors.join("; "));
}

async function scenarioHidesForOtherSession() {
  const env = boot({
    pending: { ...PENDING, session_id: "s-9" },
    sessions: [{ id: "s-1", title: "会话一" }],
  });
  await wait(260);
  const doc = env.window.document;
  assert.equal(doc.getElementById("resume-bar").hidden, true,
    "bar must stay hidden when the pending task belongs to another session");
  assert.equal(env.errors.length, 0, env.errors.join("; "));
}

async function scenarioNoPending() {
  const env = boot({ pending: null });
  await wait(220);
  const doc = env.window.document;
  assert.equal(doc.getElementById("resume-bar").hidden, true, "no pending → hidden bar");
  assert.equal(env.calls.pending >= 1, true, "frontend must query pending_resume on boot");
  assert.equal(env.errors.length, 0, env.errors.join("; "));
}

(async () => {
  await scenarioShowsPending();
  await scenarioDismissPersists();
  await scenarioContinueSendsMessage();
  await scenarioHidesForOtherSession();
  await scenarioNoPending();
  console.log("UI resume bar: 5 scenarios passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
