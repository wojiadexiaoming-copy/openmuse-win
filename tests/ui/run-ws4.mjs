// run-ws4.mjs — v2 适配层回归：消息流 + 历史回合卡 + 审批闭环 + 停止链路（mock-harness，v1.1.17）
import { createHarness, makeCheckers } from './_harness.mjs';

const root = process.argv[2] || process.cwd();
const h = createHarness(root, { payload: {
  snapshot: {
    agent_state: { state: 'idle', detail: '就绪' },
    activity: [{ id: 'a1', tool: 'file_read', summary: '读 README', status: 'done', started_at: '20:03', result_summary: '路径X' }],
    approvals: [], status: { current_model: 'auto', token_usage: { input: 120, output: 40, known_requests: 2, unknown_requests: 0 } },
  },
  // 以往的动态：跨重启的会话消息（assistant_final = 一个已完成回合）
  messages: [{ id: 'tm0', type: 'assistant_final', role: 'agent',
    content: '读取 README 总结：安装与用法两章。',
    session_id: 's1', created_at: '20:05',
    tool_calls: [{ tool: 'file_read', status: 'ok', summary: '读 README', result_summary: '路径X' }] }],
}});
await h.ready;
const { ok, finish } = makeCheckers();
const { window } = h;
const A = window.__alterego;

// ---- A. 开机即见以往的动态（会话消息派生，跨重启） ----
ok('历史回合卡开机即在列', h.$$('#task-list .tk').length === 1 &&
  h.$('#task-list .tk .tk__goal').textContent.includes('读取 README 总结'),
  h.$$('#task-list .tk').length);
ok('计数徽标 = 真实卡数', h.$('#task-count').textContent === '1', h.$('#task-count').textContent);

// ---- B. 点历史卡 → 总览弹窗（tracker/tool_calls 步骤） ----
h.click(h.$('#task-list [data-task="turn-tm0"]'));
ok('总览打开', !h.$('#modal-overview').classList.contains('is-hidden'));
ok('总览步骤来自 tool_calls', h.$$('#ov-steps .steps__item').length === 1, h.$$('#ov-steps .steps__item').length);
h.click(h.$('#modal-overview [data-close]'));

// ---- C. 消息流：渲染 / 幂等 / 防串台 ----
A.onServerMessage({ id: 'm1', type: 'assistant_final', role: 'agent', content: '**结论**：README 讲了 `安装` 与 `用法`。', session_id: 's1', created_at: '20:10' });
ok('assistant 气泡 MD 渲染', h.$('#msg-m1') && h.$('#msg-m1 .bubble').innerHTML.includes('<strong>'));
A.onServerMessage({ id: 'm1', type: 'assistant_final', role: 'agent', content: '重复消息', session_id: 's1' });
ok('同 id 幂等去重', h.$$('#msg-m1').length === 1 && !h.$('#view-chat').textContent.includes('重复消息'));
A.onServerMessage({ id: 'm2', role: 'user', content: '帮我看看', session_id: 's1' });
ok('用户气泡渲染', h.$('#msg-m2') && h.$('#msg-m2 .bubble').textContent.includes('帮我看看'));
A.onServerMessage({ id: 'm3', role: 'agent', content: '别的会话', session_id: 's-other' });
ok('跨会话消息丢弃', !h.$('#msg-m3'));

// ---- D. 新回合完成 → 即时成为历史卡 ----
ok('新回合落卡', h.$$('#task-list .tk').length === 2 &&
  h.$('#task-list [data-task="turn-m1"]'), h.$$('#task-list .tk').length);
ok('新卡在最前', h.$$('#task-list .tk')[0].getAttribute('data-task') === 'turn-m1');

// ---- E. 发送 → POST /api/chat + thinking 占位 ----
h.input(h.$('#composer-input'), '总结一下 README');
h.click(h.$('#btn-send'));
ok('thinking 占位出现', Boolean(h.$('#view-chat .thinking')));
await h.wait(80);
ok('chat POST 载荷', h.posts.some((p) => p.url.includes('/api/chat') && p.body.content === '总结一下 README' && p.body.session_id === 's1'),
  h.posts.filter((p) => p.url.includes('/api/chat')).map((p) => p.body));
ok('响应后占位清理', !h.$('#view-chat .thinking'));

// ---- F. 审批闭环（L2 三档 / resolve 载荷 / 收敛） ----
A.onApprovalRequest({ approval_id: 'ap-1', action: 'file_delete', action_cn: '删除临时文件',
  risk_level: 'L2', scope_options: ['once', 'session', 'always'], expires_at: Date.now() + 120000, status: 'pending' });
await h.wait(30);
ok('审批卡出现', !h.$('#approval-card').classList.contains('is-hidden'));
ok('L2 三档全出', h.$('#approval-card [data-scope="always"]').hidden === false);
h.click(h.$('#approval-card [data-scope="session"]'));
h.click(h.$('#approval-card [data-approve="yes"]'));
await h.wait(60);
ok('resolve POST 载荷', h.posts.some((p) => p.url.includes('/api/approvals/ap-1/resolve') &&
  p.body.approved === true && p.body.scope === 'session'),
  h.posts.filter((p) => p.url.includes('resolve')).map((p) => p.body));
ok('批准后卡片收敛', h.$('#approval-card').classList.contains('is-hidden'));

// ---- G. 停止链路 ----
A.applyAgentState('acting', '正在执行');
await h.wait(30);
ok('acting 出现活卡', h.$$('#task-list .tk')[0] && h.$$('#task-list .tk')[0].getAttribute('data-task') === 'live-turn');
ok('runstrip 停止钮可见', h.$('#runstrip-stop').hidden === false);
h.click(h.$('#runstrip-stop'));
await h.wait(40);
ok('stop POST', h.posts.some((p) => p.url.includes('/api/stop')));
A.applyAgentState('cancelled', '已停止');
await h.wait(30);
ok('cancelled 复位停止态', A.state.stopping === false && A.state.state === 'idle');

// ---- H. done 后活卡消失，历史回合卡保留 ----
A.applyAgentState('done', '已完成');
await h.wait(30);
ok('done 后活卡消失、历史保留', h.$$('#task-list .tk').length === 2 &&
  Boolean(h.$('#task-list [data-task="turn-tm0"]')), h.$$('#task-list .tk').length);

ok('无运行时错误', h.errors.length === 0, h.errors);
h.window.close();
finish('ws4');
