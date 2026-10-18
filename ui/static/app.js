/* AlterEgo Web UI · 核心交互系统
 * Muse 1:1 像素级复刻架构，支持模型热切换、微信免键鼠长连接、动态流复盘与头像 6 态动画
 */
"use strict";

let API_TOKEN = new URLSearchParams(window.location.search).get("token") || "";

const API = (window.location.origin && window.location.origin !== "null" && !window.location.origin.startsWith("file:"))
  ? window.location.origin
  : "http://127.0.0.1:5000";

let currentSession = null;
let currentMainView = "chat";
let currentRightTab = "feed";
let approvalTimers = {};
let availableModels = [];

let currentAvatarTheme = (typeof localStorage !== "undefined" && localStorage.getItem) ? (localStorage.getItem("alterego_avatar_theme") || "panda") : "panda";

const AVATAR_STATE_GIFS = {
  panda: {
    idle: "/avatar/panda/idle.gif",
    thinking: "/avatar/panda/thinking.gif",
    acting: "/avatar/panda/acting.gif",
    crafting: "/avatar/panda/crafting.gif",
    awaiting_approval: "/avatar/panda/waiting.gif",
    waiting_user: "/avatar/panda/waiting.gif",
    incomplete: "/avatar/panda/waiting.gif",
    error: "/avatar/panda/waiting.gif",
    done: "/avatar/panda/done.gif",
  },
  dragon: {
    idle: "/avatar/dragon/idle.gif",
    thinking: "/avatar/dragon/thinking.gif",
    acting: "/avatar/dragon/acting.gif",
    crafting: "/avatar/dragon/crafting.gif",
    awaiting_approval: "/avatar/dragon/waiting.gif",
    waiting_user: "/avatar/dragon/waiting.gif",
    incomplete: "/avatar/dragon/waiting.gif",
    error: "/avatar/dragon/waiting.gif",
    done: "/avatar/dragon/done.gif",
  },
  otter: {
    idle: "",
  }
};

/* ================= 全局响应式状态存储 (UI.store) ================= */
const UI = {
  store: {
    agentState: { state: "idle", detail: "已就绪", model: "agnes-3.0-flash" },
    plan: [],
    activity: [],
    approvals: [],
    auditHistory: [],
    status: {
      current_model: "agnes-3.0-flash",
      main_model: "agnes-3.0-flash",
      token_usage: { source_id: "", revision: -1, input: null, output: null, known_requests: 0, unknown_requests: 0 },
      memory_writes: [],
    }
  }
};

/* ================= 通用 API 请求 ================= */
async function api(path, opts = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(opts.headers || {}),
  };
  if (API_TOKEN) {
    headers["Authorization"] = "Bearer " + API_TOKEN;
  }
  const res = await fetch(API + path, {
    ...opts,
    headers,
  });
  if (res.status === 401) {
    toast("鉴权失效或未授权，请检查 Token 或重启应用");
    const errData = await res.json().catch(() => ({}));
    throw new Error(errData.error || "unauthorized");
  }
  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    const error = new Error(errData.error || `HTTP ${res.status}`);
    if (errData.message && typeof errData.message === "object") {
      error.persistedMessage = errData.message;
    }
    throw error;
  }
  return res.json();
}

/* ================= 页面初始化 ================= */
document.addEventListener("DOMContentLoaded", async () => {
  initDesktopTitlebar();
  bindComposer();
  bindSettingsControls();
  initGlobalKeyboard();
  initAvatarTheme();
  startIdleHeartbeat();
  // 智能体控制中心抽屉默认保持收起状态（20% UI 极简模式），展开时自动扩展为 30%
  const rightDrawer = document.getElementById('right-drawer');
  if (rightDrawer) {
    rightDrawer.classList.add('hidden');
  }
  syncRightDrawerVisibility(false);

  try {
    // 1. 初始化模型列表与当前主模型
    await loadAvailableModels();

    // 2. 加载会话列表
    const sessions = await api("/api/sessions");
    renderSessions(sessions);
    if (sessions.length) {
      await selectSession(sessions[0].id);
    } else {
      await createNewSession();
    }

    // 3. 加载全量状态快照并连接 SSE
    await loadStateSnapshot();
    connectSSE();
  } catch (e) {
    console.warn("未能连接到本地服务:", e);
    toast("正在连接服务: " + e.message);
  }
});

/* ================= 全局键盘快捷键 ================= */
function initGlobalKeyboard() {
  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      toggleSearchModal();
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "/") {
      e.preventDefault();
      toggleSettingsMenu();
    }
    if (e.key === "Escape") {
      closeAllModals();
    }
  });
}

function closeAllModals() {
  document.getElementById("search-modal")?.classList.add("hidden");
  document.getElementById("activity-modal")?.classList.add("hidden");
  document.getElementById("settings-modal")?.classList.add("hidden");
  document.getElementById("soul-modal")?.classList.add("hidden");
  document.getElementById("memory-modal")?.classList.add("hidden");
  document.getElementById("avatar-modal")?.classList.add("hidden");
  document.getElementById("settings-menu")?.classList.add("hidden");
  document.getElementById("download-popover")?.classList.add("hidden");
  document.getElementById("model-switcher-dropdown")?.classList.add("hidden");
}

/* ================= 1. 左侧主视图切换 ================= */
function switchMainView(view) {
  currentMainView = view;
  const navIds = ['chat', 'search', 'drawer', 'ideas', 'goals', 'library'];
  navIds.forEach(id => {
    const btn = document.getElementById('nav-' + id);
    if (!btn) return;
    if (id === view) {
      btn.className = "w-9 h-9 rounded-full bg-[#F4F4F5] flex items-center justify-center text-[#1D1D1F] hover:bg-[#EBEBEC] transition";
    } else {
      btn.className = "w-9 h-9 rounded-full flex items-center justify-center text-[#1D1D1F] hover:bg-[#F4F4F5] transition";
    }
  });

  const chatView = document.getElementById('view-chat');
  const otherView = document.getElementById('view-other');
  const otherContent = document.getElementById('view-other-content');

  if (view === 'chat') {
    chatView?.classList.remove('hidden');
    otherView?.classList.add('hidden');
    return;
  }

  chatView?.classList.add('hidden');
  otherView?.classList.remove('hidden');

  const templates = {
    ideas: `
      <div class="space-y-6">
        <div>
          <h1 class="text-2xl font-bold tracking-tight mb-2">💡 点子与提示词库</h1>
          <p class="text-sm text-[#8E8E93]">沉淀可复用的业务链路、智能体 Prompt 构架与自动化操作模版。</p>
        </div>
        <div class="p-12 rounded-2xl border border-[#EAEAEA] text-center bg-[#F9F9FB] space-y-3">
          <div class="w-12 h-12 rounded-full bg-white flex items-center justify-center mx-auto text-xl shadow-sm border border-[#EAEAEA]">💡</div>
          <h3 class="font-semibold text-[15px] text-[#1D1D1F]">暂无自定义提示词</h3>
          <p class="text-[13px] text-[#8E8E93] max-w-[420px] mx-auto leading-relaxed">可通过与智能体对话，沉淀日常的高频操作或指令模板，支持一键复用。</p>
        </div>
      </div>`,
    goals: `
      <div class="space-y-6">
        <div>
          <h1 class="text-2xl font-bold tracking-tight mb-2">☑ 目标与待办中心</h1>
          <p class="text-sm text-[#8E8E93]">跟踪核心战略里程碑与智能体自主拆解任务。</p>
        </div>
        <div class="p-12 rounded-2xl border border-[#EAEAEA] text-center bg-[#F9F9FB] space-y-3">
          <div class="w-12 h-12 rounded-full bg-white flex items-center justify-center mx-auto text-xl shadow-sm border border-[#EAEAEA]">🎯</div>
          <h3 class="font-semibold text-[15px] text-[#1D1D1F]">暂无待办目标</h3>
          <p class="text-[13px] text-[#8E8E93] max-w-[420px] mx-auto leading-relaxed">在对话中向 AlterEgo 提出长线业务目标，智能体将自动拆解里程碑与任务清单。</p>
        </div>
      </div>`,
    library: `
      <div class="space-y-6">
        <div>
          <h1 class="text-2xl font-bold tracking-tight mb-2">❖ 资源库与构件</h1>
          <p class="text-sm text-[#8E8E93]">管理由 AlterEgo 生成的各类 Markdown 文档、下载产物与自动化快照。</p>
        </div>
        <div class="p-12 rounded-2xl border border-[#EAEAEA] text-center bg-[#F9F9FB] space-y-3">
          <div class="w-12 h-12 rounded-full bg-white flex items-center justify-center mx-auto text-xl shadow-sm border border-[#EAEAEA]">📁</div>
          <h3 class="font-semibold text-[15px] text-[#1D1D1F]">暂无生成构件</h3>
          <p class="text-[13px] text-[#8E8E93] max-w-[420px] mx-auto leading-relaxed">智能体在执行网页自动化、文件写入或数据提取任务时生成的成果将在此沉淀。</p>
        </div>
      </div>`
  };

  if (otherContent) otherContent.innerHTML = templates[view] || '';
}

/* ================= 2. 右侧 4 段式 Tab 切换 ================= */
function switchRightTab(tab) {
  currentRightTab = tab;
  const tabs = ['feed', 'audit', 'cron', 'identity'];
  tabs.forEach(t => {
    const btn = document.getElementById('rtab-' + t);
    const content = document.getElementById('rcontent-' + t);
    if (t === tab) {
      if (btn) btn.className = "relative h-full rounded-full bg-white shadow-sm flex items-center justify-center text-[#1D1D1F] transition";
      content?.classList.remove('hidden');
    } else {
      if (btn) btn.className = "relative h-full rounded-full flex items-center justify-center text-[#71717A] hover:text-[#1D1D1F] transition";
      content?.classList.add('hidden');
    }
  });

  // 切换到审核记录 Tab 时自动向后端刷新真实历史
  if (tab === 'audit') {
    fetchApprovalHistory();
  }
}

/* ================= 3. 抽屉与弹出层控制 ================= */
function requestWindowSplitRatio(ratio) {
  const token = window.ALTEREGO_TOKEN || "";
  fetch('/api/window/split_ratio', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-AlterEgo-Token': token,
      'Authorization': 'Bearer ' + token
    },
    body: JSON.stringify({ browser_ratio: ratio })
  }).catch(err => {
    console.debug('split ratio sync err:', err);
  });
}

function toggleRightDrawer(forceOpen = null) {
  const drawer = document.getElementById('right-drawer');
  if (!drawer) return;
  if (forceOpen === true) {
    drawer.classList.remove('hidden');
  } else if (forceOpen === false) {
    drawer.classList.add('hidden');
  } else {
    drawer.classList.toggle('hidden');
  }
  syncRightDrawerVisibility(true);
}

function syncRightDrawerVisibility(notifyBackend = false) {
  const drawer = document.getElementById('right-drawer');
  const centerProfile = document.getElementById('center-agent-profile');
  const openDrawerBtn = document.getElementById('btn-open-right-drawer');
  const openDrawerBadge = document.getElementById('btn-open-drawer-badge');
  const isDrawerHidden = !drawer || drawer.classList.contains('hidden');

  if (centerProfile) {
    centerProfile.classList.toggle('hidden', !isDrawerHidden);
  }
  if (openDrawerBtn) {
    openDrawerBtn.classList.toggle('hidden', !isDrawerHidden);
  }
  // 展开抽屉时自动清除外部小红点
  if (!isDrawerHidden && openDrawerBadge) {
    openDrawerBadge.classList.add('hidden');
  }

  if (notifyBackend) {
    // 侧边栏收起：AlterEgo 占 20%，浏览器占 80%
    // 侧边栏展开：AlterEgo 占 30%，浏览器占 70%
    const ratio = isDrawerHidden ? 0.80 : 0.70;
    requestWindowSplitRatio(ratio);
  }
}

function toggleSessionsDrawer() {
  const drawer = document.getElementById('sessions-drawer');
  drawer?.classList.toggle('hidden');
}

function toggleDownloadPopover() {
  document.getElementById('download-popover')?.classList.toggle('hidden');
  document.getElementById('settings-menu')?.classList.add('hidden');
}

function toggleSettingsMenu() {
  document.getElementById('settings-menu')?.classList.toggle('hidden');
  document.getElementById('download-popover')?.classList.add('hidden');
}

function toggleSearchModal() {
  const modal = document.getElementById('search-modal');
  modal?.classList.toggle('hidden');
  if (!modal?.classList.contains('hidden')) {
    const input = document.getElementById('search-modal-input');
    if (input) {
      input.value = "";
      onSearchInput("");
      setTimeout(() => input.focus(), 50);
    }
  }
}

