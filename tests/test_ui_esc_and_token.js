"use strict";
/* v2.5 V1/V2/V3 回归：esc 引号转义、jsq 属性安全、md 消毒降级、token 去 URL 化。 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadApp(windowOverrides) {
  const context = vm.createContext(Object.assign({
    window: { location: { origin: "http://127.0.0.1:5000", search: "" } },
    URLSearchParams, console, Date, setTimeout, clearTimeout, setInterval, clearInterval,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    sessionStorage: { _m: new Map(), getItem(k) { return this._m.get(k) ?? null; },
                      setItem(k, v) { this._m.set(k, v); }, removeItem(k) { this._m.delete(k); } },
    history: { replaceState() {} },
    document: { addEventListener() {}, getElementById: () => null,
      createElement: () => ({ innerHTML: "", className: "", id: "" }) },
  }, windowOverrides || {}));
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "ui", "static", "app.js"), "utf8"),
                  context, { filename: "app.js" });
  return context;
}

/* V1: esc 必须转义引号（旧版只转 & < >，属性可被引号闭合） */
{
  const ctx = loadApp();
  const out = ctx.esc(`x" onmouseover="alert(1) ' y`);
  assert.ok(!out.includes('"'), "esc 输出不得含原始双引号: " + out);
  assert.ok(!out.includes("'"), "esc 输出不得含原始单引号: " + out);
  assert.equal(out.includes("&quot;") && out.includes("&#39;"), true);
}

/* V1: jsq 产物在 HTML 属性解码后必须是合法 JSON 字面量，无法逃逸出双引号属性 */
{
  const ctx = loadApp();
  const evil = `'); alert(1); ('`;
  const attr = `onclick="selectSettingModel(${ctx.jsq(evil)})"`;
  assert.ok(!/[^\"]\"[^\"]*$/.test(attr.replace(/^onclick="/, "").replace(/"$/, "")),
    "jsq 产物不得提前闭合双引号属性: " + attr);
  // 模拟 HTML 属性实体解码后 JS 引擎实际看到的源码
  const decoded = ctx.jsq(evil).replace(/&quot;/g, '"').replace(/&amp;/g, "&");
  const parsed = JSON.parse(decoded); // 能被 JSON.parse = 合法 JS 字符串字面量
  assert.equal(parsed, evil);
}

/* V2: 有 marked 无 DOMPurify 时，md() 严禁返回原始 HTML */
{
  const ctx = loadApp({
    window: { location: { origin: "http://127.0.0.1:5000", search: "" },
              marked: { parse: () => `<img src=x onerror="alert(1)"><b>bold</b>` } },
  });
  const out = ctx.md("hello");
  assert.ok(!out.includes("<img"), "DOMPurify 缺失时不得直插原始 HTML: " + out);
  assert.ok(!out.includes("<b>"), "降级管线必须是纯文本: " + out);
  assert.ok(out.includes("hello"));
}

/* V2: marked + DOMPurify 齐备时走消毒路径 */
{
  const ctx = loadApp({
    window: { location: { origin: "http://127.0.0.1:5000", search: "" },
              marked: { parse: () => `<b>bold</b>` },
              DOMPurify: { sanitize: (h) => h } },
  });
  assert.equal(ctx.md("x"), "<b>bold</b>");
}

/* V3: 首屏 URL 带 token → 存 sessionStorage 且地址栏剥离；刷新后从 sessionStorage 恢复。
 * 注意：app.js 顶层 `let API_TOKEN` 是词法绑定，不挂在 ctx 对象上，
 * 必须用 runInContext 在同一全局词法作用域内读取。 */
{
  const cap = { url: null };  // bootstrap 在 loadApp 期间同步执行，不能引用尚未赋值的 ctx（TDZ）
  const ctx = loadApp({
    window: { location: { origin: "http://127.0.0.1:5000", pathname: "/",
                          search: "?token=abc123&desktop=1" },
              history: { replaceState: (_a, _b, url) => { cap.url = url; } } },
  });
  assert.equal(vm.runInContext("API_TOKEN", ctx), "abc123");
  assert.equal(cap.url, "/?desktop=1");
  assert.equal(ctx.sessionStorage.getItem("alterego_api_token"), "abc123");

  const ctx2 = loadApp({ // 刷新：无 query token
    window: { location: { origin: "http://127.0.0.1:5000", search: "" } },
    sessionStorage: { _m: new Map([["alterego_api_token", "abc123"]]),
                      getItem(k) { return this._m.get(k) ?? null; },
                      setItem(k, v) { this._m.set(k, v); }, removeItem(k) { this._m.delete(k); } },
  });
  assert.equal(vm.runInContext("API_TOKEN", ctx2), "abc123",
    "刷新后应从 sessionStorage 恢复 token");
}

console.log("test_ui_esc_and_token: all assertions passed");
