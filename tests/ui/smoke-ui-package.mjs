import fs from 'fs';
import path from 'path';
import { JSDOM } from 'jsdom';

const root = process.argv[2];
const idx = path.join(root, 'ui');
const st = path.join(idx, 'static');

function boot(file, jsFile, url) {
  const html = fs.readFileSync(file, 'utf8');
  const js = fs.readFileSync(path.join(st, jsFile), 'utf8');
  const dom = new JSDOM(html, { url, runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  const errors = [];
  const calls = [];
  window.fetch = (u, init) => {
    calls.push({ url: u, init });
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => ({ ok: true }) });
  };
  window.addEventListener('error', (e) => errors.push(String(e.message)));
  window.addEventListener('unhandledrejection', (e) => errors.push('reject: ' + String(e.reason)));
  window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.scrollTo = function () {};
  window.eval(js);
  return { window, errors, calls, $: (s) => window.document.querySelector(s), $$: (s) => [...window.document.querySelectorAll(s)] };
}

let pass = 0, fail = 0;
const ok = (n, c, x) => { if (c) pass++; else { fail++; console.log('FAIL', n, x === undefined ? '' : JSON.stringify(x)); } };
const wait = (ms = 60) => new Promise((r) => setTimeout(r, ms));

// ---------- main workbench ----------
const m = boot(path.join(idx, 'index.html'), 'app.js', 'http://127.0.0.1:5000/?token=T0K&desktop=1');
const { window: w } = m;
await wait(120);

ok('no console errors (main)', m.errors.length === 0, m.errors);
ok('titlebar present', !!m.$('#titlebar, .titlebar'));
ok('titlebar drag region', !!m.$('.titlebar.pywebview-drag-region'));
ok('app shell present', !!m.$('.app'));
ok('rail nav buttons', m.$$('.rail__btn[data-nav]').length >= 5, m.$$('.rail__btn[data-nav]').length);
ok('right drawer avatar', !!m.$('#avatar-img'));
ok('all avatars routed', m.$$('img[src]').every((i) => !i.getAttribute('src').startsWith('assets/')), m.$$('img[src]').map((i) => i.getAttribute('src')).filter((s) => s.startsWith('assets/')).slice(0, 3));
ok('avatar theme panda', /^\/avatar\/panda\/[a-z]+\.gif$/.test(m.$('#avatar-img').getAttribute('src')), m.$('#avatar-img').getAttribute('src'));