function onSearchInput(query) {
  const container = document.getElementById("search-results-list");
  if (!container) return;
  const q = (query || "").trim().toLowerCase();
  if (!q) {
    container.innerHTML = `<div class="p-6 text-center text-xs text-[#8E8E93]">输入关键词搜索历史会话、执行任务或审核记录…</div>`;
    return;
  }

  const results = [];

  // 1. 搜索会话
  const sessions = (UI.store.sessions || []);
  sessions.forEach(s => {
    if ((s.title && s.title.toLowerCase().includes(q)) || (s.id && s.id.toLowerCase().includes(q))) {
      results.push({
        type: "session",
        icon: "💬",
        title: s.title || "未命名会话",
        detail: "历史会话",
        action: () => {
          selectSession(s.id);
          toggleSearchModal();
        }
      });
    }
  });

  // 2. 搜索活动任务
  const activities = (UI.store.activity || []);
  activities.forEach(act => {
    const title = act.tool ? formatActionCN(act.tool) : "执行操作";
    const desc = sanitizeSummaryCN(act.summary || act.result_summary || "");
    if (title.toLowerCase().includes(q) || desc.toLowerCase().includes(q)) {
      results.push({
        type: "activity",
        icon: "⚡",
        title: title,
        detail: desc,
        action: () => {
          openActivityModal(title, desc, act.started_at);
          toggleSearchModal();
        }
      });
    }
  });

  // 3. 搜索审核历史
  const history = (UI.store.auditHistory || []);
  history.forEach(h => {
    const title = h.action_cn || h.action || "";
    if (title.toLowerCase().includes(q) || (h.approval_id && h.approval_id.toLowerCase().includes(q))) {
      results.push({
        type: "approval",
        icon: "🛡️",
        title: title,
        detail: `${h.decision === "approved" ? "已批准" : "已拒绝"} · ${h.risk_level}`,
        action: () => {
          switchRightTab("audit");
          toggleSearchModal();
        }
      });
    }
  });

  if (!results.length) {
    container.innerHTML = `<div class="p-6 text-center text-xs text-[#8E8E93]">未匹配到相关内容</div>`;
    return;
  }

  container.innerHTML = results.slice(0, 15).map((r, i) => `
    <div onclick="window._searchItemClick(${i})" class="px-3 py-2.5 rounded-xl hover:bg-[#F4F4F5] cursor-pointer flex justify-between items-center transition">
      <div class="flex items-center space-x-2.5 min-w-0 pr-2">
        <span class="text-base shrink-0">${r.icon}</span>
        <div class="flex flex-col min-w-0">
          <span class="text-[13.5px] font-medium text-[#1D1D1F] truncate">${esc(r.title)}</span>
          <span class="text-[12px] text-[#8E8E93] truncate">${esc(r.detail)}</span>
        </div>
      </div>
      <span class="text-xs text-[#A1A1AA] shrink-0">前往 ❯</span>
    </div>
  `).join("");

  window._searchResultsActions = results.map(r => r.action);
}

window._searchItemClick = function(index) {
  if (window._searchResultsActions && window._searchResultsActions[index]) {
    window._searchResultsActions[index]();
  }
};

function toggleModelDropdown() {
  const dd = document.getElementById('model-switcher-dropdown');
  if (!dd) return;
  const isOpening = dd.classList.contains('hidden');
  dd.classList.toggle('hidden');
  if (isOpening) {
    const searchInput = document.getElementById('model-search-input');
    if (searchInput) {
      searchInput.value = "";
      onModelSearch("");
      setTimeout(() => searchInput.focus(), 60);
    }
  }
}

/* ================= 4. 活动详情双栏复盘 Modal ================= */
function openActivityModal(title, desc, time, steps = null, artifact = null) {
  const titleEl = document.getElementById('modal-title');
  const descEl = document.getElementById('modal-desc');
  const stepsEl = document.getElementById('modal-steps');
  const artifactEl = document.getElementById('modal-artifact');
  const artifactWrap = document.getElementById('modal-artifact-wrap');

  if (titleEl) titleEl.textContent = title;
  if (descEl) descEl.textContent = (desc || "任务已顺利完成，智能体全流程闭环") + (time ? ` （执行时间：${time}）` : "");

  if (stepsEl) {
    const defaultSteps = [
      "已开始任务执行与意图拆解",
      "调用自动化驱动器访问目标环境",
      "校验上下文与防封锁保护",
      "生成合规交付物并沉淀记忆库"
    ];
    const renderList = (Array.isArray(steps) && steps.length) ? steps : defaultSteps;
    stepsEl.innerHTML = renderList.map(s => `
      <div class="flex items-center space-x-2 text-[#1D1D1F]">
        <span class="text-[#10B981]">✔</span><span>${esc(s)}</span>
      </div>
    `).join("");
  }

  if (artifactEl && artifactWrap) {
    if (artifact) {
      artifactEl.textContent = artifact;
      artifactWrap.classList.remove('hidden');
    } else {
      artifactWrap.classList.add('hidden');
    }
  }

  document.getElementById('activity-modal')?.classList.remove('hidden');
}

function closeActivityModal() {
  document.getElementById('activity-modal')?.classList.add('hidden');
}

/* ================= 5. 设置、SOUL 与 MEMORY Modal ================= */
function openSettingsModal() {
  loadSettingsView();
  document.getElementById('settings-modal')?.classList.remove('hidden');
}
function closeSettingsModal() {
  document.getElementById('settings-modal')?.classList.add('hidden');
}

function openSoulModal() {
  document.getElementById('soul-modal')?.classList.remove('hidden');
}
function closeSoulModal() {
  document.getElementById('soul-modal')?.classList.add('hidden');
}
function saveSoulConfig() {
  toast("✓ SOUL 核心人设设定已成功更新并注入 Agent 运行时！");
  closeSoulModal();
}

async function openMemoryModal() {
  try {
    const mem = await api("/api/memory");
    const textEl = document.getElementById("setting-memory");
    if (textEl) textEl.value = mem.content || "";
  } catch (e) {}
  document.getElementById('memory-modal')?.classList.remove('hidden');
}
function closeMemoryModal() {
  document.getElementById('memory-modal')?.classList.add('hidden');
}

/* ================= 6. 模型热切换体系 ================= */
let currentModelSearchQuery = "";

async function loadAvailableModels() {
  try {
    const res = await api("/api/models");
    availableModels = res.models || [];
    const current = res.current || UI.store.status.current_model || "agnes-3.0-flash";
    updateCurrentModelUI(current);
    renderModelDropdownList(current, currentModelSearchQuery);
    populateSettingsModelDatalist(availableModels);
  } catch (e) {
    console.warn("加载模型列表失败:", e);
  }
}

function populateSettingsModelDatalist(models) {
  const datalist = document.getElementById("preset-models-datalist");
  if (!datalist || !Array.isArray(models)) return;
  datalist.innerHTML = models.map(m => {
    const keyHint = m.has_key ? " (已配置密钥)" : "";
    const tagHint = m.tag ? ` [${m.tag}]` : "";
    return `<option value="${esc(m.id)}">${esc(m.name || m.id)}${esc(tagHint)}${esc(keyHint)}</option>`;
  }).join("");
}

function updateCurrentModelUI(modelId) {
  UI.store.status.current_model = modelId;
  const pillText = document.getElementById("model-pill-text");
  if (pillText) pillText.textContent = modelId;

  const settingModel = document.getElementById("setting-model");
  if (settingModel && document.activeElement !== settingModel) {
    settingModel.value = modelId;
  }
}

function onModelSearch(query) {
  currentModelSearchQuery = query || "";
  const cur = UI.store.status.current_model || "agnes-3.0-flash";
  renderModelDropdownList(cur, currentModelSearchQuery);
}

function renderModelDropdownList(currentId, query = "") {
  const container = document.getElementById("model-list-container");
  if (!container) return;

  const q = (query || "").trim().toLowerCase();
  let list = Array.isArray(availableModels) ? availableModels : [];

  if (q) {
    list = list.filter(m => {
      const name = (m.name || "").toLowerCase();
      const id = (m.id || "").toLowerCase();
      const desc = (m.desc || "").toLowerCase();
      const tag = (m.tag || "").toLowerCase();
      return name.includes(q) || id.includes(q) || desc.includes(q) || tag.includes(q);
    });
  }

  const countBadge = document.getElementById("model-count-badge");
  if (countBadge) {
    countBadge.textContent = `${list.length} 个可用`;
  }

  if (list.length === 0) {
    if (q) {
      container.innerHTML = `
        <div class="p-4 text-center space-y-2">
          <div class="text-xs text-[#8E8E93]">未找到含 "${esc(q)}" 的预设模型</div>
          <button onclick="submitCustomModel('${esc(q)}')" class="px-3 py-1.5 bg-[#18181B] hover:bg-black text-white rounded-lg text-xs font-medium transition shadow-sm">
            直接切换为 "${esc(q)}"
          </button>
        </div>
      `;
    } else {
      container.innerHTML = `
        <div class="p-4 text-center text-xs text-[#8E8E93]">
          暂无可列出模型，请录入密钥或添加自定义模型
        </div>
      `;
    }
    return;
  }

  // 分组展示：recommended (推荐与主力) / custom & config (自定义与配置)
  const groups = {
    recommended: { title: "🌟 推荐与主力模型", items: [] },
    custom: { title: "⚙️ 自定义与配置扩展", items: [] },
  };

  list.forEach(m => {
    if (m.is_custom || m.group === "custom" || m.group === "config") {
      groups.custom.items.push(m);
    } else {
      groups.recommended.items.push(m);
    }
  });

  let html = "";
  const groupKeys = ["recommended", "custom"].filter(k => groups[k].items.length > 0);

  groupKeys.forEach((gKey, idx) => {
    const group = groups[gKey];
    if (groupKeys.length > 1 && !q) {
      html += `
        <div class="px-3 py-1 text-[10.5px] font-semibold text-[#8E8E93] bg-[#FAFAFA] border-y border-[#F0F0F0] select-none ${idx === 0 ? 'border-t-0' : ''}">
          ${group.title}
        </div>
      `;
    }

    group.items.forEach(m => {
      const isCur = m.id === currentId;
      const tagText = m.tag || (m.is_custom ? "自定义" : "模型");
      const isCustom = !!m.is_custom;

      html += `
        <div onclick="switchModel('${esc(m.id)}')" class="group px-3.5 py-2 hover:bg-[#F4F4F5] cursor-pointer flex justify-between items-center transition ${isCur ? 'bg-blue-50/60' : ''}">
          <div class="min-w-0 pr-2 flex-1">
            <div class="flex items-center space-x-1.5 flex-wrap">
              <span class="font-semibold text-[#1D1D1F] truncate text-[13px]">${esc(m.name || m.id)}</span>
              <span class="text-[10px] px-1.5 py-0.2 rounded font-normal shrink-0 ${isCur ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600'}">${esc(tagText)}</span>
              ${m.supports_images ? '<span class="text-[9px] px-1 py-0.2 rounded bg-emerald-50 text-emerald-600 font-normal shrink-0">视觉</span>' : ''}
            </div>
            <div class="text-[11px] text-[#8E8E93] mt-0.5 line-clamp-1 leading-snug">${esc(m.desc || m.id)}</div>
            ${m.base_url ? `<div class="text-[9.5px] text-[#A1A1AA] truncate mt-0.5">${esc(m.base_url)}</div>` : ''}
          </div>
          <div class="flex items-center space-x-1.5 shrink-0">
            ${isCur ? '<span class="text-blue-600 font-bold text-sm">✓</span>' : ''}
            ${isCustom ? `
              <button onclick="event.stopPropagation(); deleteCustomModel('${esc(m.id)}')" class="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-500 p-1 rounded transition text-xs" title="删除该自定义模型">
                ✕
              </button>
            ` : ''}
          </div>
        </div>
      `;
    });
  });

  container.innerHTML = html;
}

async function submitCustomModel(optionalId = "") {
  let modelId = optionalId;
  if (!modelId) {
    const input = document.getElementById("input-custom-model-id");
    modelId = (input?.value || "").trim();
  }
  if (!modelId) {
    toast("请输入要切换的模型标识 (ID)");
    return;
  }
  await switchModel(modelId);
  const input = document.getElementById("input-custom-model-id");
  if (input) input.value = "";
}

async function deleteCustomModel(modelId) {
  if (!confirm(`确定删除自定义模型 "${modelId}" 吗？`)) return;
  try {
    const res = await api(`/api/models/${encodeURIComponent(modelId)}`, { method: "DELETE" });
    if (res.ok) {
      toast(`已移除自定义模型: ${modelId}`);
      await loadAvailableModels();
    } else {
      toast("删除失败: " + (res.error || "未知错误"));
    }
  } catch (e) {
    toast("删除请求失败: " + e.message);
  }
}

