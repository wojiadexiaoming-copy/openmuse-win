// run-ws6.mjs — v2 适配层回归：设置/模型角色路由/记忆（mock-harness，v1.1.19）
// 覆盖：roles 驱动路由表（全角色可切换）、switch_model target_role 传递、接口列表、
//       新增接口校验与保存、test_llm、记忆读写、SOUL 只读。
import { createHarness, makeCheckers } from './_harness.mjs';

const root = process.argv[2] || process.cwd();
const h = createHarness(root, { payload: {
  models: { current: 'auto', current_display: 'EchoBird', roles: {
    main: { name: '👑 主脑 (Main Brain)', desc: '核心对话、意图识别与任务调度', model: 'auto' },
    worker: { name: '⚡ 后台工作者 (Worker)', desc: '记忆沉淀、历史提炼与上下文压缩', model: 'auto' },
    subagent: { name: '🤖 子任务智能体 (Subagent)', desc: '专注复杂单项任务', model: 'gpt-5-mini' },
    planner: { name: '📋 任务规划器 (Planner)', desc: '长程复杂任务分解', model: 'auto' },
    fallback: { name: '🛡️ 容灾兜底 (Fallback)', desc: '主模型故障时接管', model: 'gpt-5-mini' },
  }, models: [
    { id: 'auto', name: 'EchoBird', base_url: 'http://127.0.0.1:9/v1', has_key: true },
    { id: 'gpt-5-mini', name: 'OpenAI 备用', base_url: 'https://api.openai.com/v1', has_key: true },
  ] },
  memory: { content: 'MEMORY 第一行' },
}});
await h.ready;
const { ok, finish } = makeCheckers();
const { window } = h;

// ---- A. 设置弹窗与 roles 驱动路由表 ----
h.click(h.$('#btn-settings'));
ok('设置打开', !h.$('#modal-settings').classList.contains('is-hidden'));
ok('路由表 = 5 个后端角色', h.$$('#route-table .rrow').length === 5, h.$$('#route-table .rrow').length);
ok('主脑行含当前模型', h.$('#route-table [data-route="main"]').value === 'auto',
  h.$('#route-table [data-route="main"]').value);
ok('子任务行当前生效值', h.$('#route-table [data-route="subagent"]').value === 'gpt-5-mini',
  h.$('#route-table [data-route="subagent"]').value);
ok('全部下拉可操作（无 disabled）', h.$$('#route-table select[data-route]').every((s) => !s.disabled),
  'disabled found');
ok('角色名去掉装饰符', h.$('#route-table .rrow__t').textContent.includes('主脑'),
  h.$('#route-table .rrow__t').textContent);

// ---- B. 子 Agent 换模型 → switch_model target_role 真实传递 ----
h.change(h.$('#route-table [data-route="subagent"]'), 'auto');
await h.wait(40);
ok('subagent POST 载荷', h.posts.some((p) => p.url.includes('/api/switch_model') &&
  p.body.target_role === 'subagent' && p.body.model === 'auto'),
  h.posts.filter((p) => p.url.includes('switch_model')).map((p) => p.body));
ok('行内即时更新', h.$('#route-table [data-route="subagent"]').value === 'auto');

// ---- B2. 后端拒绝切换 → 下拉回滚到真实值 + 失败提示 ----
let rejectOnce = true;
const realFetch = window.fetch;
window.fetch = (u, init) => {
  if (rejectOnce && String(u).includes('/api/switch_model')) {
    rejectOnce = false;
    return Promise.resolve({
      ok: true, status: 200, headers: { get: () => 'application/json' },
      json: async () => ({ ok: false, error: '模型不可用' }),
    });
  }
  return realFetch(u, init);
};
h.change(h.$('#route-table [data-route="planner"]'), 'gpt-5-mini');
await h.wait(40);
ok('拒绝后下拉回滚', h.$('#route-table [data-route="planner"]').value === 'auto',
  h.$('#route-table [data-route="planner"]').value);
ok('拒绝提示可见', h.$('#toasts').textContent.includes('切换失败'), h.$('#toasts').textContent);
h.change(h.$('#route-table [data-route="fallback"]'), '');
await h.wait(40);
ok('兜底不可关闭（回滚）', h.$('#route-table [data-route="fallback"]').value === 'gpt-5-mini',
  h.$('#route-table [data-route="fallback"]').value);
