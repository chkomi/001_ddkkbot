// ddkkbot Dashboard — Frontend Logic

const API = {
  status:  () => fetch('/api/status').then(r => r.json()),
  authStatus: () => fetch('/api/auth-status').then(r => r.json()),
  tasks:   (limit=20) => fetch(`/api/tasks?limit=${limit}`).then(r => r.json()),
  settings: () => fetch('/api/settings').then(r => r.json()),
  saveSetting: (key, value) => fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key, value})
  }).then(r => r.json()),
  start: () => fetch('/api/daemon/start', {method:'POST'}).then(r => r.json()),
  stop:  () => fetch('/api/daemon/stop',  {method:'POST'}).then(r => r.json()),
  botsList: () => fetch('/api/bots').then(r => r.json()),
  botCreate: (body) => fetch('/api/bots', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(async r => ({ok: r.ok, status: r.status, data: await r.json().catch(()=>({}))})),
  botUpdate: (botId, body) => fetch(`/api/bots/${encodeURIComponent(botId)}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(async r => ({ok: r.ok, status: r.status, data: await r.json().catch(()=>({}))})),
  botDelete: (botId) => fetch(`/api/bots/${encodeURIComponent(botId)}`, {method:'DELETE'})
    .then(async r => ({ok: r.ok, status: r.status, data: await r.json().catch(()=>({}))})),
  setAllowedUsers: (userIds) => fetch('/api/bots/allowed-users', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user_ids: userIds})
  }).then(async r => ({ok: r.ok, status: r.status, data: await r.json().catch(()=>({}))})),
};

// ── 상태 ──────────────────────────────────────────────────────────────────────

let currentSection = 'dashboard';
let statusData = null;
let logEventSource = null;

// ── 섹션 전환 ──────────────────────────────────────────────────────────────────

function showSection(name) {
  currentSection = name;
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.section === name);
  });
  document.querySelectorAll('.section-panel').forEach(el => {
    el.style.display = el.id === `panel-${name}` ? 'flex' : 'none';
  });
  if (name === 'logs' && !logEventSource) startLogStream();
  if (name === 'tasks') loadTasks();
  if (name === 'settings') { loadSettings(); refreshAuthStatus(); }
  if (name === 'bots') loadBots();
}

// ── 상태 패널 ──────────────────────────────────────────────────────────────────

async function refreshStatus() {
  try {
    statusData = await API.status();
    renderStatus(statusData);
  } catch(e) {
    renderStatus(null);
  }
}

async function refreshAuthStatus() {
  const list = document.getElementById('auth-list');
  if (!list) return;
  try {
    const data = await API.authStatus();
    renderAuthStatus(data);
  } catch(e) {
    list.innerHTML = '<div class="empty-state">인증 상태 로딩 실패</div>';
  }
}

function renderAuthStatus(data) {
  const targets = ['auth-list', 'auth-list-settings']
    .map(id => document.getElementById(id))
    .filter(Boolean);
  if (!targets.length) return;
  const list = targets[0]; // 아래 로직의 호환성 유지용
  const providers = [
    { id: 'codex',  icon: '⚡', name: 'Codex',  data: data?.codex  },
    { id: 'claude', icon: '◈', name: 'Claude', data: data?.claude },
  ];
  const html = providers.map(p => {
    const d = p.data || {};
    let badge, badgeClass, detail = '';
    if (!d.installed) {
      badge = 'CLI 미설치';
      badgeClass = 'badge-muted';
    } else if (d.logged_in) {
      badge = '로그인';
      badgeClass = 'badge-green';
      const parts = [];
      if (d.email) parts.push(d.email);
      if (d.method) parts.push(`(${d.method})`);
      if (d.subscription_type) parts.push(`· ${d.subscription_type}`);
      if (d.subscription_until) parts.push(`~ ${d.subscription_until}`);
      detail = parts.join(' ');
      if (!detail && d.message) detail = d.message;
    } else {
      badge = '로그아웃';
      badgeClass = 'badge-muted';
      detail = d.message || '';
    }
    return `<div class="messenger-item ${d.logged_in ? 'enabled' : ''}">
      <div class="messenger-icon">${p.icon}</div>
      <div class="messenger-info">
        <div class="messenger-name">${p.name}</div>
        <div class="messenger-detail">${escHtml(detail || '-')}</div>
      </div>
      <span class="badge ${badgeClass}">${badge}</span>
    </div>`;
  }).join('');
  targets.forEach(el => { el.innerHTML = html; });
}

function renderStatus(data) {
  const dot = document.getElementById('topbar-dot');
  const label = document.getElementById('topbar-label');
  if (!data) {
    dot.className = 'status-dot stopped';
    label.textContent = '연결 오류';
    return;
  }
  dot.className = 'status-dot ' + (data.running ? 'running' : 'stopped');
  label.textContent = data.running ? '실행 중' : '중지됨';

  setEl('stat-running',   data.running ? '실행 중' : '중지됨');
  setEl('stat-ai',        data.ai_provider?.toUpperCase() || '-');
  setEl('stat-pid',       data.daemon_pid || '-');
  setEl('stat-platforms', (data.platforms || []).join(', ') || '-');

  // AI provider 하이라이트
  document.querySelectorAll('.provider-btn').forEach(btn => {
    btn.classList.toggle('selected', btn.dataset.provider === (data.ai_provider || 'codex'));
  });

  // 메신저 상태
  renderMessengers(data.platforms || []);
}

function renderMessengers(platforms) {
  const list = document.getElementById('messenger-list');
  if (!list) return;
  const messengers = [
    { id: 'telegram', icon: '✈️', name: 'Telegram', detail: 'TELEGRAM_BOT_TOKEN' },
    { id: 'discord',  icon: '🎮', name: 'Discord',  detail: 'DISCORD_BOT_TOKEN' },
    { id: 'slack',    icon: '💬', name: 'Slack',    detail: 'SLACK_BOT_TOKEN + SLACK_APP_TOKEN' },
  ];
  list.innerHTML = messengers.map(m => {
    const enabled = platforms.includes(m.id);
    return `<div class="messenger-item ${enabled ? 'enabled' : ''}">
      <div class="messenger-icon">${m.icon}</div>
      <div class="messenger-info">
        <div class="messenger-name">${m.name}</div>
        <div class="messenger-detail">${m.detail}</div>
      </div>
      <span class="badge ${enabled ? 'badge-green' : 'badge-muted'}">${enabled ? '연결됨' : '미설정'}</span>
    </div>`;
  }).join('');
}

// ── 작업 목록 ──────────────────────────────────────────────────────────────────

async function loadTasks() {
  const container = document.getElementById('task-list');
  if (!container) return;
  container.innerHTML = '<div class="empty-state">불러오는 중...</div>';
  try {
    const data = await API.tasks(20);
    const tasks = data.tasks || [];
    if (!tasks.length) {
      container.innerHTML = '<div class="empty-state">작업이 없습니다</div>';
      return;
    }
    container.innerHTML = tasks.map(t => {
      const status = t.work_status || 'active';
      const dotClass = statusDotClass(status);
      const ts = t.timestamp ? t.timestamp.slice(0,16).replace('T',' ') : '-';
      const title = escHtml(t.display_title || t.instruction || t.task_id || '(제목 없음)');
      const sub = escHtml(t.result_summary || '');
      return `<div class="task-item">
        <div class="task-status-dot ${dotClass}"></div>
        <div class="task-info">
          <div class="task-title">${title}</div>
          ${sub ? `<div class="task-meta">${sub}</div>` : ''}
          <div class="task-meta">${ts}</div>
        </div>
        <div class="task-id">${escHtml(String(t.task_id || '').slice(0,10))}</div>
      </div>`;
    }).join('');
  } catch(e) {
    container.innerHTML = '<div class="empty-state">로딩 실패</div>';
  }
}

function statusDotClass(status) {
  const s = String(status).toLowerCase();
  if (s === 'waiting')   return 'waiting';
  if (s === 'blocked')   return 'blocked';
  if (s === 'completed' || s === 'updated') return 'completed';
  return 'active';
}

// ── 봇 관리 ────────────────────────────────────────────────────────────────────

async function loadBots() {
  const list = document.getElementById('bot-list');
  if (!list) return;
  list.innerHTML = '<div class="empty-state">불러오는 중...</div>';
  try {
    const data = await API.botsList();
    renderBots(data.bots || []);
    const input = document.getElementById('allowed-users-input');
    if (input) input.value = (data.allowed_users_global || []).join(', ');
  } catch (e) {
    list.innerHTML = '<div class="empty-state">로딩 실패</div>';
  }
}

function renderBots(bots) {
  const list = document.getElementById('bot-list');
  if (!list) return;
  if (!bots.length) {
    list.innerHTML = '<div class="empty-state">등록된 봇이 없습니다. ＋ 봇 추가 버튼으로 새 봇을 등록하세요.</div>';
    return;
  }
  list.innerHTML = bots.map(b => {
    const icon = b.platform === 'discord' ? '🎮' : (b.platform === 'slack' ? '💬' : '✈️');
    const platformLabel = b.platform === 'discord' ? 'Discord' : (b.platform === 'slack' ? 'Slack' : 'Telegram');
    const subtitle = [b.bot_username && `@${b.bot_username}`, b.bot_id, b.token_masked]
      .filter(Boolean)
      .join(' · ');
    const aliasOrName = b.alias || b.bot_name || b.bot_id;
    return `<div class="bot-card ${b.active ? '' : 'inactive'}" data-bot-id="${escHtml(b.bot_id)}">
      <div class="bot-icon">${icon}</div>
      <div class="bot-info-main">
        <div class="bot-info-title">
          <span>${escHtml(aliasOrName)}</span>
          <span class="badge ${b.platform === 'discord' ? 'badge-muted' : 'badge-muted'}">${platformLabel}</span>
        </div>
        <div class="bot-info-meta">${escHtml(subtitle)}</div>
      </div>
      <div class="bot-actions">
        <label class="toggle-switch" title="${b.active ? '비활성화' : '활성화'}">
          <input type="checkbox" ${b.active ? 'checked' : ''} onchange="toggleBotActive('${escAttr(b.bot_id)}', this.checked)">
          <span class="toggle-slider"></span>
        </label>
        <button class="btn btn-ghost" title="삭제" onclick="deleteBot('${escAttr(b.bot_id)}')">🗑</button>
      </div>
    </div>`;
  }).join('');
}

let _modalPlatform = 'telegram';

function openAddBotDialog() {
  _modalPlatform = 'telegram';
  ['modal-token', 'modal-alias', 'modal-discord-allowed',
   'modal-slack-app', 'modal-slack-allowed', 'modal-slack-channels'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  document.getElementById('modal-active').checked = true;
  selectModalPlatform('telegram');
  document.getElementById('bot-modal').style.display = 'flex';
}

function closeAddBotDialog(e) {
  if (e && e.target.id !== 'bot-modal' && e.type === 'click') return;
  document.getElementById('bot-modal').style.display = 'none';
}

function selectModalPlatform(p) {
  _modalPlatform = p;
  document.querySelectorAll('#modal-platform .provider-btn').forEach(btn => {
    btn.classList.toggle('selected', btn.dataset.platform === p);
  });
  const isDiscord = (p === 'discord');
  const isSlack = (p === 'slack');
  document.getElementById('modal-discord-allowed-row').style.display = isDiscord ? '' : 'none';
  document.getElementById('modal-slack-app-row').style.display = isSlack ? '' : 'none';
  document.getElementById('modal-slack-allowed-row').style.display = isSlack ? '' : 'none';
  document.getElementById('modal-slack-channels-row').style.display = isSlack ? '' : 'none';

  const tokenInput = document.getElementById('modal-token');
  const tokenLabel = document.getElementById('modal-token-label');
  if (p === 'telegram') {
    tokenLabel.textContent = '봇 토큰';
    tokenInput.placeholder = '123456:ABC...';
  } else if (p === 'discord') {
    tokenLabel.textContent = '봇 토큰';
    tokenInput.placeholder = '디스코드 봇 토큰';
  } else if (p === 'slack') {
    tokenLabel.textContent = 'Slack Bot Token (xoxb-…)';
    tokenInput.placeholder = 'xoxb-...';
  }
}

async function submitAddBot() {
  const submitBtn = document.getElementById('modal-submit-btn');
  const body = {
    platform: _modalPlatform,
    token: document.getElementById('modal-token').value.trim(),
    alias: document.getElementById('modal-alias').value.trim(),
    active: document.getElementById('modal-active').checked,
    discord_allowed_users: document.getElementById('modal-discord-allowed').value.trim(),
    slack_app_token: document.getElementById('modal-slack-app').value.trim(),
    slack_allowed_users: document.getElementById('modal-slack-allowed').value.trim(),
    slack_allowed_channels: document.getElementById('modal-slack-channels').value.trim(),
  };
  if (!body.token) { showToast('토큰을 입력하세요', true); return; }
  if (_modalPlatform === 'slack' && !body.slack_app_token) {
    showToast('Slack App-Level Token도 필요합니다', true); return;
  }
  submitBtn.disabled = true;
  try {
    const r = await API.botCreate(body);
    if (!r.ok) {
      showToast(r.data?.detail || '추가 실패', true);
      return;
    }
    showToast('봇이 추가되었습니다');
    closeAddBotDialog();
    loadBots();
  } catch (e) {
    showToast('추가 오류', true);
  } finally {
    submitBtn.disabled = false;
  }
}

async function toggleBotActive(botId, active) {
  const r = await API.botUpdate(botId, { active });
  if (!r.ok) {
    showToast(r.data?.detail || '변경 실패', true);
    loadBots();
    return;
  }
  showToast(active ? '활성화됨' : '비활성화됨');
  loadBots();
}

async function deleteBot(botId) {
  if (!confirm(`정말 삭제하시겠습니까?\n${botId}`)) return;
  const r = await API.botDelete(botId);
  if (!r.ok) {
    showToast(r.data?.detail || '삭제 실패', true);
    return;
  }
  showToast('삭제됨');
  loadBots();
}

async function saveAllowedUsers() {
  const raw = document.getElementById('allowed-users-input').value;
  const ids = raw.split(/[,\s]+/).map(s => parseInt(s.trim(), 10)).filter(n => Number.isFinite(n) && n > 0);
  const r = await API.setAllowedUsers(ids);
  if (!r.ok) {
    showToast(r.data?.detail || '저장 실패', true);
    return;
  }
  showToast('허용 사용자 저장됨');
  loadBots();
}

function escAttr(s) {
  return String(s).replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

// ── 설정 ──────────────────────────────────────────────────────────────────────

const EDITABLE_SETTINGS = [
  'SONOLBOT_AI_PROVIDER',
  'SONOLBOT_CLAUDE_MODEL',
  'TELEGRAM_POLLING_INTERVAL',
  'DISCORD_RESPOND_WITHOUT_MENTION',
  'DAEMON_POLL_INTERVAL_SEC',
  'DAEMON_AGENT_REWRITER_ENABLED',
  'WEB_DASHBOARD_PORT',
];

async function loadSettings() {
  const container = document.getElementById('settings-form');
  if (!container) return;
  try {
    const data = await API.settings();
    const settings = data.settings || {};
    const allKeys = [...new Set([...EDITABLE_SETTINGS, ...Object.keys(settings)])].sort();
    container.innerHTML = allKeys.map(key => {
      const val = settings[key] ?? '';
      const editable = EDITABLE_SETTINGS.includes(key);
      return `<div class="form-row">
        <label class="form-label" for="setting-${key}">${escHtml(key)}</label>
        <input class="form-input" id="setting-${key}"
          data-key="${key}"
          value="${escHtml(val)}"
          ${editable ? '' : 'readonly'}
          ${editable ? `onchange="saveSetting('${key}', this.value)"` : ''}
        />
      </div>`;
    }).join('');
  } catch(e) {
    container.innerHTML = '<div class="empty-state">설정 로딩 실패</div>';
  }
}

async function saveSetting(key, value) {
  try {
    const result = await API.saveSetting(key, value);
    if (result.ok) showToast(`${key} 저장됨`);
    else showToast('저장 실패', true);
  } catch(e) {
    showToast('저장 오류', true);
  }
}

// ── AI Provider 변경 ───────────────────────────────────────────────────────────

async function setAIProvider(provider) {
  await saveSetting('SONOLBOT_AI_PROVIDER', provider);
  document.querySelectorAll('.provider-btn').forEach(btn => {
    btn.classList.toggle('selected', btn.dataset.provider === provider);
  });
  showToast(`AI 프로바이더: ${provider.toUpperCase()}`);
}

// ── 봇 시작/중지 ──────────────────────────────────────────────────────────────

async function startBot() {
  const btn = document.getElementById('btn-start');
  if (btn) btn.disabled = true;
  try {
    const result = await API.start();
    showToast(result.message || '시작됨');
    setTimeout(refreshStatus, 1500);
  } catch(e) {
    showToast('시작 실패', true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function stopBot() {
  const btn = document.getElementById('btn-stop');
  if (btn) btn.disabled = true;
  try {
    const result = await API.stop();
    showToast(result.message || '중지됨');
    setTimeout(refreshStatus, 1000);
  } catch(e) {
    showToast('중지 실패', true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── 로그 스트리밍 ──────────────────────────────────────────────────────────────

function startLogStream() {
  const viewer = document.getElementById('log-viewer');
  if (!viewer) return;
  if (logEventSource) { logEventSource.close(); logEventSource = null; }

  logEventSource = new EventSource('/api/logs/stream');
  logEventSource.onmessage = (e) => {
    try {
      const { line } = JSON.parse(e.data);
      appendLogLine(viewer, line);
    } catch {}
  };
  logEventSource.onerror = () => {
    appendLogLine(viewer, '— 로그 스트림 연결 끊김 —');
  };
}

function appendLogLine(viewer, line) {
  const div = document.createElement('div');
  div.className = 'log-line ' + classifyLog(line);
  div.textContent = line;
  viewer.appendChild(div);
  // 오래된 줄 제거 (최대 500줄)
  while (viewer.children.length > 500) viewer.removeChild(viewer.firstChild);
  viewer.scrollTop = viewer.scrollHeight;
}

function classifyLog(line) {
  if (/ERROR|FAIL|exception/i.test(line)) return 'error';
  if (/WARN/i.test(line)) return 'warn';
  if (/OK|success|started|completed/i.test(line)) return 'success';
  return 'info';
}

// ── 토스트 알림 ────────────────────────────────────────────────────────────────

function showToast(msg, isError=false) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    toast.style.cssText = `
      position:fixed; bottom:24px; right:24px; padding:10px 16px;
      border-radius:6px; font-size:12px; font-weight:600; z-index:9999;
      transition:opacity 0.3s; pointer-events:none;
    `;
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.background = isError ? '#ef4444' : '#22c55e';
  toast.style.color = '#fff';
  toast.style.opacity = '1';
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { toast.style.opacity = '0'; }, 2500);
}

// ── 유틸 ──────────────────────────────────────────────────────────────────────

function setEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '-';
}

function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── 초기화 ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // 네비게이션
  document.querySelectorAll('.nav-item').forEach(el => {
    el.addEventListener('click', () => showSection(el.dataset.section));
  });

  // 초기 섹션
  showSection('dashboard');
  refreshStatus();
  refreshAuthStatus();
  setInterval(refreshStatus, 5000);
  setInterval(refreshAuthStatus, 30000);
});