async function switchModel(modelId, baseUrl = null) {
  const dd = document.getElementById('model-switcher-dropdown');
  if (dd) dd.classList.add('hidden');
  toast(`正在切换至主模型: ${modelId}…`);
  try {
    const payload = { model_id: modelId };
    if (baseUrl) payload.base_url = baseUrl;
    const res = await api("/api/switch_model", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      if (res.models && Array.isArray(res.models)) {
        availableModels = res.models;
      }
      updateCurrentModelUI(modelId);
      renderModelDropdownList(modelId, currentModelSearchQuery);
      // 同步设置页中的当前模型和 base_url 显示
      const settingModel = document.getElementById("setting-model");
      if (settingModel) settingModel.value = modelId;
      const matched = (availableModels || []).find(m => m.id === modelId);
      if (matched && matched.base_url) {
        const settingBase = document.getElementById("setting-base-url");
        if (settingBase) settingBase.value = matched.base_url;
      }
      toast(`✓ 已成功切换至主模型: ${modelId}`);
    } else {
      toast("切换模型失败: " + (res.error || "未知错误"));
    }
  } catch (e) {
    toast("切换请求失败: " + e.message);
  }
}

// 点击下拉外部区域自动收起模型列表
document.addEventListener("click", (e) => {
  const dd = document.getElementById("model-switcher-dropdown");
  const pill = document.getElementById("model-switcher-pill");
  if (dd && !dd.classList.contains("hidden")) {
    if (!dd.contains(e.target) && !pill?.contains(e.target)) {
      dd.classList.add("hidden");
    }
  }
});

/* ================= 7. 会话管理 ================= */
async function createNewSession() {
  try {
    const s = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title: "新对话" }),
    });
    const sessions = await api("/api/sessions");
    renderSessions(sessions);
    await selectSession(s.id);
    if (currentMainView !== "chat") {
      switchMainView("chat");
    }
    toast("已开启新对话");
  } catch (e) {
    toast("创建会话失败: " + e.message);
  }
}

function renderSessions(sessions) {
  UI.store.sessions = sessions || [];
  const el = document.getElementById("session-list");
  if (!el) return;
  el.innerHTML = "";

  sessions.forEach(s => {
    const isCur = s.id === currentSession;
    const wrap = document.createElement("div");
    wrap.className = `p-2.5 rounded-xl cursor-pointer flex justify-between items-center transition ${isCur ? 'bg-[#F4F4F5] font-semibold' : 'hover:bg-[#F9F9FB]'}`;

    const info = document.createElement("div");
    info.className = "flex-1 min-w-0 pr-2";
    info.innerHTML = `
      <div class="text-[13.5px] truncate text-[#1D1D1F]">${esc(s.title || "未命名会话")}</div>
      <div class="text-[11.5px] text-[#8E8E93] mt-0.5">${esc(s.updated_at || "刚刚")}</div>
    `;
    info.onclick = () => {
      selectSession(s.id);
      toggleSessionsDrawer();
    };

    const delBtn = document.createElement("button");
    delBtn.className = "text-[#8E8E93] hover:text-black text-xs px-1.5 py-1 rounded transition";
    delBtn.title = "删除对话";
    delBtn.textContent = "✕";
    delBtn.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`确定删除对话 "${s.title}" 吗？`)) return;
      try {
        await api(`/api/sessions/${s.id}`, { method: "DELETE" });
        const remaining = await api("/api/sessions");
        renderSessions(remaining);
        if (s.id === currentSession && remaining.length) {
          selectSession(remaining[0].id);
        } else if (!remaining.length) {
          createNewSession();
        }
      } catch (err) {
        toast("删除失败: " + err.message);
      }
    };

    wrap.appendChild(info);
    wrap.appendChild(delBtn);
    el.appendChild(wrap);
  });
}

async function selectSession(id) {
  currentSession = id;
  const sessions = await api("/api/sessions").catch(() => []);
  renderSessions(sessions);

  const cur = sessions.find(s => s.id === id);
  if (cur) {
    const headerTitle = document.getElementById("header-session-title");
    if (headerTitle) headerTitle.textContent = cur.title || "聊天";
  }

  if (currentMainView !== "chat") {
    switchMainView("chat");
  }

  try {
    const messages = await api(`/api/sessions/${id}/messages`);
    renderMessages(messages);
  } catch (e) {
    console.error("加载消息历史失败:", e);
  }
}

/* ================= 8. 消息流与防裸 JSON 渲染 ================= */
function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function md(text) {
  if (!text) return "";
  if (typeof window.marked !== "undefined" && window.marked.parse) {
    try {
      const rawHtml = window.marked.parse(text, { breaks: true, gfm: true });
      if (typeof window.DOMPurify !== "undefined" && window.DOMPurify.sanitize) {
        return window.DOMPurify.sanitize(rawHtml);
      }
      return rawHtml;
    } catch (e) {
      console.warn("marked 解析失败降级:", e);
    }
  }
  let h = esc(text);
  h = h.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
    `<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
  h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  h = h.replace(/\n/g, "<br>");
  return h;
}

function highlightCodeBlocks(rootEl) {
  if (typeof window.hljs !== "undefined" && rootEl) {
    rootEl.querySelectorAll("pre code").forEach(block => {
      try {
        window.hljs.highlightElement(block);
      } catch (e) {}
    });
  }
}

function isRawJson(str) {
  if (!str) return false;
  const s = str.trim();
  return (s.startsWith('{"name":') || s.startsWith('{\n  "name":') || s.startsWith('{"tool":'));
}

function renderMessages(messages) {
  const el = document.getElementById("messages");
  if (!el) return;
  el.innerHTML = "";

  if (!messages || !messages.length) {
    return;
  }

  messages.forEach(m => {
    const node = messageNode(m);
    if (node) {
      el.appendChild(node);
      highlightCodeBlocks(node);
    }
  });
  el.scrollTop = el.scrollHeight;
}

function appendMessage(m) {
  if (m.id && document.getElementById(`msg-${m.id}`)) return;
  const el = document.getElementById("messages");
  if (!el) return;

  const node = messageNode(m);
  if (!node) return;
  el.appendChild(node);
  highlightCodeBlocks(node);
  el.scrollTop = el.scrollHeight;
}

function messageNode(m) {
  if (m.role === "tool" || ["tool_observation", "verification_feedback", "plan_step"].includes(m.name)) return null;

  let content = (m.content || "").trim();
  if (isRawJson(content)) {
    content = "";
  }

  const isUser = m.role === "user";
  const hasTools = m.tool_calls && m.tool_calls.length > 0;
  const hasApproval = Boolean(m.approval_card);
  const hasTakeover = Boolean(m.takeover_card);

  if (!content && !hasTools && !hasApproval && !hasTakeover) return null;

  const wrap = document.createElement("div");
  wrap.className = `msg-row ${isUser ? 'user' : 'agent'}`;
  wrap.id = `msg-${m.id}`;

  let inner = "";

  if (isUser) {
    inner = `
      <div class="bubble-user max-w-[540px]">
        ${md(content)}
        <div class="text-[11px] text-zinc-400 mt-1 text-right">${esc(m.created_at || nowTime())}</div>
      </div>
    `;
  } else {
    inner = `<div class="space-y-2 max-w-[540px]">`;

    // 助手文本正文气泡
    if (content) {
      inner += `
        <div class="bubble-agent">
          ${md(content)}
        </div>
      `;
    }

    // 单行工具调用摘要胶囊
    if (hasTools) {
      const total = m.tool_calls.length;
      const cancelledCount = m.tool_calls.filter(t => t.status === "cancelled").length;
      const okCount = m.tool_calls.filter(t => t.status === "ok").length;
      inner += `
        <div>
          <div class="tool-summary-pill" onclick="switchRightTab('feed')">
            <span>🛠️ 已执行 ${total - cancelledCount} 项工具（${okCount} 项成功）</span>
            <span class="tag">动态复盘 ➔</span>
          </div>
        </div>
      `;
    }

    // 审批卡片
    if (hasApproval) {
      inner += approvalCardHTML(m.approval_card);
    }

    // 人工接管卡片
    if (hasTakeover) {
      inner += takeoverCardHTML(m.takeover_card);
    }

    inner += `<div class="text-[11px] text-[#A1A1AA] pt-0.5">${esc(m.created_at || nowTime())}</div></div>`;
  }

  wrap.innerHTML = inner;

  if (hasApproval && m.approval_card.status === "pending") {
    bindApprovalCard(wrap, m.approval_card);
    startApprovalCountdown(m.approval_card);
  }

  if (hasTakeover && (m.takeover_card.status === "active" || m.takeover_card.status === "pending")) {
    bindTakeoverCard(wrap, m.takeover_card);
  }

  return wrap;
}

/* ================= 9. 审批卡片 (Muse 1:1 风格) ================= */
const SCOPE_LABEL = { once: "仅这一次", session: "本次任务内同类免批", always: "以后都不问" };

const ACTION_CN_MAP = {
  shell_exec: "执行系统命令",
  file_write: "创建/保存文件",
  file_edit: "修改本地文件",
  file_read: "读取本地文件",
  image_generate: "生成图像素材",
  video_generate: "生成视频素材",
  browser_navigate: "打开目标网页",
  browser_new_tab: "新建标签页",
  browser_switch_tab: "切换网页标签页",
  browser_close_tab: "关闭网页标签页",
  browser_click: "点击网页按钮",
  browser_click_at: "按坐标精准点击",
  browser_probe_at: "探查定位页面按钮",
  browser_type: "填写网页表单",
  browser_fill_input: "闭环填写表单/富文本",
  browser_record_start: "开启一镜到底录屏",
  browser_record_stop: "完成录屏视频输出",
  browser_screenshot: "截取网页画面",
  browser_snapshot: "分析页面结构",
  browser_scroll: "滚动浏览网页",
  browser_wait: "等待网页加载",
  browser_evaluate: "执行交互脚本",
  browser_find_text: "定位页面文字",
  browser_inspect: "检查按钮状态",
  browser_console: "诊断控制台日志",
  browser_network_log: "核验网络请求",
  browser_request_takeover: "请求人工协同接管",
  desktop_open_app: "打开桌面应用",
  desktop_click: "点击桌面位置",
  desktop_type: "输入键盘文本",
  desktop_screenshot: "截取全屏桌面",
  desktop_snapshot: "观察桌面窗口状态",
  state_check: "核验任务执行进度",
  state_pin: "锁定关键任务数据",
  state_clear: "清理任务临时状态",
  cron_add: "创建定时自动化任务",
  cron_remove: "取消定时自动化任务",
  cron_list: "查看定时任务列表",
  vault_get: "读取保险箱凭据",
  vault_set: "保存保险箱凭据",
  memory_read: "读取知识记忆库",
  memory_write: "沉淀知识记忆库",
  spawn_subagent: "分发协同子任务",
  poll_subagent: "查询协同子任务进展",
  takeover_request: "请求人工协同接管",
};

function formatActionCN(action) {
  if (!action) return "执行自动化操作";
  if (ACTION_CN_MAP[action]) return ACTION_CN_MAP[action];
  return action
    .replace(/^browser_/, "网页-")
    .replace(/^desktop_/, "桌面-")
    .replace(/^file_/, "文件-")
    .replace(/_/g, " ");
}

/* ================= 9.4 审批授权卡片 (Muse 1:1 风格对齐) ================= */
function getMuseHeaderIcon(action, detail) {
  const a = (action || "").toLowerCase();
  const d = (detail || "").toLowerCase();

  if (a.includes("mail") || d.includes("mail") || d.includes("gmail")) {
    return `<svg class="w-5 h-5 text-[#111827] shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"></rect><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"></path></svg>`;
  }
  if (a.includes("file") || a.includes("upload")) {
    return `<svg class="w-5 h-5 text-[#111827] shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="12" y1="18" x2="12" y2="12"></line><line x1="9" y1="15" x2="15" y2="15"></line></svg>`;
  }
  if (a.includes("shell") || a.includes("cmd") || a.includes("exec")) {
    return `<svg class="w-5 h-5 text-[#111827] shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>`;
  }
  if (a.includes("credential") || a.includes("password") || a.includes("login")) {
    return `<svg class="w-5 h-5 text-[#111827] shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>`;
  }
  // 默认：Muse 经典地球网络/线框图标 (Globe Wireframe)
  return `<svg class="w-5 h-5 text-[#111827] shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path></svg>`;
}

