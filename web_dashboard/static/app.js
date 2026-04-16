// ddkkbot Dashboard — Frontend Logic

const API = {
  status:  () => fetch('/api/status').then(r => r.json()),
  tasks:   (limit=20) => fetch(`/api/tasks?limit=${limit}`).then(r => r.json()),
  settings: () => fetch('/api/settings').then(r => r.json()),
  saveSetting: (key, value) => fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key, value})
  }).then(r => r.json()),
  start: () => fetch('/api/daemon/start', {method:'POST'}).then(r => r.json()),
  stop:  () => fetch('/api/daemon/stop',  {method:'POST'}).then(r => r.json()),
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
  if (name === 'settings') loadSettings();
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
  setInterval(refreshStatus, 5000);
});
