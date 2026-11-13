// tests/ui/_harness.mjs — P5 mock-harness（MIGRATION.md v1.1.8）
// 以假后端载荷驱动 v2 适配层：断言的是「适配后行为」而不是演示数据。
// 用法：const h = createHarness(root, { payload: {...} }); await h.ready;
//       h.$ / h.$$ / h.click / h.change / h.input / h.wait / h.posts / h.errors
import fs from 'fs';
import path from 'path';
import { JSDOM } from 'jsdom';

export function createHarness(root, opts = {}) {
  const html = fs.readFileSync(path.join(root, 'ui', 'index.html'), 'utf8');
  const js = fs.readFileSync(path.join(root, 'ui', 'static', 'app.js'), 'utf8');
  const dom = new JSDOM(html, {
    url: opts.url || 'http://127.0.0.1:5000/?token=T&desktop=1',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;
  window.matchMedia = window.matchMedia || (() => ({
    matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
  }));
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.scrollTo = function () {};

  const errors = [];
  const posts = [];
  const payload = Object.assign({
    snapshot: {
      agent_state: { state: 'idle', detail: '就绪' },
      activity: [], approvals: [], active_takeover: null, plan: [],
      status: { current_model: 'auto', main_model: 'auto', token_usage: { input: 0, output: 0, known_requests: 0, unknown_requests: 0 } },
    },
    sessions: [{ id: 's1', title: '测试会话', updated_at: '20:00' }],
    messages: [],
    running: [],
    history: [],
    models: { current: 'auto', current_display: 'EchoBird', models: [{ id: 'auto', name: 'EchoBird', base_url: 'http://127.0.0.1:9/v1', has_key: true }], roles: {} },
    themes: { themes: [] },
    approvalHistory: [],
    memory: { content: '' },
  }, opts.payload || {});

  window.fetch = (u, init) => {
    const url = String(u);
    const method = (init && init.method) || 'GET';
    const json = (d) => Promise.resolve({
      ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => d,
    });
    if (method === 'POST') {
      let body = null;
      try { body = init && init.body ? JSON.parse(init.body) : null; } catch (e) { body = init && init.body; }
      posts.push({ url, body });
      return json({ ok: true });
    }
    if (url.includes('/api/state/snapshot')) return json(payload.snapshot);
    if (url.includes('/api/sessions') && url.includes('/messages')) return json(payload.messages);
    if (url.includes('/api/sessions')) return json(payload.sessions);
    if (url.includes('/api/tasks/history')) return json(payload.history);
    if (url.includes('/api/tasks/running')) return json(payload.running);
    if (url.includes('/api/models')) return json(payload.models);
    if (url.includes('/api/avatar/themes')) return json(payload.themes);
    if (url.includes('/api/approvals/history')) return json(payload.approvalHistory);
    if (url.includes('/api/memory')) return json(payload.memory);
    if (url.includes('/api/health')) return json({ status: 'ok' });
    return json({ ok: true });
  };

  window.addEventListener('error', (e) => errors.push(String(e.message)));
  window.addEventListener('unhandledrejection', (e) => errors.push('reject: ' + String(e.reason)));
  window.eval(js);

  const doc = window.document;
  return {
    window, dom, errors, posts, payload,
    ready: new Promise((r) => setTimeout(r, opts.bootWait || 140)),
    $: (s) => doc.querySelector(s),
    $$: (s) => [...doc.querySelectorAll(s)],
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    change: (el, v) => { el.value = v; el.dispatchEvent(new window.Event('change', { bubbles: true })); },
    input: (el, v) => { el.value = v; el.dispatchEvent(new window.Event('input', { bubbles: true })); },
    keydown: (el, key) => el.dispatchEvent(new window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })),
    wait: (ms = 60) => new Promise((r) => setTimeout(r, ms)),
  };
}

export function makeCheckers() {
  let pass = 0, fail = 0;
  return {
    ok(name, cond, extra) {
      if (cond) { pass++; } else { fail++; console.log('FAIL', name, extra === undefined ? '' : JSON.stringify(extra)); }
    },
    finish(tag) {
      console.log(`RESULT ${tag} pass=${pass} fail=${fail}`);
      process.exit(fail ? 1 : 0);
    },
  };
}