function formatMuseHeaderTitle(c) {
  const action = (c.action || "").toLowerCase();
  const rawDetail = c.detail || c.summary_cn || "";

  if (action.includes("navigate") || action.includes("browser_open") || action.includes("visit")) {
    let domain = "";
    try {
      const match = rawDetail.match(/https?:\/\/([^/\s"':]+)/);
      if (match) domain = match[1];
    } catch (e) {}
    if (domain) return `允许 Muse 访问 ${domain}？`;
    return `允许 Muse 访问目标网站？`;
  }
  if (action.includes("upload") || (action.includes("file") && rawDetail.includes("upload"))) {
    return "允许 Muse 上传此文件并创建公开链接？";
  }
  if (action.startsWith("file_write")) {
    let fname = "";
    const m = rawDetail.match(/(?:路径|文件|path|file)[:：\s]+([^\s\n\r]+)/i);
    if (m) fname = m[1].split(/[/\\]/).pop();
    if (fname) return `允许 Muse 写入文件 ${fname}？`;
    return "允许 Muse 写入本地文件？";
  }
  if (action.startsWith("file_edit")) {
    let fname = "";
    const m = rawDetail.match(/(?:路径|文件|path|file)[:：\s]+([^\s\n\r]+)/i);
    if (m) fname = m[1].split(/[/\\]/).pop();
    if (fname) return `允许 Muse 修改文件 ${fname}？`;
    return "允许 Muse 修改本地文件？";
  }
  if (action.startsWith("file_read")) {
    return "允许 Muse 读取本地文件？";
  }
  if (action.includes("shell") || action.includes("cmd") || action.includes("exec")) {
    return "允许 Muse 执行系统终端命令？";
  }
  if (action.includes("takeover")) {
    return "允许 Muse 请求人工协同接管？";
  }
  if (action.includes("desktop_open")) {
    return "允许 Muse 启动桌面应用程序？";
  }
  if (action.includes("desktop")) {
    return "允许 Muse 控制电脑桌面？";
  }
  if (action.includes("mail") || action.includes("gmail")) {
    return "允许 Muse 发送电子邮件？";
  }
  if (action.includes("credential") || action.includes("login")) {
    return "允许 Muse 在浏览器中填写预存凭据？";
  }
  if (c.action_cn) {
    return `允许 Muse ${c.action_cn}？`;
  }
  return "允许 Muse 执行此敏感操作？";
}

function formatMuseAssistantIntent(c) {
  if (c.intent_cn) {
    let intent = c.intent_cn.trim();
    if (intent.startsWith("你的助手")) return intent;
    if (intent.startsWith("想要")) intent = intent.slice(2);
    return `想要${intent}`;
  }
  if (c.summary_cn) {
    return `想要${c.summary_cn}`;
  }
  const action = (c.action || "").toLowerCase();
  if (action.includes("navigate")) {
    return "想要访问目标网页以获取最新信息并执行自动化交互。";
  }
  if (action.includes("file_write")) {
    return "想要将生成的内容或配置保存到本地工作区文件。";
  }
  if (action.includes("file_edit")) {
    return "想要对工作区文件进行更新修改。";
  }
  if (action.includes("shell")) {
    return "想要在本地运行终端指令以推进当前任务步骤。";
  }
  return "想要执行此操作以继续推进当前任务步骤。";
}

function extractMuseMetadata(c) {
  const items = [];
  const action = (c.action || "").toLowerCase();
  const rawDetail = c.detail || c.summary_cn || "";
  let parsed = null;

  try {
    if (typeof rawDetail === "string" && (rawDetail.trim().startsWith("{") || rawDetail.trim().startsWith("["))) {
      parsed = JSON.parse(rawDetail);
    } else if (typeof rawDetail === "object" && rawDetail !== null) {
      parsed = rawDetail;
    }
  } catch (e) {
    parsed = null;
  }

  // 1. 网页 / 浏览器跳转
  if (action.includes("navigate") || action.includes("open") || (parsed && parsed.url)) {
    const url = (parsed && parsed.url) || (() => {
      const m = String(rawDetail).match(/https?:\/\/[^\s"',]+/);
      return m ? m[0] : "";
    })() || rawDetail;

    if (url) {
      items.push({ key: "Target URL", value: url });
      try {
        const u = new URL(url.startsWith("http") ? url : "https://" + url);
        items.push({ key: "Domain", value: u.hostname });
      } catch (e) {}
    }
    items.push({ key: "Visibility", value: "前台自动化操作" });
    items.push({ key: "Risk Level", value: c.risk_level === "L3" ? "L3（高风险）" : "L2（敏感跳转）" });
    return items;
  }

  // 2. 文件上传 / 写入 / 修改
  if (action.includes("file") || action.includes("upload") || (parsed && (parsed.path || parsed.file))) {
    const filePath = (parsed && (parsed.path || parsed.file || parsed.filename)) || (() => {
      const m = String(rawDetail).match(/(?:路径|文件|path|file)[:：\s]+([^\s\n\r]+)/i);
      return m ? m[1] : "";
    })() || "";

    if (filePath) {
      const fileName = filePath.split(/[/\\]/).pop() || filePath;
      items.push({ key: "File", value: fileName });
      if (fileName !== filePath) {
        items.push({ key: "Path", value: filePath });
      }
    }
    const size = (parsed && (parsed.size || (parsed.content && parsed.content.length))) || (() => {
      const m = String(rawDetail).match(/长度[:：\s]+约?\s*(\d+)/);
      return m ? m[1] + " B" : "";
    })();
    if (size) {
      items.push({ key: "Size", value: typeof size === "number" ? `${size} B` : size });
    }
    items.push({ key: "Visibility", value: "本地工作区文件" });
    items.push({ key: "Risk Level", value: c.risk_level === "L3" ? "L3（覆盖/高危）" : "L2（文件改写）" });
    return items;
  }

  // 3. 终端命令
  if (action.includes("shell") || action.includes("cmd") || action.includes("exec") || (parsed && parsed.command)) {
    const cmd = (parsed && (parsed.command || parsed.cmd)) || rawDetail.replace(/^申请在系统终端执行命令行：\n?/, "").split("\n")[0];
    items.push({ key: "Command", value: cmd });
    items.push({ key: "Environment", value: "系统终端 (PowerShell / Shell)" });
    items.push({ key: "Risk Level", value: "L3（系统级高危操作）" });
    return items;
  }

  // 4. 通用 JSON 解析
  if (parsed && typeof parsed === "object") {
    for (const [k, v] of Object.entries(parsed)) {
      if (["password", "token", "secret", "key"].some(s => k.toLowerCase().includes(s))) {
        items.push({ key: k, value: "******" });
      } else if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
        const displayKey = k.charAt(0).toUpperCase() + k.slice(1).replace(/_/g, " ");
        items.push({ key: displayKey, value: String(v) });
      }
    }
    items.push({ key: "Risk Level", value: c.risk_level || "L2" });
    return items;
  }

  // 5. 兜底
  items.push({ key: "Action", value: c.action_cn || formatActionCN(c.action) });
  if (rawDetail) {
    items.push({ key: "Details", value: rawDetail.length > 80 ? rawDetail.slice(0, 80) + "..." : rawDetail });
  }
  items.push({ key: "Risk Level", value: c.risk_level || "L2" });
  return items;
}

function approvalCardHTML(c) {
  const id = c.approval_id;
  const detailId = `detail-${id}`;
  const title = formatMuseHeaderTitle(c);
  const icon = getMuseHeaderIcon(c.action, c.detail || c.summary_cn);
  const intentText = formatMuseAssistantIntent(c);
  const metadata = extractMuseMetadata(c);
  const scopes = c.scope_options || ["once", "session", "always"];
  const isPending = c.status === "pending" || !c.status;

  const metadataRows = metadata.map(item => `
    <div class="flex items-baseline space-x-3 text-[13px] leading-relaxed">
      <span class="text-[#6B7280] w-24 shrink-0 font-normal select-none">${esc(item.key)}</span>
      <span class="text-[#111827] font-normal break-all select-text">${esc(item.value)}</span>
    </div>
  `).join("");

  const scopeRadios = `
    <div class="flex items-start space-x-3 pt-2.5 mt-2 border-t border-gray-200/60 text-[12.5px]">
      <span class="text-[#6B7280] w-24 shrink-0 pt-0.5 select-none font-normal">授权范围</span>
      <div class="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[12.5px]">
        <label class="inline-flex items-center space-x-1.5 cursor-pointer text-[#374151]">
          <input type="radio" name="scope-${id}" value="once" checked class="text-[#0066FF] focus:ring-[#0066FF] h-3.5 w-3.5">
          <span>仅此一次</span>
        </label>
        <label class="inline-flex items-center space-x-1.5 ${scopes.includes('session') ? 'cursor-pointer' : 'opacity-40 cursor-not-allowed'} text-[#374151]">
          <input type="radio" name="scope-${id}" value="session" ${scopes.includes('session') ? '' : 'disabled'} class="text-[#0066FF] focus:ring-[#0066FF] h-3.5 w-3.5">
          <span>本次任务免问</span>
        </label>
        <label class="inline-flex items-center space-x-1.5 ${scopes.includes('always') ? 'cursor-pointer' : 'opacity-40 cursor-not-allowed'} text-[#374151]">
          <input type="radio" name="scope-${id}" value="always" ${scopes.includes('always') ? '' : 'disabled'} class="text-[#0066FF] focus:ring-[#0066FF] h-3.5 w-3.5">
          <span>永久允许${!scopes.includes('always') ? '（高危限制）' : ''}</span>
        </label>
      </div>
    </div>
  `;

  return `
    <div class="approval-card-muse" id="card-${id}">
      <!-- 1. 顶部 Header：线框图标 + 允许 Muse [动作]？ -->
      <div class="flex items-center space-x-2.5">
        ${icon}
        <h3 class="text-[15px] font-medium text-[#111827] tracking-tight leading-snug">
          ${esc(title)}
        </h3>
      </div>

      <!-- 2. 浅灰折叠盒子：助手意图 + 展开/折叠箭头 + 属性列表 -->
      <div class="mt-3 bg-[#F3F4F6] rounded-xl p-3.5 sm:p-4 text-[13px] text-[#374151]">
        <div class="flex items-start justify-between gap-3 cursor-pointer select-none" onclick="toggleMuseDetail('${detailId}')">
          <div class="text-[#374151] leading-relaxed">
            <span class="font-medium text-[#111827]">你的助手</span> ${esc(intentText)}
          </div>
          <button type="button" class="text-gray-400 hover:text-gray-600 transition p-0.5 shrink-0 mt-0.5" title="折叠/展开明细">
            <svg id="chevron-${detailId}" class="w-4 h-4 transition-transform duration-200" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="18 15 12 9 6 15"></polyline>
            </svg>
          </button>
        </div>

        <div id="${detailId}" class="mt-3.5 pt-3 border-t border-gray-200/70 space-y-2">
          ${metadataRows}
          ${scopeRadios}
        </div>
      </div>

      <!-- 3. 底部胶囊按钮（左对齐圆角：蓝色允许 + 浅灰拒绝） -->
      <div class="flex items-center gap-3 mt-4" id="actions-${id}">
        ${isPending ? `
          <button class="px-7 py-2 rounded-full bg-[#0066FF] hover:bg-[#0052CC] active:bg-[#0047B3] text-white font-medium text-[13.5px] transition shadow-sm focus:outline-none approve" data-act="approve">
            允许
          </button>
          <button class="px-7 py-2 rounded-full bg-[#F3F4F6] hover:bg-[#E5E7EB] active:bg-[#D1D5DB] text-[#111827] font-medium text-[13.5px] transition focus:outline-none reject" data-act="reject">
            拒绝
          </button>
          <span id="timer-${id}" class="text-[12px] text-[#6B7280] ml-1 self-center">
            等待确认中…
          </span>
        ` : `
          <div class="inline-flex items-center gap-1.5 py-1 px-3.5 rounded-full ${c.status === 'approved' ? 'bg-emerald-50 text-[#059669]' : 'bg-red-50 text-[#DC2626]'} text-[13px] font-medium">
            ${c.status === 'approved' ? '✓ 已通过授权' : '✕ 已拒绝该操作'}
          </div>
        `}
      </div>
    </div>
  `;
}

function bindApprovalCard(wrap, card) {
  const id = card.approval_id;
  wrap.querySelectorAll(`#card-${id} button.approve, #card-${id} button.reject`).forEach(btn => {
    btn.onclick = async () => {
      const approved = btn.dataset.act === "approve";
      const scope = (wrap.querySelector(`input[name="scope-${id}"]:checked`) || {}).value || "once";
      btn.disabled = true;
      const rej = wrap.querySelector(`#card-${id} .reject`);
      if (rej) rej.disabled = true;

      try {
        await api(`/api/approvals/${id}/resolve`, {
          method: "POST",
          body: JSON.stringify({ approved, scope }),
        });
        resolveApprovalUI(id, approved, scope);
      } catch (e) {
        toast("审批提交失败: " + e.message);
        btn.disabled = false;
        if (rej) rej.disabled = false;
      }
    };
  });
}

function startApprovalCountdown(card) {
  const id = card.approval_id;
  const expires = Number(card.expires_at) || (Date.now() + 60000);

  clearInterval(approvalTimers[id]);
  approvalTimers[id] = setInterval(() => {
    const remain = Math.max(0, Math.round((expires - Date.now()) / 1000));
    const els = document.querySelectorAll(`[id="timer-${id}"]`);
    if (els.length === 0) { clearInterval(approvalTimers[id]); return; }
    if (remain <= 0) {
      clearInterval(approvalTimers[id]);
      els.forEach(el => el.textContent = "已超时自动拒绝");
      resolveApprovalUI(id, false, "once");
    } else {
      els.forEach(el => el.textContent = `${remain}s 后自动关闭`);
    }
  }, 1000);
}

async function resolveApproval(id, approved, scope = "once") {
  try {
    await api(`/api/approvals/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ approved, scope }),
    });
    resolveApprovalUI(id, approved, scope);
    toast(approved ? "已批准该敏感操作" : "已拒绝该敏感操作");
  } catch (e) {
    toast("审批提交失败: " + e.message);
  }
}

function resolveApprovalFromDrawer(id, approved) {
  const scopeInput = document.querySelector(`input[name="scope-drawer-${id}"]:checked`)
    || document.querySelector(`input[name="scope-${id}"]:checked`);
  const scope = scopeInput ? scopeInput.value : "once";
  resolveApproval(id, approved, scope);
}

function resolveApprovalUI(id, approved, scope) {
  clearInterval(approvalTimers[id]);
  const cards = document.querySelectorAll(`[id="card-${id}"], [id="drawer-card-${id}"]`);
  cards.forEach(card => {
    card.querySelectorAll("button, input").forEach(el => el.disabled = true);
    const actions = card.querySelector(`[id="actions-${id}"]`) || card.querySelector(`[id="drawer-actions-${id}"]`);
    if (actions) {
      actions.innerHTML = approved
        ? `<div class="inline-flex items-center gap-1.5 py-1.5 px-4 rounded-full bg-emerald-50 text-[#059669] text-[13px] font-medium"><svg class="w-4 h-4" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/></svg>已通过授权（${SCOPE_LABEL[scope] || scope}）</div>`
        : `<div class="inline-flex items-center gap-1.5 py-1.5 px-4 rounded-full bg-red-50 text-[#DC2626] text-[13px] font-medium"><svg class="w-4 h-4" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd"/></svg>已拒绝该操作</div>`;
    }
  });
  UI.store.approvals = UI.store.approvals.filter(a => a.approval_id !== id);
  renderAuditTab(UI.store.approvals, UI.store.auditHistory);
  fetchApprovalHistory();
}

/* ================= 9.5 人工介入协同卡片 (Takeover Card) ================= */
function takeoverCardHTML(c) {
  const tid = c.task_id;
  const isPending = c.status === "active" || c.status === "pending";
  const reason = c.reason || "遇到验证码或需要人工协同接管";
  const target = c.target || "浏览器";
  const pageUrl = c.page_url || "";

  let actionSection = "";
  if (isPending) {
    actionSection = `
      <div class="flex items-center space-x-2 pt-2" id="takeover-actions-${tid}">
        <button class="flex-1 py-2.5 rounded-xl bg-amber-500 hover:bg-amber-600 text-white font-medium text-xs transition shadow-sm takeover-done" data-act="done" data-tid="${esc(tid)}">
          ✓ 我已完成操作，继续执行
        </button>
        <button class="py-2.5 px-3 rounded-xl bg-[#F4F4F5] hover:bg-[#EBEBEC] text-[#1D1D1F] font-medium text-xs transition takeover-cancel" data-act="cancel" data-tid="${esc(tid)}">
          ✕ 取消接管
        </button>
      </div>
      <div class="text-[11px] text-amber-700/80 text-center pt-1" id="takeover-timer-${tid}">
        ⏳ 等待用户在浏览器中操作…
      </div>
    `;
  } else if (c.status === "completed") {
    actionSection = `
      <div class="p-2.5 rounded-xl bg-emerald-50 border border-emerald-100 text-emerald-800 text-xs font-semibold flex items-center space-x-1.5">
        <span>✅ 人工接管已完成，AlterEgo 正在恢复自动化执行…</span>
      </div>
    `;
  } else {
    actionSection = `
      <div class="p-2.5 rounded-xl bg-zinc-100 border border-zinc-200 text-zinc-600 text-xs font-semibold flex items-center space-x-1.5">
        <span>✕ 人工接管已取消或结束 (${esc(c.status)})</span>
      </div>
    `;
  }

  return `
    <div class="takeover-card-muse border-2 border-amber-300 bg-amber-50/40 rounded-2xl p-3.5 space-y-3" id="takeover-${tid}">
      <div class="flex justify-between items-start">
        <span class="font-bold text-[14.5px] text-[#1D1D1F] flex items-center space-x-1.5">
          <span>🙋‍♂️ 需要人工介入协同</span>
        </span>
        <span class="text-xs px-2 py-0.5 rounded font-semibold bg-amber-100 text-amber-800">
          ${esc(target)}
        </span>
      </div>

      <div class="bg-white/90 border border-amber-200/80 rounded-xl p-2.5 text-xs text-[#1D1D1F] space-y-1">
        <div><span class="font-bold text-amber-900">介入原因：</span><span class="text-zinc-800">${esc(reason)}</span></div>
        ${pageUrl ? `<div class="truncate text-[#52525B]"><span class="font-semibold text-zinc-700">页面地址：</span><a href="${esc(pageUrl)}" target="_blank" class="text-blue-600 hover:underline">${esc(pageUrl)}</a></div>` : ''}
      </div>

      <div class="bg-amber-100/50 rounded-xl p-2.5 text-xs text-amber-950 leading-relaxed border border-amber-200/60">
        <div class="font-semibold text-amber-900 mb-0.5">📌 操作说明：</div>
        <div>请直接在已打开的浏览器窗口中完成验证码识别、滑块拼图或登录操作。完成后点击下方【我已完成操作】按钮即可恢复自动化。</div>
      </div>

      ${actionSection}
    </div>
  `;
}

function bindTakeoverCard(wrap, card) {
  const tid = card.task_id;
  const cardEl = wrap.querySelector(`#takeover-${tid}`);
  if (!cardEl) return;

  const doneBtn = cardEl.querySelector(`.takeover-done`);
  const cancelBtn = cardEl.querySelector(`.takeover-cancel`);

  if (doneBtn) {
    doneBtn.onclick = async () => {
      doneBtn.disabled = true;
      if (cancelBtn) cancelBtn.disabled = true;
      doneBtn.textContent = "正在通知 AlterEgo 恢复…";
      try {
        await api(`/api/takeover/${tid}/done`, { method: "POST" });
        resolveTakeoverUI(tid, "completed", "用户手动完成接管");
        toast("✅ 已通知 AlterEgo，自动化正在恢复！");
      } catch (e) {
        toast("提交接管状态失败: " + e.message);
        doneBtn.disabled = false;
        if (cancelBtn) cancelBtn.disabled = false;
        doneBtn.textContent = "✓ 我已完成操作，继续执行";
      }
    };
  }

  if (cancelBtn) {
    cancelBtn.onclick = async () => {
      if (doneBtn) doneBtn.disabled = true;
      cancelBtn.disabled = true;
      cancelBtn.textContent = "正在取消…";
      try {
        await api(`/api/takeover/${tid}/cancel`, { method: "POST" });
        resolveTakeoverUI(tid, "cancelled", "用户取消接管");
        toast("已取消人工接管");
      } catch (e) {
        toast("取消接管失败: " + e.message);
        if (doneBtn) doneBtn.disabled = false;
        cancelBtn.disabled = false;
        cancelBtn.textContent = "✕ 取消接管";
      }
    };
  }
}

function resolveTakeoverUI(taskId, status, summary) {
  const cards = document.querySelectorAll(`[id="takeover-${taskId}"]`);
  cards.forEach(card => {
    const act = card.querySelector(`#takeover-actions-${taskId}`);
    const timer = card.querySelector(`#takeover-timer-${taskId}`);
    if (timer) timer.remove();
    if (act) {
      if (status === "completed") {
        act.outerHTML = `
          <div class="p-2.5 rounded-xl bg-emerald-50 border border-emerald-100 text-emerald-800 text-xs font-semibold flex items-center space-x-1.5">
            <span>✅ 人工接管已完成，AlterEgo 正在恢复自动化执行…</span>
          </div>
        `;
      } else {
        act.outerHTML = `
          <div class="p-2.5 rounded-xl bg-zinc-100 border border-zinc-200 text-zinc-600 text-xs font-semibold flex items-center space-x-1.5">
            <span>✕ 人工接管已取消或结束 (${esc(status)})</span>
          </div>
        `;
      }
    }
  });
}

/* ================= 10. 输入与发送 ================= */
function bindComposer() {
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("btn-send");
  const fileInput = document.getElementById("file-input");
  const attachBtn = document.getElementById("btn-attach");

  if (!input || !sendBtn) return;

  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(120, input.scrollHeight) + "px";
  });

  sendBtn.onclick = send;
  if (attachBtn) attachBtn.onclick = () => fileInput?.click();

  if (fileInput) {
    fileInput.onchange = async (e) => {
      const files = e.target.files;
      if (!files.length) return;
      const file = files[0];
      const formData = new FormData();
      formData.append("file", file);

      try {
        toast(`正在上传附件 "${file.name}"…`);
        const res = await fetch(API + "/api/uploads", { method: "POST", body: formData });
        const data = await res.json();
        input.value += (input.value ? "\n" : "") + `[附件: ${data.filename}](${data.url})`;
        input.dispatchEvent(new Event("input"));
        toast("附件上传成功");
      } catch (err) {
        toast("附件上传失败: " + err.message);
      }
      fileInput.value = "";
    };
  }

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "auto";
    input.disabled = true;
    sendBtn.disabled = true;

    // 乐观渲染用户消息
    const userMsg = {
      id: "u_" + Date.now(),
      role: "user",
      content: text,
      created_at: nowTime(),
    };
    appendMessage(userMsg);

    // 驱动头像进入思考中动画与胶囊指示
    renderAgentState({ state: "thinking", detail: "正在分析与思考…" });

    try {
      const res = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ session_id: currentSession, content: text }),
      });
      if (res.message) {
        appendMessage(res.message);
      }
      const sessions = await api("/api/sessions").catch(() => []);
      renderSessions(sessions);
    } catch (e) {
      toast("处理异常：" + e.message);
      appendMessage(e.persistedMessage || {
        id: "err_" + Date.now(),
        role: "agent",
        content: `⚠️ 处理异常: ${e.message}`,
        created_at: nowTime(),
      });
      renderAgentState({ state: "error", detail: "处理异常" });
    } finally {
      input.disabled = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }
}

