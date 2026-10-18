"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const nodes = new Map([["activity-timeline", { innerHTML: "" }]]);
const context = vm.createContext({
  window: { location: { origin: "http://127.0.0.1:5000", search: "" } },
  URLSearchParams, console, Date, setTimeout, clearTimeout, setInterval, clearInterval,
  document: { addEventListener() {}, getElementById: id => nodes.get(id) || null,
    createElement: () => ({ innerHTML: "", className: "", id: "" }) },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "ui", "static", "app.js"), "utf8"),
                context, { filename: "app.js" });
const message = statuses => context.messageNode({
  id: "offline-tools", role: "agent", content: "", created_at: "02:00",
  tool_calls: statuses.map(status => ({ status })),
}).innerHTML;

let html = message(["error", "cancelled", "ok"]);
assert.match(html, /已执行 2 项工具（1 项成功，1 项未执行）/);
html = message(["cancelled", "cancelled"]);
assert.match(html, /已执行 0 项工具（0 项成功，2 项未执行）/);
html = message(["ok", "error"]);
assert.match(html, /已执行 2 项工具（1 项成功）/);
assert.doesNotMatch(html, /未执行/);

context.renderActivityTab(["cancelled", "error", "ok"].map(status => ({
  status, tool: "desktop_click", summary: "offline action", duration_ms: 0,
})));
const activity = nodes.get("activity-timeline").innerHTML;
assert.equal((activity.match(/未执行/g) || []).length, 1);
assert.equal((activity.match(/失败 ✗/g) || []).length, 1);
assert.equal((activity.match(/成功 ✓/g) || []).length, 1);
console.log("UI tool control: cancelled actions are not executed/successful; mixed and normal counts passed");
