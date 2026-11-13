/* AlterEgo · 桌面任务浮窗原型
   状态：run / gate / take / done / paused / reject
   与主工作台同一套令牌与语义（状态色仅用于状态点、进度与审批） */
(function () {
  'use strict';

  var MODES = {
    run: {
      tag: '后台任务',
      state: '正在执行',
      stateCls: 'state-run',
      step: '· 执行中',
      ava: 'working',
      track: '',
      gate: false,
      take: false,
      done: false,
      acts: [
        { act: 'open', label: '打开主窗口', cls: 'act-secondary' },
        { act: 'stopTask', label: '停止', cls: 'act-ghost', right: true }
      ],
      hint: '主窗口已关闭，任务继续在后台执行'
    },
    gate: {
      tag: '待审批',
      state: '等待你审批',
      stateCls: 'state-gate',
      step: '· 等待你确认',
      ava: 'waiting',
      track: 'is-gate',
      gate: true,
      take: false,
      done: false,
      acts: [
        { act: 'deny', label: '暂不发布', cls: 'act-ghost' },
        { act: 'allow', label: '批准并发布', cls: 'act-secondary--solid', right: true }
      ],
      hint: ''
    },
    take: {
      tag: '需接管',
      state: '需要你接管',
      stateCls: 'state-take',
      step: '· 等待你接管',
      ava: 'connecting',
      track: 'is-take',
      gate: false,
      take: true,
      done: false,
      acts: [
        { act: 'stop', label: '取消任务', cls: 'act-ghost' },
        { act: 'resumeTake', label: '我已登录，继续', cls: 'act-secondary--solid', right: true }
      ],
      hint: ''
    },
    done: {
      tag: '已完成',
      state: '任务已完成',
      stateCls: 'state-ok',
      step: '· 已完成',
      ava: 'celebrate',
      track: 'is-done',
      gate: false,
      take: false,
      done: true,
      acts: [
        { act: 'close', label: '关闭浮窗', cls: 'act-ghost' },
        { act: 'open', label: '查看发布结果', cls: 'act-secondary', right: true }
      ],
      hint: ''
    },
    paused: {
      tag: '已暂停',
      state: '任务已暂停',
      stateCls: '',
      step: '· 已停止',
      ava: 'idle',
      track: 'is-idle',
      gate: false,
      take: false,
      done: false,
      acts: [
        { act: 'close', label: '关闭浮窗', cls: 'act-ghost' },
        { act: 'open', label: '打开主窗口', cls: 'act-secondary--solid', right: true }
      ],
      hint: ''
    },
    reject: {
      tag: '已拒绝',
      state: '已按拒绝处理',
      stateCls: 'state-risk',
      step: '· 未执行对外动作',
      ava: 'idle',
      track: 'is-idle',
      gate: false,
      take: false,
      done: false,
      acts: [
        { act: 'close', label: '关闭浮窗', cls: 'act-ghost' },
        { act: 'open', label: '打开主窗口', cls: 'act-secondary--solid', right: true }
      ],
      hint: ''
    }
  };

  var s = {
    mode: 'run',
    pct: 36,
    collapsed: false,
    dragged: false
  };

  var timers = { tick: 0, gate: 0, take: 0 };
  var gateLeft = 168;
  var takeLeft = 24;
  /* ---------- 宿主桥（pywebview / 浏览器降级） ---------- */
  var AVA_MODE = { idle: 'idle', working: 'acting', connecting: 'thinking', waiting: 'waiting', celebrate: 'done' };
  var BOTTOM_PAD = 8;
  var live = false;

  function token() {
    var m = /[?&]token=([^&]*)/.exec(location.search || '');
    return m ? decodeURIComponent(m[1]) : '';
  }

  /* ---------- 真实状态轮询（浮窗无 SSE，3 秒快照轮询，P4） ---------- */
  var API = (window.location.origin && window.location.origin !== 'null' && !/^file:/.test(window.location.origin))
    ? window.location.origin : 'http://127.0.0.1:5000';
  var pollTimer = 0;
  var pollState = { approvalId: '', approvalExpiresAt: 0, takeoverId: '' };
  var pollTaskCount = 0;
  var pollGoal = '';        // 任务名（tracker）→ 会话标题（兜底）
  var trackerPct = null;    // tracker 进度；无规划任务时为 null
  var tokTotal = 0;         // 进程内 token 累计（真实，替代假 MB）

  function fetchJson(path) {
    if (typeof fetch !== 'function') return Promise.reject(new Error('no-fetch'));
    var headers = {};
    var t = token();
    if (t) headers['Authorization'] = 'Bearer ' + t;
    return fetch(API + path, { headers: headers }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var ct = res.headers.get('content-type') || '';
      var p = ct.indexOf('json') >= 0 ? res.json() : res.text();
      return Promise.resolve(p).then(function (v) {
        if (typeof v === 'string') { try { return JSON.parse(v); } catch (e) { return v; } }
        return v;
      });
    });
  }

  function postJson(path, body) {
    if (typeof fetch !== 'function') return Promise.reject(new Error('no-fetch'));
    var headers = { 'Content-Type': 'application/json' };
    var t = token();
    if (t) headers['Authorization'] = 'Bearer ' + t;
    return fetch(API + path, { method: 'POST', headers: headers, body: JSON.stringify(body || {}) })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .catch(function (e) { toast(String(e.message || e)); });
  }

  function applySnapshot(snap) {
    if (!snap || !snap.agent_state) return;
    var st = snap.agent_state.state || 'idle';
    if (snap.status && snap.status.token_usage) {
      var tu = snap.status.token_usage;
      tokTotal = (tu.input || 0) + (tu.output || 0);
    }
    // 优先级：待审批 > 待接管 > 运行/停止/完成；idle 保持当前模式
    var pend = Array.isArray(snap.approvals) && snap.approvals.length ? snap.approvals[0] : null;
    if (pend && pend.approval_id) {
      pollState.approvalId = pend.approval_id;
      pollState.approvalExpiresAt = pend.expires_at || 0;
      update({
        mode: 'gate',
        step: pend.summary_cn || pend.action_cn || '等待你审批',
        goal: pend.action_cn || pend.action || '',
        ttl: Math.max(0, Math.round(((pend.expires_at || 0) - Date.now()) / 1000))
      });
      return;
    }
    if (snap.active_takeover && snap.active_takeover.task_id) {
      pollState.takeoverId = snap.active_takeover.task_id;
      var reqAt = typeof snap.active_takeover.requested_at === 'number'
        ? snap.active_takeover.requested_at * 1000 : Date.now();
      update({
        mode: 'take',
        step: snap.active_takeover.reason || '等待人工接管',
        goal: (snap.active_takeover.target || '浏览器') + ' 接管',
        waited: Math.max(0, Math.round((Date.now() - reqAt) / 1000))
      });
      return;
    }
    pollState.approvalId = '';
    pollState.takeoverId = '';
    var act = Array.isArray(snap.activity) && snap.activity.length ? snap.activity[0] : null;
    var stepText = snap.agent_state.detail || (act ? (act.tool || '') + ' · ' + (act.summary || '') : '');
    if (pollTaskCount > 1) stepText = pollTaskCount + ' 个任务并行 · ' + stepText;
    if (st === 'thinking' || st === 'acting' || st === 'waiting_subagent') {
      // 进度：tracker 优先；无规划任务时按最近活动完成比估算（真实数据，非假分片）
      var acts = Array.isArray(snap.activity) ? snap.activity : [];
      var doneN = 0;
      acts.forEach(function (it) { if (it.status === 'done' || it.status === 'ok') doneN++; });
      var actPct = acts.length ? Math.round(doneN / acts.length * 100) : 0;
      update({
        mode: 'run',
        step: stepText,
        goal: pollGoal || '任务执行中',
        pct: trackerPct !== null ? trackerPct : actPct
      });
      return;
    }
    if (st === 'done') { update({ mode: 'done', step: stepText || '任务已完成', goal: pollGoal || '任务已完成' }); return; }
    if (st === 'cancelled') { update({ mode: 'paused', step: '已停止 · 子任务已回收，可回主窗口重新发送' }); return; }
    if (st === 'error' || st === 'incomplete') { update({ mode: 'paused', step: stepText || '任务未完成' }); return; }
    if (st === 'idle') { update({ mode: 'paused', step: '空闲中 · 主窗口已最小化，点「打开主窗口」召回' }); return; }
  }

  function poll() {
    fetchJson('/api/state/snapshot').then(applySnapshot).catch(function () {});
    fetchJson('/api/tasks/running').then(function (rows) {
      pollTaskCount = Array.isArray(rows) ? rows.length : 0;
      if (pollTaskCount && rows[0].name) {
        pollGoal = rows[0].name;
        trackerPct = Math.round(rows[0].progress || 0);
        update({ goal: pollGoal, pct: trackerPct });
      } else {
        trackerPct = null;
      }
    }).catch(function () {});
    fetchJson('/api/sessions').then(function (list) {
      if (Array.isArray(list) && list.length && !pollGoal) {
        pollGoal = list[0].title || '';
        update({ goal: pollGoal });
      }
    }).catch(function () {});
  }

  function openMain() {
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.open_main) {
        window.pywebview.api.open_main();
        return;
      }
    } catch (_) {}
    // 子进程浮窗：让主进程窗口恢复显示（show/restore），然后关掉自己
    if (typeof fetch === 'function') {
      var headers = { 'Content-Type': 'application/json' };
      var t = token();
      if (t) headers['Authorization'] = 'Bearer ' + t;
      fetch(API + '/api/window/control', { method: 'POST', headers: headers, body: JSON.stringify({ action: 'show' }) })
        .then(function () { closeWindow(); })
        .catch(function () { location.href = '/' + (t ? '?token=' + encodeURIComponent(t) : ''); });
      return;
    }
    var t2 = token();
    location.href = '/' + (t2 ? '?token=' + encodeURIComponent(t2) : '');
  }

  function closeWindow() {
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.close) {
        window.pywebview.api.close();
        return;
      }
    } catch (_) {}
    window.close();
  }

  /* 宿主推送真实任务数据：AlterEgoFloat.update({ mode, pct, goal, step, ttl, waited }) */
  window.AlterEgoFloat = {
    update: function (data) {
      data = data || {};
      live = true;
      if (typeof data.goal === 'string') s.goal = data.goal;
      if (typeof data.step === 'string') s.step = data.step;
      if (typeof data.pct === 'number') s.pct = clamp(data.pct, 0, 100);
      if (data.mode && MODES[data.mode]) setMode(data.mode);
      if (typeof data.ttl === 'number') {
        gateLeft = Math.max(0, data.ttl);
        var ttlEl = $('#f-ttl');
        if (ttlEl) ttlEl.textContent = mmss(gateLeft);
      }
      if (typeof data.waited === 'number') {
        takeLeft = Math.max(0, data.waited);
        var waitEl = $('#f-wait');
        if (waitEl) waitEl.textContent = mmss(takeLeft);
      }
      paint();
    },
    collapse: function () { collapse(); },
    expand: function () { expand(); }
  };


  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function mmss(sec) {
    sec = Math.max(0, Math.round(sec));
    return pad(Math.floor(sec / 60)) + ':' + pad(sec % 60);
  }
  function clamp(v, a, b) { return Math.min(b, Math.max(a, v)); }

  /* ---------- 轻提示 ---------- */
  var toastTimer = 0;
  function toast(text) {
    var el = $('#ftoast');
    if (!el) return;
    el.textContent = text;
    el.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('is-on'); }, 2000);
  }

  /* ---------- 进度与数值 ---------- */
  function fmtTok(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(n);
  }
  function bytes() { return tokTotal ? fmtTok(tokTotal) + ' tok' : ''; }
  function eta() {
    if (s.mode === 'done') return '已完成';
    if (s.mode === 'paused') return '已停止';
    if (s.mode === 'reject') return '已拒绝';
    return '执行中';
  }

  function paintProgress() {
    var pct = Math.round(s.pct);
    $$('#f-bar, #f-edge').forEach(function (el) {
      el.style.width = pct + '%';
    });
    var fl = $('#float');
    if (fl) fl.style.setProperty('--p', pct + '%');
    var pill = $('#pill');
    if (pill) {
      pill.style.setProperty('--p', pct + '%');
      pill.style.setProperty('--pn', String(pct));
      pill.setAttribute('data-state',
        s.mode === 'done' ? 'done' :
        s.mode === 'gate' ? 'gate' :
        s.mode === 'take' ? 'take' :
        s.mode === 'paused' || s.mode === 'reject' ? 'idle' : 'run');
    }
    var wrap = $('#f-trackwrap');
    if (wrap) {
      wrap.setAttribute('aria-valuenow', String(pct));
      wrap.className = 'float__track' + (MODES[s.mode].track ? ' ' + MODES[s.mode].track : '');
    }
    var p = $('#f-pct');
    if (p) p.textContent = pct + '%';
    var b = $('#f-bytes');
    if (b) b.textContent = bytes();
    var e = $('#f-eta');
    if (e) e.textContent = eta();
    var sub = $('#pill-sub');
    if (sub) {
      sub.textContent = s.mode === 'done' ? '已完成' :
        s.mode === 'gate' ? '等待审批 ' + pct + '%' :
        s.mode === 'take' ? '等待接管 ' + pct + '%' :
        s.mode === 'paused' ? '已停止' :
        s.mode === 'reject' ? '已拒绝' : '执行 ' + pct + '%';
    }
  }

  /* ---------- 动作区 ---------- */
  function paintActs() {
    var host = $('#f-acts');
    if (!host) return;
    var m = MODES[s.mode];
    var html = '';
    m.acts.forEach(function (a) {
      if (a.right) html += '<span class="spacer"></span>';
      html += '<button class="btn ' + a.cls + '" type="button" data-act="' + a.act + '">' + a.label + '</button>';
    });
    if (m.hint) html += '<span class="float__hint">' + m.hint + '</span>';
    host.innerHTML = html;
  }

  /* ---------- 主渲染 ---------- */
  function fitHeight() {
    // 无边框窗口高度自适应内容；resize 后实测视口差值并补偿（DPI 缩放也不会切底）
    try {
      if (!(window.pywebview && window.pywebview.api && window.pywebview.api.resize)) return;
      var el = s.collapsed ? $('#pill') : $('#float');
      if (!el) return;
      var target = Math.ceil(el.scrollHeight) + 2;
      window.__fitTarget = target;
      window.pywebview.api.resize(target);
      setTimeout(function () {
        try {
          if (window.__fitTarget !== target) return;
          var diff = target - window.innerHeight;
          if (Math.abs(diff) >= 2) window.pywebview.api.resize(target + diff);
        } catch (_) {}
      }, 60);
    } catch (_) {}
  }

  function paint() {
    var m = MODES[s.mode];
    var fl = $('#float');
    if (fl) fl.setAttribute('data-mode', s.mode);

    var ava = $('#f-ava');
    if (ava) {
      ava.src = '/avatar/panda/' + (AVA_MODE[m.ava] || 'idle') + '.gif';
      ava.alt = 'AlterEgo 形象：' + m.state;
    }
    var pillImg = $('#pill .pill__ring img');
    if (pillImg) pillImg.src = '/avatar/panda/' + (AVA_MODE[m.ava] || 'idle') + '.gif';

    var tag = $('#f-tag');
    if (tag) tag.textContent = m.tag;

    var st = $('#f-state');
    if (st) {
      st.textContent = m.state;
      st.className = m.stateCls;
    }
    var step = $('#f-step');
    if (step) step.textContent = s.step || m.step;

    var gate = $('#f-gate');
    if (gate) gate.hidden = !m.gate;
    var take = $('#f-take');
    if (take) take.hidden = !m.take;
    var done = $('#f-done');
    if (done) done.hidden = !m.done;

    var goal = $('#f-goal');
    if (goal) {
      goal.textContent = s.goal || (s.mode === 'done' ? '任务已完成' : '任务执行中');
    }

    var badge = $('#pill-badge');
    if (badge) {
      var need = s.mode === 'gate' || s.mode === 'take';
      badge.hidden = !need;
      badge.textContent = '1';
    }

    paintActs();
    paintProgress();
    fitHeight();
  }

  /* ---------- 模式切换 ---------- */
  function setMode(next) {
    if (!MODES[next]) return;
    s.mode = next;
    clearInterval(timers.tick);
    timers.tick = 0;

    if (next === 'run' && !live) {
      timers.tick = setInterval(function () {
        if (s.mode !== 'run') return;
        s.pct = clamp(s.pct + 0.42, 0, 99);
        paintProgress();
      }, 700);
    }
    if (next === 'gate') {
      clearInterval(timers.gate);
      timers.gate = 0;
      if (!live) {
        gateLeft = 168;
        timers.gate = setInterval(function () {
          gateLeft -= 1;
          var ttl = $('#f-ttl');
          if (ttl) ttl.textContent = mmss(gateLeft);
          if (gateLeft <= 0) {
            clearInterval(timers.gate);
            timers.gate = 0;
            toast('未在时限内确认，已按拒绝处理');
            setMode('reject');
          }
        }, 1000);
      }
      var ttl0 = $('#f-ttl');
      if (ttl0) ttl0.textContent = mmss(gateLeft);
    }
    if (next === 'take') {
      clearInterval(timers.take);
      timers.take = 0;
      if (!live) {
        takeLeft = 24;
        timers.take = setInterval(function () {
          takeLeft += 1;
          var w = $('#f-wait');
          if (w) w.textContent = mmss(takeLeft);
        }, 1000);
      }
      var w0 = $('#f-wait');
      if (w0) w0.textContent = mmss(takeLeft);
    }
    if (next !== 'gate') { clearInterval(timers.gate); timers.gate = 0; }
    if (next !== 'take') { clearInterval(timers.take); timers.take = 0; }
    if (next === 'done') s.pct = 100;

    // 展开浮窗时同步收起胶囊
    if (!s.collapsed) {
      var pill = $('#pill');
      if (pill) pill.hidden = true;
      var f = $('#float');
      if (f) f.hidden = false;
    }

    paint();
  }

  /* ---------- 收起 / 展开 ---------- */
  function collapse() {
    s.collapsed = true;
    var f = $('#float');
    var pill = $('#pill');
    if (f) f.hidden = true;
    if (pill) {
      pill.hidden = false;
      pill.classList.add('is-entering');
      setTimeout(function () { pill.classList.remove('is-entering'); }, 260);
    }
    paintProgress();
    fitHeight();
    toast('已收起为胶囊，任务继续执行');
  }

  function expand() {
    s.collapsed = false;
    var f = $('#float');
    var pill = $('#pill');
    if (pill) pill.hidden = true;
    if (f) {
      f.hidden = false;
      f.classList.add('is-entering');
      setTimeout(function () { f.classList.remove('is-entering'); }, 260);
    }
    paint();
  }

  /* ---------- 动作分发（真实端点，P4） ---------- */
  function run(act) {
    if (act === 'open') {
      openMain();
      return;
    }
    if (act === 'stopTask') {   // run 态「停止」：与主窗口同一 POST /api/stop，收敛等轮询
      postJson('/api/stop').then(function () { toast('停止指令已发送，正在收尾'); });
      return;
    }
    if (act === 'allow') {   // gate 态批准
      if (pollState.approvalId) {
        postJson('/api/approvals/' + encodeURIComponent(pollState.approvalId) + '/resolve', { approved: true, scope: 'once' });
      } else { toast('当前没有待审批'); }
      return;
    }
    if (act === 'deny') {   // gate 态拒绝
      if (pollState.approvalId) {
        postJson('/api/approvals/' + encodeURIComponent(pollState.approvalId) + '/resolve', { approved: false, scope: 'once' });
      } else { toast('当前没有待审批'); }
      return;
    }
    if (act === 'stop') {   // take 态：取消接管任务
      if (pollState.takeoverId) postJson('/api/takeover/' + encodeURIComponent(pollState.takeoverId) + '/cancel');
      toast('已取消任务');
      return;
    }
    if (act === 'resumeTake') {   // take 态：我已完成
      if (pollState.takeoverId) postJson('/api/takeover/' + encodeURIComponent(pollState.takeoverId) + '/done');
      toast('已确认完成');
      return;
    }
    if (act === 'close') { collapse(); return; }
  }

  /* ---------- 拖拽 ---------- */
  function bindDrag() {
    var handle = $('#float-grip');
    var box = $('#float');
    if (!handle || !box) return;
    var start = null;

    handle.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      if (e.target.closest('button')) return;
      var r = box.getBoundingClientRect();
      box.style.left = r.left + 'px';
      box.style.top = r.top + 'px';
      box.style.right = 'auto';
      start = { x: e.clientX, y: e.clientY, l: r.left, t: r.top };
      box.classList.add('is-dragging');
      handle.setPointerCapture(e.pointerId);
    });

    handle.addEventListener('pointermove', function (e) {
      if (!start) return;
      var w = box.offsetWidth;
      var h = box.offsetHeight;
      var left = clamp(start.l + (e.clientX - start.x), 8, window.innerWidth - w - 8);
      var top = clamp(start.t + (e.clientY - start.y), 8, window.innerHeight - h - BOTTOM_PAD);
      box.style.left = left + 'px';
      box.style.top = top + 'px';
    });

    function end(e) {
      if (!start) return;
      start = null;
      box.classList.remove('is-dragging');
      try { handle.releasePointerCapture(e.pointerId); } catch (_) {}
    }
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
  }

  /* ---------- 托盘 ---------- */
  function bindTray() {
    var box = $('#float');
    var menu = $('#traymenu');
    if (!box || !menu) return;

    box.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      menu.hidden = false;
      var w = menu.offsetWidth || 208;
      var h = menu.offsetHeight || 168;
      menu.style.left = clamp(e.clientX, 8, Math.max(8, window.innerWidth - w - 8)) + 'px';
      menu.style.top = clamp(e.clientY, 8, Math.max(8, window.innerHeight - h - 8)) + 'px';
      menu.style.right = 'auto';
      menu.style.bottom = 'auto';
    });

    document.addEventListener('pointerdown', function (e) {
      if (menu.hidden) return;
      if (e.target.closest('#traymenu')) return;
      menu.hidden = true;
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') menu.hidden = true;
    });

    $$('#traymenu [data-tray]').forEach(function (b) {
      b.addEventListener('click', function () {
        var kind = b.getAttribute('data-tray');
        menu.hidden = true;
        if (kind === 'open') { openMain(); return; }
        if (kind === 'expand') { expand(); return; }
        if (kind === 'pause') { postJson('/api/stop').then(function () { toast('停止指令已发送'); }); return; }
        if (kind === 'quit') {
          // 真正退出：主窗口（可能隐藏）销毁 + 停服，随后关掉浮窗自己
          postJson('/api/exit').then(function () { setTimeout(closeWindow, 300); });
          return;
        }
      });
    });
  }

  /* ---------- 绑定 ---------- */
  function bind() {
    var acts = $('#f-acts');
    if (acts) {
      acts.addEventListener('click', function (e) {
        var b = e.target.closest('[data-act]');
        if (b) run(b.getAttribute('data-act'));
      });
    }

    var pill = $('#pill');
    if (pill) pill.addEventListener('click', expand);

    var miniPill = $('#f-pill');
    if (miniPill) miniPill.addEventListener('click', collapse);

    var openMainBtn = $('#f-open');
    if (openMainBtn) openMainBtn.addEventListener('click', function () { openMain(); });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        var menu = $('#traymenu');
        if (menu && !menu.hidden) menu.hidden = true;
      }
    });

    bindDrag();
    bindTray();

  }

  function boot() {
    bind();
    if (typeof fetch === 'function') {
      poll();
      pollTimer = setInterval(poll, 3000);   // 浮窗无 SSE，3 秒快照轮询
    }
    var hash = (location.hash || '').replace('#', '');
    if (hash === 'pill') {
      setMode('run');
      collapse();
      return;
    }
    setMode(MODES[hash] ? hash : 'run');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