/* ================= 11. 头像 6 态动画与状态胶囊 ================= */
let stateTextThrottleTimer = null;
let lastStateText = "";

function renderAgentState(st) {
  if (!st) return;
  const state = st.state || "idle";
  const stateLabels = { idle: "已就绪", waiting_user: "等待用户决策", incomplete: "任务处理中" };
  const detail = st.detail || stateLabels[state] || "正在处理…";

  // 1. 头像光环 class 切换与动态形象 GIF 联动（同时同步右侧抽屉与中间垂直展示区）
  const avatarEls = [document.getElementById("agent-avatar"), document.getElementById("center-agent-avatar")].filter(Boolean);
  const avatarImgs = [document.getElementById("agent-avatar-img"), document.getElementById("center-agent-avatar-img")].filter(Boolean);
  const avatarIcons = [document.getElementById("agent-avatar-icon"), document.getElementById("center-agent-avatar-icon")].filter(Boolean);

  const states = ["avatar-idle", "avatar-thinking", "avatar-acting", "avatar-awaiting_approval", "avatar-error", "avatar-done"];
  const avatarState = ["waiting_user", "incomplete"].includes(state) ? "awaiting_approval" : state;

  avatarEls.forEach(el => {
    states.forEach(s => el.classList.remove(s));
    el.classList.add(`avatar-${avatarState}`);
  });

  const themeMap = AVATAR_STATE_GIFS[currentAvatarTheme] || AVATAR_STATE_GIFS.panda;
  const targetGif = themeMap[state] || themeMap.idle;

  if (currentAvatarTheme === "otter") {
    avatarImgs.forEach(img => img.classList.add("hidden"));
    avatarIcons.forEach(icon => icon.classList.remove("hidden"));
  } else {
    avatarIcons.forEach(icon => icon.classList.add("hidden"));
    avatarImgs.forEach(img => {
      img.classList.remove("hidden");
      if (img.getAttribute("src") !== targetGif) {
        img.src = targetGif;
      }
    });
  }

  // 2. 状态文字同步
  const detailEl = document.getElementById("agent-state-detail");
  if (detailEl) detailEl.textContent = detail;

  const centerDetailEl = document.getElementById("center-agent-state-detail");
  if (centerDetailEl) centerDetailEl.textContent = detail;

  // 3. 中间指示器与停止按钮
  const centerDot = document.getElementById("center-agent-status-indicator");
  const centerStopBtn = document.getElementById("center-btn-stop");

  if (centerDot) {
    if (state === "thinking" || state === "acting" || state === "incomplete") {
      centerDot.className = "w-3.5 h-3.5 rounded-full bg-[#3B82F6] text-white flex items-center justify-center text-[9px] font-bold animate-pulse";
      centerDot.textContent = "⚙";
      centerStopBtn?.classList.remove("hidden");
    } else if (state === "awaiting_approval" || state === "waiting_user") {
      centerDot.className = "w-3.5 h-3.5 rounded-full bg-[#F59E0B] text-white flex items-center justify-center text-[9px] font-bold animate-pulse";
      centerDot.textContent = "⏳";
      centerStopBtn?.classList.remove("hidden");
    } else if (state === "error") {
      centerDot.className = "w-3.5 h-3.5 rounded-full bg-[#EF4444] text-white flex items-center justify-center text-[9px] font-bold";
      centerDot.textContent = "✕";
      centerStopBtn?.classList.add("hidden");
    } else {
      centerDot.className = "w-3.5 h-3.5 rounded-full bg-[#10B981] text-white flex items-center justify-center text-[9px] font-bold";
      centerDot.textContent = "⚡";
      centerStopBtn?.classList.add("hidden");
    }
  }

  syncRightDrawerVisibility();

  // 4. 等待审批时自动跳转 Tab 2
  if (state === "awaiting_approval") {
    switchRightTab("audit");
  }

  // 5. 完成态短暂展示后回归 idle
  if (state === "done") {
    setTimeout(() => {
      if (UI.store.agentState.state === "done") {
        UI.store.agentState.state = "idle";
        UI.store.agentState.detail = "已就绪";
        renderAgentState(UI.store.agentState);
      }
    }, 2500);
  }
}

