"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "ui", "static", "app.js"), "utf8");
const nodes = new Map([
  ["status-current-model", { textContent: "" }],
  ["status-model-desc", { textContent: "" }],
]);
let events;
let avatarModel;
const context = vm.createContext({
  window: { location: { origin: "http://127.0.0.1:5000", search: "" } },
  URLSearchParams, console, Date, setTimeout, clearTimeout, setInterval, clearInterval,
  document: { addEventListener() {}, getElementById: id => nodes.get(id) || null },
  EventSource: class {
    constructor() { this.handlers = {}; events = this; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
  },
});
vm.runInContext(source, context, { filename: "app.js" });
// Exercise real SSE and status rendering; the avatar's animation DOM is irrelevant here.
context.renderAgentState = data => { avatarModel = data.model; };
vm.runInContext(`UI.store.status.main_model = "primary";
  UI.store.status.fallback_model = "fallback";
  UI.store.status.current_model = "primary";
  UI.store.agentState.model = "primary";
  renderStatusTab(UI.store.status);`, context);
context.connectSSE();
const emit = (name, data) => events.handlers[name]({ data: JSON.stringify(data) });
const currentModel = () => vm.runInContext("UI.store.status.current_model", context);

emit("agent_state", { state: "thinking", model: "actual-fallback", request_attempted: true });
assert.equal(currentModel(), "actual-fallback");
assert.equal(nodes.get("status-current-model").textContent, "actual-fallback");
assert.equal(avatarModel, "actual-fallback");
assert.match(nodes.get("status-model-desc").textContent, /primary.*fallback/);

emit("agent_state", { state: "thinking", model: "blocked-candidate", request_attempted: false });
assert.equal(currentModel(), "actual-fallback", "a skipped candidate is not the current model");
assert.equal(nodes.get("status-current-model").textContent, "actual-fallback");
assert.equal(avatarModel, "actual-fallback");

// AgentServer keeps its last observed actual model on outer completion events.
emit("agent_state", { state: "done", model: "actual-fallback" });
emit("token_usage", { input: 3, output: 4 });
assert.equal(currentModel(), "actual-fallback");
assert.equal(nodes.get("status-current-model").textContent, "actual-fallback");

emit("agent_state", { state: "thinking", model: "primary", request_attempted: true });
assert.equal(currentModel(), "primary", "a later real primary request may become current");
assert.equal(nodes.get("status-current-model").textContent, "primary");
console.log("UI actual model status: fallback, skipped candidate, completion and next request passed");