ok('兜底提示可见', h.$('#toasts').textContent.includes('容灾兜底'), h.$('#toasts').textContent);

// ---- C. 主 Agent 换模型（输入区菜单） ----
h.click(h.$('#modal-settings [data-close]'));
h.click(h.$('#btn-model'));
ok('模型菜单打开', !h.$('#model-menu').hidden);
ok('菜单项来自目录', h.$$('#model-menu [data-pick-model]').length === 2, h.$$('#model-menu [data-pick-model]').length);
h.click(h.$$('#model-menu [data-pick-model]').find((b) => b.getAttribute('data-pick-model') === 'gpt-5-mini'));
await h.wait(40);
ok('main POST 载荷', h.posts.some((p) => p.url.includes('/api/switch_model') &&
  p.body.target_role === 'main' && p.body.model === 'gpt-5-mini'),
  h.posts.filter((p) => p.url.includes('switch_model')).map((p) => p.body));
ok('主模型标签更新', h.$('#btn-model-name').textContent === 'gpt-5-mini', h.$('#btn-model-name').textContent);

// ---- D. 接口列表来自 /api/models ----
h.click(h.$('#btn-settings'));
ok('接口行渲染', h.$$('#key-list .keyrow').length === 2, h.$$('#key-list .keyrow').length);
ok('行内容含 base_url', h.$('#key-list').textContent.includes('api.openai.com'));

// ---- E. 新增接口：校验 + 预设 + 保存 POST ----
h.click(h.$('#set-tab-keys'));
h.click(h.$('#btn-add-endpoint'));
h.click(h.$('#btn-ep-save'));
ok('空表单拦截', !h.$('#modal-endpoint').classList.contains('is-hidden'));
ok('URL 标错', h.$('#ep-url').closest('.field').classList.contains('has-err'));
const preset = h.$$('#modal-endpoint [data-preset]').find((b) => b.getAttribute('data-preset') === 'deepseek');
h.click(preset);
ok('预设填充', h.$('#ep-url').value === 'https://api.deepseek.com/v1');
h.input(h.$('#ep-name'), 'DeepSeek 生产');
h.input(h.$('#ep-key'), 'sk-test-1234567890abc');
h.click(h.$('#btn-ep-save'));
await h.wait(40);
ok('save_model_config POST 载荷', h.posts.some((p) => p.url.includes('/api/settings/save_model_config') &&
  p.body.model === 'deepseek-chat' && p.body.base_url === 'https://api.deepseek.com/v1'),
  h.posts.filter((p) => p.url.includes('save_model_config')).map((p) => p.body));

// ---- F. test_llm ----
h.click(h.$('#btn-add-endpoint'));
h.click(h.$$('#modal-endpoint [data-preset]').find((b) => b.getAttribute('data-preset') === 'deepseek'));
h.click(h.$('#btn-ep-test'));
await h.wait(40);
ok('test_llm POST 载荷', h.posts.some((p) => p.url.includes('/api/settings/test_llm') &&
  p.body.base_url === 'https://api.deepseek.com/v1' && p.body.model === 'deepseek-chat'),
  h.posts.filter((p) => p.url.includes('test_llm')).map((p) => p.body));

// ---- G. 记忆读写 / SOUL 只读 ----
h.click(h.$('#modal-endpoint [data-close]'));
h.click(h.$('#modal-settings [data-close]'));
h.click(h.$('#card-memory'));
await h.wait(40);
ok('记忆读取填充', h.$('#memory-text').value === 'MEMORY 第一行', h.$('#memory-text').value);
h.input(h.$('#memory-text'), 'MEMORY 第二行');
h.click(h.$('#btn-memory-save'));
await h.wait(40);
ok('记忆 POST 载荷', h.posts.some((p) => p.url.includes('/api/memory') && p.body.content === 'MEMORY 第二行'),
  h.posts.filter((p) => p.url.includes('/api/memory') && p.body).map((p) => p.body));
h.click(h.$('#card-soul'));
ok('SOUL 只读', h.$('#soul-text').hasAttribute('readonly'));
ok('SOUL 假保存隐藏', h.$('#btn-soul-save').hidden === true);
h.keydown(h.window.document, 'Escape');

ok('无运行时错误', h.errors.length === 0, h.errors);
h.window.close();
finish('ws6');