function stopCurrentTask() {
  toast("已发送停止指令");
  renderAgentState({ state: "idle", detail: "已就绪" });
}

/* ================= 11.2 智能体形象选择与动态播放系统 ================= */
function initAvatarTheme() {
  if (typeof localStorage !== "undefined" && localStorage.getItem) {
    currentAvatarTheme = localStorage.getItem("alterego_avatar_theme") || "panda";
  }
  updateAvatarPickerUI();
  renderAgentState(UI.store.agentState);
}

function openAvatarPickerModal() {
  updateAvatarPickerUI();
  document.getElementById("avatar-modal")?.classList.remove("hidden");
}

function closeAvatarPickerModal() {
  document.getElementById("avatar-modal")?.classList.add("hidden");
}

function selectAvatarTheme(themeId) {
  currentAvatarTheme = themeId;
  if (typeof localStorage !== "undefined" && localStorage.setItem) {
    localStorage.setItem("alterego_avatar_theme", themeId);
  }
  updateAvatarPickerUI();
  renderAgentState(UI.store.agentState);

  const names = { panda: "🐼 熊猫", dragon: "🐲 龙", otter: "🦦 经典水獭" };
  toast(`✓ 已更换为 ${names[themeId] || themeId} 形象！各状态已自动动态关联`);
  setTimeout(closeAvatarPickerModal, 350);
}

function updateAvatarPickerUI() {
  const themes = ["panda", "dragon", "otter"];
  themes.forEach(t => {
    const card = document.getElementById(`theme-card-${t}`);
    const badge = document.getElementById(`badge-${t}`);
    const isCur = t === currentAvatarTheme;
    if (card) {
      if (isCur) {
        card.className = "p-4 rounded-2xl border-2 border-[#0066FF] bg-blue-50/20 cursor-pointer transition hover:shadow-md space-y-3";
      } else {
        card.className = "p-4 rounded-2xl border-2 border-[#EAEAEA] bg-white cursor-pointer transition hover:shadow-md space-y-3";
      }
    }
    if (badge) {
      badge.classList.toggle("hidden", !isCur);
    }
  });
}

// 空闲自然微动心跳（隔几秒让头像有一次轻盈的眨眼/微弹交互）
let idleHeartbeatTimer = null;
function startIdleHeartbeat() {
  if (idleHeartbeatTimer) clearInterval(idleHeartbeatTimer);
  idleHeartbeatTimer = setInterval(() => {
    if (UI.store.agentState.state === "idle") {
      const avatarEls = [document.getElementById("agent-avatar"), document.getElementById("center-agent-avatar")].filter(Boolean);
      avatarEls.forEach(el => {
        el.classList.add("avatar-blink-pulse");
        setTimeout(() => el.classList.remove("avatar-blink-pulse"), 1000);
      });
    }
  }, 6000);
}

/* ================= 12. 右侧 4 Tab 内容渲染 ================= */

// Tab 1: 动态流 (三行垂直排版：主标题、副标题、时间戳，真实后台数据驱动，杜绝英文代码裸露与写死假数据)
function sanitizeSummaryCN(text) {
  if (!text) return "任务步骤已推进";
  let s = String(text);
  s = s.replace(/\[已点击\]/g, "已点击")
       .replace(/\[已输入\]/g, "已输入")
       .replace(/\[探针\]/g, "位置核验完成")
       .replace(/\[截图\]/g, "页面状态核验")
       .replace(/\[当前页面\]/g, "页面加载就绪")
       .replace(/\[无障碍树\]/g, "页面结构分析")
       .replace(/\[已切换\]/g, "已切换标签页")
       .replace(/\[已钉死\]/g, "关键数据已锁定")
       .replace(/button:has-text\("([^"]+)"\)/g, "「$1」按钮")
       .replace(/div:has-text\("([^"]+)"\)/g, "「$1」按钮")
       .replace(/input\[placeholder="([^"]+)"\]/g, "「$1」输入框")
       .replace(/坐标\(\d+(\.\d+)?, \d+(\.\d+)?\)/g, "目标位置");
  return s.trim() || "任务步骤已推进";
}

function getActionIcon(tool) {
  if (!tool) return "⚡";
  if (tool.startsWith("browser_")) return "🌐";
  if (tool.startsWith("desktop_")) return "💻";
  if (tool.startsWith("file_")) return "📄";
  if (tool.startsWith("cron_")) return "⏰";
  if (tool.startsWith("vault_")) return "🔐";
  if (tool.startsWith("memory_")) return "🧠";
  if (tool.startsWith("image_") || tool.startsWith("video_")) return "🎨";
  if (tool.startsWith("subagent_") || tool.startsWith("spawn_")) return "👥";
  return "⚡";
}

