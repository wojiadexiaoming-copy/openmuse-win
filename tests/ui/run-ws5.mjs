// run-ws5.mjs — v2 适配层回归：总览弹窗状态机（mock-harness，v1.1.17）
// 覆盖：历史回合卡（会话消息派生，含 cancelled 步骤）/ 活卡 run / 等审批 wait / 取消收敛。
import { createHarness, makeCheckers } from './_harness.mjs';

const root = process.argv[2] || process.cwd();
const h = createHarness(root, { payload: {
  snapshot: {
    agent_state: { state: 'idle', detail: '就绪' }, activity: [], approvals: [],
    status: { current_model: 'auto', token_usage: { input: 500, output: 200, known_requests: 3, unknown_requests: 0 } },
  },
  messages: [
    { id: 'um0', type: 'user', role: 'user', session_id: 's1',
      content: '重试',
      task_goal: '把下载文件夹归档到 D 盘，217 个文件别漏' },
    { id: 'sn0', type: 'assistant_notice', role: 'agent', session_id: 's1',
      content: '[时间预算] 本轮已用约 80%（剩余 66 秒）' },
    { id: 'tm0', type: 'assistant_final', role: 'agent',
      content: '归档 217 个文件到 D:\\归档',
      session_id: 's1', created_at: '18:42',
      tool_calls: [
        { tool: 'file_list', status: 'ok', summary: '扫描下载文件夹', result_summary: '217 个文件' },
        { tool: 'file_delete', status: 'error', summary: '等你确认删除清单', result_summary: '超时未确认' },
        { tool: 'memory_write', status: 'ok', summary: '写回记忆', result_summary: 'MEMORY.md' },
      ] },
  ],
}});
await h.ready;
const { ok, finish } = makeCheckers();
const A = h.window.__alterego;

function openCard(id) {
  h.click(h.$(`#task-list [data-task="${id}"]`));
  return {
    hidden: h.$('#modal-overview').classList.contains('is-hidden'),
    state: h.$('#ov-card').getAttribute('data-state'),
    title: h.$('#ov-title').textContent,
    steps: h.$$('#ov-steps .steps__item').length,
    stopped: h.$$('#ov-steps .is-stopped').length,
    running: h.$$('#ov-steps .is-run').length,
    pct: h.$('#ov-pct').textContent,
    stopVisible: h.$('#btn-ov-stop').style.display !== 'none',
  };
}

// ---- A. 历史回合卡：done 状态 + tool_calls 步骤 + error→stopped ----
ok('历史卡=1（系统通知不成卡）', h.$$('#task-list .tk').length === 1, h.$$('#task-list .tk').length);
ok('系统通知不冒充用户气泡', h.$$('.msg--user').length === 1, h.$$('.msg--user').length);
ok('用户气泡保留原话', h.$('.msg--user .bubble').textContent.trim() === '重试',
  h.$('.msg--user .bubble').textContent);
let r = openCard('turn-tm0');
ok('归档卡打开', !r.hidden);
ok('归档状态 done', r.state === 'done', r.state);
ok('归档标题取 task_goal 而非短指令', r.title.includes('归档到 D 盘'), r.title);
ok('归档标题不是重试', !r.title.includes('重试'), r.title);
ok('归档标题不取助手回复内容', !r.title.includes('归档 217 个文件到'), r.title);
ok('步骤数 3', r.steps === 3, r.steps);
ok('含 is-stopped 步骤（error→cancelled→stopped）', r.stopped === 1, r.stopped);
ok('进度 100%', r.pct === '100%', r.pct);
ok('归档无停止钮', !r.stopVisible);
h.click(h.$('#modal-overview [data-close]'));

// ---- B. 活卡：acting → run 态，步骤来自 ACT 活动镜像 ----
// 续跑指令（重试）到达后，后端解析出的任务书应覆盖活卡标题
A.onServerMessage({ id: 'um1', type: 'user', role: 'user', session_id: 's1', content: '重试' });
A.applyAgentState('thinking', '正在思考…', 'auto', '把下载文件夹归档到 D 盘，217 个文件别漏');
A.applyAgentState('acting', '正在执行');
ok('后端 task_goal 覆盖续跑指令标题',
  h.$('#task-list [data-task="live-turn"] .tk__goal').textContent.includes('归档到 D 盘'),
  h.$('#task-list [data-task="live-turn"] .tk__goal').textContent);
ok('活卡标题不是重试',
  !h.$('#task-list [data-task="live-turn"] .tk__goal').textContent.includes('重试'),
  h.$('#task-list [data-task="live-turn"] .tk__goal').textContent);
A.onToolCallStarted({ id: 't1', tool: 'browser_click', summary: '点击提交', args: {} });
A.onToolCallStarted({ id: 't2', tool: 'file_read', summary: '读配置', args: {} });
A.onToolCallDone({ id: 't1', status: 'ok', duration_ms: 320, result_summary: '点击成功' });
await h.wait(40);
r = openCard('live-turn');
ok('活卡打开总览', !r.hidden);
ok('活卡 run 态', r.state === 'run', r.state);
ok('活卡标题=本回合用户请求', r.title.includes('归档到 D 盘'), r.title);
ok('活卡标题不取会话名', !r.title.includes('测试会话'), r.title);
ok('活卡步骤 = ACT 镜像 2 条', r.steps === 2, r.steps);
ok('运行中步骤 1 条', r.running === 1, r.running);
ok('活卡有停止钮', r.stopVisible);
ok('活卡进度 = 完成比 50%', r.pct === '50%', r.pct);
h.click(h.$('#modal-overview [data-close]'));

// ---- C. 等审批：wait 态 ----
A.onApprovalRequest({ approval_id: 'ap-9', action: 'publish', action_cn: '对外发布',
  risk_level: 'L3', scope_options: ['once', 'session'], expires_at: Date.now() + 60000, status: 'pending' });
await h.wait(40);
ok('等审批时活卡转 wait', h.$('#task-list .tk .tk__status span').textContent === '待审批',
  h.$('#task-list .tk .tk__status span').textContent);
ok('审批中标题仍是任务请求', h.$('#task-list [data-task="live-turn"] .tk__goal').textContent.includes('归档到 D 盘'),
  h.$('#task-list [data-task="live-turn"] .tk__goal').textContent);
r = openCard('live-turn');
ok('总览 wait 态', r.state === 'wait', r.state);
h.click(h.$('#modal-overview [data-close]'));

// ---- D. 审批拒绝收敛 → cancelled → 活卡消失，历史回合卡保留 ----
A.onApprovalResolved({ approval_id: 'ap-9', approved: false, reason: 'timeout' });
A.applyAgentState('cancelled', '已按拒绝处理');
await h.wait(40);
ok('取消后活卡消失', h.$$('#task-list .tk').length === 1, h.$$('#task-list .tk').length);
ok('历史回合卡保留', Boolean(h.$('#task-list [data-task="turn-tm0"]')));

ok('无运行时错误', h.errors.length === 0, h.errors);
h.window.close();
finish('ws5');
