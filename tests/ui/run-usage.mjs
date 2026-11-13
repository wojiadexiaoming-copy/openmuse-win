// run-usage.mjs — v2 适配层回归：用量看板（mock-harness，MIGRATION.md v1.1.8）
// R2 定稿：token_usage 为进程内累计（无 30 天历史），KPI 真实、热力/趋势空态。
import { createHarness, makeCheckers } from './_harness.mjs';

const root = process.argv[2] || process.cwd();
const h = createHarness(root, { payload: {
  snapshot: {
    agent_state: { state: 'idle', detail: '就绪' }, activity: [], approvals: [],
    status: { current_model: 'auto', token_usage: { input: 120000, output: 40000, known_requests: 6, unknown_requests: 1 } },
  },
}});
await h.ready;
const { ok, finish } = makeCheckers();
const { window } = h;
const A = window.__alterego;

// ---- A. 用量视图：本次运行 KPI ----
h.click(h.$('[data-nav="usage"]'));
ok('用量视图可见', h.$('#view-usage').style.display === 'block');
ok('KPI 3 张', h.$$('#usage-kpis .kpi').length === 3, h.$$('#usage-kpis .kpi').length);
ok('Token KPI = 160k', h.$('#usage-kpis .kpi .kpi__v').textContent.includes('160k'),
  h.$('#usage-kpis .kpi .kpi__v').textContent);
ok('请求次数 KPI', h.$('#usage-kpis').textContent.includes('7'), h.$$('#usage-kpis .kpi')[2].textContent);
ok('标注本次运行', h.$('#usage-kpis').textContent.includes('本次运行'));

// ---- B. 热力/趋势/模型表空态 ----
ok('热力图空', h.$$('#usage-heat i').length === 0, h.$$('#usage-heat i').length);
ok('热力空态说明', h.$('#usage-heat-note').textContent.includes('暂无历史'));
ok('单柱渲染', h.$$('#usage-bars .bar').length === 1, h.$$('#usage-bars .bar').length);
ok('趋势标题本次运行', h.$('#usage-trend-title').textContent.includes('本次运行'));
ok('模型表空态', h.$('#usage-models-body').textContent.includes('暂无历史'));
ok('无伪造金额', !h.$('#usage-models-foot').textContent.includes('¥'));

// ---- C. token_usage 事件驱动更新 ----
A.onTokenUsage({ input: 1200000, output: 300000, known_requests: 20, unknown_requests: 2 });
await h.wait(30);
ok('KPI 随事件更新 1.5M', h.$('#usage-kpis .kpi .kpi__v').textContent.includes('1.5M'),
  h.$('#usage-kpis .kpi .kpi__v').textContent);
ok('请求数更新 22', h.$('#usage-kpis').textContent.includes('22'), h.$$('#usage-kpis .kpi')[2].textContent);

// ---- D. 视图切换 ----
h.click(h.$('[data-nav="chat"]'));
ok('切回对话', h.$('#view-chat').style.display === '' && h.$('#view-usage').style.display === 'none');
h.click(h.$('[data-nav="ideas"]'));
ok('灵感视图静态保留', h.$('#view-other').style.display === 'block' && h.$$('#view-other .altcard').length > 0);
ok('view-other 标注 TODO', h.$('#view-other').textContent.includes('TODO'));
h.click(h.$('[data-nav="usage"]'));
ok('再次进入用量', h.$('#view-usage').style.display === 'block');

ok('无运行时错误', h.errors.length === 0, h.errors);
h.window.close();
finish('usage');