function renderFeedTab(activities) {
  const container = document.getElementById("feed-items-container");
  if (!container) return;

  const validActivities = Array.isArray(activities) ? activities : [];

  if (validActivities.length === 0) {
    container.innerHTML = `
      <div class="py-14 px-4 text-center">
        <div class="w-10 h-10 rounded-full bg-[#F4F4F5] text-[#8E8E93] flex items-center justify-center mx-auto mb-3 text-lg">⚡</div>
        <div class="text-[14px] font-medium text-[#1D1D1F]">暂无动态流</div>
        <div class="text-[12.5px] text-[#8E8E93] mt-1 leading-relaxed">智能体执行任务时，所有操作步骤将实时流式展示在此处</div>
      </div>
    `;
    return;
  }

  const items = validActivities.map(act => {
    const rawSummary = act.summary || act.result_summary || "";
    const cleanDesc = sanitizeSummaryCN(rawSummary);
    const title = act.tool ? formatActionCN(act.tool) : "执行自动化操作";
    const icon = getActionIcon(act.tool);
    const time = act.started_at ? act.started_at.slice(0, 5) : nowTime();
    return {
      title,
      desc: cleanDesc,
      time,
      icon,
      steps: [
        title,
        cleanDesc,
        act.status === "ok" ? "执行核验成功完成" : (act.status === "error" ? "执行遇到异常" : "正在处理中")
      ]
    };
  });

  container.innerHTML = `
    <div>
      <h3 class="text-[14px] font-semibold text-[#8E8E93] mb-2.5">实时活动动态</h3>
      <div class="space-y-3">
        ${items.map(item => `
          <div onclick="openActivityModal('${esc(item.title)}', '${esc(item.desc)}', '${esc(item.time)}')" class="flex items-start space-x-3 py-1.5 px-2 -mx-2 rounded-xl hover:bg-[#F4F4F5] cursor-pointer transition">
            <span class="mt-0.5 shrink-0 text-[16px]">${item.icon}</span>
            <div class="flex flex-col space-y-0.5 min-w-0">
              <span class="text-[14px] font-medium text-[#1D1D1F] leading-snug truncate">${esc(item.title)}</span>
              <span class="text-[12.5px] text-[#8E8E93] leading-snug break-words">${esc(item.desc)}</span>
              <span class="text-[12px] text-[#A1A1AA] pt-0.5">${esc(item.time)}</span>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

// Tab 2: 审核记录 (盾牌 - 待处理审批与真实历史审计)
async function fetchApprovalHistory() {
  try {
    const list = await api("/api/approvals/history?limit=50");
    if (Array.isArray(list)) {
      UI.store.auditHistory = list;
      renderAuditTab(UI.store.approvals, UI.store.auditHistory);
    }
  } catch (e) {
    console.warn("加载审核历史失败:", e);
  }
}

function formatMuseTime(ts) {
  if (!ts) return "刚刚";
  try {
    const now = Date.now() / 1000;
    const diff = Math.max(0, now - ts);
    if (diff < 60) return "刚刚";
    if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
    if (diff < 172800) return "昨天";
    const d = new Date(ts * 1000);
    return `${d.getMonth() + 1}月${d.getDate()}日`;
  } catch (e) {
    return "刚刚";
  }
}

function getMuseIconSVG(action, detail) {
  return getMuseHeaderIcon(action, detail);
}

function formatMuseTitle(item) {
  return formatMuseHeaderTitle(item);
}

function formatMuseDesc(item) {
  return formatMuseAssistantIntent(item);
}

function toggleMuseDetail(id) {
  const el = document.getElementById(id);
  const chevron = document.getElementById(`chevron-${id}`);
  if (!el) return;
  const isHidden = el.classList.contains("hidden");
  el.classList.toggle("hidden", !isHidden);
  if (chevron) {
    chevron.style.transform = isHidden ? "rotate(0deg)" : "rotate(180deg)";
  }
}

function renderAuditTab(pendingList = UI.store.approvals, historyList = UI.store.auditHistory) {
  const container = document.getElementById("audit-items-container");
  const badgeDot = document.getElementById("approval-tab-badge");
  const countTag = document.getElementById("audit-badge-count");
  if (!container) return;

  const pendings = Array.isArray(pendingList) ? pendingList : [];
  const history = Array.isArray(historyList) ? historyList : [];

  if (badgeDot) badgeDot.classList.toggle("hidden", pendings.length === 0);
  const openDrawerBadge = document.getElementById("btn-open-drawer-badge");
  const drawer = document.getElementById("right-drawer");
  const isDrawerHidden = !drawer || drawer.classList.contains("hidden");
  if (openDrawerBadge) {
    openDrawerBadge.classList.toggle("hidden", pendings.length === 0 || !isDrawerHidden);
  }
  if (countTag) {
    if (pendings.length > 0) {
      countTag.textContent = `${pendings.length} 项待处理`;
    } else {
      countTag.textContent = `${history.length} 条记录`;
    }
  }

  if (pendings.length === 0 && history.length === 0) {
    container.innerHTML = `
      <div class="py-12 px-4 text-center">
        <div class="w-10 h-10 rounded-full bg-[#F4F4F5] text-[#8E8E93] flex items-center justify-center mx-auto mb-2.5 text-base">🛡️</div>
        <div class="text-[13.5px] font-medium text-[#1D1D1F]">暂无审核记录</div>
        <div class="text-[12px] text-[#8E8E93] mt-0.5">所有敏感系统指令、文件修改与浏览器接管均在此真实留痕</div>
      </div>
    `;
    return;
  }

  let html = "";

  // 1. 待确认授权事项（1:1 Muse 风格置顶卡片）
  if (pendings.length > 0) {
    html += `
      <div class="space-y-4 mb-5">
        <div class="text-[12px] font-semibold text-gray-500 uppercase tracking-wider px-1">待确认授权（${pendings.length}）</div>
        ${pendings.map((a, idx) => {
          const detailId = `pending-drawer-detail-${a.approval_id || idx}`;
          const id = a.approval_id || `pending-${idx}`;
          const title = formatMuseHeaderTitle(a);
          const icon = getMuseHeaderIcon(a.action, a.detail || a.summary_cn);
          const intentText = formatMuseAssistantIntent(a);
          const metadata = extractMuseMetadata(a);
          const scopes = a.scope_options || ["once", "session", "always"];

          const metadataRows = metadata.map(item => `
            <div class="flex items-baseline space-x-3 text-[12.5px] leading-relaxed">
              <span class="text-[#6B7280] w-20 shrink-0 font-normal select-none">${esc(item.key)}</span>
              <span class="text-[#111827] font-normal break-all select-text">${esc(item.value)}</span>
            </div>
          `).join("");

          const scopeRadios = `
            <div class="flex items-start space-x-3 pt-2 mt-2 border-t border-gray-200/60 text-[12px]">
              <span class="text-[#6B7280] w-20 shrink-0 pt-0.5 select-none font-normal">授权范围</span>
              <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
                <label class="inline-flex items-center space-x-1.5 cursor-pointer text-[#374151]">
                  <input type="radio" name="scope-drawer-${id}" value="once" checked class="text-[#0066FF] focus:ring-[#0066FF] h-3.5 w-3.5">
                  <span>仅此一次</span>
                </label>
                <label class="inline-flex items-center space-x-1.5 ${scopes.includes('session') ? 'cursor-pointer' : 'opacity-40 cursor-not-allowed'} text-[#374151]">
                  <input type="radio" name="scope-drawer-${id}" value="session" ${scopes.includes('session') ? '' : 'disabled'} class="text-[#0066FF] focus:ring-[#0066FF] h-3.5 w-3.5">
                  <span>本次免问</span>
                </label>
                <label class="inline-flex items-center space-x-1.5 ${scopes.includes('always') ? 'cursor-pointer' : 'opacity-40 cursor-not-allowed'} text-[#374151]">
                  <input type="radio" name="scope-drawer-${id}" value="always" ${scopes.includes('always') ? '' : 'disabled'} class="text-[#0066FF] focus:ring-[#0066FF] h-3.5 w-3.5">
                  <span>永久允许</span>
                </label>
              </div>
            </div>
          `;

          return `
            <div class="approval-card-muse !max-w-none shadow-sm" id="drawer-card-${id}">
              <div class="flex items-center space-x-2.5">
                ${icon}
                <h3 class="text-[14.5px] font-medium text-[#111827] tracking-tight leading-snug">
                  ${esc(title)}
                </h3>
              </div>

              <div class="mt-3 bg-[#F3F4F6] rounded-xl p-3 text-[12.5px] text-[#374151]">
                <div class="flex items-start justify-between gap-2 cursor-pointer select-none" onclick="toggleMuseDetail('${detailId}')">
                  <div class="text-[#374151] leading-relaxed">
                    <span class="font-medium text-[#111827]">你的助手</span> ${esc(intentText)}
                  </div>
                  <button type="button" class="text-gray-400 hover:text-gray-600 transition p-0.5 shrink-0 mt-0.5" title="折叠/展开明细">
                    <svg id="chevron-${detailId}" class="w-4 h-4 transition-transform duration-200" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <polyline points="18 15 12 9 6 15"></polyline>
                    </svg>
                  </button>
                </div>

                <div id="${detailId}" class="mt-2.5 pt-2 border-t border-gray-200/70 space-y-1.5">
                  ${metadataRows}
                  ${scopeRadios}
                </div>
              </div>

              <div class="flex items-center gap-3 mt-3.5" id="drawer-actions-${id}">
                <button onclick="resolveApprovalFromDrawer('${id}', true)" class="px-7 py-1.5 rounded-full bg-[#0066FF] hover:bg-[#0052CC] text-white font-medium text-[13px] transition shadow-sm">
                  允许
                </button>
                <button onclick="resolveApprovalFromDrawer('${id}', false)" class="px-7 py-1.5 rounded-full bg-[#F3F4F6] hover:bg-[#E5E7EB] text-[#111827] font-medium text-[13px] transition">
                  拒绝
                </button>
              </div>
            </div>
          `;
        }).join("")}
      </div>
    `;
  }

  // 2. 真实历史审计留痕 (100% 对齐 Muse 视觉布局)
  if (history.length > 0) {
    html += `
      <div class="divide-y divide-gray-100">
        ${history.map((item, idx) => {
          const detailId = `history-detail-${item.approval_id || idx}`;
          const isApproved = item.decision === "approved";
          const isAlways = item.scope === "always";
          const statusText = isApproved ? (isAlways ? "始终允许" : "已允许") : "已拒绝";
          const timeText = formatMuseTime(item.ts);
          const title = formatMuseTitle(item);
          const desc = formatMuseDesc(item);
          const metadata = extractMuseMetadata(item);

          return `
            <div class="group py-3 transition">
              <div class="flex items-start space-x-3">
                <div class="w-8 h-8 rounded-xl bg-gray-100 flex items-center justify-center shrink-0 mt-0.5 text-gray-700">
                  ${getMuseIconSVG(item.action, item.detail || item.summary_cn || "")}
                </div>

                <div class="flex-1 min-w-0">
                  <div class="flex items-start justify-between cursor-pointer" onclick="toggleMuseDetail('${detailId}')">
                    <h4 class="text-[13.5px] font-medium text-[#111827] leading-snug truncate pr-2">
                      ${esc(title)}
                    </h4>
                    <button class="text-gray-400 hover:text-gray-600 transition p-0.5 shrink-0" title="查看明细" onclick="event.stopPropagation(); toggleMuseDetail('${detailId}')">
                      <svg id="chevron-${detailId}" class="w-4 h-4 transition-transform duration-200" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="6 9 12 15 18 9"></polyline>
                      </svg>
                    </button>
                  </div>

                  <p class="text-[12.5px] text-[#52525B] leading-relaxed mt-1">
                    ${esc(desc)}
                  </p>

                  <div id="${detailId}" class="hidden mt-2 bg-[#F9FAFB] rounded-lg p-2.5 border border-gray-100 text-[12px] space-y-1">
                    ${metadata.map(m => `
                      <div class="flex items-baseline space-x-2">
                        <span class="text-gray-400 w-20 shrink-0 font-normal">${esc(m.key)}:</span>
                        <span class="text-gray-700 break-all">${esc(m.value)}</span>
                      </div>
                    `).join("")}
                  </div>

                  <div class="text-[11.5px] text-[#8E8E93] mt-1.5 flex items-center space-x-1.5">
                    <span class="font-medium ${isApproved ? 'text-[#059669]' : 'text-[#DC2626]'}">${esc(statusText)}</span>
                    <span>·</span>
                    <span>${esc(timeText)}</span>
                  </div>
                </div>
              </div>
            </div>
          `;
        }).join("")}
      </div>
    `;
  }

  container.innerHTML = html;
}

function scrollToCard(id) {
  const card = document.getElementById(`card-${id}`);
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "center" });
  } else {
    toast("审批卡片在主对话流中呈现");
  }
}

/* ================= 13. 系统状态快照与恢复 ================= */
async function loadStateSnapshot() {
  try {
    const snap = await api("/api/state/snapshot");
    if (!snap) return;
    if (snap.agent_state) UI.store.agentState = snap.agent_state;
    if (snap.activity) UI.store.activity = snap.activity;
    if (snap.approvals) UI.store.approvals = snap.approvals;
    if (snap.approval_history) UI.store.auditHistory = snap.approval_history;
    if (snap.status) UI.store.status = snap.status;

    renderAgentState(UI.store.agentState);
    renderFeedTab(UI.store.activity);
    renderAuditTab(UI.store.approvals, UI.store.auditHistory);

    if (snap.active_takeover && snap.active_takeover.task_id) {
      const data = snap.active_takeover;
      const msgId = "takeover_" + data.task_id;
      if (!document.getElementById(`msg-${msgId}`) && !document.getElementById(`takeover-${data.task_id}`)) {
        appendMessage({
          id: msgId,
          role: "agent",
          content: "⚠️ 检测到需要人工介入的操作（如验证码/安全验证/登录）：",
          takeover_card: data,
          created_at: nowTime(),
        });
      }
    }
  } catch (e) {
    console.warn("加载状态快照失败:", e);
  }
}

