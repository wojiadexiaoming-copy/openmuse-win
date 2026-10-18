"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "ui", "static", "app.js"), "utf8");

async function runDelivery(order, networkFailure = false) {
  const nodes = new Map();
  const messages = {
    children: [], scrollHeight: 100, scrollTop: 0,
    appendChild(node) { this.children.push(node); nodes.set(node.id, node); },
  };
  nodes.set("messages", messages);
  for (const id of ["input", "btn-send", "file-input", "btn-attach"]) {
    nodes.set(id, { value: "", style: {}, disabled: false, scrollHeight: 20,
      addEventListener() {}, focus() {}, click() {} });
  }
  const persisted = { id: "msg-runtime-error", role: "agent", content: "Task failed",
    type: "assistant_final", status: "error" };
  let eventSource;
  class EventSourceMock {
    constructor() { this.handlers = {}; eventSource = this; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
  }
  const emit = () => eventSource.handlers.message({ data: JSON.stringify(persisted) });
  const context = vm.createContext({
    window: { location: { origin: "http://127.0.0.1:5000", search: "" } },
    URLSearchParams, console, Date, setTimeout, clearTimeout, setInterval, clearInterval,
    document: { addEventListener() {}, getElementById: id => nodes.get(id) || null },
    EventSource: EventSourceMock,
    fetch: async () => {
      if (networkFailure) throw new Error("offline");
      if (order === "sse-first") emit();
      return { ok: false, status: 500,
        json: async () => ({ error: "Task failed", message: persisted }) };
    },
  });
  vm.runInContext(source, context, { filename: "app.js" });
  // Real api(), send(), appendMessage() and SSE handlers; only rendering primitives are mocked.
  context.messageNode = message => ({ id: `msg-${message.id}`, content: message.content });
  context.highlightCodeBlocks = () => {};
  context.toast = () => {};
  context.renderAgentState = () => {};
  context.connectSSE();
  context.bindComposer();
  nodes.get("input").value = "test request";
  await nodes.get("btn-send").onclick();
  if (!networkFailure && order === "http-first") emit();
  const errors = messages.children.filter(node => node.id === "msg-msg-runtime-error");
  if (networkFailure) {
    assert.equal(messages.children.length, 2, "network failure keeps a local fallback message");
    assert.equal(errors.length, 0);
  } else {
    assert.equal(errors.length, 1, `${order} must render one persisted error`);
    assert.equal(messages.children.length, 2, "one user message and one error");
    assert.equal(errors[0].content, "Task failed");
  }
  assert.equal(nodes.get("input").disabled, false);
  assert.equal(nodes.get("btn-send").disabled, false);
}

(async () => {
  await runDelivery("sse-first");
  await runDelivery("http-first");
  await runDelivery("network", true);
  console.log("UI persisted error delivery: 3 scenarios passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