m.$('#btn-avatar-rail').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
await wait(80);
ok('avatar modal opens', !m.$('#modal-avatar').classList.contains('is-hidden'), m.$('#modal-avatar').className);
m.$('.pickset[data-theme="dragon"]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
await wait(30);
m.$('#btn-avatar-save').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
await wait(80);
ok('avatar switches to /avatar/dragon/*', /^\/avatar\/dragon\/[a-z]+\.gif$/.test(m.$('#avatar-img').getAttribute('src')), m.$('#avatar-img').getAttribute('src'));

// nav switch
const usageNav = m.$('.rail__btn[data-nav="usage"]');
if (usageNav) {
  usageNav.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await wait(60);
  ok('usage view activates', usageNav.classList.contains('is-active'), usageNav.className);
}

// window buttons must not throw
const ctl = m.$$('.winctl button');
ok('window controls 3', ctl.length === 3, ctl.length);
ctl.forEach((b) => b.dispatchEvent(new w.MouseEvent('click', { bubbles: true })));
await wait(60);
ok('no errors after window clicks', m.errors.length === 0, m.errors);
const winCalls = m.calls.filter((c) => String(c.url).includes('/api/window/control'));
ok('window control posts 3 actions', winCalls.length === 3, winCalls.length);
ok('window control actions', JSON.stringify(winCalls.map((c) => JSON.parse(c.init.body).action)) === '["minimize","maximize","close"]', winCalls.map((c) => c.init.body));
ok('window control bearer', winCalls.every((c) => c.init.headers.Authorization === 'Bearer T0K'), winCalls[0] && winCalls[0].init.headers);
ok('token stripped from url', !w.location.search.includes('token='), w.location.search);
ok('token in sessionStorage', w.sessionStorage.getItem('alterego_api_token') === 'T0K');

// main cta is black solid, no blue
const solid = m.$$('.act-secondary--solid');
ok('solid cta exists', solid.length > 0, solid.length);

// ---------- float window ----------
const f = boot(path.join(st, 'float.html'), 'float.js', 'http://127.0.0.1:5000/static/float.html?task_id=t1&target=Chrome&token=T0K');
const fw = f.window;
await wait(120);
ok('no console errors (float)', f.errors.length === 0, f.errors);
ok('float root', !!f.$('#float'));
ok('float mode run', f.$('#float').getAttribute('data-mode') === 'run', f.$('#float').getAttribute('data-mode'));
ok('float avatar routed', /^\/avatar\/panda\//.test(f.$('#f-ava').getAttribute('src')), f.$('#f-ava').getAttribute('src'));
ok('float host api', typeof fw.AlterEgoFloat === 'object' && typeof fw.AlterEgoFloat.update === 'function');

fw.AlterEgoFloat.update({ mode: 'gate', pct: 62, goal: '目标X', step: '步骤Y', ttl: 30 });
await wait(60);
ok('mode gate applied', f.$('#float').getAttribute('data-mode') === 'gate', f.$('#float').getAttribute('data-mode'));
ok('goal updated', f.$('#f-goal').textContent === '目标X', f.$('#f-goal').textContent);
ok('step updated', f.$('#f-step').textContent === '步骤Y', f.$('#f-step').textContent);
ok('pct updated', f.$('#f-pct').textContent === '62%', f.$('#f-pct').textContent);
ok('ttl formatted', /00:30|00:29/.test(f.$('#f-ttl').textContent), f.$('#f-ttl').textContent);
ok('progress bar width', f.$('#f-bar').style.width === '62%', f.$('#f-bar').style.width);

for (const mode of ['take', 'done', 'paused', 'reject', 'run']) {
  fw.AlterEgoFloat.update({ mode });
  await wait(20);
  ok('mode ' + mode, f.$('#float').getAttribute('data-mode') === mode, f.$('#float').getAttribute('data-mode'));
}

// collapse / expand
const pill = f.$('#f-pill');
pill.dispatchEvent(new fw.MouseEvent('click', { bubbles: true }));
await wait(220);
ok('collapse hides float', f.$('#float').hidden === true || f.$('#float').classList.contains('is-collapsed'), f.$('#float').className);
ok('pill visible', f.$('#f-pill') !== null);
fw.AlterEgoFloat.expand();
await wait(220);
ok('expand restores float', !f.$('#float').hidden, f.$('#float').className);

// tray menu via right click
const box = f.$('#float');
box.dispatchEvent(new fw.MouseEvent('contextmenu', { bubbles: true, clientX: 40, clientY: 40, cancelable: true }));
await wait(40);
ok('tray menu opens', f.$('#traymenu').hidden === false, f.$('#traymenu').className);
fw.document.dispatchEvent(new fw.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await wait(30);
ok('tray menu closes on esc', f.$('#traymenu').hidden === true);

// main action button per mode
for (const mode of ['run', 'gate', 'take', 'done', 'paused', 'reject']) {
  fw.AlterEgoFloat.update({ mode });
  await wait(20);
  const btn = f.$('#f-main');
  if (btn) btn.dispatchEvent(new fw.MouseEvent('click', { bubbles: true }));
}
await wait(60);
ok('no errors after float actions', f.errors.length === 0, f.errors);

// live mode: host push must freeze demo timers
const l = boot(path.join(st, 'float.html'), 'float.js', 'http://127.0.0.1:5000/static/float.html?task_id=t2&token=T0K');
const lw = l.window;
await wait(120);
lw.AlterEgoFloat.update({ mode: 'gate', pct: 50, ttl: 120 });
await wait(60);
const ttlA = l.$('#f-ttl').textContent;
lw.AlterEgoFloat.update({ pct: 50 });
await wait(1300);
ok('live gate ttl not auto-decremented', l.$('#f-ttl').textContent === ttlA, [ttlA, l.$('#f-ttl').textContent]);
const pctA = l.$('#f-pct').textContent;
lw.AlterEgoFloat.update({ mode: 'run', pct: 50 });
await wait(1600);
ok('live run progress frozen', l.$('#f-pct').textContent === pctA, [pctA, l.$('#f-pct').textContent]);
const ttlB = (() => { lw.AlterEgoFloat.update({ mode: 'gate', ttl: 60 }); return l.$('#f-ttl').textContent; })();
await wait(1300);
ok('live take/gate timer stays put', l.$('#f-ttl').textContent === ttlB, [ttlB, l.$('#f-ttl').textContent]);

console.log(`\nRESULT pass=${pass} fail=${fail}`);
process.exit(fail ? 1 : 0);
