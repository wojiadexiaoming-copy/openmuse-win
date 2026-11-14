/* AlterEgo 桌面工作台 · v2 适配层
   视觉状态机：idle / thinking / acting / awaiting / done（后端 11 态映射见 STATE_FROM_SERVER）
   持久化：sessionStorage['alterego.ui.prefs.v2'] 仅存 avatar / tab / drawerOpen */
(function () {
  'use strict';

  /* ---------- 宿主桥：token 引导 + 窗口控制 ---------- */
  var API = (window.location.origin && window.location.origin !== 'null' && !/^file:/.test(window.location.origin))
    ? window.location.origin
    : 'http://127.0.0.1:5000';
  let API_TOKEN = '';

  (function bootstrapToken() {
    try {
      var params = new URLSearchParams(window.location.search || '');
      var t = params.get('token') || '';
      if (t) {
        try { sessionStorage.setItem('alterego_api_token', t); } catch (_) {}
        try {
          params.delete('token');
          var qs = params.toString();
          window.history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : '') + (window.location.hash || ''));
        } catch (_) {}
        API_TOKEN = t;
      }
      if (!API_TOKEN) {
        try { API_TOKEN = sessionStorage.getItem('alterego_api_token') || ''; } catch (_) {}
      }
    } catch (_) {}
  })();

  function authHeaders(extra) {
    var h = extra || {};
    if (API_TOKEN) h['Authorization'] = 'Bearer ' + API_TOKEN;
    return h;
  }

  function parseIfString(v) {
    if (typeof v !== 'string') return v;
    try { return JSON.parse(v); } catch (e) { return v; }
  }

  function api(path, opts) {
    if (typeof fetch !== 'function') return Promise.reject(new Error('no-fetch'));
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: authHeaders(opts.headers) };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
    }
    return fetch(API + path, init).then(function (res) {
      if (res.status === 401) { toast('鉴权失效，请重启 AlterEgo'); throw new Error('unauthorized'); }
      if (!res.ok) {
        return res.text().then(function (txt) {
          var data = null;
          try { data = JSON.parse(txt); } catch (_) {}
          var err = new Error('HTTP ' + res.status + ' · ' + txt.slice(0, 120));
          err.status = res.status;
          err.data = data;   // 后端错误体里的 message 是持久化消息，走 onServerMessage 去重渲染
          throw err;
        });
      }
      var ct = res.headers.get('content-type') || '';
      // 后端约 8 个端点返回 json.dumps 字符串（content-type 仍为 text/html），统一二次解析
      return Promise.resolve(ct.indexOf('json') >= 0 ? res.json() : res.text()).then(parseIfString);
    });
  }

  function uploadFile(file) {
    var fd = new FormData();
    fd.append('file', file);
    return fetch(API + '/api/uploads', { method: 'POST', headers: authHeaders({}), body: fd }).then(function (r) {
      if (!r.ok) throw new Error('上传失败 HTTP ' + r.status);
      return r.json();
    });
  }

  function isDesktop() {
    return /[?&]desktop=1/.test(location.search || '') || typeof window.pywebview !== 'undefined';
  }

  function handleWindowControl(action) {
    if (!action) return;
    if (typeof fetch !== 'function') return;
    api('/api/window/control', { method: 'POST', body: JSON.stringify({ action: action }) })
      .catch(function (err) {
        console.warn('窗口控制失败:', err);
        toast('窗口控制失败');
      });
  }

  var KEY = 'alterego.ui.prefs.v2';
  var AVATARS = ['panda', 'dragon'];

  var STATE_LABEL = {
    idle: '待机中',
    thinking: '正在思考',
    acting: '正在执行',
    awaiting: '等待你确认',
    done: '已完成'
  };

  var STATE_FILE = {
    idle: 'idle',
    thinking: 'thinking',
    acting: 'acting',
    awaiting: 'waiting',
    done: 'done'
  };

  var defaults = {
    avatar: 'panda',
    tab: 'feed',
    drawerOpen: (window.innerWidth || 1200) > 960,   // 窄窗口（≤960）抽屉是 overlay，默认收起避免遮住聊天
    approvalScope: 'session',
    // —— 运行时状态（不持久化）——
    state: 'idle',
    rawState: 'idle',
    detail: '',
    model: '',
    error: '',
    sessionId: null,
    stopping: false,
    activeTakeover: null,
    tokenUsage: null,
    usageRange: '30',
    usageMetric: 'tok',
    usageData: null
  };

  var store = load();
  var s = clone(defaults);
  Object.keys(store).forEach(function (k) { if (k in s) s[k] = store[k]; });

  var busy = false;
  var approvalTimer = 0;
  var approvalState = null;        // 最近一条待审批卡（/api/approvals 载荷）
  var approvalHistory = [];
  var turnCards = [];              // 历史回合卡（由会话消息派生，跨重启可见）
  var turnGoals = {};              // 会话 → 本回合用户请求（活卡标题，不取会话名）
  var lastGoalBySession = {};      // 会话 → 最近回合任务目标（user.task_goal 优先，历史卡配对用）
  var sessions = [];
  var trackerPct = null;           // tracker 进度（活卡进度条数据源）
  var pollTaskCount = 0;
  var seenMsg = {};
  var feedSeen = {};
  var ACT = {};                    // 工具/子任务活动镜像（id -> 条目，snapshot 回放权威源）
  var sessionUnread = {};          // 会话 -> 未读消息数（非当前会话的微信/桌面消息）
  var sessionMsgCount = {};        // 会话 -> 已加载的消息数（用于判断是否空会话）
  var modelCatalog = { current: '', current_display: '', models: [], roles: {} };
  var es = null;

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function load() {
    try {
      var raw = sessionStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (e) { return {}; }
  }
  function save() {
    // 铁律 4 收尾：只持久化 UI 偏好，且走 sessionStorage（pywebview private_mode 下随窗口生命周期）
    try { sessionStorage.setItem(KEY, JSON.stringify({ avatar: s.avatar, tab: s.tab, drawerOpen: s.drawerOpen })); } catch (e) {}
  }
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function clock(d) { d = d || new Date(); return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
  function mmss(sec) { sec = Math.max(0, Math.round(sec)); return pad(Math.floor(sec / 60)) + ':' + pad(sec % 60); }

  /* ---------- Toast ---------- */
  var lastToastText = '';
  var lastToastTime = 0;
  function toast(text) {
    if (!text) return;
    var now = Date.now();
    if (text === lastToastText && now - lastToastTime < 1200) {
      return;
    }
    lastToastText = text;
    lastToastTime = now;
    var wrap = $('#toasts');
    if (!wrap) return;
    var el = document.createElement('div');
    el.className = 'toast';
    el.textContent = text;
    wrap.appendChild(el);
    setTimeout(function () {
      el.style.opacity = '0';
      el.style.transition = 'opacity .2s ease';
      setTimeout(function () { el.remove(); }, 220);
    }, 2200);
  }

  /* ---------- 形象与状态 ---------- */
  function avatarSrc(theme, state) {
    return '/avatar/' + theme + '/' + (STATE_FILE[state] || 'idle') + '.gif';
  }

  function paintAvatar() {
    var src = avatarSrc(s.avatar, s.state);
    var label = (s.avatar === 'dragon' ? '龙' : '熊猫') + ' · ' + STATE_LABEL[s.state];
    var main = $('#avatar-img');
    if (main) { main.src = src; main.alt = 'AlterEgo 形象：' + label; }
    var rail = $('#rail-avatar-img');
    if (rail) { rail.src = src; rail.alt = '当前形象：' + (s.avatar === 'dragon' ? '龙' : '熊猫'); }
    var card = $('#agentcard');
    if (card) card.setAttribute('data-state', s.state);
    var txt = $('#agent-state-text');
    if (txt) txt.textContent = stateText();
    $$('.msg__avatar img').forEach(function (img) {
      img.src = src;
    });
    var railBtn = $('#btn-avatar-rail');
    if (railBtn) railBtn.setAttribute('data-tip', '形象：' + (s.avatar === 'dragon' ? '龙' : '熊猫'));
    paintRunstrip();
  }

  function stateText() {
    if (s.stopping) return '正在停止';
    if (s.state === 'done') return '任务已完成';
    return STATE_LABEL[s.state] || '待机中';
  }

  function setState(next) {
    if (s.state === next) { paintAvatar(); return; }
    s.state = next;
    paintAvatar();
  }

  function pendingApproval() { return !!(approvalState && approvalState.status === 'pending'); }
  function activeTakeover() { return !!s.activeTakeover; }

  function deriveState() {
    var next = 'idle';
    if (s.stopping) next = 'acting';
    else if (busy) next = 'thinking';
    else if (pendingApproval() || activeTakeover()) next = 'awaiting';
    setState(next);
    renderTasks();   // 状态迁移连带活卡形态（run/wait/idle）
  }

  /* ---------- 审批（真实：SSE approval_request / POST resolve / expires_at 倒计时） ---------- */
  var approvalCard, approveActions, approveTtl, approveResultText;

  function fmtScope(scope) {
    return scope === 'once' ? '仅本次' : scope === 'session' ? '本会话' : '总是';
  }

  function approvalRemainingSec() {
    if (!approvalState || !approvalState.expires_at) return 0;
    return Math.max(0, Math.round((approvalState.expires_at - Date.now()) / 1000));
  }

  function paintApproval() {
    if (!approvalCard) return;
    var pending = pendingApproval();
    approvalCard.classList.toggle('is-hidden', !pending);
    approvalCard.classList.toggle('is-resolved', !pending);
    approvalCard.classList.toggle('is-settled', !pending);
    if (pending) {
      var a = approvalState;
      approvalCard.setAttribute('data-approval-id', a.approval_id || '');
      var titleEl = approvalCard.querySelector('.gate__title');
      if (titleEl) {
        // 微信发起的审批带来源标签，用户在桌面端一眼可辨
        var sourceTag = a.weixin_notify && a.weixin_notify === 'sent' ? '📱 ' : '';
        titleEl.textContent = sourceTag + (a.action_cn || a.action || '操作确认');
      }
      var noteEl = approvalCard.querySelector('.gate__note');
      if (noteEl) noteEl.textContent = a.summary_cn || a.detail || a.risk_note || '';
      var riskEl = approvalCard.querySelector('.gate__head .risk');
      if (riskEl) {
        var lvl = String(a.risk_level || 'L2').toUpperCase();
        riskEl.textContent = lvl;
        riskEl.className = 'risk risk--' + lvl.toLowerCase();
      }
      var opts = a.scope_options || ['once', 'session'];
      if (opts.indexOf(s.approvalScope) === -1) s.approvalScope = opts[0] || 'once';
      $$('.gate__scope button', approvalCard).forEach(function (b) {
        var allowed = opts.indexOf(b.dataset.scope) > -1;
        b.hidden = !allowed;
        b.disabled = !allowed;
        b.setAttribute('aria-pressed', String(b.dataset.scope === s.approvalScope && allowed));
      });
      var ttl = $('#approve-ttl');
      if (ttl) ttl.textContent = mmss(approvalRemainingSec()) + ' 后自动拒绝';
      if (approveResultText) approveResultText.textContent = a.intent_cn || a.detail_cn || '批准后动作才会执行，过程记录只写入右侧审核面板。';
    }
    renderReview();
  }

  function startApprovalTimer() {
    stopApprovalTimer();
    approvalTimer = setInterval(function () {
      if (!pendingApproval()) { stopApprovalTimer(); return; }
      var ttl = $('#approve-ttl');
      if (ttl) ttl.textContent = mmss(approvalRemainingSec()) + ' 后自动拒绝';
      paintRunstrip();
      renderReview();
      if (approvalRemainingSec() <= 0) {
        stopApprovalTimer();
        addFeed('warn', '审批倒计时归零', '后端将按拒绝处理（fail closed）');
      }
    }, 1000);
  }

  function stopApprovalTimer() {
    if (approvalTimer) { clearInterval(approvalTimer); approvalTimer = 0; }
  }

  function onApprovalRequest(a) {
    if (!a || !a.approval_id) return;
    approvalState = a;
    var opts = a.scope_options || ['once', 'session'];
    var prefScope = (behaviorSettings && behaviorSettings.hazardous_default_scope) || s.approvalScope || 'session';
    if (opts.indexOf(prefScope) > -1) {
      s.approvalScope = prefScope;
    } else if (opts.indexOf(s.approvalScope) === -1) {
      s.approvalScope = opts[0] || 'once';
    }
    paintApproval();
    startApprovalTimer();
    addFeed('live', '等待审批 · ' + (a.risk_level || '') + ' ' + (a.action_cn || a.action || ''), a.summary_cn || '');
    deriveState();
    toast('需要你审批：' + (a.action_cn || a.action || '操作'));
  }

  function resolveApproval(kind) {
    if (!pendingApproval()) return;
    var a = approvalState;
    var approved = kind === 'yes';
    approvalState = null;            // 乐观收卡，等 approval_resolved 收敛
    stopApprovalTimer();
    paintApproval();
    api('/api/approvals/' + encodeURIComponent(a.approval_id) + '/resolve', {
      method: 'POST',
      body: { approved: approved, scope: s.approvalScope, version_id: a.version_id }
    }).then(function () {
      toast(approved ? '已批准（' + fmtScope(s.approvalScope) + '）' : '已拒绝，未执行对外动作');
      addFeed(approved ? 'ok' : 'risk',
        approved ? '审批通过 · ' + fmtScope(s.approvalScope) : '审批被拒绝',
        a.action_cn || a.action || '');
      deriveState();
    }).catch(function (err) {
      addFeed('risk', '审批提交失败', String(err.message || err));
      toast('审批提交失败：' + (err.message || err));
      deriveState();
    });
  }

  function onApprovalResolved(d) {
    if (approvalState && (!d || !d.approval_id || d.approval_id === approvalState.approval_id)) {
      approvalState = null;
      stopApprovalTimer();
      paintApproval();
    }
    addFeed(d && d.approved ? 'ok' : 'risk',
      d && d.approved ? '审批通过' : '审批被拒绝',
      d && d.reason === 'timeout' ? '超时按拒绝处理（fail closed）' : ((d && d.scope) ? 'scope: ' + d.scope : ''));
    deriveState();
  }

  function loadApprovalHistory() {
    api('/api/approvals/history?limit=50').then(function (rows) {
      approvalHistory = Array.isArray(rows) ? rows : [];
      paintReviewDone();
    }).catch(function () {});
  }

  /* ---------- 动态 · 任务数据（真实：tracker 进度 + SSE task_update） ---------- */
  function loadTasks() {
    api('/api/tasks/running').then(function (rows) {
      pollTaskCount = Array.isArray(rows) ? rows.length : 0;
      if (pollTaskCount && rows[0].name) {
        pollGoal = rows[0].name;
        trackerPct = Math.round(rows[0].progress || 0);
      }
      renderTasks();
      paintRunstrip();
    }).catch(function () {});
  }

  function onTaskUpdate(d) {
    if (!d || !d.task_id) return;
    if (d.name) pollGoal = d.name;
    if (typeof d.progress === 'number') trackerPct = Math.round(d.progress);
    renderTasks();
    paintRunstrip();
    paintOverviewIfOpen(d.task_id);
  }

    /* ---------- 动态 · 任务卡片列表 ---------- */
  function turnTitle() {
    var goal = turnGoals[s.sessionId];
    if (goal) return goal;
    return '当前任务';   // 无本回合请求时用中性占位，绝不取会话名/系统通知
  }

  // 当前回合的工具/子任务活动 → 总览弹窗的步骤明细（大局观卡片里不逐条铺开）
  function actSteps() {
    var keys = Object.keys(ACT);
    var steps = [];
    for (var i = keys.length - 1; i >= 0 && steps.length < 30; i--) {
      var it = ACT[keys[i]];
      if (!it || !it.status) continue;
      steps.unshift({
        step_id: keys[i],
        name: (it.tool || '工具') + (it.summary ? ' · ' + it.summary : ''),
        status: it.status === 'running' ? 'running' : (it.status === 'error' || it.status === 'cancelled') ? 'cancelled' : 'done',
        started_at: it.started_at || '', ended_at: '',
        detail: it.result_summary || ''
      });
    }
    return steps;
  }

  // 数据源（v1.1.17 定稿）：大局观 = 任务/回合级。列表两张来源——
  //   ① 当前回合活卡（单张，agent_state/审批驱动），点击开总览看本回合步骤明细（ACT）；
  //   ② 历史回合卡：由会话消息（conversations.db）派生，**跨重启可见**——以往的动态开机即在列；
  //      tracker 仅供进度（/api/tasks/running），其内存态历史不再作为卡片来源。
  function firstLine(text, n) {
    var t = String(text || '').replace(/\s+/g, ' ').trim();
    n = n || 46;
    return t.length > n ? t.slice(0, n) + '…' : t;
  }

  function taskList() {
    var out = [];
    var turnActive = busy || pendingApproval() || s.state === 'thinking' || s.state === 'acting';
    if (turnActive) {
      var steps = actSteps();
      var doneN = steps.filter(function (st) { return st.status === 'done'; }).length;
      var pct = trackerPct !== null ? trackerPct
        : (steps.length ? Math.round(doneN / steps.length * 100) : 0);
      out.push({
        id: 'live-turn', kind: '任务', live: true,
        steps: steps,
        status: pendingApproval() ? 'wait' : 'run',
        pct: pct,
        goal: turnTitle(),
        meta: runDetail()
      });
    }
    turnCards.forEach(function (c) { out.push(c); });
    return out;
  }

  var TASK_STATUS = {
    run: '进行中', wait: '待审批', ask: '待你补充', done: '已完成', closed: '未完成'
  };
  var TASK_CLASS = { run: 'run', wait: 'wait', ask: 'wait', done: 'done', closed: 'closed' };

  function taskStatusFromBackend(s) {
    if (s === 'done') return 'done';
    if (s === 'waiting_user') return 'ask';
    if (s === 'running' || s === 'thinking' || s === 'acting') return 'run';
    return 'closed';
  }

  // 把一条 assistant_final 合并进任务卡（按 task_origin_id 归组，一个任务一张卡）
  function mergeTurnCard(map, order, m, userText) {
    if (!m || m.type !== 'assistant_final' || m.role === 'user') return;
    if (!m.content && !(m.tool_calls || []).length) return;
    var key = (m.task_origin_id ? 'task-' + m.task_origin_id : null) || 'turn-' + m.id;
    var card = map[key];
    if (!card) {
      card = {
        id: key, kind: '任务',
        steps: [], status: 'closed', pct: 0,
        goal: firstLine(m.task_goal || userText || m.content),
        meta: '', turns: 0, subagents: 0,
        created_at: m.created_at || '', lastStatus: 'closed'
      };
      map[key] = card;
      order.push(key);
    }
    card.turns += 1;
    (m.tool_calls || []).forEach(function (tc) {
      card.steps.push({
        step_id: tc.tool || 'step',
        name: (tc.tool || '工具') + (tc.summary ? ' · ' + tc.summary : ''),
        status: tc.status === 'error' ? 'cancelled' : 'done',
        started_at: '', ended_at: m.created_at || '',
        detail: tc.result_summary || ''
      });
      if (/subagent/i.test(tc.tool || '')) card.subagents += 1;
    });
    var st = taskStatusFromBackend(m.task_status || m.status);
    card.lastStatus = st;
    var rank = { done: 3, ask: 2, closed: 1, run: 0 };
    if ((rank[st] || 0) >= (rank[card.status] || 0)) card.status = st;
    if (card.status === 'done') card.pct = 100;
    else {
      var total = card.steps.length || 1;
      var doneN = card.steps.filter(function (s) { return s.status === 'done'; }).length;
      card.pct = Math.min(95, Math.round(doneN / total * 100));
    }
    card.meta = (card.created_at || '') + (card.turns > 1 ? ' · ' + card.turns + ' 个回合' : '');
    if (card.subagents) card.meta += ' · ' + card.subagents + ' 个子 Agent';
  }

  function renderTasks() {
    var host = $('#task-list');
    if (!host) return;
    host.innerHTML = '';
    var list = taskList();
    list.forEach(function (t) {
      var el = document.createElement('button');
      var cls = TASK_CLASS[t.status] || 'closed';
      el.className = 'tk' + (t.status === 'done' ? ' is-done' : '') + (t.status === 'closed' ? ' is-closed' : '');
      el.type = 'button';
      el.setAttribute('data-task', t.id);
      el.setAttribute('data-od-id', 'task-card-' + t.id);
      el.setAttribute('data-open-overview', t.id);
      el.setAttribute('aria-haspopup', 'dialog');
      el.setAttribute('aria-label', t.kind + '：' + t.goal + '（' + (TASK_STATUS[t.status] || t.status) + '）打开详细信息');
      el.innerHTML =
        '<div class="tk__top"><span class="tk__kind"></span><span class="tk__status tk__status--' + cls + '"><i aria-hidden="true"></i><span></span></span></div>' +
        '<div class="tk__goal"></div>' +
        '<div class="tk__meta"><span></span></div>' +
        '<div class="tk__track"><span></span></div>' +
        '<div class="tk__foot"><span class="mono"></span><span class="tk__go"></span></div>';
      el.querySelector('.tk__kind').textContent = t.kind;
      el.querySelector('.tk__status span').textContent = TASK_STATUS[t.status] || t.status;
      el.querySelector('.tk__goal').textContent = t.goal;
      el.querySelector('.tk__meta span').textContent = t.meta;
      el.querySelector('.tk__track span').style.width = t.pct + '%';
      el.querySelector('.tk__foot .mono').textContent = t.pct + '%';
      el.querySelector('.tk__foot .tk__go').innerHTML = '查看步骤与用量<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 6l6 6-6 6"/></svg>';
      host.appendChild(el);
    });
    var count = $('#task-count');
    if (count) count.textContent = String(list.length);
  }

  function openOverview(taskId) {
    var task = null;
    taskList().forEach(function (t) { if (t.id === taskId) task = t; });
    if (!task) return;
    var box = $('#ov-card');
    if (box) box.setAttribute('data-task', task.id);
    paintOverview(buildTaskCtx(task));
    openModal('modal-overview');
  }

  function paintOverviewIfOpen(taskId) {
    var box = $('#ov-card');
    if (!box) return;
    var modal = box.closest('.modal');
    if (!modal || modal.classList.contains('is-hidden')) return;
    var id = box.getAttribute('data-task');
    if (taskId && id && id !== taskId) return;
    taskList().forEach(function (t) { if (t.id === id) paintOverview(buildTaskCtx(t)); });
  }

  function buildTaskCtx(task) {
    var stMap = { done: 'done', running: 'run', failed: 'stopped', cancelled: 'stopped', pending: 'idle', skip: 'stopped', ask: 'wait' };
    var steps = (task.steps || []).map(function (st, i) {
      return {
        t: st.name || ('步骤 ' + (i + 1)),
        tool: st.detail || '',
        meta: (st.started_at || '') + (st.ended_at ? ' → ' + st.ended_at : (st.started_at ? ' · 进行中' : '')),
        state: stMap[st.status] || 'idle'
      };
    });
    if (!steps.length) {
      steps.push({
        t: '等待任务细节',
        tool: '任务步骤会随执行实时更新（task_update）',
        meta: '',
        state: task.status === 'run' ? 'run' : 'idle'
      });
    }
    var running = task.status === 'run';
    var u = s.tokenUsage || {};
    var inTok = u.input || 0, outTok = u.output || 0;
    return {
      state: task.status === 'done' ? 'done' : running ? 'run' : (task.status === 'wait' || task.status === 'ask') ? 'wait' : 'closed',
      kind: task.kind || '任务',
      goal: task.goal,
      title: task.goal,
      pct: task.pct,
      pctLabel: task.status === 'done' ? '已完成' : running ? '总进度' : task.status === 'ask' ? '等待你补充' : '已结束',
      line: task.meta,
      eta: task.status === 'done' ? '已完成' : running ? '执行中' : task.status === 'ask' ? '待回复' : '—',
      steps: steps,
      usage: {
        inTok: num(inTok), outTok: num(outTok),
        ctx: fmtTok(inTok + outTok) + ' <em>/ 本次运行</em>',
        calls: String((u.known_requests || 0) + (u.unknown_requests || 0)) + ' 次',
        tools: String(u.known_requests || 0) + ' 次',
        elapsed: '—',
        rules: []
      },
      primary: '回到对话',
      canStop: running && !s.stopping,
      stopLabel: s.stopping ? '停止中…' : '停止任务',
      footnote: '所有工具调用都在本地执行，界面只写入日志，不写回对话框。'
    };
  }

  function num(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }

  var STEP_STATE = { run: 'is-run', gate: 'is-gate', done: 'is-done', hold: 'is-hold', stopped: 'is-stopped' };

  function paintOverview(ctx) {
    var box = $('#ov-card');
    if (!box) return;
    box.classList.remove('is-done', 'is-closed', 'is-wait');
    if (ctx.state === 'done') box.classList.add('is-done');
    if (ctx.state === 'closed') box.classList.add('is-closed');
    if (ctx.state === 'wait') box.classList.add('is-wait');

    var set = function (sel, text) {
      var el = $(sel);
      if (el) el.textContent = text;
    };
    var eyebrow = $('.ov__eyebrow', box);
    if (eyebrow) {
      var label = ctx.state === 'done' ? '已完成任务' : ctx.state === 'closed' ? '已关闭任务' : ctx.state === 'wait' ? '等待你补充' : '正在执行';
      eyebrow.innerHTML = '<span class="ov__dot" aria-hidden="true"></span>' + label + '<span class="ov__kind">' + ctx.kind + '</span>';
    }
    set('#ov-title', ctx.title);
    set('#ov-pct', ctx.pct + '%');
    set('#ov-pctlabel', ctx.pctLabel);
    set('#ov-status', ctx.line);
    set('#ov-eta', ctx.eta);
    var bar = $('#ov-bar');
    if (bar) {
      bar.style.width = ctx.pct + '%';
      bar.classList.toggle('is-done', ctx.state === 'done');
      bar.style.background = '';
    }

    var goal = $('.ov__goal', box);
    if (goal) goal.textContent = ctx.goal;

    var list = $('#ov-steps');
    if (list) {
      list.innerHTML = '';
      ctx.steps.forEach(function (st) {
        var li = document.createElement('li');
        li.className = 'steps__item ' + (STEP_STATE[st.state] || '');
        var mark = document.createElement('span');
        mark.className = 'steps__mark';
        mark.setAttribute('aria-hidden', 'true');
        var main = document.createElement('div');
        main.className = 'steps__main';
        var row = document.createElement('div');
        row.className = 'steps__row';
        var h = document.createElement('h4');
        h.textContent = st.t;
        var right = document.createElement('span');
        right.className = 'mono';
        right.textContent = st.right || '';
        row.appendChild(h);
        row.appendChild(right);
        var p = document.createElement('p');
        p.textContent = st.tool;
        main.appendChild(row);
        main.appendChild(p);
        if (typeof st.bar === 'number') {
          var wrap = document.createElement('div');
          wrap.className = 'steps__bar';
          var fill = document.createElement('span');
          fill.style.width = st.bar + '%';
          wrap.appendChild(fill);
          main.appendChild(wrap);
        }
        var meta = document.createElement('span');
        meta.className = 'meta';
        meta.textContent = st.meta;
        main.appendChild(meta);
        li.appendChild(mark);
        li.appendChild(main);
        list.appendChild(li);
      });
    }

    var u = ctx.usage;
    set('#ov-in', u.inTok);
    set('#ov-out', u.outTok);
    set('#ov-calls', u.calls);
    set('#ov-tools', u.tools);
    set('#ov-elapsed', u.elapsed);
    var ctxEl = $('#ov-ctx');
    if (ctxEl) ctxEl.innerHTML = u.ctx;
    var rules = $('#ov-rules');
    if (rules) {
      rules.innerHTML = '';
      u.rules.forEach(function (r) {
        var d = document.createElement('div');
        d.className = 'ov__rule';
        var tag = document.createElement('span');
        tag.className = 'risk risk--' + r.lvl;
        tag.textContent = r.lvl.toUpperCase();
        d.appendChild(tag);
        d.appendChild(document.createTextNode(r.text));
        rules.appendChild(d);
      });
    }

    var foot = $('.ov__foot', box);
    if (foot) {
      var note = $('#ov-footnote');
      if (note) note.textContent = ctx.footnote;
      var stop = $('#btn-ov-stop');
      if (stop) {
        stop.textContent = ctx.stopLabel;
        stop.classList.toggle('is-hidden', !ctx.canStop);
        stop.style.display = ctx.canStop ? '' : 'none';
      }
      var prim = $('#btn-ov-primary');
      if (prim) prim.textContent = ctx.primary;
    }
    box.setAttribute('data-state', ctx.state);
    box.setAttribute('data-origin', ctx.origin ? 'archive' : 'live');
  }

  function addFeed(kind, title, sub) {
    var tl = $('#feed-timeline');
    if (!tl) return;
    var item = document.createElement('div');
    item.className = 'tl__item';
    var dotClass = kind === 'live' ? 'tl__dot--live' : kind === 'ok' ? 'tl__dot--ok' : kind === 'warn' ? 'tl__dot--warn' : kind === 'risk' ? 'tl__dot--risk' : '';
    item.innerHTML =
      '<div class="tl__gutter"><span class="tl__dot ' + dotClass + '"></span><span class="tl__line"></span></div>' +
      '<div class="tl__body"><h4></h4><p></p><span class="meta">' + clock() + '</span></div>';
    item.querySelector('h4').textContent = title;
    item.querySelector('p').textContent = sub;
    tl.insertBefore(item, tl.firstChild);
    while (tl.children.length > 40) tl.removeChild(tl.lastChild);   // 事件流封顶，防刷屏
    $$('#feed-timeline .tl__dot--live').forEach(function (d, i) { if (i > 0) d.classList.remove('tl__dot--live'); });
  }

  function paintReviewDone() {
    var host = $('#review-done');
    if (!host) return;
    host.innerHTML = '';
    approvalHistory.forEach(function (h) {
      var el = document.createElement('div');
      el.className = 'queueitem';
      var lvl = document.createElement('span');
      lvl.className = 'risk risk--' + (h.approved ? 'ok' : 'l3');
      lvl.textContent = h.risk_level || 'L3';
      var box = document.createElement('div');
      var t = document.createElement('div');
      t.className = 'queueitem__t';
      t.textContent = h.action_cn || h.action || '操作';
      var sub = document.createElement('div');
      sub.className = 'queueitem__s';
      sub.textContent = (h.approved ? '已批准' : '已拒绝') + (h.scope ? '（' + fmtScope(h.scope) + '）' : '');
      box.appendChild(t);
      box.appendChild(sub);
      el.appendChild(lvl);
      el.appendChild(box);
      host.appendChild(el);
    });
  }

  function renderReview() {
    var host = $('#review-pending');
    if (!host) return;
    if (pendingApproval()) {
      var a = approvalState;
      host.innerHTML = '';
      var item = document.createElement('button');
      item.className = 'queueitem queueitem--live';
      item.style.textAlign = 'left';
      item.style.width = '100%';
      var lvl = String(a.risk_level || 'L2').toUpperCase();
      item.innerHTML = '<span class="risk risk--' + lvl.toLowerCase() + '"></span><div><div class="queueitem__t"></div>' +
        '<div class="queueitem__s"></div></div>';
      item.querySelector('.risk').textContent = lvl;
      item.querySelector('.queueitem__t').textContent = a.action_cn || a.action || '操作确认';
      item.querySelector('.queueitem__s').textContent = '剩余 ' + mmss(approvalRemainingSec()) + ' · 超时拒绝';
      item.addEventListener('click', function () {
        var gate = $('#approval-card [data-approve="yes"]');
        if (gate && gate.focus) gate.focus();
      });
      host.appendChild(item);
    } else {
      host.innerHTML = '<div class="queueitem__empty">没有待处理的审批。</div>';
    }
    var count = $('#review-count');
    if (count) {
      count.textContent = pendingApproval() ? '1' : '0';
      count.classList.toggle('is-zero', !pendingApproval());
    }
    paintReviewDone();
  }

  /* ---------- 富文本：Markdown + 表情（无外链，纯本地渲染） ---------- */
  var EMOJI_MAP = {
    smile: '\uD83D\uDE04', grin: '\uD83D\uDE01', joy: '\uD83D\uDE02', wink: '\uD83D\uDE09',
    heart: '\u2764\uFE0F', fire: '\uD83D\uDD25', thumbsup: '\uD83D\uDC4D', '+1': '\uD83D\uDC4D',
    tada: '\uD83C\uDF89', rocket: '\uD83D\uDE80', check: '\u2705', white_check_mark: '\u2705',
    x: '\u274C', warning: '\u26A0\uFE0F', bulb: '\uD83D\uDCA1', eyes: '\uD83D\uDC40',
    wave: '\uD83D\uDC4B', ok_hand: '\uD83D\uDC4C', pray: '\uD83D\uDE4F', clap: '\uD83D\uDC4F',
    thinking: '\uD83E\uDD14', coffee: '\u2615', sparkles: '\u2728', zap: '\u26A1',
    lock: '\uD83D\uDD12', memo: '\uD83D\uDCDD', package: '\uD83D\uDCE6', film: '\uD83C\uDFAC',
    chart: '\uD83D\uDCC8', target: '\uD83C\uDFAF', bell: '\uD83D\uDD14', clock: '\uD83D\uDD52'
  };

  function escHtml(str) {
    return String(str).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function escAttr(str) { return escHtml(str); }

  /* v2.5 契约函数（tests/test_ui_esc_and_token.js）：jsq = JSON 字面量 + HTML 实体编码，
     md = marked/DOMPurify 管线（缺失时降级纯文本，严禁直插原始 HTML）；与内置 renderMd 并存 */
  function jsq(v) {
    return JSON.stringify(String(v === null || v === undefined ? "" : v))
      .replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  }

  function md(text) {
    if (!text) return "";
    if (typeof window.marked !== "undefined" && window.marked.parse) {
      try {
        var rawHtml = window.marked.parse(text, { breaks: true, gfm: true });
        if (typeof window.DOMPurify !== "undefined" && window.DOMPurify.sanitize) {
          return window.DOMPurify.sanitize(rawHtml);
        }
        console.warn("DOMPurify 未加载：markdown 降级为纯文本渲染（防注入）");
      } catch (e) {
        console.warn("marked 解析失败降级:", e);
      }
    }
    var h = esc(text);
    h = h.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
      return "<pre><code>" + code.replace(/\n$/, "") + "</code></pre>";
    });
    h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    h = h.replace(/\n/g, "<br>");
    return h;
  }

  /* ================= 模型与接口（真实：/api/models + /api/switch_model） ================= */

  var PRESETS = {
    deepseek: { name: 'DeepSeek', base: 'https://api.deepseek.com/v1', model: 'deepseek-chat', ctx: '128000' },
    moonshot: { name: 'Moonshot', base: 'https://api.moonshot.cn/v1', model: 'kimi-k2-turbo', ctx: '256000' },
    zhipu: { name: '智谱 GLM', base: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4.6', ctx: '200000' },
    qwen: { name: '通义千问', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3-max', ctx: '256000' },
    ollama: { name: '本地 Ollama', base: 'http://127.0.0.1:11434/v1', model: 'qwen3-vl-32b', ctx: '128000', noKey: true },
    custom: { name: '', base: '', model: '', ctx: '128000' }
  };

  function esc(v) { return escHtml(v); }

  function modelPool() {
    var out = [];
    (modelCatalog.models || []).forEach(function (m, i) {
      // 后端约定：id 是切换用内部标识（如 auto），name/display_name 是展示名
      var id = m.id || m.model || m.name || ('model-' + i);
      out.push({ model: id, ep: { id: 'ep-' + id, name: m.display_name || m.name || id } });
    });
    return out;
  }
  function mainModel() { return modelCatalog.current || (modelPool()[0] || {}).model || ''; }
  function loadModels() {
    api('/api/models').then(function (data) {
      if (!data) return;
      modelCatalog.current = data.current || modelCatalog.current;
      modelCatalog.current_display = data.current_display || '';
      modelCatalog.models = Array.isArray(data.models) ? data.models : [];
      modelCatalog.roles = data.roles || {};
      modelCatalog.fallback_chain = Array.isArray(data.fallback_chain) ? data.fallback_chain : [];
      renderRoutes();
      renderEndpoints();
      paintModelRefs();
    }).catch(function () {});
  }

  function optionHtml(value, label, selected) {
    return '<option value="' + escAttr(value) + '"' + (selected ? ' selected' : '') + '>' + escAttr(label) + '</option>';
  }
  var isSavingFallback = false;
  var fallbackEditSeq = 0;
  var fallbackSyncTimer = null;
  var lastSyncedSeq = 0;
  var lastLocalSwitchTime = 0;
  var lastLocalSwitchedModel = '';

  function queueFallbackChainSync(toastMsg) {
    fallbackEditSeq++;
    var curSeq = fallbackEditSeq;
    if (fallbackSyncTimer) clearTimeout(fallbackSyncTimer);
    fallbackSyncTimer = setTimeout(function () {
      fallbackSyncTimer = null;
      flushFallbackChainSync(toastMsg, curSeq);
    }, 90);
  }

  function flushFallbackChainSync(toastMsg, specificSeq) {
    if (fallbackSyncTimer) {
      clearTimeout(fallbackSyncTimer);
      fallbackSyncTimer = null;
    }
    var curSeq = specificSeq || (++fallbackEditSeq);
    var cleanChain = (modelCatalog.fallback_chain || []).map(function (c) {
      return typeof c === 'string' ? c : (c && c.model);
    }).filter(Boolean);

    if (cleanChain[0] && modelCatalog.roles && modelCatalog.roles.fallback) {
      modelCatalog.roles.fallback.model = cleanChain[0];
    }
    var fbSel = $('#route-table [data-route="fallback"]');
    if (fbSel && cleanChain[0]) fbSel.value = cleanChain[0];

    isSavingFallback = true;
    lastLocalSwitchTime = Date.now();
    lastLocalSwitchedModel = cleanChain[0] || '';

    api('/api/settings/fallback_chain', {
      method: 'POST',
      body: { chain: cleanChain }
    }).then(function (res) {
      isSavingFallback = false;
      if (res && res.ok === false) {
        toast('更新备用链失败：' + (res.error || '未知错误'));
        renderFallbackChain();
        return;
      }
      // 只有在本次请求发出后用户没有进行更新的编辑操作时，才把服务端的返回反写回前端
      // 杜绝网络请求延迟导致服务端旧数据覆盖用户刚刚添加/切换的最新状态！
      if (curSeq >= fallbackEditSeq) {
        lastSyncedSeq = curSeq;
        if (res && res.fallback_chain) {
          modelCatalog.fallback_chain = res.fallback_chain;
        }
        if (res && res.roles) {
          modelCatalog.roles = res.roles;
        }
        if (toastMsg) toast(toastMsg);
      }
    }).catch(function (err) {
      isSavingFallback = false;
      toast('保存备用链失败：' + (err.message || err));
      renderFallbackChain();
    });
  }

  function saveFallbackChainToServer(chain, toastMsg) {
    modelCatalog.fallback_chain = chain;
    queueFallbackChainSync(toastMsg);
  }

  function renderFallbackChain() {
    var host = $('#fallback-table');
    if (!host) return;

    var chain = (modelCatalog.fallback_chain || []).slice();
    if (!chain.length) {
      var roles = modelCatalog.roles || {};
      var fbModel = (roles.fallback && roles.fallback.model) || '';
      if (fbModel) {
        chain = [{ model: fbModel }];
      } else {
        var pList = modelPool();
        chain = [{ model: (pList[1] || pList[0] || {}).model || 'auto' }];
      }
      modelCatalog.fallback_chain = chain;
    }

    var pool = modelPool();
    var html = '';

    chain.forEach(function (fbItem, idx) {
      var curM = (typeof fbItem === 'string' ? fbItem : (fbItem && fbItem.model)) || '';
      var stageNum = idx + 1;
      var stageDesc = idx === 0 ? '首选降级 · 主脑超时、429 限流或报错时优先切入' : ('第 ' + idx + ' 顺位失败后继续级联降级接管');

      html += '<div class="rrow" data-fallback-idx="' + idx + '">';
      html += '<div>';
      html += '<div class="rrow__t"><span class="fb-badge">顺位 ' + stageNum + '</span>' + (idx === 0 ? '第一备选 (首选接管)' : ('第 ' + stageNum + ' 备选 (次级接管)')) + '</div>';
      html += '<div class="rrow__s">' + escAttr(stageDesc) + '</div>';
      html += '</div>';

      html += '<select data-fallback-select="' + idx + '" aria-label="第 ' + stageNum + ' 顺位降级模型">';
      pool.forEach(function (p) {
        html += optionHtml(p.model, p.model, p.model === curM);
      });
      if (curM && !pool.some(function (p) { return p.model === curM; })) {
        html += optionHtml(curM, curM, true);
      }
      html += '</select>';

      if (chain.length > 1) {
        html += '<button type="button" class="btn-fb-del" data-fallback-del="' + idx + '" title="移除此降级顺位" aria-label="移除此降级顺位">';
        html += '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" d="M6 18L18 6M6 6l12 12"/></svg>';
        html += '</button>';
      } else {
        html += '<span style="width:26px;flex:0 0 26px"></span>';
      }

      html += '</div>';
    });

    // 终极保底锚点
    html += '<div class="rrow" style="background:var(--bg-secondary);opacity:0.85">';
    html += '<div>';
    html += '<div class="rrow__t"><span class="fb-badge" style="background:var(--border)">底层锚点</span>系统离线锚点 (System Anchor)</div>';
    html += '<div class="rrow__s">外部所有公网模型均不可用或网络全断时，由 EchoBird 本机离线通道绝对托底（系统不可删除）</div>';
    html += '</div>';
    html += '<span class="tag" style="font-family:var(--font-mono);font-size:11px">EchoBird (127.0.0.1:53683)</span>';
    html += '<span style="width:26px;flex:0 0 26px"></span>';
    html += '</div>';

    host.innerHTML = html;

    host.querySelectorAll('[data-fallback-select]').forEach(function (sel) {
      sel.addEventListener('change', function () {
        var idx = parseInt(sel.getAttribute('data-fallback-select'), 10);
        var newModel = sel.value;
        var curChain = (modelCatalog.fallback_chain || []).slice();
        if (!curChain[idx]) curChain[idx] = {};
        if (typeof curChain[idx] === 'string') {
          curChain[idx] = { model: newModel };
        } else {
          curChain[idx].model = newModel;
        }
        modelCatalog.fallback_chain = curChain;
        if (idx === 0) {
          var fbSel = $('#route-table [data-route="fallback"]');
          if (fbSel) fbSel.value = newModel;
        }
        queueFallbackChainSync('已更新第 ' + (idx + 1) + ' 顺位备选模型为 ' + newModel);
      });
    });

    host.querySelectorAll('[data-fallback-del]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var idx = parseInt(btn.getAttribute('data-fallback-del'), 10);
        var curChain = (modelCatalog.fallback_chain || []).slice();
        if (curChain.length <= 1) return;
        var removed = curChain.splice(idx, 1);
        modelCatalog.fallback_chain = curChain;
        var rName = (removed[0] && (removed[0].model || removed[0])) || '模型';
        renderFallbackChain();
        queueFallbackChainSync('已移除第 ' + (idx + 1) + ' 顺位备选（' + rName + '）');
      });
    });
  }

  function renderRoutes() {
    var host = $('#route-table');
    if (!host) return;
    var roles = modelCatalog.roles || {};
    var keys = Object.keys(roles);
    var html = '';
    keys.forEach(function (rk) {
      var r = roles[rk];
      var name = String(r.name || rk).replace(/^[^\w\u4e00-\u9fa5]+\s*/, '');
      var cur = r.model || '';
      html += '<div class="rrow" data-route-row="' + rk + '">';
      html += '<div><div class="rrow__t">' + escAttr(name);
      html += '</div><div class="rrow__s">' + escAttr(r.desc || '') + '</div></div>';
      html += roleSelectHtml(rk, cur);
      html += '</div>';
    });
    if (!keys.length) html = '<div class="queueitem__empty">模型角色信息加载中…</div>';
    host.innerHTML = html;
    renderFallbackChain();
    paintModelRefs();
  }

  function roleSelectHtml(role, cur) {
    var pool = modelPool();
    var html = '<select data-route="' + escAttr(role) + '" aria-label="' + escAttr(role) + ' 使用的模型">';
    if (role === 'fallback') html += optionHtml('', '未启用', cur === '');
    pool.forEach(function (p) { html += optionHtml(p.model, p.model, p.model === cur); });
    if (cur && !pool.some(function (p) { return p.model === cur; })) {
      html += optionHtml(cur, cur, true);   // 当前模型不在目录里（如锚点模型）也要可见
    }
    html += '</select>';
    return html;
  }

  function paintModelRefs() {
    var name = $('#btn-model-name');
    if (name) name.textContent = mainModel();
    var rm = $('#rail-model-main');
    if (rm) rm.textContent = mainModel();
    var rs = $('#rail-model-subs');
    if (rs) {
      var mainM = mainModel();
      var roles = modelCatalog.roles || {};
      var n = Object.keys(roles).filter(function (k) {
        return k !== 'main' && roles[k].model && roles[k].model !== mainM;
      }).length;
      rs.textContent = n ? n + ' 项单独绑定' : '全部跟随主模型';
      rs.style.color = n ? '' : 'var(--muted)';
    }
  }

  function setRoute(role, value) {
    if (role === 'fallback' && !value) {
      toast('暂不支持从界面关闭容灾兜底');
      renderRoutes();   // 下拉已视觉变更，回滚到后端真实值
      return;
    }
    lastLocalSwitchTime = Date.now();
    lastLocalSwitchedModel = value;
    api('/api/switch_model', { method: 'POST', body: { model: value, target_role: role } })
      .then(function (res) {
        if (res && res.ok === false) {
          toast('切换失败：' + (res.error || '未知错误'));
          renderRoutes();   // 后端拒绝时下拉回滚到真实值
          return;
        }
        if (modelCatalog.roles[role]) modelCatalog.roles[role].model = value;
        if (role === 'main') modelCatalog.current = value;
        if (role === 'fallback') {
          if (modelCatalog.fallback_chain && modelCatalog.fallback_chain.length) {
            if (typeof modelCatalog.fallback_chain[0] === 'string') {
              modelCatalog.fallback_chain[0] = value;
            } else {
              modelCatalog.fallback_chain[0].model = value;
            }
          } else {
            modelCatalog.fallback_chain = [{ model: value }];
          }
        }
        renderRoutes();
        paintModelRefs();
        toast('已切到 ' + value + '（' + role + '）');
      })
      .catch(function (err) {
        toast('切换失败：' + (err.message || err));
        renderRoutes();
      });
  }

  var currentEditingModel = null;

  function openEndpointEditor(modelId) {
    var list = modelCatalog.models || [];
    var m = list.find(function (item) { return item.id === modelId; });
    if (!m) {
      m = list.find(function (item) { return item.name === modelId; });
    }
    if (!m) return;
    currentEditingModel = m;

    var dispName = m.display_name || m.name || m.id;
    var titleEl = $('#ep-modal-title');
    if (titleEl) titleEl.textContent = '调整接口参数 · ' + dispName;

    var nameInput = $('#ep-name');
    if (nameInput) nameInput.value = dispName;

    var urlInput = $('#ep-url');
    if (urlInput) urlInput.value = m.base_url || '';

    var modelInput = $('#ep-model');
    if (modelInput) modelInput.value = m.id || '';

    var ctxInput = $('#ep-ctx');
    if (ctxInput) ctxInput.value = m.context_window || m.ctx || '128000';

    var keyInput = $('#ep-key');
    if (keyInput) {
      keyInput.value = '';
      if (m.has_key) {
        keyInput.placeholder = '●●●●●●●●（已配置密钥，留空则保持原密钥）';
      } else {
        keyInput.placeholder = 'sk-...';
      }
    }

    var isLocal = m.group === 'local' || m.id === 'auto' ||
      (m.base_url && (m.base_url.indexOf('127.0.0.1') !== -1 || m.base_url.indexOf('localhost') !== -1 || m.base_url.indexOf('53683') !== -1));
    var nk = $('#ep-nokey');
    if (nk) nk.setAttribute('aria-checked', isLocal ? 'true' : 'false');

    var en = $('#ep-enable');
    if (en) en.setAttribute('aria-checked', 'true');

    var btnSave = $('#btn-ep-save');
    if (btnSave) btnSave.textContent = '保存调整';

    var btnDel = $('#btn-ep-delete');
    if (btnDel) {
      btnDel.style.display = m.is_custom ? '' : 'none';
    }

    ['ep-name', 'ep-url', 'ep-key', 'ep-model', 'ep-ctx'].forEach(function (id) {
      var el = $('#' + id);
      if (el) {
        el.removeAttribute('aria-invalid');
        var f = el.closest('.field');
        if (f) f.classList.remove('has-err');
      }
    });

    openModal('modal-endpoint');
  }

  function renderEndpoints() {
    var host = $('#key-list');
    if (!host) return;
    var list = modelCatalog.models || [];
    if (!list.length) {
      host.innerHTML = '<div class="queueitem__empty">尚未配置任何模型。</div>';
      return;
    }
    host.innerHTML = list.map(function (m, i) {
      var name = m.display_name || m.name || m.id || ('模型 ' + (i + 1));
      var base = m.base_url || '';
      return '<div class="keyrow" data-model-id="' + escAttr(m.id || '') + '" role="button" tabindex="0" title="点击调整此接口参数与密钥">' +
        '<span class="dot-s' + (m.has_key ? ' is-on' : '') + '" aria-hidden="true"></span>' +
        '<div><div class="keyrow__t">' + escAttr(name) + '</div>' +
        '<div class="keyrow__s">' + escAttr(base || (m.has_key ? '已配置密钥' : '未配置密钥')) + '</div></div>' +
        '<span class="meta" style="font-size:11px;color:var(--muted);white-space:nowrap">' + escAttr(m.id || '') + '</span>' +
        '<svg class="keyrow__arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
          '<path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/>' +
        '</svg>' +
        '</div>';
    }).join('');

    host.querySelectorAll('.keyrow').forEach(function (row) {
      var mid = row.getAttribute('data-model-id');
      if (!mid) return;
      var trigger = function () { openEndpointEditor(mid); };
      row.addEventListener('click', trigger);
      row.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          trigger();
        }
      });
    });
  }

  function setSettingTab(name) {
    $$('[data-set-tab]').forEach(function (b) {
      var on = b.getAttribute('data-set-tab') === name;
      b.setAttribute('aria-selected', String(on));
      b.setAttribute('tabindex', on ? '0' : '-1');
    });
    $$('[data-set-panel]').forEach(function (p) {
      p.hidden = p.getAttribute('data-set-panel') !== name;
    });
    if (name === 'weixin') loadWeixinStatus();
    if (name === 'behavior') loadBehaviorSettings();
  }

  /* ---------- 微信 ClawBot 绑定 ---------- */
  var weixinBusy = false;

  function weixinHoursText(h) {
    var n = Number(h);
    if (!isFinite(n) || n < 0) return '';
    if (n >= 1) return (n >= 10 ? Math.round(n) : Math.round(n * 10) / 10) + ' 小时';
    return Math.max(1, Math.round(n * 60)) + ' 分钟';
  }

  function paintWeixinStatus(st) {
    var badge = $('#weixin-badge');
    var text = $('#weixin-status-text');
    var qrBox = $('#weixin-qr-box');
    if (!badge || !text) return;
    if (!st || st.status === 'disabled') {
      badge.textContent = '○ 通道未启用';
      text.textContent = (st && st.message) || '微信通道未启用';
      return;
    }
    if (st.connected) {
      badge.textContent = '● 已连接';
      text.textContent = '微信 ClawBot 在线' + (st.user_id ? ' · ' + st.user_id : '');
      if (st.last_error) {
        badge.textContent = '⚠ 已连接 · 通道异常';
        text.textContent += ' · ' + st.last_error;
      } else if (st.last_save_error) {
        badge.textContent = '⚠ 已连接 · 凭证未落盘';
        text.textContent += ' · ' + st.last_save_error + '（重启后需重新扫码）';
      } else if (st.expires_soon) {
        badge.textContent = '● 已连接 · 即将到期';
        text.textContent += ' · 授权约 ' + weixinHoursText(st.remaining_hours) + '后过期，请尽快重新扫码';
      }
      if (qrBox) {
        qrBox.style.color = 'var(--state-ok)';
        qrBox.innerHTML = '';
        qrBox.textContent = '✅ 已绑定';
      }
      return;
    }
    if (qrBox) qrBox.style.color = '';
    if (st.has_active_qr) {
      badge.textContent = '◔ 等待扫码';
      text.textContent = '请使用手机微信扫码，并在手机上确认授权';
      return;
    }
    if (st.token_expired) {
      var ttlHours = Number(st.token_ttl_hours) > 0 ? Number(st.token_ttl_hours) : 72;
      badge.textContent = '⚠ 授权已过期';
      text.textContent = 'bot_token 已超过 ' + ttlHours + ' 小时，请重新扫码授权';
    } else if (st.credential_missing) {
      badge.textContent = '○ 未登录';
      text.textContent = '本机没有微信授权凭证，点击下方按钮生成登录二维码';
    } else {
      badge.textContent = '○ 未登录';
      text.textContent = '尚未绑定微信，点击下方按钮生成登录二维码';
    }
    if (st.last_save_error) text.textContent += ' · 凭证保存失败：' + st.last_save_error;
  }

  function showWeixinQr() {
    var qrBox = $('#weixin-qr-box');
    if (!qrBox) return;
    var tq = API_TOKEN ? '&token=' + encodeURIComponent(API_TOKEN) : '';
    qrBox.style.color = '';
    qrBox.innerHTML = '<img src="' + API + '/api/weixin/qr_img?t=' + Date.now() + tq +
      '" alt="微信扫码绑定" style="width:100%;height:100%;object-fit:contain;border-radius:10px">';
  }

  function loadWeixinStatus() {
    return api('/api/weixin/status').then(function (st) {
      paintWeixinStatus(st);
      if (st && !st.connected && st.has_active_qr) showWeixinQr();
      return st;
    }).catch(function () {
      var badge = $('#weixin-badge');
      if (badge) badge.textContent = '○ 状态获取失败';
    });
  }

  function generateWeixinQr() {
    if (weixinBusy) return;
    weixinBusy = true;
    var btn = $('#btn-weixin-qr');
    var hint = $('#weixin-hint');
    if (btn) { btn.disabled = true; btn.textContent = '正在获取…'; }
    api('/api/weixin/login', { method: 'POST' }).then(function (res) {
      if (res && res.status === 'already_connected') {
        toast(res.message || '微信已绑定连接，无需重复扫码');
        return loadWeixinStatus();
      }
      showWeixinQr();
      loadWeixinStatus();
      if (hint) {
        hint.hidden = false;
        hint.textContent = '绑定路径：手机微信「我 → 设置 → 插件 → 微信 ClawBot」→ 扫码并确认授权；二维码约 8 分钟内有效。';
      }
      toast('微信专属二维码已就绪，请扫码绑定');
    }).catch(function (e) {
      toast('生成微信登录码失败：' + ((e && e.message) || e));
    }).then(function () {
      weixinBusy = false;
      if (btn) { btn.disabled = false; btn.textContent = '重新生成二维码'; }
    });
  }

  /* ---------- 新建 / 调整接口弹窗 ---------- */
  function resetEndpointForm() {
    currentEditingModel = null;
    var titleEl = $('#ep-modal-title');
    if (titleEl) titleEl.textContent = '新增自定义接口';

    var btnSave = $('#btn-ep-save');
    if (btnSave) btnSave.textContent = '保存接口';

    var btnDel = $('#btn-ep-delete');
    if (btnDel) btnDel.style.display = 'none';

    ['ep-name', 'ep-url', 'ep-key', 'ep-model', 'ep-ctx'].forEach(function (id) {
      var el = $('#' + id);
      if (el) {
        el.value = id === 'ep-ctx' ? '128000' : '';
        el.removeAttribute('aria-invalid');
        var f = el.closest('.field');
        if (f) f.classList.remove('has-err');
      }
    });
    var keyInput = $('#ep-key');
    if (keyInput) keyInput.placeholder = 'sk-...';
    var proto = $('#ep-proto');
    if (proto) proto.value = 'OpenAI 兼容 / chat/completions';
    var nk = $('#ep-nokey');
    if (nk) nk.setAttribute('aria-checked', 'false');
    var en = $('#ep-enable');
    if (en) en.setAttribute('aria-checked', 'true');
  }

  function markBad(id, bad) {
    var el = $('#' + id);
    if (!el) return;
    var f = el.closest('.field');
    if (f) f.classList.toggle('has-err', !!bad);
    if (bad) el.setAttribute('aria-invalid', 'true');
    else el.removeAttribute('aria-invalid');
  }

  function saveEndpoint() {
    var noKey = $('#ep-nokey').getAttribute('aria-checked') === 'true';
    var enableNow = $('#ep-enable').getAttribute('aria-checked') === 'true';
    var name = $('#ep-name').value.trim();
    var base = $('#ep-url').value.trim();
    var key = $('#ep-key').value.trim();
    var models = $('#ep-model').value.split(/[,，]/).map(function (x) { return x.trim(); }).filter(Boolean);
    var ctxRaw = $('#ep-ctx').value.trim();
    var ok = true;

    var hasExistingKey = currentEditingModel && currentEditingModel.has_key;

    markBad('ep-name', !name);
    if (!name) ok = false;
    var baseOk = /^https?:\/\/.+/.test(base);
    markBad('ep-url', !baseOk);
    if (!baseOk) ok = false;

    // 如果未选本机无鉴权、未输入新 key，且既无已有 key，则报错
    var keyNeeded = !noKey && !key && !hasExistingKey;
    markBad('ep-key', keyNeeded);
    if (keyNeeded) ok = false;

    markBad('ep-model', models.length === 0);
    if (!models.length) ok = false;
    var ctxOk = /^\d+$/.test(ctxRaw);
    markBad('ep-ctx', !ctxOk);
    if (!ctxOk) ok = false;

    if (!ok) {
      var first = $('#modal-endpoint .field.has-err .input');
      if (first) first.focus();
      toast('还有几项没填对');
      return;
    }

    var firstModel = models[0];
    var payload = {
      model: firstModel,
      model_id: firstModel,
      name: name,
      display_name: name,
      base_url: base,
      api_key: noKey ? '' : key,
      no_key: noKey,
      context_window: parseInt(ctxRaw, 10) || 128000,
      target_role: enableNow ? 'main' : 'none',
      switch_active: enableNow
    };

    if (currentEditingModel && currentEditingModel.id) {
      payload.previous_model_id = currentEditingModel.id;
    }

    api('/api/settings/save_model_config', {
      method: 'POST',
      body: payload
    }).then(function (res) {
      if (res && res.ok === false) { toast('保存失败：' + (res.error || '未知错误')); return; }
      closeModal($('#modal-endpoint'));
      toast('已保存接口「' + name + '」');
      loadModels();
    }).catch(function (err) {
      toast('保存失败：' + (err.message || err));
    });
  }

  function testEndpoint() {
    var base = $('#ep-url').value.trim();
    var model = $('#ep-model').value.split(/[,，]/)[0].trim();
    var name = $('#ep-name').value.trim() || '未命名接口';
    var key = $('#ep-key').value.trim();
    var noKey = $('#ep-nokey').getAttribute('aria-checked') === 'true';

    if (!/^https?:\/\/.+/.test(base)) {
      markBad('ep-url', true);
      toast('先把 Base URL 填成 http(s):// 开头');
      return;
    }
    var btn = $('#btn-ep-test');
    if (btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = '正在测试…';
    var finish = function (msg) {
      btn.classList.remove('is-busy');
      btn.removeAttribute('aria-busy');
      btn.textContent = '测试连接';
      toast(msg);
    };

    var testBody = { model: model, base_url: base };
    if (key) {
      testBody.api_key = key;
    } else if (noKey) {
      testBody.api_key = 'eb-local-no-key';
    }

    api('/api/settings/test_llm', { method: 'POST', body: testBody })
      .then(function (res) {
        var okRes = res && res.ok !== false;
        finish(name + ' · ' + (okRes ? '连通正常' : '失败：' + ((res && res.error) || '未知错误')));
      })
      .catch(function (err) { finish(name + ' · 测试失败：' + (err.message || err)); });
  }

  /* ---------- 输入区模型挑选菜单 ---------- */
  function openModelMenu() {
    var menu = $('#model-menu');
    var btn = $('#btn-model');
    if (!menu || !btn) return;
    var pool = modelPool();
    var cur = mainModel();
    var html = '<p class="menu__label">主 Agent 模型</p>';
    pool.forEach(function (p) {
      html += '<button class="menu__item" role="menuitemradio" aria-checked="' + (p.model === cur ? 'true' : 'false') +
        '" data-pick-model="' + escAttr(p.model) + '">' +
        '<span class="mono">' + escAttr(p.model) + '</span>' +
        '<span class="tag">' + escAttr(p.ep.name) + '</span>' +
        '<svg class="chk" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>' +
        '</button>';
    });
    html += '<div class="menu__sep"></div>';
    html += '<button class="menu__item" role="menuitem" data-open-settings="models">' +
      '<span class="mono" style="flex:1">为子 Agent 单独绑定…</span>' +
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 6l6 6-6 6"/></svg></button>';
    html += '<p class="menu__foot"><span>' + escAttr(mainModel()) + '</span><span class="mono">Ctrl+K 设置</span></p>';
    menu.innerHTML = html;

    var r = btn.getBoundingClientRect();
    menu.hidden = false;
    var mw = menu.offsetWidth;
    var left = Math.min(Math.max(8, r.left), window.innerWidth - mw - 8);
    menu.style.left = left + 'px';
    menu.style.top = Math.max(8, r.top - menu.offsetHeight - 8) + 'px';
    btn.setAttribute('aria-expanded', 'true');

    $$('[data-pick-model]', menu).forEach(function (it) {
      it.addEventListener('click', function () {
        var m = it.getAttribute('data-pick-model');
        setRoute('main', m);
        closeModelMenu();
      });
    });
    var goSet = $('[data-open-settings]', menu);
    if (goSet) goSet.addEventListener('click', function () {
      closeModelMenu();
      openSettings('models');
    });
  }

  function closeModelMenu() {
    var menu = $('#model-menu');
    var btn = $('#btn-model');
    if (menu) menu.hidden = true;
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  /* ---------- 行为设置 (审批超时、危险动作默认粒度、托盘常驻、自动降级) ---------- */
  var behaviorSettings = {
    approval_timeout_sec: 180,
    hazardous_default_scope: 'session',
    tray_resident: true,
    auto_failover: true
  };

  function loadBehaviorSettings() {
    return api('/api/settings/behavior').then(function (res) {
      var b = (res && res.behavior) || {};
      if (b.approval_timeout_sec) {
        behaviorSettings.approval_timeout_sec = b.approval_timeout_sec;
        var selTimeout = $('#sel-approval-timeout');
        if (selTimeout) selTimeout.value = String(b.approval_timeout_sec);
      }
      if (b.hazardous_default_scope) {
        behaviorSettings.hazardous_default_scope = b.hazardous_default_scope;
        s.approvalScope = b.hazardous_default_scope;
        var selScope = $('#sel-hazardous-scope');
        if (selScope) selScope.value = b.hazardous_default_scope;
      }
      if (b.tray_resident !== undefined) {
        behaviorSettings.tray_resident = !!b.tray_resident;
        var swTray = $('#sw-tray');
        if (swTray) swTray.setAttribute('aria-checked', String(behaviorSettings.tray_resident));
      }
      if (b.auto_failover !== undefined) {
        behaviorSettings.auto_failover = !!b.auto_failover;
        var swFailover = $('#sw-failover');
        if (swFailover) swFailover.setAttribute('aria-checked', String(behaviorSettings.auto_failover));
      }
      return behaviorSettings;
    }).catch(function (e) {
      console.warn('加载行为配置失败:', e);
    });
  }

  function saveBehaviorSettings(patch) {
    Object.assign(behaviorSettings, patch);
    return api('/api/settings/behavior', {
      method: 'POST',
      body: patch
    }).then(function (res) {
      if (res && res.behavior) {
        Object.assign(behaviorSettings, res.behavior);
      }
      toast('行为设置已保存');
    }).catch(function (err) {
      toast('保存行为设置失败: ' + (err.message || err));
    });
  }

  function openSettings(tab) {
    loadModels();
    renderRoutes();
    renderEndpoints();
    loadBehaviorSettings();
    setSettingTab(tab || 'models');
    openModal('modal-settings');
  }

  function bindModelUi() {
    var btn = $('#btn-model');
    if (btn) btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var menu = $('#model-menu');
      if (menu && !menu.hidden) closeModelMenu();
      else openModelMenu();
    });

    document.addEventListener('click', function (e) {
      var menu = $('#model-menu');
      if (!menu || menu.hidden) return;
      if (e.target.closest && e.target.closest('#model-menu')) return;
      if (e.target.closest && e.target.closest('#btn-model')) return;
      closeModelMenu();
    });

    window.addEventListener('resize', closeModelMenu);

    $$('[data-set-tab]').forEach(function (b) {
      b.addEventListener('click', function () { setSettingTab(b.getAttribute('data-set-tab')); });
    });

    var wxQrBtn = $('#btn-weixin-qr');
    if (wxQrBtn) wxQrBtn.addEventListener('click', generateWeixinQr);

    var routeHost = $('#route-table');
    if (routeHost) routeHost.addEventListener('change', function (e) {
      var sel = e.target.closest('[data-route]');
      if (!sel) return;
      setRoute(sel.getAttribute('data-route'), sel.value);   // 全角色真实切换
    });

    var addFb = $('#btn-add-fallback');
    if (addFb) addFb.addEventListener('click', function () {
      if (addFb.disabled) return;
      var chain = (modelCatalog.fallback_chain || []).slice();
      var pool = modelPool();
      var existing = chain.map(function (c) { return typeof c === 'string' ? c : (c && c.model); });
      // 优先从可用公网模型中选尚未被添加的模型，过滤掉已作为底层系统锚点的 auto (EchoBird)
      var candidate = pool.find(function (p) { return p.model !== 'auto' && existing.indexOf(p.model) === -1; });
      var nextModel = (candidate && candidate.model) || (pool.find(function (p) { return p.model !== 'auto'; }) || {}).model || (pool[0] && pool[0].model) || 'auto';
      chain.push({ model: nextModel });
      modelCatalog.fallback_chain = chain;

      // 瞬间响应：0ms 立即在 DOM 中追加新顺位行
      renderFallbackChain();

      // 按钮 200ms 防误触
      addFb.disabled = true;
      setTimeout(function () { if (addFb) addFb.disabled = false; }, 200);

      queueFallbackChainSync('已新增第 ' + chain.length + ' 顺位备用降级模型：' + nextModel);
    });

    var addEp = $('#btn-add-endpoint');
    if (addEp) addEp.addEventListener('click', function () {
      resetEndpointForm();
      openModal('modal-endpoint');
    });

    $$('#modal-endpoint [data-preset]').forEach(function (b) {
      b.addEventListener('click', function () {
        var p = PRESETS[b.getAttribute('data-preset')] || PRESETS.custom;
        $('#ep-name').value = p.name;
        $('#ep-url').value = p.base;
        $('#ep-model').value = p.model;
        $('#ep-ctx').value = p.ctx || '128000';
        $('#ep-nokey').setAttribute('aria-checked', p.noKey ? 'true' : 'false');
        ['ep-name', 'ep-url', 'ep-key', 'ep-model', 'ep-ctx'].forEach(function (id) { markBad(id, false); });
      });
    });

    var epDel = $('#btn-ep-delete');
    if (epDel) {
      epDel.addEventListener('click', function () {
        if (!currentEditingModel) return;
        var mid = currentEditingModel.id;
        var name = currentEditingModel.display_name || currentEditingModel.name || mid;
        if (!confirm('确定要删除自定义模型「' + name + '」吗？')) return;
        api('/api/models/' + encodeURIComponent(mid), { method: 'DELETE' })
          .then(function (res) {
            if (res && res.ok === false) {
              toast('删除失败：' + (res.error || '未知错误'));
              return;
            }
            closeModal($('#modal-endpoint'));
            toast('已删除模型「' + name + '」');
            loadModels();
          })
          .catch(function (err) {
            toast('删除失败：' + (err.message || err));
          });
      });
    }

    var epSave = $('#btn-ep-save');
    if (epSave) epSave.addEventListener('click', saveEndpoint);
    var epTest = $('#btn-ep-test');
    if (epTest) epTest.addEventListener('click', testEndpoint);
    var nk = $('#ep-nokey');
    if (nk) nk.addEventListener('click', function () {
      var on = nk.getAttribute('aria-checked') === 'true';
      nk.setAttribute('aria-checked', String(!on));
      if (!on) markBad('ep-key', false);
    });
    var en = $('#ep-enable');
    if (en) en.addEventListener('click', function () {
      en.setAttribute('aria-checked', en.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
    });

    ['ep-name', 'ep-url', 'ep-key', 'ep-model', 'ep-ctx'].forEach(function (id) {
      var el = $('#' + id);
      if (el) el.addEventListener('input', function () { markBad(id, false); });
    });

    var selTimeout = $('#sel-approval-timeout');
    if (selTimeout) {
      selTimeout.addEventListener('change', function () {
        var val = parseInt(selTimeout.value, 10);
        if (val > 0) saveBehaviorSettings({ approval_timeout_sec: val });
      });
    }

    var selScope = $('#sel-hazardous-scope');
    if (selScope) {
      selScope.addEventListener('change', function () {
        var val = selScope.value;
        s.approvalScope = val;
        saveBehaviorSettings({ hazardous_default_scope: val });
      });
    }

    var swTray = $('#sw-tray');
    if (swTray) {
      swTray.addEventListener('click', function () {
        var on = swTray.getAttribute('aria-checked') === 'true';
        var next = !on;
        swTray.setAttribute('aria-checked', String(next));
        saveBehaviorSettings({ tray_resident: next });
      });
    }

    var swFailover = $('#sw-failover');
    if (swFailover) {
      swFailover.addEventListener('click', function () {
        var on = swFailover.getAttribute('aria-checked') === 'true';
        var next = !on;
        swFailover.setAttribute('aria-checked', String(next));
        saveBehaviorSettings({ auto_failover: next });
      });
    }

    setSettingTab('models');
    renderRoutes();
    renderEndpoints();
  }


  function safeHref(href) {
    var v = String(href || '').trim();
    return /^(https?:\/\/|\/|#|mailto:)/i.test(v) ? v : '';
  }

  function emojify(text) {
    return text.replace(/:([a-z0-9_+-]{2,24}):/gi, function (all, name) {
      var hit = EMOJI_MAP[String(name).toLowerCase()];
      return hit || all;
    });
  }

  function inlineMd(src) {
    var out = escHtml(emojify(src));
    var codes = [];
    out = out.replace(/`([^`]+)`/g, function (all, code) {
      codes.push(code);
      return '\u0000C' + (codes.length - 1) + '\u0000';
    });
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (all, label, href) {
      var url = safeHref(href.replace(/&amp;/g, '&'));
      if (!url) return label;
      return '<a class="md-link" href="' + escAttr(url) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });
    out = out.replace(/(^|[\s(])((?:https?:\/\/)[^\s<)]+)/g, function (all, pre, url) {
      var clean = url.replace(/[.,;:!?\u3002\uFF0C]+$/, '');
      var tail = url.slice(clean.length);
      return pre + '<a class="md-link" href="' + escAttr(clean) + '" target="_blank" rel="noopener noreferrer">' + clean + '</a>' + tail;
    });
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    out = out.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    out = out.replace(/\u0000C(\d+)\u0000/g, function (all, i) {
      return '<code>' + codes[Number(i)] + '</code>';
    });
    return out;
  }

  function renderMd(source) {
    var lines = String(source == null ? '' : source).replace(/\r\n?/g, '\n').split('\n');
    var html = [];
    var i = 0;
    var para = [];

    function flushPara() {
      if (!para.length) return;
      html.push('<p>' + para.map(inlineMd).join('<br>') + '</p>');
      para = [];
    }

    while (i < lines.length) {
      var line = lines[i];

      if (/^\s*```/.test(line)) {
        var lang = line.replace(/^\s*```/, '').trim();
        var buf = [];
        i += 1;
        while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
        i += 1;
        flushPara();
        html.push('<pre class="md-pre"><code>' + escHtml(buf.join('\n')) + '</code></pre>');
        if (lang) html[html.length - 1] = html[html.length - 1].replace('<pre class="md-pre">', '<pre class="md-pre" data-lang="' + escAttr(lang) + '">');
        continue;
      }

      if (/^\s*$/.test(line)) { flushPara(); i += 1; continue; }

      var quote = line.match(/^\s*>\s?(.*)$/);
      if (quote) {
        flushPara();
        var qbuf = [];
        while (i < lines.length) {
          var q = lines[i].match(/^\s*>\s?(.*)$/);
          if (!q) break;
          qbuf.push(q[1]);
          i += 1;
        }
        html.push('<blockquote class="md-quote">' + qbuf.map(inlineMd).join('<br>') + '</blockquote>');
        continue;
      }

      if (/^\s*(?:---+|\*\*\*+)\s*$/.test(line)) {
        flushPara();
        html.push('<hr class="md-hr">');
        i += 1;
        continue;
      }

      var head = line.match(/^\s*(#{1,6})\s+(.*)$/);
      if (head) {
        flushPara();
        var level = Math.min(4, head[1].length) + 2;
        html.push('<h' + level + ' class="md-h">' + inlineMd(head[2].replace(/\s+#+\s*$/, '')) + '</h' + level + '>');
        i += 1;
        continue;
      }

      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        flushPara();
        var cells = function (row) {
          return row.trim().replace(/^\||\|$/g, '').split('|').map(function (cell) { return cell.trim(); });
        };
        var headCells = cells(line);
        i += 2;
        var rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i += 1; }
        html.push('<table class="md-table"><thead><tr>' + headCells.map(function (h) {
          return '<th>' + inlineMd(h) + '</th>';
        }).join('') + '</tr></thead><tbody>' + rows.map(function (r) {
          return '<tr>' + headCells.map(function (_, ci) {
            return '<td>' + inlineMd(r[ci] || '') + '</td>';
          }).join('') + '</tr>';
        }).join('') + '</tbody></table>');
        continue;
      }

      if (/^\s*(?:[-*+]|\d+[.)])\s+/.test(line)) {
        flushPara();
        var ordered = /^\s*\d+[.)]\s+/.test(line);
        var items = [];
        while (i < lines.length) {
          var item = lines[i].match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
          if (!item) break;
          items.push(item[1]);
          i += 1;
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && items.length) {
            items[items.length - 1] += '\n' + lines[i].trim();
            i += 1;
          }
        }
        var tag = ordered ? 'ol' : 'ul';
        html.push('<' + tag + ' class="md-list">' + items.map(function (t) {
          return '<li>' + t.split('\n').map(inlineMd).join('<br>') + '</li>';
        }).join('') + '</' + tag + '>');
        continue;
      }

      para.push(line);
      i += 1;
    }
    flushPara();
    return html.join('');
  }

  function paintBubble(el, source) {
    if (!el) return '';
    var safe = renderMd(source);
    el.innerHTML = safe;
    return safe;
  }

  /* ================= 用量看板（真实历史数据 + 折算花费） ================= */

  function fmtTok(n) {
    n = Number(n) || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1).replace(/\.0$/, '') + 'M';
    if (n >= 1000) return (n / 1000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, '') + 'k';
    return String(n);
  }

  var usageEventsBound = false;

  function bindUsageEvents() {
    if (usageEventsBound) return;
    var rangeGroup = $('#usage-range');
    if (rangeGroup) {
      $$('button', rangeGroup).forEach(function (btn) {
        btn.addEventListener('click', function () {
          var r = btn.getAttribute('data-range') || '30';
          if (s.usageRange === r) return;
          s.usageRange = r;
          $$('button', rangeGroup).forEach(function (b) {
            b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
          });
          loadAndRenderUsage();
        });
      });
    }

    var metricGroup = $('#usage-metric');
    if (metricGroup) {
      $$('button', metricGroup).forEach(function (btn) {
        btn.addEventListener('click', function () {
          var m = btn.getAttribute('data-metric') || 'tok';
          if (s.usageMetric === m) return;
          s.usageMetric = m;
          $$('button', metricGroup).forEach(function (b) {
            b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
          });
          paintUsageTrend();
        });
      });
    }
    usageEventsBound = true;
  }

  function paintUsageTrend() {
    var data = s.usageData;
    var days = (data && data.days) || [];
    var metric = s.usageMetric || 'tok';
    var isCost = metric === 'cost';

    var t = $('#usage-trend-title');
    var rName = s.usageRange === '7' ? '近 7 天' : s.usageRange === 'all' ? '全部' : '近 30 天';
    if (t) t.textContent = rName + '走势（' + (isCost ? '花费' : 'Token') + '）';

    var legend = $('#usage-legend');
    if (legend) {
      legend.innerHTML = isCost
        ? '<span><i class="lg-in"></i>预估花费</span>'
        : '<span><i class="lg-in"></i>输入</span><span><i class="lg-out"></i>输出</span>';
    }

    var bars = $('#usage-bars');
    var x = $('#usage-x');
    if (!bars) return;

    if (!days.length) {
      bars.innerHTML = '<div style="margin:auto;color:var(--text-muted);font-size:12px">暂无走势数据</div>';
      if (x) x.innerHTML = '';
      return;
    }

    var maxVal = 1;
    if (isCost) {
      maxVal = Math.max.apply(null, days.map(function (d) { return d.cost || 0; })) || 0.01;
    } else {
      maxVal = Math.max.apply(null, days.map(function (d) { return (d.total_tokens || 0); })) || 1;
    }

    var barsHtml = '';
    days.forEach(function (d) {
      var tip = '';
      var inPct = 0, outPct = 0;
      if (isCost) {
        var cost = d.cost || 0;
        var costFormatted = d.cost_formatted || ('¥' + cost.toFixed(2));
        tip = d.day_label + ' · ' + costFormatted;
        inPct = cost > 0 ? Math.max(3, Math.min(100, Math.round(cost / maxVal * 100))) : 0;
        outPct = 0;
      } else {
        var tin = d.input_tokens || 0, tout = d.output_tokens || 0;
        tip = d.day_label + ' · in ' + fmtTok(tin) + ' / out ' + fmtTok(tout);
        inPct = tin > 0 ? Math.max(1, Math.min(100, Math.round(tin / maxVal * 100))) : 0;
        outPct = tout > 0 ? Math.max(1, Math.min(100, Math.round(tout / maxVal * 100))) : 0;
        if (inPct + outPct > 100) {
          var ratio = 100 / (inPct + outPct);
          inPct = Math.round(inPct * ratio);
          outPct = Math.round(outPct * ratio);
        }
      }

      var outSpan = outPct > 0 ? '<span class="bar__out" style="height:' + outPct + '%"></span>' : '';
      var inSpan = inPct > 0 ? '<span class="bar__in" style="height:' + inPct + '%"></span>' : '';
      barsHtml += '<div class="bar" role="img" aria-label="' + escHtml(tip) + '">' +
        '<span class="bar__tip">' + escHtml(tip) + '</span>' +
        outSpan +
        inSpan +
      '</div>';
    });
    bars.innerHTML = barsHtml;

    if (x) {
      var step = Math.max(1, Math.floor(days.length / 5));
      var xHtml = '';
      for (var i = 0; i < days.length; i += step) {
        xHtml += '<span>' + escHtml(days[i].day_label) + '</span>';
      }
      if ((days.length - 1) % step !== 0) {
        xHtml += '<span>' + escHtml(days[days.length - 1].day_label) + '</span>';
      }
      x.innerHTML = xHtml;
    }
  }

  function paintUsageView(data) {
    if (!$('#view-usage')) return;
    bindUsageEvents();

    var summary = (data && data.summary) || {};
    var inTok = summary.input_tokens || 0;
    var outTok = summary.output_tokens || 0;
    var total = summary.total_tokens || (inTok + outTok);
    var reqs = summary.requests || 0;
    var costStr = summary.total_cost_formatted || ('¥' + (summary.total_cost || 0).toFixed(2));

    var rName = s.usageRange === '7' ? '近 7 天' : s.usageRange === 'all' ? '全部' : '近 30 天';

    var host = $('#usage-kpis');
    if (host) {
      host.innerHTML =
        '<div class="kpi" data-od-id="usage-kpi-token">' +
          '<div class="kpi__k">Token</div>' +
          '<div class="kpi__v">' + fmtTok(total) + '<em>tok</em></div>' +
          '<div class="kpi__s"><span class="meta">' + rName + '累计 · 预估 ' + escHtml(costStr) + '</span></div>' +
        '</div>' +
        '<div class="kpi" data-od-id="usage-kpi-split">' +
          '<div class="kpi__k">输入 / 输出</div>' +
          '<div class="kpi__v">' + fmtTok(inTok) + '<em>in</em></div>' +
          '<div class="kpi__s"><span>输出 ' + fmtTok(outTok) + ' tok</span></div>' +
        '</div>' +
        '<div class="kpi" data-od-id="usage-kpi-request">' +
          '<div class="kpi__k">请求</div>' +
          '<div class="kpi__v">' + num(reqs) + '<em>次</em></div>' +
          '<div class="kpi__s"><span class="meta">' + rName + '累计调用</span></div>' +
        '</div>';
    }

    var days = (data && data.days) || [];
    var heat = $('#usage-heat');
    if (heat) {
      heat.innerHTML = days.map(function (d) {
        var tip = d.date + '：' + (d.total_tokens ? fmtTok(d.total_tokens) + ' tok (' + d.requests + ' 次调用)' : '无调用');
        var cls = d.is_today ? ' class="is-today"' : '';
        return '<i data-lv="' + (d.level || 0) + '"' + cls + ' title="' + escHtml(tip) + '" aria-label="' + escHtml(tip) + '"></i>';
      }).join('');
    }

    var heatNote = $('#usage-heat-note');
    if (heatNote) heatNote.textContent = rName + '活跃（共 ' + reqs + ' 次调用）';

    var heatMonths = $('.heat__months');
    if (heatMonths && days.length >= 7) {
      var ticks = [];
      for (var hi = 0; hi < days.length; hi += 7) {
        ticks.push(days[hi].day_label);
      }
      if (ticks.length) {
        heatMonths.innerHTML = ticks.map(function (t) { return '<span>' + escHtml(t) + '</span>'; }).join('');
      }
    }

    paintUsageTrend();

    var models = (data && data.models) || [];
    var body = $('#usage-models-body');
    if (body) {
      if (!models.length) {
        body.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted)">当前区间暂无模型调用数据。</td></tr>';
      } else {
        body.innerHTML = models.map(function (m) {
          var pct = Math.round((m.ratio || 0) * 100);
          return '<tr>' +
            '<td><div class="mname"><b>' + escHtml(m.model) + '</b><span>' + escHtml(m.provider || '官方 API') + ' · ' + (m.requests || 0) + ' 次调用</span></div></td>' +
            '<td class="num">' + fmtTok(m.total_tokens || 0) + '</td>' +
            '<td class="num"><span>' + pct + '%</span><span class="share"><i style="width:' + pct + '%"></i></span></td>' +
            '<td class="num">' + escHtml(m.cost_formatted || ('¥' + (m.cost || 0).toFixed(2))) + '</td>' +
          '</tr>';
        }).join('');
      }
    }

    var foot = $('#usage-models-foot');
    if (foot) {
      if (models.length) {
        foot.innerHTML = '<tr>' +
          '<td>总计（' + models.length + ' 个模型）</td>' +
          '<td class="num">' + fmtTok(total) + '</td>' +
          '<td class="num">100%</td>' +
          '<td class="num">' + escHtml(costStr) + '</td>' +
        '</tr>';
      } else {
        foot.innerHTML = '';
      }
    }

    var note = $('#usage-note');
    if (note) {
      note.innerHTML = '金额按各家官方 API 的目录价折算：输入 ¥1.00 / 1M，输出 ¥2.00 / 1M。自建与本地模型不计费。数据来源：conversations.db 历史对话记录。';
    }
  }

  function loadAndRenderUsage() {
    s.usageRange = s.usageRange || '30';
    s.usageMetric = s.usageMetric || 'tok';
    api('/api/usage?range=' + encodeURIComponent(s.usageRange))
      .then(function (res) {
        if (res) {
          s.usageData = res;
          paintUsageView(res);
        }
      })
      .catch(function (err) {
        console.warn('load usage failed', err);
        var u = s.tokenUsage || {};
        var fallback = {
          summary: {
            input_tokens: u.input || 0,
            output_tokens: u.output || 0,
            total_tokens: (u.input || 0) + (u.output || 0),
            requests: (u.known_requests || 0) + (u.unknown_requests || 0),
            total_cost: 0,
            total_cost_formatted: '¥0.00'
          },
          days: [],
          models: []
        };
        paintUsageView(fallback);
      });
  }

  function renderUsage() {
    loadAndRenderUsage();
  }


  /* ---------- 对话 / 发送 ---------- */
  function appendAgentBubble(text, id) {
    var stream = $('#view-chat');
    if (!stream) return;
    var msg = document.createElement('div');
    msg.className = 'msg msg--agent';
    if (id) msg.id = 'msg-' + id;
    msg.innerHTML = '<div class="msg__avatar"><img src="' + avatarSrc(s.avatar, 'idle') + '" alt="AlterEgo 形象"></div>' +
      '<div class="msg__body"><div class="bubble md"></div></div>';
    paintBubble(msg.querySelector('.bubble'), text);
    var anchor = $('#after-approval');
    if (anchor && anchor.parentNode === stream) stream.insertBefore(msg, anchor);
    else stream.appendChild(msg);
    scrollStream();
    return msg;
  }

  function appendUserBubble(text, id) {
    var stream = $('#view-chat');
    if (!stream) return;
    var msg = document.createElement('div');
    msg.className = 'msg msg--user';
    if (id) msg.id = 'msg-' + id;
    msg.innerHTML = '<div class="msg__body"><div class="bubble md"></div><span class="msg__time">' + clock() + '</span></div>';
    paintBubble(msg.querySelector('.bubble'), text);
    var anchor = $('#after-approval');
    if (anchor && anchor.parentNode === stream) stream.insertBefore(msg, anchor);
    else stream.appendChild(msg);
    scrollStream();
    return msg;
  }

  function scrollStream() {
    var stream = $('#stream');
    if (stream) stream.scrollTop = stream.scrollHeight;
  }

  /* ---------- 未完成任务提醒（进程重启后遗留任务） ---------- */
  var resumeTask = null;

  var RESUME_STATUS_LABEL = {
    incomplete: '未完成', waiting_user: '等待你补充',
    error: '执行出错', running: '进程重启中断'
  };

  function paintResumeBar() {
    var bar = $('#resume-bar');
    if (!bar) return;
    var task = resumeTask;
    var show = !!(task && task.session_id && task.session_id === s.sessionId);
    bar.hidden = !show;
    if (!show) return;
    var title = $('#resume-bar-title');
    var desc = $('#resume-bar-desc');
    if (title) title.textContent = '上次有个任务没做完（' + (RESUME_STATUS_LABEL[task.task_status] || '未完成') + '）';
    if (desc) desc.textContent = task.task_desc || '';
  }

  function checkPendingResume() {
    api('/api/pending_resume').then(function (d) {
      resumeTask = (d && d.has_pending && d.task) ? d.task : null;
      paintResumeBar();
    }).catch(function () {});
  }

  function dismissResume() {
    resumeTask = null;
    paintResumeBar();
    api('/api/dismiss_resume', { method: 'POST', body: {} }).catch(function () {});
  }

  function sendMessage() {
    var input = $('#composer-input');
    if (!input) return;
    var text = input.value.trim();
    if (!text || busy) return;
    input.value = '';
    autosize();
    syncSend();
    submitText(text);
  }

  function submitText(text) {
    if (!text || busy) return;
    resumeTask = null;
    paintResumeBar();                               // 已开口后由 Agent 在回复里提醒，不再重复占位
    turnGoals[s.sessionId] = firstLine(text, 60);

    // 动态自动命名：若当前标题为默认值（新会话/发布录屏），用用户第一句指令自动重命名标题
    var titleEl = $('#view-title');
    var curT = titleEl ? titleEl.textContent.trim() : '';
    if (curT === '新会话' || curT === '发布录屏到 B 站' || curT === '默认会话' || !curT) {
      var autoT = text.slice(0, 16).trim();
      if (text.length > 16) autoT += '...';
      if (titleEl) titleEl.textContent = autoT;
      (sessions || []).forEach(function (x) {
        if (x.id === s.sessionId) x.title = autoT;
      });
      renderSessions();
    }   // 任务卡标题跟随本回合请求
    appendUserBubble(text);

    busy = true;
    setState('thinking');
    var tip = appendAgentBubble('');
    var bubble = tip.querySelector('.bubble');
    bubble.innerHTML = '<span class="thinking" aria-label="正在生成"><i></i><i></i><i></i></span>';
    // /api/chat 同步阻塞（整个 turn 跑完才返回）；真实回复经 SSE message 到达，此处只挂占位
    api('/api/chat', { method: 'POST', body: { session_id: s.sessionId, content: text } })
      .then(function () {
        if (tip && tip.parentNode) tip.remove();   // 真实回复由 SSE message 渲染，移除 thinking 占位
        loadSessions();
        busy = false;
        deriveState();
        scrollStream();
      }, function (err) {
        if (err && err.data && err.data.message) {
          onServerMessage(err.data.message);   // 与 SSE 共用 seenMsg 去重：无论谁先到只渲染一次
          if (tip && tip.parentNode) tip.remove();
        } else {
          bubble.textContent = '请求失败：' + (err.message || err);   // 断网等本地失败：保留占位改为错误气泡
        }
        busy = false;
        deriveState();
        scrollStream();
      });
  }

  /* ---------- SSE 与快照（18 个命名事件，见 MIGRATION.md §4.2） ---------- */
  function connectSSE() {
    if (typeof EventSource !== 'function') return;   // jsdom / 无 SSE 环境跳过
    if (es) { try { es.close(); } catch (e) {} }
    var url = API + '/api/events' + (API_TOKEN ? '?token=' + encodeURIComponent(API_TOKEN) : '');
    es = new EventSource(url);

    es.onopen = function () { refreshSnapshot(); };

    es.addEventListener('model_failover', function (e) {
      try {
        var d = JSON.parse(e.data);
        var msg = '【自动故障切换】原模型 ' + (d.from_model || '') + ' ' + (d.summary || '额度已用尽') + '，已自动丝滑切换至 ' + (d.to_model || '备用模型') + ' 继续执行。';
        toast(msg);
        refreshSnapshot();
      } catch (err) {}
    });
    es.addEventListener('agent_state', function (e) { var d = JSON.parse(e.data); applyAgentState(d.state, d.detail, d.model, d.task_goal, d.session_id); });
    es.addEventListener('tool_call_started', function (e) { onToolCallStarted(JSON.parse(e.data)); });
    es.addEventListener('tool_call_done', function (e) { onToolCallDone(JSON.parse(e.data)); });
    es.addEventListener('subagent_update', function (e) { onSubagentUpdate(JSON.parse(e.data)); });
    es.addEventListener('message', function (e) { onServerMessage(JSON.parse(e.data)); });
    es.addEventListener('approval_request', function (e) { onApprovalRequest(JSON.parse(e.data)); });
    es.addEventListener('approval_resolved', function (e) { onApprovalResolved(JSON.parse(e.data)); });
    es.addEventListener('task_update', function (e) { onTaskUpdate(JSON.parse(e.data)); });
    es.addEventListener('token_usage', function (e) { onTokenUsage(JSON.parse(e.data)); });
    es.addEventListener('takeover_request', function (e) { onTakeoverRequest(JSON.parse(e.data)); });
    es.addEventListener('takeover_update', function (e) { onTakeoverUpdate(JSON.parse(e.data)); });
    es.addEventListener('plan_update', function (e) { onPlanUpdate(JSON.parse(e.data)); });
    es.addEventListener('task_progress', function (e) { onTaskProgress(JSON.parse(e.data)); });
    es.addEventListener('turn_notice', function (e) { onTurnNotice(JSON.parse(e.data)); });
    es.addEventListener('schedule_fire', function (e) { onScheduleFire(JSON.parse(e.data)); });
    es.addEventListener('model_switched', function (e) { onModelSwitched(JSON.parse(e.data)); });
    es.addEventListener('model_deleted', function (e) { onModelDeleted(JSON.parse(e.data)); });
    es.addEventListener('weixin_login_progress', function (e) { onWeixinProgress(JSON.parse(e.data)); });
    es.addEventListener('session_created', function (e) { onSessionActivity(JSON.parse(e.data), true); });
    es.addEventListener('session_updated', function (e) { onSessionActivity(JSON.parse(e.data), false); });

    es.onerror = function () {
      try { es.close(); } catch (e) {}
      es = null;
      setTimeout(connectSSE, 3000);
    };
  }

  function refreshSnapshot() {
    api('/api/state/snapshot').then(function (snap) {
      if (!snap) return;
      if (snap.agent_state) applyAgentState(snap.agent_state.state, snap.agent_state.detail, snap.agent_state.model, snap.agent_state.task_goal);
      if (Array.isArray(snap.activity)) {
        snap.activity.slice().reverse().forEach(function (it) { upsertActivity(it); });
        paintActivity();
        renderTasks();
      }
      if (Array.isArray(snap.approvals) && snap.approvals.length && !pendingApproval()) onApprovalRequest(snap.approvals[0]);
      if (snap.active_takeover) s.activeTakeover = snap.active_takeover;
      if (snap.status && snap.status.token_usage) onTokenUsage(snap.status.token_usage);
      if (snap.status && snap.status.current_model) {
        modelCatalog.current = snap.status.current_model;
        paintModelRefs();
        renderRoutes();
      }
      paintRunstrip();
    }).catch(function () {});
  }

  function upsertActivity(item) {
    if (!item || !item.id) return null;
    var prev = ACT[item.id] || {};
    var merged = {};
    Object.keys(prev).forEach(function (k) { merged[k] = prev[k]; });
    Object.keys(item).forEach(function (k) { if (item[k] !== null && item[k] !== undefined) merged[k] = item[k]; });
    ACT[item.id] = merged;
    if (item.status && item.status !== 'running' && !feedSeen[item.id]) {
      feedSeen[item.id] = true;
      addFeed(item.status === 'error' || item.status === 'cancelled' ? 'risk' : 'ok',
        (item.tool || '工具') + (item.summary ? ' · ' + item.summary : ''), item.result_summary || '');
    }
    return merged;
  }

  function paintActivity() {
    var host = $('#live-activity');
    if (!host) return;
    host.innerHTML = '';
    Object.keys(ACT).forEach(function (id) {
      var it = ACT[id];
      if (!it || it.status !== 'running') return;
      var row = document.createElement('div');
      row.className = 'tl__item';
      var body = document.createElement('div');
      body.className = 'tl__body';
      var h = document.createElement('h4');
      h.textContent = (it.tool || '工具') + (it.summary ? ' · ' + it.summary : '');
      var p = document.createElement('p');
      p.textContent = '执行中…';
      body.appendChild(h);
      body.appendChild(p);
      var gutter = document.createElement('div');
      gutter.className = 'tl__gutter';
      gutter.innerHTML = '<span class="tl__dot tl__dot--live"></span><span class="tl__line"></span>';
      row.appendChild(gutter);
      row.appendChild(body);
      host.appendChild(row);
    });
  }

  function onToolCallStarted(d) {
    if (d.session_id && s.sessionId && d.session_id !== s.sessionId) return;
    upsertActivity({ id: d.id, tool: d.tool, summary: d.summary, args: d.args, status: 'running', started_at: clock() });
    paintActivity();
    renderTasks();   // 活卡进度随步骤比例推进
    // 不再逐条 addFeed：工具调用在活卡的总览弹窗里看步骤明细（大局观，防刷屏）
  }

  function onToolCallDone(d) {
    if (d.session_id && s.sessionId && d.session_id !== s.sessionId) return;
    upsertActivity({ id: d.id, status: d.status, duration_ms: d.duration_ms, result_summary: d.result_summary, result: d.result });
    paintActivity();
    renderTasks();   // 完成的活动即时落为动态列表卡片
  }

  function onSubagentUpdate(d) {
    if (d && d.session_id && s.sessionId && d.session_id !== s.sessionId) return;
    if (d && d.queue_waiting !== undefined) { paintRunstrip(); return; }   // 排队数载荷（server.py:474）
    var merged = upsertActivity({
      id: d && d.id, tool: 'subagent',
      summary: d && d.brief ? '子任务: ' + d.brief : '子任务',
      status: (d && d.status) || 'running',
      result_summary: (d && d.result_summary) || ''
    });
    paintActivity();
    if (merged && merged.status && merged.status !== 'running') renderTasks();
  }

  function onTokenUsage(d) {
    if (!d) return;
    s.tokenUsage = d;
    var tk = $('#runstrip-tokens');
    if (tk) tk.textContent = '本次运行 ' + fmtTok((d.input || 0) + (d.output || 0)) + ' tok';
    var usageView = $('#view-usage');
    if (usageView && usageView.style.display === 'block') renderUsage();
    paintOverviewIfOpen(null);
  }

  function onPlanUpdate(d) { /* 计划步骤暂无独立 UI；快照已带回 */ }

  function onTaskProgress(d) {
    if (d && d.text) { s.detail = d.text; paintRunstrip(); }
  }

  function onTurnNotice(d) {
    // server 同时会把它转成 message(assistant_notice) 渲染气泡；这里只更新运行条，避免双份
    if (d && d.text) { s.detail = d.text; paintRunstrip(); }
  }

  function onScheduleFire(d) {
    addFeed('live', '定时任务触发', (d && d.brief) || '');
    toast('定时任务触发：' + ((d && d.brief) || ''));
  }

  function onModelSwitched(d) {
    if (!d) return;
    var role = d.target_role || 'main';

    if (role === 'fallback') {
      // 备用链更新：同步数据模型，不破坏主模型指针，不弹通用提示（本地操作已有针对性提示）
      if (d.fallback_chain && Array.isArray(d.fallback_chain)) {
        modelCatalog.fallback_chain = d.fallback_chain;
      }
      if (modelCatalog.roles && modelCatalog.roles.fallback && d.model) {
        modelCatalog.roles.fallback.model = d.model;
      }
      return;
    }

    if (role === 'main' || role === 'all') {
      if (d.model) {
        modelCatalog.current = d.model;
        if (modelCatalog.roles && modelCatalog.roles.main) {
          modelCatalog.roles.main.model = d.model;
        }
        paintModelRefs();
      }
      // 本地短时间内已主动触发过切换提示的，不重复弹框
      var isLocalRecent = (lastLocalSwitchedModel === d.model && (Date.now() - lastLocalSwitchTime) < 2500);
      if (!isLocalRecent) {
        toast(d.model ? '模型已切换：' + d.model : '模型已切换');
      }
      renderRoutes();
      return;
    }

    // 其他特定子角色
    if (modelCatalog.roles && modelCatalog.roles[role] && d.model) {
      modelCatalog.roles[role].model = d.model;
    }
    paintModelRefs();
    renderRoutes();
  }

  function onModelDeleted(d) {
    toast('模型已删除' + (d && d.fallback_model ? '，已回退到 ' + d.fallback_model : ''));
    loadModels();
  }

  function onWeixinProgress(d) {
    var st = (d && d.status) || '';
    var badge = $('#weixin-badge');
    if (st === 'scaned') {
      if (badge) badge.textContent = '◔ 已扫码，请在手机确认';
      return;
    }
    if (st === 'confirmed') {
      loadWeixinStatus();
      toast('微信绑定成功：' + ((d && d.user_id) || '已连接'));
      return;
    }
    if (st === 'expired') {
      if (badge) badge.textContent = '⚠ 二维码已过期';
      toast('微信二维码已过期，请重新生成');
    }
  }

  var takeoverTimer = 0;

  function stopTakeoverTimer() {
    if (takeoverTimer) { clearInterval(takeoverTimer); takeoverTimer = 0; }
  }

  function onTakeoverRequest(d) {
    if (!d || !d.task_id) return;
    s.activeTakeover = d;
    var m = $('#modal-takeover');
    if (m) {
      var tgt = $('#takeover-target');
      if (tgt) tgt.textContent = d.target || '浏览器';
      var rsn = $('#takeover-reason');
      if (rsn) rsn.textContent = (d.reason || '需要人工操作') + (d.page_url ? ' · ' + d.page_url : '');
      openModal('modal-takeover');
    }
    stopTakeoverTimer();
    var start = Date.now();
    if (typeof d.requested_at === 'number') start = d.requested_at * 1000;
    else if (d.requested_at) { var p = Date.parse(d.requested_at); if (!isNaN(p)) start = p; }
    takeoverTimer = setInterval(function () {
      var w = $('#takeover-wait');
      if (w) w.textContent = mmss(Math.max(0, Math.round((Date.now() - start) / 1000)));
    }, 1000);
    addFeed('warn', '请求人工接管 · ' + (d.target || ''), d.reason || '');
    deriveState();
    toast('需要你接管：' + (d.target || '浏览器'));
  }

  function onTakeoverUpdate(d) {
    var wasActive = s.activeTakeover && (!d || !d.task_id || d.task_id === s.activeTakeover.task_id);
    if (!wasActive) return;
    s.activeTakeover = null;
    stopTakeoverTimer();
    closeModal($('#modal-takeover'));
    addFeed('ok', '接管结束 · ' + ((d && d.status) || ''), (d && d.summary) || '');
    toast((d && d.status) === 'completed' ? '接管完成，任务继续' : '接管结束：' + ((d && d.status) || ''));
    deriveState();
  }

  /* ---------- 会话活动事件（session_created / session_updated） ---------- */
  function onSessionActivity(d, created) {
    if (!d || !d.id) return;
    // 更新本地会话列表：已有则刷新元数据，没有则插入到顶部
    var found = false;
    sessions.forEach(function (se) {
      if (se.id === d.id) {
        found = true;
        if (d.title) se.title = d.title;
        if (d.source) se.source = d.source;
        if (d.last_ts) se.last_ts = d.last_ts;
        se.updated_at = formatTs(d.last_ts || se.last_ts);
      }
    });
    if (!found) {
      sessions.unshift({
        id: d.id,
        title: d.title || '会话',
        source: d.source || 'desktop',
        last_ts: d.last_ts || Date.now(),
        updated_at: formatTs(d.last_ts || Date.now()),
        message_count: 0
      });
    }
    // 非当前会话 → 标未读
    if (d.id !== s.sessionId) {
      sessionUnread[d.id] = (sessionUnread[d.id] || 0) + 1;
    }
    renderSessions();
  }

  function formatTs(ms) {
    if (!ms) return '';
    var d = new Date(ms);
    var pad = function (n) { return n < 10 ? '0' + n : '' + n; };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  /* ---------- 服务器消息渲染（message 事件，§4.6） ---------- */
  function onServerMessage(d) {
    if (!d) return;
    if (d.session_id && s.sessionId && d.session_id !== s.sessionId) {
      // 非当前会话的消息：标未读，不渲染到当前聊天流，避免串台
      sessionUnread[d.session_id] = (sessionUnread[d.session_id] || 0) + 1;
      renderSessions();
      return;
    }
    if (d.id) { if (seenMsg[d.id]) return; seenMsg[d.id] = true; }
    if (d.type === 'tool_step') {
      (d.tool_calls || []).forEach(function (tc) {
        addFeed(tc.status === 'error' ? 'risk' : 'live', (tc.tool || '工具') + (tc.summary ? ' · ' + tc.summary : ''), tc.result_summary || '');
      });
      return;
    }
    if (d.type === 'takeover_request') return;   // 由 takeover_request 事件处理，避免双开
    if (d.type === 'assistant_notice') {
      appendAgentBubble(d.content || '', d.id);
      return;
    }
    if (d.role === 'user') {
      var uid = d.session_id || s.sessionId;
      var ugoal = d.task_goal || d.content || '';
      lastGoalBySession[uid] = ugoal;
      turnGoals[uid] = firstLine(ugoal, 60);
      appendUserBubble(d.content || '', d.id);
      renderTasks();   // 新回合请求到达：活卡标题跟随
      return;
    }
    var b = appendAgentBubble(d.content || '', d.id);
    // 按 task_origin_id 归组：同一任务的多个回合合并到一张卡
    var map = {};
    turnCards.forEach(function (c) { map[c.id] = c; });
    var order = turnCards.map(function (c) { return c.id; });
    mergeTurnCard(map, order, d, lastGoalBySession[d.session_id || s.sessionId]);
    var newKey = (d.task_origin_id ? 'task-' + d.task_origin_id : null) || 'turn-' + d.id;
    if (map[newKey]) {
      turnCards = [map[newKey]].concat(turnCards.filter(function (c) { return c.id !== newKey; }));
      renderTasks();
    }
    scrollStream();
    return b;
  }

  /* ---------- 状态机映射（后端 11 态 → 视觉 5 态，§4.3） ---------- */
  var STATE_FROM_SERVER = {
    idle: 'idle',
    thinking: 'thinking',
    acting: 'acting',
    waiting_subagent: 'acting',
    awaiting_approval: 'awaiting',
    paused_takeover: 'awaiting',
    waiting_user: 'awaiting',
    done: 'done',
    cancelled: 'idle',
    incomplete: 'idle',
    error: 'idle'
  };
  var DONE_HOLD_MS = 3000;

  function applyAgentState(state, detail, model, goal, sessionId) {
    // 跨会话隔离：微信会话的状态不劫持桌面状态栏，但任务标题仍按会话记录
    if (sessionId && sessionId !== s.sessionId) {
      if (goal) turnGoals[sessionId] = firstLine(goal, 60);
      return;
    }
    var next = STATE_FROM_SERVER[state] || 'idle';
    s.rawState = state;
    s.state = next;
    s.detail = detail || '';
    s.model = model || s.model;
    s.error = state === 'error' ? (detail || '执行出错') : '';
    if (goal) turnGoals[s.sessionId] = firstLine(goal, 60);   // 后端解析后的任务书优先于续跑指令原文
    if (state === 'cancelled') {
      s.stopping = false;
      clearTimeout(s.stopTimer);
      addFeed('warn', '已停止', '子任务已回收，可重新发送指令');
    }
    if (next === 'done') {
      clearTimeout(s.doneTimer);
      s.doneTimer = setTimeout(function () {
        if (s.state === 'done') { s.state = 'idle'; paintAvatar(); paintRunstrip(); }
      }, DONE_HOLD_MS);
    }
    paintAvatar();
    paintRunstrip();
    renderTasks();   // 活卡随状态机出现/消失（acting→run、done→历史）
  }

  /* ---------- 输入框 ---------- */
  function autosize() {
    var input = $('#composer-input');
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = Math.min(156, input.scrollHeight) + 'px';
  }

  function syncSend() {
    var input = $('#composer-input');
    var btn = $('#btn-send');
    if (!input || !btn) return;
    var has = input.value.trim().length > 0;
    btn.disabled = !has;
    var icon = $('#send-icon');
    if (icon) {
      icon.innerHTML = has
        ? '<path stroke-linecap="round" stroke-linejoin="round" d="M12 19V5M6 11l6-6 6 6"/>'
        : '<path stroke-linecap="round" d="M12 5v14"/>';
    }
  }

  /* ---------- 抽屉 / Tab / 视图 ---------- */
  function applyDrawer() {
    var drawer = $('#drawer');
    if (!drawer) return;
    drawer.classList.toggle('is-hidden', !s.drawerOpen);
    // ≤960px 时抽屉变 overlay（媒体查询），显隐由 is-open 控制位移；桌面态无此规则，无害
    drawer.classList.toggle('is-open', s.drawerOpen);
    // 窄屏 overlay 模式的关闭钮：仅 ≤960px 且展开时显示（桌面栅格态由顶栏按钮负责）
    var dclose = $('#btn-drawer-close');
    if (dclose) {
      var overlayMode = window.matchMedia && window.matchMedia('(max-width:960px)').matches;
      dclose.classList.toggle('is-hidden', !(s.drawerOpen && overlayMode));
    }
    var btn = $('#btn-drawer');
    if (btn) {
      btn.classList.toggle('is-on', s.drawerOpen);
      btn.setAttribute('data-tip', s.drawerOpen ? '收起右栏' : '展开右栏');
    }
    var main = $('.main');
    if (main) main.classList.toggle('is-lone', !s.drawerOpen);
    paintRunstrip();
  }

  /* ---------- 顶部运行状态条 ---------- */
  function runDetail() {
    if (s.stopping) return '正在停止 · 子任务回收中';
    if (busy) return s.detail || '正在组织回复，随时可切换会话';
    if (pendingApproval()) return (approvalState.action_cn || approvalState.action || '操作') + ' · 剩余 ' + mmss(approvalRemainingSec()) + ' 自动拒绝';
    if (s.detail) return s.detail;
    return '本机会话 · 已自动保存';
  }

  function paintRunstrip() {
    var strip = $('#runstrip');
    if (!strip) return;
    var state = s.state;
    if (busy) state = 'thinking';
    if (pendingApproval() || activeTakeover()) state = 'awaiting';
    strip.setAttribute('data-state', state);

    var label = strip.querySelector('#runstrip-state');
    if (label) label.textContent = stateText();

    var detail = $('#runstrip-detail');
    if (detail) detail.textContent = runDetail();

    var link = $('.runlink');
    if (link) link.hidden = true;   // 演示上传的进度链接，真实任务进度走 #runstrip-progress

    var prog = $('#runstrip-progress');
    if (prog) prog.hidden = trackerPct === null;
    if (trackerPct !== null) {
      var bar = $('#runstrip-bar');
      if (bar) bar.style.width = trackerPct + '%';
      var pctEl = $('#runstrip-pct');
      if (pctEl) pctEl.textContent = trackerPct + '%';
    }

    var go = $('#runstrip-go');
    if (go) go.hidden = !(state === 'awaiting');

    var stopBtn = $('#runstrip-stop');
    if (stopBtn) {
      var canStop = !s.stopping && (state === 'thinking' || state === 'acting' || state === 'awaiting');
      stopBtn.hidden = !canStop;
      stopBtn.disabled = !!s.stopping;
      stopBtn.textContent = s.stopping ? '停止中…' : '停止';
    }
  }

  /* ---------- 停止链路（真实：POST /api/stop，v3.4；无 resume 接口） ---------- */
  function stopTask(origin) {
    if (s.stopping) return;
    if (typeof fetch !== 'function') return;
    s.stopping = true;
    // 不要把 s.state 直接改成 idle——真正的收敛只认 SSE 的 agent_state{state:'cancelled'}
    s.detail = '正在停止 · 子任务回收中';
    paintRunstrip();

    api('/api/stop', { method: 'POST' })
      .then(function () {
        toast('已发送停止指令，正在收尾');
        clearTimeout(s.stopTimer);
        s.stopTimer = setTimeout(function () {
          if (s.stopping) {
            s.stopping = false;
            s.detail = '停止指令未确认';
            paintRunstrip();
          }
        }, 15000);
      })
      .catch(function (err) {
        s.stopping = false;
        s.detail = '';
        toast('停止失败：' + (err.message || err));
        paintRunstrip();
      });
  }

  function switchTab(name) {
    s.tab = name;
    save();
    if (name === 'feed') renderTasks();
    if (name === 'review') { renderReview(); loadApprovalHistory(); }
    $$('.segmented [role="tab"]').forEach(function (b) {
      b.setAttribute('aria-selected', String(b.dataset.tab === name));
    });
    $$('.panel').forEach(function (p) {
      p.classList.toggle('is-on', p.dataset.panel === name);
    });
  }

  var VIEW_TITLES = {
    chat: '新会话',
    ideas: '灵感与提示词',
    goals: '目标与任务',
    library: '资源与技能',
    usage: '用量看板'
  };

  var VIEW_TITLES_HTML = {
    ideas: '灵感与提示词',
    goals: '目标与任务',
    library: '资源与技能',
    usage: '用量看板'
  };

  function viewHTML(view) {
    if (view === 'ideas') {
      return '<div class="altview" data-od-id="view-ideas">' +
        '<h2>灵感与提示词 <span class="badge" style="font-size:12px;font-weight:normal;padding:2px 8px;border-radius:10px;background:oklch(0.2 0 0 / .05);color:var(--text-muted);margin-left:8px">● 提纯模板</span></h2>' +
        '<p>提炼沉淀的高频指令模板，点击单条可直接填入输入框快速触发。</p>' +
        '<div class="altgrid">' +
        card('新建', '新建文件与Markdown', '新建指定目录文件，按 md 格式规范排版后落盘。', '提纯模板 · 3 日沉淀', '新建一个文件夹，新建txt文件测试但要求md格式的内容。然后保存') +
        card('浏览', '网页元素抓取', '打开目标网页并提取关键结构化文本或截图。', '提纯模板 · 自动化', '在网页中抓取关键信息并保存到本地。') +
        card('对比', '多文档差异对照', '对比文档差异，核心结论置顶并附引用原文。', '提纯模板 · 结构化', '对比两份文档的差异，把结论放最前，原文引用附在后面。') +
        card('定时', '每周五自动周报', '从本周提交与未完成项自动生成规范工作周报。', '提纯模板 · 已就绪', '生成本周工作周报，从本周执行的未完成项和提交项提取重点。') +
        '</div>' +
        '<div style="margin-top:28px">' +
        '<div style="font-size:13px;font-weight:600;color:var(--text-normal);margin-bottom:10px;">⚡ 常用指令速查</div>' +
        row('扫描目录重复的大文件并列出（只读不删除）', '只读 · 文件分析', '扫描目录重复的大文件并列出，只读不删除', '填入') +
        row('将会议记录转换为结构化要点与待办清单', '多模态转写 · 结构化抽取', '将会议记录转换为结构化要点与待办清单', '填入') +
        row('检查系统空间占用并在表格中列出前 5 大目录', '运维 · 确认只读', '检查系统空间占用并在表格中列出前 5 大目录', '填入') +
        row('从当前提交记录提取本周变更摘要', 'Git · 增量对比', '从当前提交记录提取本周变更摘要', '填入') +
        '</div></div>';
    }
    if (view === 'goals') {
      return '<div class="altview" data-od-id="view-goals">' +
        '<h2>目标与任务 <span class="badge" style="font-size:12px;font-weight:normal;padding:2px 8px;border-radius:10px;background:oklch(0.2 0 0 / .05);color:var(--text-muted);margin-left:8px">● 常驻定时</span></h2>' +
        '<p>展示当前系统常驻后台的 APScheduler 定时调度任务看板，自动周期执行与自愈，不可逆动作始终安全优先。</p>' +
        '<div class="altgrid">' +
        card('常驻定时', '每小时轻量整理', '任务ID: mem_hourly · 每 1 小时自动将最新工作记忆提炼入长期存储。', '调度 · 每 1 小时') +
        card('常驻定时', '每晚深度记忆蒸馏', '任务ID: mem_nightly · 每天 02:00 提取核心偏好与重要事实，清理冗余临时记忆。', '定时 · 每天 02:00') +
        card('常驻定时', '每晚经验置信度衰减与归档', '任务ID: exp_decay · 每天 02:10 自动按半衰期衰减老旧经验置信度，归档被推翻法则。', '定时 · 每天 02:10') +
        card('常驻定时', '每晚技能评审与轨迹蒸馏', '任务ID: skill_review · 每天 02:30 审查高频成功执行轨迹，自动抽象提炼标准技能包。', '定时 · 每天 02:30') +
        '</div></div>';
    }
    return '<div class="altview" data-od-id="view-library">' +
      '<h2>资源与技能 <span class="badge" style="font-size:12px;font-weight:normal;padding:2px 8px;border-radius:10px;background:oklch(0.2 0 0 / .05);color:var(--text-muted);margin-left:8px">● 提纯知识</span></h2>' +
      '<p>展示本地提炼的核心技能包、已验证经验法则与安全防御机制。</p>' +
      '<div class="altgrid">' +
      card('精炼技能包', '浏览器自动化 (browser.md)', '35.8 KB 精炼技能定义：网页点击、表单填写、DOM 结构化提取与登录态接管，规避整页盲扫。', '核心技能 · 已就绪') +
      card('精炼技能包', 'Windows 桌面交互 (desktop.md)', '5.6 KB 精炼技能定义：UIA Pattern 底层精准定位与键盘鼠标模拟交互，异步防抖重试。', '核心技能 · 已就绪') +
      card('安全机制', '本地文件系统与命令护栏', '强安全边界：写入前自动 .bak 备份，不可逆删除与外部执行强制触发 L3 人工审批。', 'fail closed 防护') +
      card('认知模型', '自演化经验法则与长期记忆', '沉淀经过验证的经验法则 127 条，MEMORY.md 提纯核心准则 57,268 字，终身自愈不遗忘。', '核心知识库') +
      '</div>' +
      '<div style="margin-top:28px;">' +
      '<div style="font-size:13px;font-weight:600;color:var(--text-normal);margin-bottom:10px;display:flex;align-items:center;gap:6px;"><span>📚 自演化提纯法则库</span><span class="badge" style="font-size:11px;font-weight:normal;padding:1px 6px;border-radius:8px;background:oklch(0.2 0 0 / .05);color:var(--text-muted);">高置信度 · 已验证</span></div>' +
      row('[提纯法则] 浏览器自动化优先尝试精准选择器，避免大面积全文盲扫', '经验法则 · 置信度 0.90 · 自演化沉淀', null, '已验证') +
      row('[提纯法则] 桌面窗口未就绪时等待 1-2 秒防抖重试，避免提前抛出异常', '经验法则 · 置信度 0.85 · 自演化沉淀', null, '已验证') +
      row('[提纯法则] 文件操作前备份关键状态，遇中断自动安全回滚', '经验法则 · 置信度 0.88 · 自演化沉淀', null, '已验证') +
      row('[提纯法则] 复杂子任务验收标准收窄到单一字段，规避长步数超时', '经验法则 · 置信度 0.90 · 自演化沉淀', null, '已验证') +
      '</div></div>';
  }

  function esc(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function card(kind, title, desc, meta, prompt) {
    var pAttr = prompt ? ' data-prompt="' + esc(prompt) + '"' : '';
    return '<button class="altcard"' + pAttr + '><h3>' + esc(title) + '</h3><p>' + esc(desc) + '</p><span class="meta">' + esc(kind) + (meta ? ' · ' + esc(meta) : '') + '</span></button>';
  }

  function row(title, meta, prompt, actionText) {
    var pAttr = prompt ? ' data-prompt="' + esc(prompt) + '"' : '';
    var badge = actionText !== undefined ? actionText : (prompt ? '填入' : '');
    var badgeHtml = badge ? '<span class="meta" style="display:inline-block;padding:2px 8px;border-radius:6px;background:oklch(0.2 0 0 / .04);font-size:11px;">' + esc(badge) + '</span>' : '';
    return '<button class="altrow"' + pAttr + '><div><div class="altrow__t">' + esc(title) + '</div><div class="altrow__s">' + esc(meta) + '</div></div>' + badgeHtml + '</button>';
  }

  function bindAltClicks(container) {
    $$('.altcard, .altrow', container).forEach(function (b) {
      b.addEventListener('click', function () {
        var promptText = b.dataset.prompt;
        if (promptText) {
          switchView('chat');
          var input = $('#composer-input');
          if (input) {
            input.value = promptText;
            autosize();
            syncSend();
            input.focus();
            toast('提示词已填入，可直接回车发送');
          }
          return;
        }
        var t = b.querySelector('h3, .altrow__t');
        var title = t ? t.textContent.trim() : '';
        if (title) {
          toast(title);
        }
      });
    });
  }

  function renderDynamicView(view, other) {
    other.innerHTML = viewHTML(view);
    bindAltClicks(other);

    var apiUrl = '';
    if (view === 'ideas') apiUrl = '/api/ideas';
    else if (view === 'goals') apiUrl = '/api/goals';
    else if (view === 'library') apiUrl = '/api/library';
    if (!apiUrl) return;

    api(apiUrl)
      .then(function (data) {
        if (!data || data.status !== 'ok') return;
        var curBtn = $('.rail__btn.is-active');
        if (curBtn && curBtn.dataset.nav !== view) return;

        var html = '';
        if (view === 'ideas') {
          html = '<div class="altview" data-od-id="view-ideas">' +
            '<h2>灵感与提示词 <span class="badge" style="font-size:12px;font-weight:normal;padding:2px 8px;border-radius:10px;background:oklch(0.2 0 0 / .05);color:var(--text-muted);margin-left:8px">● 提纯模板</span></h2>' +
            '<p>提炼沉淀的高频指令模板，点击单条可直接填入输入框快速触发。</p>' +
            '<div class="altgrid">';
          (data.cards || []).forEach(function (c) {
            html += card(c.kind, c.title, c.desc, c.meta, c.prompt);
          });
          html += '</div>';
          if (data.rows && data.rows.length) {
            html += '<div style="margin-top:28px;"><div style="font-size:13px;font-weight:600;color:var(--text-normal);margin-bottom:10px;">⚡ 常用指令速查</div>';
            data.rows.forEach(function (r) {
              html += row(r.title, r.meta, r.prompt, r.action || '填入');
            });
            html += '</div>';
          }
          html += '</div><p class="meta" style="padding:6px 24px 18px">标准提纯指令 · 点击自动填入可直接回车执行</p>';
        } else if (view === 'goals') {
          html = '<div class="altview" data-od-id="view-goals">' +
            '<h2>目标与任务 <span class="badge" style="font-size:12px;font-weight:normal;padding:2px 8px;border-radius:10px;background:oklch(0.2 0 0 / .05);color:var(--text-muted);margin-left:8px">● 常驻定时</span></h2>' +
            '<p>展示当前系统常驻后台的 APScheduler 定时调度任务看板，自动周期执行与自愈，不可逆动作始终安全优先。</p>' +
            '<div class="altgrid">';
          (data.cards || []).forEach(function (c) {
            html += card(c.kind, c.title, c.desc, c.meta);
          });
          html += '</div>';
          if (data.rows && data.rows.length) {
            html += '<div style="margin-top:26px"><div style="font-size:13px;font-weight:600;color:var(--text-normal);margin-bottom:10px;">📋 调度明细</div>';
            data.rows.forEach(function (r) {
              html += row(r.title, r.meta, null, r.action || '已就绪');
            });
            html += '</div>';
          }
          html += '</div><p class="meta" style="padding:6px 24px 18px">共 ' + (data.scheduled_count || (data.cards || []).length) + ' 个常驻定时调度任务正在运行中 · 周期触发</p>';
        } else if (view === 'library') {
          html = '<div class="altview" data-od-id="view-library">' +
            '<h2>资源与技能 <span class="badge" style="font-size:12px;font-weight:normal;padding:2px 8px;border-radius:10px;background:oklch(0.2 0 0 / .05);color:var(--text-muted);margin-left:8px">● 提纯知识</span></h2>' +
            '<p>展示本地提炼的核心技能包、已验证经验法则与安全防御机制。</p>' +
            '<div class="altgrid">';
          (data.cards || []).forEach(function (c) {
            html += card(c.kind, c.title, c.desc, c.meta);
          });
          html += '</div>';
          if (data.rows && data.rows.length) {
            html += '<div style="margin-top:28px;"><div style="font-size:13px;font-weight:600;color:var(--text-normal);margin-bottom:10px;display:flex;align-items:center;gap:6px;"><span>📚 自演化提纯法则库</span><span class="badge" style="font-size:11px;font-weight:normal;padding:1px 6px;border-radius:8px;background:oklch(0.2 0 0 / .05);color:var(--text-muted);">高置信度 · 已验证</span></div>';
            data.rows.forEach(function (r) {
              html += row(r.title, r.meta, null, r.action || '已验证');
            });
            html += '</div>';
          }
          var memStats = data.memory_stats || {};
          html += '</div><p class="meta" style="padding:6px 24px 18px">提纯经验法则 ' + (memStats.experience_count || 0) + ' 条 · 已载入工具 ' + (data.total_tools || 0) + ' 个 · 长期记忆准则 ' + (memStats.memory_chars || 0) + ' 字</p>';
        }

        other.innerHTML = html;
        bindAltClicks(other);
      })
      .catch(function (err) {
        console.warn('获取实时视图数据异常:', err);
      });
  }

  function switchView(view) {
    $$('.rail__btn[data-nav]').forEach(function (b) {
      b.classList.toggle('is-active', b.dataset.nav === view);
    });
    var chat = $('#view-chat');
    var other = $('#view-other');
    var usage = $('#view-usage');
    var title = $('#view-title');
    if (usage) usage.style.display = 'none';
    if (view === 'chat') {
      chat.style.display = '';
      other.style.display = 'none';
      other.innerHTML = '';
      if (title) {
        var curSe = null;
        (sessions || []).forEach(function (x) { if (x.id === s.sessionId) curSe = x; });
        title.textContent = (curSe && curSe.title) ? curSe.title : (VIEW_TITLES.chat || '新会话');
      }
    } else if (view === 'usage') {
      chat.style.display = 'none';
      other.style.display = 'none';
      other.innerHTML = '';
      if (usage) usage.style.display = 'block';
      if (title) title.textContent = VIEW_TITLES.usage;
      renderUsage();
    } else {
      chat.style.display = 'none';
      other.style.display = 'block';
      if (title) title.textContent = VIEW_TITLES[view];
      renderDynamicView(view, other);
    }
    var composer = $('#composer');
    if (composer) composer.style.display = '';
  }

  /* ---------- 会话（真实：/api/sessions + /messages） ---------- */
  function toggleSessions(force) {
    var el = $('#sessions');
    if (!el) return;
    var open = typeof force === 'boolean' ? force : !el.classList.contains('is-open');
    el.classList.toggle('is-open', open);
    el.setAttribute('aria-hidden', String(!open));
    var btn = $('#btn-sessions');
    if (btn) btn.classList.toggle('is-active', open);
  }

  function loadSessions() {
    api('/api/sessions').then(function (list) {
      var old = sessions;
      sessions = Array.isArray(list) ? list : [];
      // 保留已有的未读计数（列表刷新不丢状态）
      var unreadSnapshot = {};
      old.forEach(function (se) { unreadSnapshot[se.id] = sessionUnread[se.id] || 0; });
      sessions.forEach(function (se) {
        if (unreadSnapshot[se.id]) sessionUnread[se.id] = unreadSnapshot[se.id];
      });
      if (!s.sessionId && sessions.length) {
        switchSession(sessions[0].id, true);
      } else if (s.sessionId && sessions.length) {
        var curSe = null;
        sessions.forEach(function (x) { if (x.id === s.sessionId) curSe = x; });
        var titleEl = $('#view-title');
        if (titleEl && curSe && curSe.title) titleEl.textContent = curSe.title;
      }
      renderSessions();
    }).catch(function () {});
  }

  function renderSessions() {
    var list = $('.sessions__list');
    if (!list) return;
    list.innerHTML = '';
    sessions.forEach(function (se) {
      var b = document.createElement('button');
      b.className = 'sess' + (se.id === s.sessionId ? ' is-on' : '');
      if (se.id === s.sessionId) b.setAttribute('aria-current', 'true');
      b.setAttribute('data-session', se.id);
      b.innerHTML = '<span class="sess__t"></span><span class="meta"></span>';
      var titleEl = b.querySelector('.sess__t');
      // 微信会话打绿色标签，一眼可辨
      if (se.source === 'weixin') {
        var tag = document.createElement('span');
        tag.className = 'sess__wx';
        tag.textContent = '微信';
        titleEl.appendChild(tag);
      }
      titleEl.appendChild(document.createTextNode(se.title || '会话'));
      var metaEl = b.querySelector('.meta');
      metaEl.textContent = se.updated_at || '';
      // 未读红点：非当前会话且有未读消息
      var unread = sessionUnread[se.id] || 0;
      if (unread && se.id !== s.sessionId) {
        var badge = document.createElement('span');
        badge.className = 'sess__unread';
        badge.textContent = unread > 99 ? '99+' : unread;
        metaEl.appendChild(badge);
      }
      b.addEventListener('click', function () {
        switchSession(se.id);
        toggleSessions(false);
      });
      list.appendChild(b);
    });
  }

  function switchSession(id, silent) {
    s.sessionId = id;
    sessionUnread[id] = 0;   // 切换即清未读
    renderSessions();
    paintResumeBar();
    var se = null;
    sessions.forEach(function (x) { if (x.id === id) se = x; });
    var title = $('#view-title');
    switchView('chat');
    if (title && se) title.textContent = se.title;
    if (!silent) toast('已切换到：' + (se ? se.title : '会话'));
    var stream = $('#view-chat');
    if (stream) $$('.msg', stream).forEach(function (m) { m.remove(); });   // 保留 #after-approval 锚点
    loadMessages(id);
  }

  function turnCardFromMessage(m, userText) {
    if (!m || m.type !== 'assistant_final' || m.role === 'user') return null;
    if (!m.content && !(m.tool_calls || []).length) return null;
    return {
      id: 'turn-' + m.id, kind: '回合',
      steps: (m.tool_calls || []).map(function (tc) {
        return {
          step_id: tc.tool || 'step',
          name: (tc.tool || '工具') + (tc.summary ? ' · ' + tc.summary : ''),
          status: tc.status === 'error' ? 'cancelled' : 'done',
          started_at: '', ended_at: m.created_at || '',
          detail: tc.result_summary || ''
        };
      }),
      status: 'done', pct: 100,
      goal: firstLine(userText || m.content),   // 标题=触发本回合的用户请求；缺失时退回回复内容
      meta: m.created_at || ''
    };
  }

  function loadMessages(sid) {
    api('/api/sessions/' + encodeURIComponent(sid) + '/messages').then(function (rows) {
      var list = Array.isArray(rows) ? rows : [];
      sessionMsgCount[sid] = list.length;   // 记录消息数，用于判断是否空会话
      var map = {};
      var order = [];
      var lastUserText = '';
      list.forEach(function (m) {
        if (m.type === 'tool_step') return;   // 过程只在卡片详情内
        if (m.role === 'user') {
          lastUserText = m.task_goal || m.content || '';
          lastGoalBySession[sid] = lastUserText;
          appendUserBubble(m.content || '', m.id);
          return;
        }
        mergeTurnCard(map, order, m, lastUserText);
        if (m.type === 'assistant_final') appendAgentBubble(m.content || '', m.id);
        else if (m.type === 'assistant_notice') appendAgentBubble(m.content || '', m.id);
      });
      turnCards = order.slice().reverse().map(function (k) { return map[k]; });
      if (lastGoalBySession[sid]) turnGoals[sid] = firstLine(lastGoalBySession[sid], 60);
      renderTasks();
      scrollStream();
    }).catch(function () {});
  }

  /* ---------- Modal ---------- */
  var lastFocus = null;

  function openModal(id) {
    var m = document.getElementById(id);
    if (!m) return;
    lastFocus = document.activeElement;
    m.classList.remove('is-hidden');
    var focusable = m.querySelector('input, textarea, button:not([data-close])');
    if (focusable) setTimeout(function () { focusable.focus(); }, 30);
  }

  function closeModal(el) {
    if (!el) return;
    if (el.id === 'modal-settings' && typeof flushFallbackChainSync === 'function') {
      flushFallbackChainSync();
    }
    el.classList.add('is-hidden');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function closeAllModals() {
    $$('.modal').forEach(function (m) { m.classList.add('is-hidden'); });
  }

  /* ---------- 搜索 ---------- */
  function renderSearch(q) {
    var host = $('#search-results');
    if (!host) return;
    var list = $$('.sess__t').map(function (el) { return el.textContent; });
    var extra = [];
    var pool = list.map(function (t) { return { t: t, kind: '会话' }; })
      .concat(extra.map(function (t) { return { t: t, kind: '消息' }; }));
    var hits = q
      ? pool.filter(function (x) { return x.t.toLowerCase().indexOf(q.toLowerCase()) > -1; })
      : pool.slice(0, 5);
    if (!hits.length) {
      host.innerHTML = '<div class="queueitem__empty">没有匹配的会话或消息。</div>';
      return;
    }
    host.innerHTML = '';
    hits.forEach(function (hit) {
      var b = document.createElement('button');
      b.className = 'sess';
      b.style.width = '100%';
      b.innerHTML = '<span class="sess__t"></span><span class="meta">' + hit.kind + '</span>';
      b.querySelector('.sess__t').textContent = hit.t;
      b.addEventListener('click', function () {
        var title = $('#view-title');
        switchView('chat');
        if (hit.kind === '会话' && title) title.textContent = hit.t;
        closeAllModals();
        toast('已定位：' + hit.t);
      });
      host.appendChild(b);
    });
  }

  /* ---------- 绑定 ---------- */
  function bind() {
    approvalCard = $('#approval-card');
    approveActions = $('#approve-actions');
    approveTtl = $('#approve-ttl');
    approveResultText = $('#approve-result-text');

    $$('.winctl button').forEach(function (b) {
      b.addEventListener('click', function () {
        var act = b.getAttribute('data-win');
        handleWindowControl(act || (b.classList.contains('close') ? 'close' : 'minimize'));
      });
    });

    if (!isDesktop()) $$('.winctl button').forEach(function (b) { b.hidden = true; });

    $$('.winctl').forEach(function (box) {
      box.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    });

    var titlebar = $('.titlebar');
    if (titlebar) titlebar.addEventListener('dblclick', function (e) {
      if (e.target.closest('.winctl') || e.target.closest('button')) return;
      handleWindowControl('maximize');
    });

    $$('.rail__btn[data-nav]').forEach(function (b) {
      b.addEventListener('click', function () { switchView(b.dataset.nav); });
    });

    var btnSearch = $('#btn-search');
    if (btnSearch) btnSearch.addEventListener('click', function () { openModal('modal-search'); renderSearch(''); });
    var resumeContinue = $('#resume-bar-continue');
    if (resumeContinue) resumeContinue.addEventListener('click', function () { submitText('继续'); });
    var resumeDismiss = $('#resume-bar-dismiss');
    if (resumeDismiss) resumeDismiss.addEventListener('click', dismissResume);
    var btnSessions = $('#btn-sessions');
    if (btnSessions) btnSessions.addEventListener('click', function () { toggleSessions(); });
    var btnSettings = $('#btn-settings');
    if (btnSettings) btnSettings.addEventListener('click', function () { openSettings(s.setTab || 'models'); });
    bindModelUi();
    loadBehaviorSettings();
    var btnNew = $('#btn-new-session');
    if (btnNew) btnNew.addEventListener('click', function () {
      api('/api/sessions', { method: 'POST', body: { title: '新会话' } })
        .then(function (se) {
          if (se && se.id) s.sessionId = se.id;
          return loadSessions();
        })
        .then(function () {
          toggleSessions(false);
          switchView('chat');
          var title = $('#view-title');
          if (title) title.textContent = '新会话';
          var stream = $('#view-chat');
          if (stream) $$('.msg', stream).forEach(function (m) { m.remove(); });
          toast('已新建会话');
        })
        .catch(function (err) { toast('新建会话失败：' + (err.message || err)); });
    });

    var btnReset = $('#btn-reset-chat');
    if (btnReset) btnReset.addEventListener('click', function () {
      if (!confirm('确定要重置当前会话吗？历史记录将被清空，开始全新净室测试。')) return;
      api('/api/sessions/reset', { method: 'POST', body: { clear_all: true } })
        .then(function (se) {
          if (se && se.id) s.sessionId = se.id;
          return loadSessions();
        })
        .then(function () {
          toggleSessions(false);
          switchView('chat');
          var title = $('#view-title');
          if (title) title.textContent = '新会话';
          var stream = $('#view-chat');
          if (stream) $$('.msg', stream).forEach(function (m) { m.remove(); });
          toast('✨ 已重置为净室会话，所有历史已清空');
        })
        .catch(function (err) { toast('重置会话失败: ' + (err.message || err)); });
    });

    var btnDrawer = $('#btn-drawer');
    if (btnDrawer) btnDrawer.addEventListener('click', function () {
      s.drawerOpen = !s.drawerOpen;
      save();
      applyDrawer();
    });

    // 窄屏 overlay 抽屉的收起路径：抽屉内关闭钮 / 点空白 / Esc（D7，仅 ≤960px 生效）
    var btnDrawerClose = $('#btn-drawer-close');
    if (btnDrawerClose) btnDrawerClose.addEventListener('click', function () {
      s.drawerOpen = false;
      save();
      applyDrawer();
    });
    document.addEventListener('click', function (e) {
      if (!s.drawerOpen) return;
      if (!(window.matchMedia && window.matchMedia('(max-width:960px)').matches)) return;
      if (e.target.closest && (e.target.closest('#drawer') || e.target.closest('#btn-drawer'))) return;
      s.drawerOpen = false;
      save();
      applyDrawer();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || !s.drawerOpen) return;
      if (!(window.matchMedia && window.matchMedia('(max-width:960px)').matches)) return;
      s.drawerOpen = false;
      save();
      applyDrawer();
    });
    var runGo = $('#runstrip-go');
    if (runGo) runGo.addEventListener('click', function () {
      s.drawerOpen = true;
      save();
      applyDrawer();
      switchTab('review');
    });
    var runStop = $('#runstrip-stop');
    if (runStop) runStop.addEventListener('click', function () { stopTask('runstrip'); });
    var btnExport = $('#btn-export');
    if (btnExport) btnExport.addEventListener('click', function () { toast('已导出本次会话为 Markdown'); });

    $$('.segmented [role="tab"]').forEach(function (b) {
      b.addEventListener('click', function () { switchTab(b.dataset.tab); });
    });

    if (approvalCard) {
      $$('.gate__scope button', approvalCard).forEach(function (b) {
        b.addEventListener('click', function () {
          if (b.disabled || !pendingApproval()) return;
          s.approvalScope = b.dataset.scope;
          paintApproval();
        });
      });
      $$('[data-approve]', approvalCard).forEach(function (b) {
        b.addEventListener('click', function () { resolveApproval(b.dataset.approve === 'yes' ? 'yes' : 'no'); });
      });
    }

    var taskHost = $('#task-list');
    if (taskHost) taskHost.addEventListener('click', function (e) {
      var card = e.target.closest('[data-open-overview]');
      if (card) openOverview(card.getAttribute('data-task'));
    });
    var ovStop = $('#btn-ov-stop');
    if (ovStop) ovStop.addEventListener('click', function () {
      if (ovStop.disabled) return;
      stopTask('overview');
    });


    var input = $('#composer-input');
    if (input) {
      input.addEventListener('input', function () { autosize(); syncSend(); });
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          sendMessage();
        }
      });
    }
    var btnSend = $('#btn-send');
    if (btnSend) btnSend.addEventListener('click', sendMessage);

    closeModelMenu();

    var btnAvatar = $('#btn-avatar');
    if (btnAvatar) btnAvatar.addEventListener('click', function () { openAvatarModal(); });
    var railAvatar = $('#btn-avatar-rail');
    if (railAvatar) railAvatar.addEventListener('click', function () { openAvatarModal(); });
    var avatar = $('#avatar');
    if (avatar) {
      avatar.addEventListener('click', function () { openAvatarModal(); });
      avatar.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openAvatarModal(); }
      });
    }

    var cardSoul = $('#card-soul');
    if (cardSoul) cardSoul.addEventListener('click', function () {
      var ta = $('#soul-text');
      if (ta) ta.setAttribute('readonly', '');   // R1：SOUL 无后端接口，降级只读
      var sb = $('#btn-soul-save');
      if (sb) { sb.hidden = true; sb.title = '待后端支持'; }
      openModal('modal-soul');
    });
    var cardMemory = $('#card-memory');
    if (cardMemory) cardMemory.addEventListener('click', function () {
      api('/api/memory').then(function (res) {
        var ta = $('#memory-text');
        if (ta) ta.value = (res && res.content) || '';
      }).catch(function (err) { toast('读取记忆失败：' + (err.message || err)); });
      openModal('modal-memory');
    });

    var btnMemorySave = $('#btn-memory-save');
    if (btnMemorySave) btnMemorySave.addEventListener('click', function () {
      btnMemorySave.disabled = true;
      api('/api/memory', { method: 'POST', body: { content: $('#memory-text').value } })
        .then(function () {
          toast('已保存到本机 MEMORY.md');
          closeModal($('#modal-memory'));
        })
        .catch(function (err) { toast('保存失败：' + (err.message || err)); })
        .then(function () { btnMemorySave.disabled = false; });
    });

    var btnTakeover = $('#btn-takeover-done');
    if (btnTakeover) btnTakeover.addEventListener('click', function () {
      var tid = s.activeTakeover && s.activeTakeover.task_id;
      if (!tid) { closeModal($('#modal-takeover')); return; }
      btnTakeover.disabled = true;
      // 后端会广播 takeover_update(completed)，modal 由 onTakeoverUpdate 收敛关闭
      api('/api/takeover/' + encodeURIComponent(tid) + '/done', { method: 'POST' })
        .catch(function (err) { toast('完成确认失败：' + (err.message || err)); })
        .then(function () { btnTakeover.disabled = false; });
    });
    var btnTakeoverCancel = $('#btn-takeover-cancel');
    if (btnTakeoverCancel) btnTakeoverCancel.addEventListener('click', function () {
      var tid = s.activeTakeover && s.activeTakeover.task_id;
      if (!tid) { closeModal($('#modal-takeover')); return; }
      api('/api/takeover/' + encodeURIComponent(tid) + '/cancel', { method: 'POST' })
        .then(function () { toast('已取消接管任务'); })
        .catch(function (err) { toast('取消失败：' + (err.message || err)); });
      closeModal($('#modal-takeover'));
    });
    var btnTakeoverFocus = $('#btn-takeover-focus');
    if (btnTakeoverFocus) btnTakeoverFocus.addEventListener('click', function () {
      var tid = s.activeTakeover && s.activeTakeover.task_id;
      if (!tid) return;
      api('/api/takeover/' + encodeURIComponent(tid) + '/focus', { method: 'POST' })
        .then(function () { toast('已把目标窗口拉到前台'); })
        .catch(function (err) { toast('聚焦失败：' + (err.message || err)); });
    });

    var search = $('#search-input');
    if (search) {
      search.addEventListener('input', function () { renderSearch(search.value.trim()); });
      search.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeAllModals(); });
    }

    $$('.modal').forEach(function (m) {
      m.addEventListener('click', function (e) {
        if (e.target === m) closeModal(m);
      });
      $$('[data-close]', m).forEach(function (b) {
        b.addEventListener('click', function () { closeModal(m); });
      });
    });

    $$('.sw').forEach(function (sw) {
      if (sw.id === 'ep-nokey' || sw.id === 'ep-enable' || sw.id === 'sw-tray' || sw.id === 'sw-failover') return;
      sw.addEventListener('click', function () {
        var on = sw.getAttribute('aria-checked') === 'true';
        sw.setAttribute('aria-checked', String(!on));
        toast(!on ? '已开启' : '已关闭');
      });
    });

    bindPicksets();

    var btnAvatarSave = $('#btn-avatar-save');
    if (btnAvatarSave) btnAvatarSave.addEventListener('click', function () {
      var on = $('.pickset.is-on');
      if (on && AVATARS.indexOf(on.dataset.theme) > -1) s.avatar = on.dataset.theme;
      save();
      paintAvatar();
      closeModal($('#modal-avatar'));
      toast('形象已切换为：' + (s.avatar === 'dragon' ? '龙' : '熊猫'));
    });

    document.addEventListener('keydown', function (e) {
      var mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        openModal('modal-search');
        renderSearch('');
        return;
      }
      if (e.key === 'Escape') {
        if ($('#model-menu') && !$('#model-menu').hidden) { closeModelMenu(); return; }
        var open = $$('.modal').filter(function (m) { return !m.classList.contains('is-hidden'); });
        if (open.length) { closeModal(open[open.length - 1]); return; }
        if (s.drawerOpen && window.innerWidth < 1180 && $('#drawer').classList.contains('is-open')) {
          s.drawerOpen = false; save(); applyDrawer(); return;
        }
        toggleSessions(false);
      }
      if (mod && e.key.toLowerCase() === 'j') {
        e.preventDefault();
        s.drawerOpen = !s.drawerOpen;
        save();
        applyDrawer();
      }
    });

    document.addEventListener('click', function (e) {
      var el = $('#sessions');
      if (!el || !el.classList.contains('is-open')) return;
      if (el.contains(e.target) || (e.target.closest && e.target.closest('#btn-sessions'))) return;
      toggleSessions(false);
    });
  }

  function bindPicksets() {
    $$('.pickset').forEach(function (p) {
      p.addEventListener('click', function () {
        $$('.pickset').forEach(function (x) { x.classList.remove('is-on'); });
        p.classList.add('is-on');
      });
    });
  }

  function openAvatarModal() {
    $$('.pickset').forEach(function (p) {
      p.classList.toggle('is-on', p.dataset.theme === s.avatar);
    });
    openModal('modal-avatar');
  }

  function loadAvatarThemes() {
    // /api/avatar/themes 驱动选择器；响应不可用时保留静态 pickset 兜底
    api('/api/avatar/themes').then(function (res) {
      var themes = res && res.themes;
      if (!Array.isArray(themes) || !themes.length) return;
      var modal = $('#modal-avatar');
      if (!modal) return;
      $$('.pickset', modal).forEach(function (p) { p.remove(); });
      var host = modal.querySelector('.picksets') || modal;
      themes.forEach(function (th) {
        var b = document.createElement('button');
        b.className = 'pickset' + (th.id === s.avatar ? ' is-on' : '');
        b.setAttribute('data-theme', th.id || '');
        var row = document.createElement('div');
        row.className = 'pickset__row';
        var img = document.createElement('img');
        img.src = (th.states && (th.states.acting || th.states.idle)) || th.preview || ('/avatar/' + (th.id || 'panda') + '/idle.gif');
        img.alt = th.name || th.id || '';
        row.appendChild(img);
        var span = document.createElement('span');
        span.textContent = th.name || th.id || '';
        var small = document.createElement('small');
        small.textContent = th.desc || '';
        b.appendChild(row);
        b.appendChild(span);
        b.appendChild(small);
        host.appendChild(b);
      });
      bindPicksets();
    }).catch(function () {});
  }

  /* ---------- 启动 ---------- */
  function loadVersion() {
    api('/api/version').then(function (d) {
      if (!d || !d.version) return;
      var el = document.querySelector('[data-version-label]');
      if (el) el.textContent = '本机 · v' + d.version;
    }).catch(function () {});
  }

  function restore() {
    paintApproval();          // 演示卡收起，等真实 approval_request
    renderReview();
    renderTasks();
    switchTab(s.tab || 'feed');
    refreshSnapshot();
    connectSSE();
    loadAvatarThemes();
    loadSessions();
    checkPendingResume();
    loadTasks();
    loadModels();
    loadApprovalHistory();
    loadVersion();
  }

  document.addEventListener('DOMContentLoaded', function () {
    bind();
    paintAvatar();
    applyDrawer();
    restore();
    syncSend();
    autosize();
    switchView('chat');
    renderRoutes();
    renderEndpoints();
    deriveState();
    renderReview();
  });

  /* ---------- 可测性导出桥（P6 重命名后旧 Node VM 测试经 __alterego 取符号） ---------- */
  function bindComposer() {
    var input = $('#composer-input');
    var btn = $('#btn-send');
    if (input) input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); }
    });
    if (btn) btn.addEventListener('click', sendMessage);
  }

  window.__alterego = {
    esc: esc, escHtml: escHtml, escAttr: escAttr, jsq: jsq, md: md, renderMd: renderMd,
    api: api, parseIfString: parseIfString, uploadFile: uploadFile,
    connectSSE: connectSSE, refreshSnapshot: refreshSnapshot,
    applyAgentState: applyAgentState, paintActivity: paintActivity,
    stopTask: stopTask, sendMessage: sendMessage,
    onServerMessage: onServerMessage, onToolCallStarted: onToolCallStarted, onToolCallDone: onToolCallDone,
    onApprovalRequest: onApprovalRequest, onApprovalResolved: onApprovalResolved,
    onTakeoverRequest: onTakeoverRequest, onTakeoverUpdate: onTakeoverUpdate,
    onTaskUpdate: onTaskUpdate, onTokenUsage: onTokenUsage,
    loadTasks: loadTasks, loadSessions: loadSessions, loadModels: loadModels,
    bindComposer: bindComposer,
    get API_TOKEN() { return API_TOKEN; },
    get state() { return s; }
  };

  /* P6：Node VM 沙箱顶层符号导出（tests/test_ui_esc_and_token.js 按顶层函数访问；
     浏览器端即为 window 上的同名函数，无副作用） */
  try {
    globalThis.esc = esc;
    globalThis.jsq = jsq;
    globalThis.md = md;
    globalThis.escHtml = escHtml;
    globalThis.escAttr = escAttr;
    globalThis.renderMd = renderMd;
    globalThis.sendMessage = sendMessage;
    globalThis.bindComposer = bindComposer;
    globalThis.connectSSE = connectSSE;
    globalThis.refreshSnapshot = refreshSnapshot;
    globalThis.API_TOKEN = API_TOKEN;
  } catch (_) {}
})();
