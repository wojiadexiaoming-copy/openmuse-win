"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "ui", "static", "app.js"), "utf8");
const nodes = new Map();
const label = { textContent: "Token consumption" };
const card = { querySelector: () => label, appendChild: node => nodes.set(node.id, node) };
for (const id of ["token-input", "token-output"]) nodes.set(id, { textContent: "", closest: () => card });
let events;
const context = vm.createContext({
  window: { location: { origin: "http://127.0.0.1:5000", search: "" } },
  URLSearchParams, console, Date, setTimeout, clearTimeout, setInterval, clearInterval,
  document: { addEventListener() {}, getElementById: id => nodes.get(id) || null,
              createElement: () => ({ textContent: "" }) },
  EventSource: class {
    constructor() { this.handlers = {}; events = this; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
  },
});
vm.runInContext(source, context, { filename: "app.js" });
context.renderAgentState = context.renderPlanTab = context.renderApprovalsTab = context.renderActivityTab = () => {};
vm.runInContext("renderStatusTab(UI.store.status)", context);
assert.equal(nodes.get("token-input").textContent, "暂无数据");
assert.match(label.textContent, /本次应用已记录请求/);
assert.match(nodes.get("token-usage-summary").textContent, /非账号余额或金额/);
context.connectSSE();
const emit = data => events.handlers.token_usage({ data: JSON.stringify(data) });
const snapshot = (revision, input, output, known, unknown, source_id = "run-a") => ({
  source_id, revision, scope: "application_recorded_requests", input, output,
  known_requests: known, unknown_requests: unknown,
});
const current = () => vm.runInContext("UI.store.status.token_usage", context);

emit(snapshot(1, null, null, 0, 1));
assert.equal(nodes.get("token-input").textContent, "暂无数据");
assert.match(nodes.get("token-usage-summary").textContent, /暂无数据，1次未知/);
emit(snapshot(3, 17, 5, 2, 1));
assert.equal(nodes.get("token-input").textContent, "17");
assert.equal(nodes.get("token-output").textContent, "5");
assert.match(nodes.get("token-usage-summary").textContent, /已知用量，1次未知/);
// Duplicates, reordered cumulative events and old delta events never add again.
emit(snapshot(3, 17, 5, 2, 1));
emit(snapshot(2, 10, 3, 1, 1));
emit({ input: 999, output: 999 });
assert.equal(current().revision, 3);
assert.equal(nodes.get("token-input").textContent, "17");

(async () => {
  // An older in-flight HTTP snapshot cannot roll usage back after a newer SSE event.
  context.api = async () => ({ status: { token_usage: snapshot(2, 10, 3, 1, 1), memory_writes: [] } });
  await context.loadStateSnapshot();
  assert.equal(current().revision, 3);
  assert.equal(nodes.get("token-input").textContent, "17");
  // A new application process resets usage only through an authoritative state snapshot.
  emit(snapshot(1, 1, 1, 1, 0, "run-b"));
  assert.equal(current().source_id, "run-a");
  context.api = async () => ({ status: { token_usage: snapshot(0, null, null, 0, 0, "run-b"), memory_writes: [] } });
  await context.loadStateSnapshot();
  assert.equal(current().source_id, "run-b");
  assert.equal(nodes.get("token-input").textContent, "暂无数据");
  emit(snapshot(4, 100, 100, 3, 1, "run-a"));
  assert.equal(current().source_id, "run-b");
  emit(snapshot(1, 0, 0, 1, 0, "run-b"));
  assert.equal(nodes.get("token-input").textContent, "0");
  assert.match(nodes.get("token-usage-summary").textContent, /已知用量，0次未知/);
  console.log("UI token usage: unknown/known, dedupe, ordering, HTTP restore and process reset passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