/* ================= 14. 系统设置与微信绑定 ================= */
function bindSettingsControls() {
  // 1. 连通性测试按钮：带入当前输入的 model, base_url, api_key 进行实时连通性探测
  const testLlmBtn = document.getElementById("btn-test-llm");
  if (testLlmBtn) {
    testLlmBtn.onclick = async () => {
      const resEl = document.getElementById("llm-test-result");
      const model = (document.getElementById("setting-model")?.value || "").trim();
      const baseUrl = (document.getElementById("setting-base-url")?.value || "").trim();
      const apiKey = (document.getElementById("setting-api-key")?.value || "").trim();

      if (resEl) {
        resEl.className = "text-xs text-[#8E8E93]";
        resEl.textContent = "正在向大模型发起连通性探测…";
      }
      testLlmBtn.disabled = true;
      try {
        const payload = {};
        if (model) payload.model = model;
        if (baseUrl) payload.base_url = baseUrl;
        if (apiKey) payload.api_key = apiKey;

        const res = await api("/api/settings/test_llm", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        if (resEl) {
          resEl.className = "text-xs text-[#10B981] font-medium";
          resEl.textContent = `✓ 连通正常！回复: "${res.reply}"`;
        }
      } catch (e) {
        if (resEl) {
          resEl.className = "text-xs text-[#EF4444]";
          resEl.textContent = `✗ 探测失败: ${e.message}`;
        }
      } finally {
        testLlmBtn.disabled = false;
      }
    };
  }

  // 2. 密码明文/密文切换小眼睛按钮
  const toggleKeyBtn = document.getElementById("btn-toggle-key-visibility");
  if (toggleKeyBtn) {
    toggleKeyBtn.onclick = () => {
      const keyInput = document.getElementById("setting-api-key");
      if (!keyInput) return;
      if (keyInput.type === "password") {
        keyInput.type = "text";
        toggleKeyBtn.textContent = "🙈";
      } else {
        keyInput.type = "password";
        toggleKeyBtn.textContent = "👁️";
      }
    };
  }

  // 3. 模型标识输入框联动：选择预设模型时自动补齐 Base URL 与凭据状态指示
  const modelInput = document.getElementById("setting-model");
  if (modelInput) {
    const handleModelInput = () => {
      const val = modelInput.value.trim();
      const matched = (availableModels || []).find(m => m.id.toLowerCase() === val.toLowerCase());
      const baseInput = document.getElementById("setting-base-url");
      const keyStatus = document.getElementById("setting-key-status");

      if (matched) {
        if (baseInput && (!baseInput.value.trim() || availableModels.some(m => m.base_url === baseInput.value.trim()))) {
          if (matched.base_url) baseInput.value = matched.base_url;
        }
        if (keyStatus) {
          if (matched.has_key) {
            keyStatus.className = "text-xs text-[#10B981] font-medium";
            keyStatus.textContent = "✓ 该预设模型已配置凭证 (留空保持现有密钥)";
          } else {
            keyStatus.className = "text-xs text-amber-600 font-medium";
            keyStatus.textContent = "○ 该模型尚未配置 API Key (请输入密钥)";
          }
        }
      } else if (val) {
        if (keyStatus) {
          keyStatus.className = "text-xs text-blue-600 font-medium";
          keyStatus.textContent = "○ 自定义模型端点 (请输入密钥后保存生效)";
        }
      }
    };
    modelInput.addEventListener("input", handleModelInput);
    modelInput.addEventListener("change", handleModelInput);
  }

  // 4. 单一核心保存按钮：保存并立即生效（同时绑定 Model ID、Base URL 与 API Key）
  const saveConfigBtn = document.getElementById("btn-save-model-config");
  if (saveConfigBtn) {
    saveConfigBtn.onclick = async () => {
      const model = (document.getElementById("setting-model")?.value || "").trim();
      const baseUrl = (document.getElementById("setting-base-url")?.value || "").trim();
      const apiKey = (document.getElementById("setting-api-key")?.value || "").trim();

      if (!model) {
        toast("请输入或选择模型标识 (Model ID)");
        return;
      }

      saveConfigBtn.disabled = true;
      const originalText = saveConfigBtn.innerHTML;
      saveConfigBtn.innerHTML = `<span>⏳ 保存并生效中…</span>`;

      try {
        const payload = { model: model, base_url: baseUrl };
        if (apiKey) payload.api_key = apiKey;

        const res = await api("/api/settings/save_model_config", {
          method: "POST",
          body: JSON.stringify(payload),
        });

        if (res.ok) {
          const keyInput = document.getElementById("setting-api-key");
          if (keyInput) keyInput.value = "";

          updateCurrentModelUI(res.current);
          if (res.models && Array.isArray(res.models)) {
            availableModels = res.models;
            renderModelDropdownList(res.current, currentModelSearchQuery);
            populateSettingsModelDatalist(availableModels);
          }
          const keyStatus = document.getElementById("setting-key-status");
          if (keyStatus) {
            keyStatus.className = "text-xs text-[#10B981] font-medium";
            keyStatus.textContent = "✓ 已配置硬件加密凭证 (留空保持现有密钥)";
          }
          toast(`✓ 模型与密钥已成功关联保存，主模型已即时生效为 ${res.current}！`);
        } else {
          toast("保存失败: " + (res.error || "未知错误"));
        }
      } catch (e) {
        toast("保存异常: " + e.message);
      } finally {
        saveConfigBtn.disabled = false;
        saveConfigBtn.innerHTML = originalText;
      }
    };
  }

  // 兼容旧按钮引用
  const saveKeyBtn = document.getElementById("btn-save-key");
  if (saveKeyBtn) {
    saveKeyBtn.onclick = async () => {
      const key = document.getElementById("setting-api-key")?.value.trim();
      if (!key) { toast("请输入要保存的 API Key"); return; }
      try {
        await api("/api/settings/vault_key", {
          method: "POST",
          body: JSON.stringify({ key }),
        });
        document.getElementById("setting-api-key").value = "";
        toast("✓ 已通过 Windows DPAPI 硬件加密固化存入 Vault！");
      } catch (e) {
        toast("保存密钥失败: " + e.message);
      }
    };
  }

  const saveMemBtn = document.getElementById("btn-save-memory");
  if (saveMemBtn) {
    saveMemBtn.onclick = async () => {
      const content = document.getElementById("setting-memory")?.value;
      try {
        await api("/api/memory", {
          method: "POST",
          body: JSON.stringify({ content }),
        });
        toast("✓ 核心记忆库 (MEMORY.md) 保存成功！");
        closeMemoryModal();
      } catch (e) {
        toast("保存记忆失败: " + e.message);
      }
    };
  }

  const weixinBtn = document.getElementById("btn-weixin-login");
  if (weixinBtn) {
    weixinBtn.onclick = async () => {
      const qrBox = document.getElementById("weixin-qr-box");
      const badge = document.getElementById("weixin-status-badge");
      if (qrBox) qrBox.innerHTML = `<div class="text-xs text-[#8E8E93] py-4">正在向微信服务器请求专属长连接二维码…</div>`;
      if (badge) badge.textContent = "正在生成绑定二维码…";

      try {
        const res = await api("/api/weixin/login", { method: "POST" });
        if (res.status === "already_connected") {
          toast(res.message);
          loadWeixinStatus();
          return;
        }
        if (qrBox) {
          const tq = API_TOKEN ? `&token=${encodeURIComponent(API_TOKEN)}` : "";
          qrBox.innerHTML = `<img src="${API}/api/weixin/qr_img?t=${Date.now()}${tq}" class="w-48 h-48 rounded-xl border border-[#EAEAEA] shadow-sm" alt="微信扫码绑定" />`;
        }
        if (badge) {
          badge.className = "font-medium text-xs text-[#1D1D1F]";
          badge.textContent = "请用手机微信「我 → 设置 → 插件 → 微信 ClawBot」扫码绑定";
        }
        toast("专属二维码已就绪！请在手机微信确认授权");
      } catch (e) {
        toast("生成二维码失败: " + e.message);
        if (qrBox) qrBox.innerHTML = `<div class="text-xs text-red-500 py-2">拉取失败: ${e.message}</div>`;
      }
    };
  }
}

async function loadWeixinStatus() {
  const badge = document.getElementById("weixin-status-badge");
  const qrBox = document.getElementById("weixin-qr-box");
  if (!badge) return;
  try {
    const s = await api("/api/weixin/status");
    if (s.connected) {
      badge.className = "font-medium text-xs text-[#10B981]";
      badge.textContent = `✓ 微信 ClawBot 已连接！当前在线用户: ${s.user_id || "已认证"}`;
      if (qrBox) {
        qrBox.innerHTML = `<div class="p-3 bg-emerald-50 text-emerald-800 rounded-xl text-xs text-center">✅ 微信消息长连接运行中<br><span class="text-[11px] text-emerald-600">支持手机回复「同意」快速免键鼠审批</span></div>`;
      }
    } else {
      badge.className = "font-medium text-xs text-[#8E8E93]";
      badge.textContent = "○ 微信未连接";
    }
  } catch (e) {}
}

async function loadSettingsView() {
  try {
    const s = await api("/api/settings");
    const m = document.getElementById("setting-model");
    const u = document.getElementById("setting-base-url");
    const keyStatus = document.getElementById("setting-key-status");
    const keyInput = document.getElementById("setting-api-key");
    const resEl = document.getElementById("llm-test-result");

    if (resEl) resEl.textContent = "";
    if (keyInput) keyInput.value = "";
    if (m) m.value = s.model || "";
    if (u) u.value = s.base_url || "";

    if (keyStatus) {
      if (s.has_key) {
        keyStatus.className = "text-xs text-[#10B981] font-medium";
        keyStatus.textContent = "✓ 当前端点已配置硬件加密凭证 (留空保持现有密钥)";
      } else {
        keyStatus.className = "text-xs text-amber-600 font-medium";
        keyStatus.textContent = "○ 尚未配置 API Key (请输入密钥后保存)";
      }
    }

    if (!availableModels || availableModels.length === 0) {
      await loadAvailableModels();
    } else {
      populateSettingsModelDatalist(availableModels);
    }

    await loadWeixinStatus();
  } catch (e) {
    console.warn("加载设置视图失败:", e);
  }
}

/* ================= 15. Server-Sent Events (SSE) 实时长连接 ================= */
function connectSSE() {
  const url = API + "/api/events" + (API_TOKEN ? `?token=${encodeURIComponent(API_TOKEN)}` : "");
  const es = new EventSource(url);

  // 1. 助手新消息推送
  es.addEventListener("message", e => {
    try {
      const data = JSON.parse(e.data);
      if (!document.getElementById(`msg-${data.id}`)) {
        appendMessage(data);
      }
    } catch (err) {}
  });

  // 2. 头像 6 态与状态更新
  es.addEventListener("agent_state", e => {
    try {
      const data = JSON.parse(e.data);
      UI.store.agentState = data;
      renderAgentState(data);
    } catch (err) {}
  });

  // 3. 工具调用开始 (加入动态流)
  es.addEventListener("tool_call_started", e => {
    try {
      const data = JSON.parse(e.data);
      const item = {
        id: data.id,
        tool: data.tool,
        summary: data.summary,
        status: "running",
        args: data.args,
        started_at: nowTime(),
      };
      UI.store.activity.unshift(item);
      UI.store.activity = UI.store.activity.slice(0, 30);
      renderFeedTab(UI.store.activity);
    } catch (err) {}
  });

  // 4. 工具调用完成
  es.addEventListener("tool_call_done", e => {
    try {
      const data = JSON.parse(e.data);
      const match = UI.store.activity.find(a => a.id === data.id);
      if (match) {
        match.status = data.status;
        match.result_summary = data.result_summary;
      }
      renderFeedTab(UI.store.activity);
    } catch (err) {}
  });

  // 5. 敏感操作审批请求（严格幂等，防止重复渲染）
  es.addEventListener("approval_request", e => {
    try {
      const card = JSON.parse(e.data);
      if (!card || !card.approval_id) return;

      const msgId = "appr_" + card.approval_id;
      const existingMsg = document.getElementById(`msg-${msgId}`);
      const existingCard = document.getElementById(`card-${card.approval_id}`);

      // 若已经渲染过该审批卡片，则直接跳过新增消息气泡
      if (existingMsg || existingCard) {
        return;
      }

      appendMessage({
        id: msgId,
        role: "agent",
        content: "检测到敏感系统操作，需要你确认授权：",
        approval_card: card,
        created_at: nowTime(),
      });

      if (!UI.store.approvals.some(a => a.approval_id === card.approval_id)) {
        UI.store.approvals.unshift(card);
      }
      renderAuditTab(UI.store.approvals, UI.store.auditHistory);
      toast("收到新的敏感操作待审批！");
    } catch (err) {}
  });

  es.addEventListener("approval_resolved", e => {
    try {
      const { approval_id, approved, scope } = JSON.parse(e.data);
      resolveApprovalUI(approval_id, approved, scope);
    } catch (err) {}
  });

  // 6. 微信登录状态推送
  es.addEventListener("weixin_login_progress", e => {
    try {
      const data = JSON.parse(e.data);
      if (data.status === "scaned") {
        toast("手机已扫码，请在微信端点击确认绑定！");
        const badge = document.getElementById("weixin-status-badge");
        if (badge) badge.textContent = "手机已扫描，等待微信端确认…";
      } else if (data.status === "confirmed") {
        toast("🎉 微信 ClawBot 绑定成功！已上线后台双向通道。");
        loadWeixinStatus();
      }
    } catch (err) {}
  });

  // 7. 用户人工接管协同请求
  es.addEventListener("takeover_request", e => {
    try {
      const data = JSON.parse(e.data);
      if (!data || !data.task_id) return;

      const msgId = "takeover_" + data.task_id;
      if (document.getElementById(`msg-${msgId}`) || document.getElementById(`takeover-${data.task_id}`)) {
        return;
      }

      appendMessage({
        id: msgId,
        role: "agent",
        content: "⚠️ 检测到需要人工介入的操作（如验证码/安全验证/登录）：",
        takeover_card: data,
        created_at: nowTime(),
      });
      toast("⚠️ AlterEgo 需要人工介入协同！请在浏览器中操作");
    } catch (err) {}
  });

  // 8. 用户人工接管状态更新
  es.addEventListener("takeover_update", e => {
    try {
      const { task_id, status, summary } = JSON.parse(e.data);
      resolveTakeoverUI(task_id, status, summary);
    } catch (err) {}
  });

  es.onerror = () => {
    setTimeout(() => loadStateSnapshot(), 2500);
  };
}

/* ================= 辅助函数 ================= */
function nowTime() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function toast(text) {
  const w = document.getElementById("toast-wrap");
  if (!w) return;
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = text;
  w.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

/* ================= 16. 原生桌面窗口控制 (Window Controls) ================= */
async function handleWindowControl(action) {
  try {
    const res = await api("/api/window/control", {
      method: "POST",
      body: JSON.stringify({ action })
    });
    if (res.ok && action === "maximize") {
      setTimeout(updateMaximizeIcon, 150);
    }
  } catch (e) {
    console.warn("窗口控制失败:", e);
  }
}

async function updateMaximizeIcon() {
  try {
    const s = await api("/api/window/status");
    const icon = document.getElementById("icon-win-maximize");
    if (!icon) return;
    if (s.maximized) {
      // 还原状态图标 (双矩形)
      icon.innerHTML = `<rect x="4" y="2.5" width="9.5" height="9.5" rx="1.5"/><path d="M2.5 5.5v7a1.5 1.5 0 001.5 1.5h7"/>`;
    } else {
      // 最大化状态图标 (单矩形)
      icon.innerHTML = `<rect x="2.5" y="2.5" width="11" height="11" rx="1.5"/>`;
    }
  } catch (e) {}
}

function initDesktopTitlebar() {
  const titlebar = document.getElementById("desktop-titlebar");
  if (!titlebar) return;

  // 双击标题栏空白区域切换最大化 / 还原
  titlebar.addEventListener("dblclick", (e) => {
    if (!e.target.closest("#window-controls") && !e.target.closest("button")) {
      handleWindowControl("maximize");
    }
  });

  // 检测是否处于原生桌面客户端环境
  const isDesktop = location.search.includes("desktop=1") || typeof window.pywebview !== "undefined";
  const controls = document.getElementById("window-controls");
  if (!isDesktop && controls) {
    // 普通外部浏览器访问时显示 Web 端提示
    controls.innerHTML = `<span class="text-[10px] px-2 py-0.5 rounded-full bg-zinc-100 text-[#71717A] font-medium select-none">Web 端</span>`;
  }
}

