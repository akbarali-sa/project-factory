// Project Factory dashboard — overview grid + live per-slice detail view.
// Talks only to the read-only /api/* endpoints in app.py. No polling loop
// hand-rolled per view: the overview grid polls (cheap, summary-only), the
// detail view subscribes to a server-sent-events stream so N open tabs don't
// turn into N independent poll loops hammering Postgres.

const STATUS_LABEL = { done: 'DONE', retrying: 'RETRYING', parked: 'PARKED', pending: 'PENDING', current: 'RUNNING' };
const PILL_LABEL = { running: 'Running', gate: 'Gate pending', parked: 'Parked', done: 'Done', idle: 'Not started', paused: 'Paused' };

const state = {
  projects: [],
  view: 'overview',
  slug: null,
  sliceId: null,
  es: null,
  liveEs: null,
  timelineTimer: null,
  overviewTimer: null,
  healthTimer: null,
  lastPayload: null,
  seenAlerts: new Set(JSON.parse(localStorage.getItem('pf-seen-alerts') || '[]')),
  notify: localStorage.getItem('pf-notify') === '1',
  logFilter: '',
  logAutoscroll: true,
  liveLines: [],
  liveFilter: '',
  liveAutoscroll: true,
  ladder: null,
  busy: false,
  confirmArmed: null,
  armTimer: null,
  gateNoteDraft: '',
  gateByDraft: '',
  specFiles: [],
  specCurrent: null,
  specDirty: false,
};

// ---------------------------------------------------------------- utilities
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function timeAgo(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function fmtDuration(totalSeconds) {
  if (totalSeconds == null) return '—';
  const s = Math.floor(totalSeconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function localTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString();
}

// ------------------------------------------------------------------ theme
function initTheme() {
  const saved = localStorage.getItem('pf-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  updateThemeBtn();
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme')
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('pf-theme', next);
  updateThemeBtn();
}
function updateThemeBtn() {
  const cur = document.documentElement.getAttribute('data-theme')
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.getElementById('theme-btn').textContent = cur === 'dark' ? '☀️' : '🌙';
}

// ------------------------------------------------------------- notifications
function initNotify() {
  updateNotifyBtn();
}
async function toggleNotify() {
  if (!state.notify) {
    if (!('Notification' in window)) { alert('This browser does not support notifications.'); return; }
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') return;
    state.notify = true;
  } else {
    state.notify = false;
  }
  localStorage.setItem('pf-notify', state.notify ? '1' : '0');
  updateNotifyBtn();
}
function updateNotifyBtn() {
  const btn = document.getElementById('notify-btn');
  btn.classList.toggle('active', state.notify);
  btn.title = state.notify ? 'Notifications on — click to disable' : 'Enable browser notifications for gates & parked runs';
}
function maybeNotify(d) {
  if (!state.notify) return;
  const key = `${d.project}:${d.slice}`;
  const alertId = d.status_label === 'gate' ? `${key}:gate:${d.gate?.gate}`
    : d.status_label === 'parked' ? `${key}:parked:${d.parked.join(',')}` : null;
  if (!alertId || state.seenAlerts.has(alertId)) return;
  state.seenAlerts.add(alertId);
  localStorage.setItem('pf-seen-alerts', JSON.stringify([...state.seenAlerts].slice(-200)));
  if (Notification.permission === 'granted') {
    const title = d.status_label === 'gate' ? `Gate pending — ${d.project}` : `Parked — ${d.project}`;
    const body = d.status_label === 'gate' ? (d.gate?.gate || 'Approval needed') : `Escalated: ${d.parked.join(', ')}`;
    new Notification(title, { body });
  }
}

// ------------------------------------------------------------------ health
async function fetchHealth() {
  try {
    const res = await fetch('/api/health');
    renderHealth(await res.json());
  } catch (e) {
    document.getElementById('health-strip').innerHTML =
      `<div class="health-chip bad"><span class="dot"></span>health check unreachable</div>`;
  }
}
function renderHealth(d) {
  document.getElementById('health-strip').innerHTML = d.checks.map(c => `
    <div class="health-chip ${c.ok ? 'ok' : 'bad'}" title="${escapeHtml(c.detail)}">
      <span class="dot"></span>${escapeHtml(c.label)}
    </div>`).join('');
}

// ---------------------------------------------------------------- routing
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  if (h.startsWith('detail/')) {
    const [, slug, sliceId] = h.split('/');
    return { view: 'detail', slug, sliceId: decodeURIComponent(sliceId || '') };
  }
  return { view: 'overview' };
}
function goOverview() { location.hash = '#/overview'; }
function goDetail(slug, sliceId) { location.hash = `#/detail/${slug}/${encodeURIComponent(sliceId)}`; }

window.addEventListener('hashchange', () => applyRoute(parseHash()));

function applyRoute(route) {
  if (route.view === 'detail' && route.slug && route.sliceId) {
    showDetailView(route.slug, route.sliceId);
  } else {
    showOverviewView();
  }
}

// --------------------------------------------------------------- overview
function showOverviewView() {
  stopStream();
  stopTimelinePolling();
  state.view = 'overview';
  document.getElementById('overview-view').classList.remove('hidden');
  document.getElementById('detail-view').classList.add('hidden');
  document.title = 'Project Factory — Live Dashboard';
  fetchOverview();
  if (state.overviewTimer) clearInterval(state.overviewTimer);
  state.overviewTimer = setInterval(fetchOverview, 4000);
}

async function fetchOverview() {
  try {
    const res = await fetch('/api/overview');
    const data = await res.json();
    renderOverview(data);
    setConn(true);
  } catch (e) {
    setConn(false);
  }
}

function renderOverview(projects) {
  state.projects = projects;
  const el = document.getElementById('overview-grid');
  if (!projects.length) {
    el.innerHTML = '';
    document.getElementById('overview-empty').classList.remove('hidden');
    return;
  }
  document.getElementById('overview-empty').classList.add('hidden');

  const totalSlices = projects.reduce((n, p) => n + p.slices.length, 0);
  const gatesPending = projects.reduce((n, p) => n + p.slices.filter(s => s.status_label === 'gate').length, 0);
  const parkedCount = projects.reduce((n, p) => n + p.slices.filter(s => s.status_label === 'parked').length, 0);
  const totalCost = projects.reduce((n, p) => n + p.slices.reduce((m, s) => m + (s.cost_usd || 0), 0), 0);
  document.getElementById('fleet-projects').textContent = projects.length;
  document.getElementById('fleet-slices').textContent = totalSlices;
  document.getElementById('fleet-gates').textContent = gatesPending;
  document.getElementById('fleet-parked').textContent = parkedCount;
  document.getElementById('fleet-cost').textContent = '$' + totalCost.toFixed(2);

  el.innerHTML = projects.map(p => `
    <div class="project-card">
      <div class="project-card-head">
        <div class="name">${escapeHtml(p.slug)}</div>
        <div class="count">${p.slices.length} slice${p.slices.length === 1 ? '' : 's'}</div>
      </div>
      ${p.error ? `<div class="empty-note">${escapeHtml(p.error)}</div>` : p.slices.map(s => `
        <div class="slice-row" onclick="goDetail('${escapeAttr(p.slug)}','${escapeAttr(s.id)}')">
          <div class="pip ${s.status_label}"></div>
          <div class="info">
            <div class="sname">${escapeHtml(s.name)} <span style="color:var(--muted); font-weight:400;">· wave ${s.wave}</span></div>
            <div class="sdetail">${escapeHtml(s.detail || '')}</div>
            ${s.stack && (s.stack.web_up || s.stack.api_up) ? `
            <div class="mini-links" onclick="event.stopPropagation()">
              ${s.stack.web_up ? `<a href="${s.stack.web_url}" target="_blank" rel="noopener">web app ↗</a>` : ''}
              ${s.stack.web_up ? (s.stack.screens || []).map(sc =>
                `<a href="${sc.url}" target="_blank" rel="noopener">${escapeHtml(sc.path)} ↗</a>`).join('') : ''}
              ${s.stack.api_up ? `<a href="${s.stack.docs_url}" target="_blank" rel="noopener">api docs ↗</a>` : ''}
            </div>` : ''}
          </div>
          <div class="sright">
            <div class="spct">${s.status_label === 'done' ? '✓' : s.progress_pct + '%'}</div>
            <div class="spill ${s.status_label}">${PILL_LABEL[s.status_label] || s.status_label}</div>
          </div>
        </div>
      `).join('')}
    </div>
  `).join('');
}
function escapeAttr(s) { return String(s).replace(/'/g, "\\'"); }

function setConn(ok) {
  const b = document.getElementById('conn-badge');
  b.textContent = ok ? 'live' : 'disconnected';
  b.classList.toggle('live', ok);
  b.classList.toggle('off', !ok);
}

// ---------------------------------------------------------------- detail
function showDetailView(slug, sliceId) {
  if (state.overviewTimer) clearInterval(state.overviewTimer);
  state.view = 'detail';
  state.slug = slug;
  state.sliceId = sliceId;
  state.confirmArmed = null;
  state.gateNoteDraft = '';
  state.gateByDraft = '';
  document.getElementById('overview-view').classList.add('hidden');
  document.getElementById('detail-view').classList.remove('hidden');
  document.getElementById('detail-title').textContent = `${slug} / ${sliceId}`;
  populateSwitcher(slug, sliceId);
  ensureLadder();
  connectStream(slug, sliceId);
  startTimelinePolling();
  loadSpecs(slug);
}

async function ensureLadder() {
  if (state.ladder) return;
  try {
    const meta = await (await fetch('/api/meta')).json();
    state.ladder = meta.ladder;
    if (state.lastPayload) renderControlPanel(state.lastPayload);
  } catch (e) { /* control panel just won't offer the ladder dropdown */ }
}

async function populateSwitcher(slug, sliceId) {
  if (!state.projects.length) {
    try { state.projects = await (await fetch('/api/projects')).json(); } catch (e) { /* ignore */ }
  }
  const projectSelect = document.getElementById('project-select');
  const sliceSelect = document.getElementById('slice-select');
  projectSelect.innerHTML = state.projects.map(p => `<option value="${p.slug}" ${p.slug === slug ? 'selected' : ''}>${p.slug}</option>`).join('');
  const proj = state.projects.find(p => p.slug === slug);
  if (proj) {
    sliceSelect.innerHTML = proj.slices.map(s =>
      `<option value="${s.id}" ${s.id === sliceId ? 'selected' : ''}>${s.name}${s.completed ? ' ✓' : ''} (wave ${s.wave})</option>`
    ).join('');
  }
}
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('project-select').addEventListener('change', (e) => {
    const proj = state.projects.find(p => p.slug === e.target.value);
    const first = proj && proj.slices[0];
    if (first) goDetail(e.target.value, first.id);
  });
  document.getElementById('slice-select').addEventListener('change', (e) => {
    goDetail(state.slug, e.target.value);
  });
});

function stopStream() {
  if (state.es) { state.es.close(); state.es = null; }
  stopLiveStream();
}

function connectStream(slug, sliceId) {
  stopStream();
  const es = new EventSource(`/api/projects/${encodeURIComponent(slug)}/slices/${encodeURIComponent(sliceId)}/stream`);
  es.onmessage = (ev) => {
    setConn(true);
    const d = JSON.parse(ev.data);
    state.lastPayload = d;
    renderDetail(d);
    maybeNotify(d);
  };
  es.onerror = () => setConn(false);
  state.es = es;
  connectLiveStream(slug, sliceId);
}

function stopLiveStream() {
  if (state.liveEs) { state.liveEs.close(); state.liveEs = null; }
}

function connectLiveStream(slug, sliceId) {
  stopLiveStream();
  state.liveLines = [];
  renderLive();
  setLiveBadge('idle');
  const es = new EventSource(`/api/projects/${encodeURIComponent(slug)}/slices/${encodeURIComponent(sliceId)}/live`);
  let idleTimer = null;
  const markActive = () => {
    setLiveBadge('active');
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => setLiveBadge('idle'), 6000);
  };
  es.onmessage = (ev) => {
    const line = JSON.parse(ev.data);
    state.liveLines.push(line);
    if (state.liveLines.length > 2000) state.liveLines.shift();
    renderLive();
    markActive();
  };
  es.onerror = () => setLiveBadge('idle');
  state.liveEs = es;
}

function setLiveBadge(mode) {
  const b = document.getElementById('live-badge');
  if (!b) return;
  b.textContent = mode === 'active' ? '● streaming' : 'idle';
  b.classList.toggle('live', mode === 'active');
  b.classList.toggle('off', false);
}

const DAY_BANNER_RE = /^─+\s*\d{4}-\d{2}-\d{2}\s*─+$/;

function renderLive() {
  const filter = state.liveFilter.trim().toLowerCase();
  const lines = filter ? state.liveLines.filter(l => l.toLowerCase().includes(filter)) : state.liveLines;
  const el = document.getElementById('live-log');
  el.innerHTML = lines.map(l => {
    if (DAY_BANNER_RE.test(l.trim())) return `<div class="log-line day-banner">${escapeHtml(l.trim())}</div>`;
    return `<div class="log-line ${/FAIL|✗|failed/.test(l) ? 'fail' : ''}">${escapeHtml(l)}</div>`;
  }).join('') || '<div class="log-line">(nothing yet — populated once a `run` process is actively working this slice)</div>';
  if (state.liveAutoscroll) el.scrollTop = el.scrollHeight;
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('live-search').addEventListener('input', (e) => {
    state.liveFilter = e.target.value;
    renderLive();
  });
  document.getElementById('live-autoscroll').addEventListener('change', (e) => {
    state.liveAutoscroll = e.target.checked;
  });
  document.getElementById('live-clear').addEventListener('click', () => {
    state.liveLines = [];
    renderLive();
  });
});

function renderDetail(d) {
  const statusStat = document.getElementById('status-stat');
  const statusMeta = document.getElementById('status-meta');
  const icon = { running: '🔵', gate: '🟡', parked: '🔴', done: '🟢', idle: '⚪️', paused: '⏸️' }[d.status_label] || '';
  document.title = `${icon} ${d.project} — ${PILL_LABEL[d.status_label] || d.status_label}`;

  if (d.status_label === 'parked') {
    statusStat.textContent = 'Parked'; statusMeta.textContent = `Escalated: ${d.parked.join(', ')}`;
  } else if (d.status_label === 'gate') {
    statusStat.textContent = 'Awaiting approval'; statusMeta.textContent = d.gate.gate || 'Gate pending';
  } else if (d.status_label === 'done') {
    statusStat.textContent = 'Done'; statusMeta.textContent = 'Slice completed';
  } else if (d.status_label === 'idle') {
    statusStat.textContent = 'Idle'; statusMeta.textContent = 'No run in progress';
  } else if (d.status_label === 'paused') {
    statusStat.textContent = 'Paused'; statusMeta.textContent = `Queued before: ${d.next.join(', ') || '—'} — click Run to continue`;
  } else {
    statusStat.textContent = 'Running'; statusMeta.textContent = `Next: ${d.next.join(', ') || '—'}`;
  }

  document.getElementById('progress-stat').textContent = d.progress_pct + '%';
  document.getElementById('progress-bar').style.width = d.progress_pct + '%';

  document.getElementById('cost-stat').textContent = '$' + d.cost_usd.toFixed(2);
  document.getElementById('cost-meta').textContent = `of $${d.budget_usd} budget`;
  const costPct = Math.min(d.cost_usd / d.budget_usd * 100, 100);
  const costBar = document.getElementById('cost-bar');
  costBar.style.width = costPct + '%';
  costBar.classList.toggle('danger', costPct > 80);
  document.getElementById('cost-agents').innerHTML = Object.entries(d.cost_by_agent || {})
    .map(([k, v]) => `<span>${k}: $${v}</span>`).join('') || '<span>no spend yet</span>';

  // phases
  const phasesEl = document.getElementById('phases');
  phasesEl.innerHTML = d.phases.map(p => {
    const isCurrent = p.status === 'pending' && d.next && d.next.some(n => phaseCoversNode(p.id, n));
    const status = isCurrent ? 'current' : p.status;
    const dotText = { done: '✓', retrying: '↻', parked: '!', pending: '', current: '●' }[status] || '';
    const hasExtra = p.diagnosis || p.failure_output;
    return `<li class="phase-item">
      <div class="phase-row">
        <div class="dot ${status}">${dotText}</div>
        <div style="flex:1; min-width:0;">
          <div class="phase-label">${p.label}</div>
          ${p.detail ? `<div class="phase-detail">${escapeHtml(p.detail)}</div>` : ''}
        </div>
        ${p.attempts ? `<div class="phase-attempts">${p.attempts} attempt${p.attempts === 1 ? '' : 's'}</div>` : ''}
        <div class="phase-status">${STATUS_LABEL[status] || ''}</div>
      </div>
      ${hasExtra ? `<div class="phase-extra">
        ${p.diagnosis ? `<div class="label">Diagnosis</div><div class="diag-box">${escapeHtml(p.diagnosis)}</div>` : ''}
        ${p.failure_output ? `<div class="label" style="margin-top:6px;">Failure output</div><div class="fail-box">${escapeHtml(p.failure_output)}</div>` : ''}
      </div>` : ''}
    </li>`;
  }).join('');

  // gate
  const gateCard = document.getElementById('gate-card');
  const gatePanel = document.getElementById('gate-panel');
  if (d.gate) {
    gateCard.classList.remove('hidden');
    const rows = Object.entries(d.gate).filter(([k]) => k !== 'gate' && k !== 'ask')
      .map(([k, v]) => `<div class="kv"><b>${k}:</b> <pre>${escapeHtml(typeof v === 'string' ? v : JSON.stringify(v, null, 1))}</pre></div>`)
      .join('');
    gatePanel.innerHTML = `<h3>${escapeHtml(d.gate.gate || 'Gate')}</h3>${rows}
      ${d.gate.ask ? `<div class="kv"><b>Ask:</b> ${escapeHtml(d.gate.ask)}</div>` : ''}
      <div class="idle-note">Approve or reject from the Pipeline control panel below.</div>`;
  } else {
    gateCard.classList.add('hidden');
  }

  renderControlPanel(d);

  // generated app — the deliverable: running modules, screens, docs, repo
  const stackCard = document.getElementById('stack-card');
  if (d.stack) {
    stackCard.classList.remove('hidden');
    const st = d.stack;
    const screenLinks = (st.screens || []).map(sc => `
      <a class="stack-link ${st.web_up ? 'up' : 'down'}" href="${sc.url}" target="_blank" rel="noopener">
        <span class="dot2"></span>${escapeHtml(sc.path)}</a>`).join('');
    document.getElementById('stack-links').innerHTML = `
      <a class="stack-link ${st.web_up ? 'up' : 'down'}" href="${st.web_url}" target="_blank" rel="noopener">
        <span class="dot2"></span>Open web app</a>
      ${screenLinks}
      <a class="stack-link ${st.api_up ? 'up' : 'down'}" href="${st.api_url}" target="_blank" rel="noopener">
        <span class="dot2"></span>Open API</a>
      <a class="stack-link ${st.api_up ? 'up' : 'down'}" href="${st.docs_url}" target="_blank" rel="noopener">
        <span class="dot2"></span>API docs (Swagger)</a>`;
    document.getElementById('stack-meta').innerHTML =
      `database <code>${escapeHtml(st.db_name)}</code>` +
      (st.sign_in_as ? ` · sign in as <code>${escapeHtml(st.sign_in_as)}</code>` : '');
  } else {
    stackCard.classList.add('hidden');
  }
  const repoLine = document.getElementById('stack-repo');
  if (repoLine) {
    repoLine.innerHTML = d.repo
      ? `repo <code>${escapeHtml(d.repo.path)}</code>${d.repo.branch ? ` · branch <code>${escapeHtml(d.repo.branch)}</code>` : ''}`
      : '';
    if (d.repo && !d.stack) {
      stackCard.classList.remove('hidden');
      document.getElementById('stack-links').innerHTML = '';
    }
  }

  document.getElementById('log').innerHTML = (d.log_tail || []).map(l =>
    `<div class="log-line ${/FAIL/.test(l) ? 'fail' : ''}">${escapeHtml(l)}</div>`).join('');
}

// -------------------------------------------------------------- control
function renderControlPanel(d) {
  const el = document.getElementById('control-panel');
  if (!el) return;

  if (d.process && d.process.running) {
    el.innerHTML = `
      <div class="control-row">
        <span class="badge live">● running</span>
        <span class="control-pid">pid ${d.process.pid} · started ${timeAgo(d.process.started_at)}</span>
        ${confirmButton('stop', 'btn-danger', '■ Stop', 'Click again to confirm Stop')}
      </div>
      <div class="meta" style="margin-top:8px;">Tool calls and output are streaming into the Live CLI panel above.</div>`;
    return;
  }

  if (d.gate) {
    el.innerHTML = `
      <div class="control-form">
        <div class="meta" style="margin-bottom:8px;">Approve to resume past <b>${escapeHtml(d.gate.gate || 'this gate')}</b>, or reject with a reason.</div>
        <textarea id="gate-note" placeholder="Note (required to reject, optional to approve)" oninput="state.gateNoteDraft = this.value">${escapeHtml(state.gateNoteDraft || '')}</textarea>
        <input id="gate-by" placeholder="Your name (optional)" value="${escapeHtml(state.gateByDraft || '')}" oninput="state.gateByDraft = this.value" />
        <div class="control-row">
          ${confirmButton('gate-approve', 'btn-primary', '✓ Approve', 'Click again to confirm Approve')}
          ${confirmButton('gate-reject', 'btn-danger', '✗ Reject', 'Click again to confirm Reject')}
        </div>
      </div>`;
    return;
  }

  const ladderOptions = state.ladder
    ? Object.entries(state.ladder).map(([node, why]) => `<option value="${node}">until ${node} — ${escapeHtml(why)}</option>`).join('')
    : '';
  el.innerHTML = `
    <div class="control-row">
      <select id="until-select" ${state.confirmArmed === 'run' ? 'disabled' : ''}>
        <option value="">(full run — through the next gate, park, or end)</option>
        ${ladderOptions}
      </select>
      ${confirmButton('run', 'btn-primary', '▶ Run', 'Click again to confirm Run')}
    </div>
    ${d.status_label === 'parked' ? '<div class="meta" style="margin-top:8px;">Parked phase(s) need a manual fix in the repo before they\'ll pass — see Diagnosis above. Running now will still proceed through the rest of the pipeline.</div>' : ''}`;
}

// Two-click arm/confirm instead of window.confirm(): a native confirm()
// dialog is easy to fat-finger through, gets silently auto-dismissed by
// some automation/testing contexts, and looks jarring against the rest of
// the UI. First click arms the button (turns it into an explicit
// "click again to confirm" state, auto-disarms after a few seconds);
// second click actually fires the action.
function confirmButton(action, cls, label, confirmLabel) {
  const armed = state.confirmArmed === action;
  const disabled = state.busy ? 'disabled' : '';
  if (armed) {
    return `<button class="btn ${cls}" ${disabled} onclick="fireAction('${action}')">${confirmLabel}</button>
            <button class="btn" ${disabled} onclick="disarm()">Cancel</button>`;
  }
  return `<button class="btn ${cls}" ${disabled} onclick="arm('${action}')">${label}</button>`;
}

function arm(action) {
  state.confirmArmed = action;
  if (state.lastPayload) renderControlPanel(state.lastPayload);
  clearTimeout(state.armTimer);
  state.armTimer = setTimeout(disarm, 8000);
}

function disarm() {
  state.confirmArmed = null;
  clearTimeout(state.armTimer);
  if (state.lastPayload) renderControlPanel(state.lastPayload);
}

function fireAction(action) {
  disarm();
  if (action === 'run') return doRun();
  if (action === 'stop') return doStop();
  if (action === 'gate-approve') return doGate('approve');
  if (action === 'gate-reject') return doGate('reject');
}

async function postControl(url, body) {
  state.busy = true;
  if (state.lastPayload) renderControlPanel(state.lastPayload);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(`${res.status}: ${data.detail || 'request failed'}`);
    }
    return data;
  } catch (e) {
    alert(`network error: ${e.message}`);
  } finally {
    state.busy = false;
    if (state.lastPayload) renderControlPanel(state.lastPayload);
  }
}

function doRun() {
  const until = document.getElementById('until-select')?.value || null;
  postControl(`/api/projects/${encodeURIComponent(state.slug)}/slices/${encodeURIComponent(state.sliceId)}/run`, { until });
}

function doStop() {
  postControl(`/api/projects/${encodeURIComponent(state.slug)}/slices/${encodeURIComponent(state.sliceId)}/stop`);
}

function doGate(action) {
  // Read from state, not the DOM: arming re-renders the panel (to swap the
  // buttons into their "click again to confirm" state), which recreates
  // these fields — a plain document.getElementById().value read here would
  // see whatever the fresh re-rendered field was seeded with, not
  // necessarily what the user actually typed. The `oninput` handlers on
  // both fields keep state.gateNoteDraft/gateByDraft authoritative instead.
  const note = (state.gateNoteDraft || '').trim();
  const by = (state.gateByDraft || '').trim();
  if (action === 'reject' && !note) {
    alert('Please add a note explaining the rejection.');
    return;
  }
  state.gateNoteDraft = '';
  state.gateByDraft = '';
  postControl(`/api/projects/${encodeURIComponent(state.slug)}/slices/${encodeURIComponent(state.sliceId)}/gate`, { action, note, by });
}

const PHASE_NODE_HINTS = {
  spec: ['ingest', 'gap_detect'], gate_a: ['gate_spec'],
  clone: ['clone_starter', 'provision_db', 'baseline', 'commit_specs'],
  architect: ['architect', 'contract_lint'], gate_b: ['gate_contract'],
  migrate: ['migrate'], tests: ['write_tests'],
  api: ['implement_api', 'verify_api', 'diagnose_api', 'park_api'],
  web: ['implement_web', 'verify_web', 'diagnose_web', 'park_web'],
  e2e: ['launch_stack', 'verify_e2e', 'diagnose_e2e', 'fix_e2e', 'park_e2e'],
  finish: ['teardown', 'finish'], gate_c: ['gate_pr'],
};
function phaseCoversNode(phaseId, node) {
  return (PHASE_NODE_HINTS[phaseId] || []).includes(node);
}

// -------------------------------------------------------------- timeline
function startTimelinePolling() {
  stopTimelinePolling();
  fetchTimeline();
  state.timelineTimer = setInterval(fetchTimeline, 5000);
}
function stopTimelinePolling() {
  if (state.timelineTimer) { clearInterval(state.timelineTimer); state.timelineTimer = null; }
}
async function fetchTimeline() {
  if (!state.slug || !state.sliceId) return;
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(state.slug)}/slices/${encodeURIComponent(state.sliceId)}/timeline`);
    if (!res.ok) return;
    renderTimeline(await res.json());
  } catch (e) { /* stream badge already reflects connectivity */ }
}
function renderTimeline(t) {
  document.getElementById('started-stat').textContent = t.started_at ? localTime(t.started_at) : '—';
  document.getElementById('started-meta').textContent = t.started_at ? timeAgo(t.started_at) : 'not started yet';
  document.getElementById('elapsed-stat').textContent = fmtDuration(t.elapsed_s);
  document.getElementById('elapsed-meta').textContent = t.last_update_at ? `last update ${timeAgo(t.last_update_at)}` : '';

  state.timelineEvents = t.events || [];
  renderLog();
}
function renderLog() {
  const events = state.timelineEvents || [];
  const filter = state.logFilter.trim().toLowerCase();
  const filtered = filter ? events.filter(e => e.line.toLowerCase().includes(filter)) : events;
  const el = document.getElementById('activity-log');
  el.innerHTML = filtered.map(e => `
    <div class="log-line ${/FAIL/.test(e.line) ? 'fail' : ''} ${filter ? 'match' : ''}">
      <span class="ts">${localTime(e.ts)}</span><span>${escapeHtml(e.line)}</span>
    </div>`).join('') || '<div class="log-line">(no activity yet)</div>';
  if (state.logAutoscroll) el.scrollTop = el.scrollHeight;
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('log-search').addEventListener('input', (e) => {
    state.logFilter = e.target.value;
    renderLog();
  });
  document.getElementById('log-autoscroll').addEventListener('change', (e) => {
    state.logAutoscroll = e.target.checked;
  });
  document.getElementById('log-copy').addEventListener('click', () => {
    const text = (state.timelineEvents || []).map(e => `${e.ts}  ${e.line}`).join('\n');
    navigator.clipboard.writeText(text);
  });
});

// ---------------------------------------------------------- new project
async function loadWorkspaceNote() {
  try {
    const d = await (await fetch('/api/workspace')).json();
    document.getElementById('workspace-note').textContent = `projects are created in ${d.workspace}`;
  } catch (e) { /* cosmetic only */ }
}

function toggleNewProjectForm(show) {
  document.getElementById('new-project-card').classList.toggle('hidden', !show);
  document.getElementById('np-status').textContent = '';
}

// Picked via the native file dialog. The browser hands us the file's CONTENT
// (never its real path — browsers hide that on purpose), which is all the
// server needs: it copies the board into specs/ anyway, so only the
// filename matters for naming the copy.
let npBoardFile = null;
let npCandidates = [];

function setNpBoardFile(file) {
  npBoardFile = file;
  document.getElementById('np-board-file-name').textContent = file ? `${file.name} (${Math.round(file.content.length / 1024)} KB)` : 'no file selected';
  document.getElementById('np-board-clear').classList.toggle('hidden', !file);
  inspectBoard();
}

function onBoardFilePicked(ev) {
  const f = ev.target.files && ev.target.files[0];
  if (!f) { setNpBoardFile(null); return; }
  const reader = new FileReader();
  reader.onload = () => setNpBoardFile({ name: f.name, content: reader.result });
  reader.onerror = () => { setNpBoardFile(null); document.getElementById('np-status').textContent = 'could not read the file'; };
  reader.readAsText(f);
}

// Body for board-carrying requests, in the same priority order the create
// call uses: picked file > typed path > pasted JSON.
function boardBody() {
  const boardPath = document.getElementById('np-board-path').value.trim();
  const boardJson = document.getElementById('np-board-json').value.trim();
  if (npBoardFile) return { board_json: npBoardFile.content, board_filename: npBoardFile.name };
  if (boardPath) return { board_path: boardPath };
  if (boardJson) return { board_json: boardJson };
  return null;
}

async function inspectBoard() {
  const body = boardBody();
  npCandidates = [];
  renderSliceCandidates();
  if (!body) return;
  try {
    const res = await fetch('/api/board/slices', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      document.getElementById('np-status').textContent = `board: ${data.detail || 'could not inspect'}`;
      return;
    }
    document.getElementById('np-status').textContent = '';
    npCandidates = data.candidates || [];
    renderSliceCandidates();
  } catch (e) { /* leave form usable; create will surface the error */ }
}

function renderSliceCandidates() {
  const wrap = document.getElementById('np-slices');
  const list = document.getElementById('np-slices-list');
  const fallback = document.getElementById('np-fallback-slice');
  wrap.classList.toggle('hidden', !npCandidates.length);
  fallback.classList.toggle('hidden', !!npCandidates.length);
  list.innerHTML = npCandidates.map((c, i) => `
    <label class="slice-candidate">
      <input type="checkbox" class="np-slice-check" data-bc="${escapeAttr(c.bounded_context)}" checked />
      <span><b>${escapeHtml(c.name)}</b>
        <span class="meta">wave ${c.wave} · ${c.events.length} event${c.events.length === 1 ? '' : 's'} · ${escapeHtml(c.file_stem)}.scenarios.yaml</span>
      </span>
    </label>`).join('');
}

async function createProject() {
  const slug = document.getElementById('np-slug').value.trim();
  const boardPath = document.getElementById('np-board-path').value.trim();
  const boardJson = document.getElementById('np-board-json').value.trim();
  const sliceName = document.getElementById('np-slice-name').value.trim();
  const status = document.getElementById('np-status');
  if (!slug) { status.textContent = 'slug is required'; return; }
  if (!npBoardFile && !boardPath && !boardJson) { status.textContent = 'choose a board file, type a path, or paste board JSON'; return; }

  const body = { slug, slice_name: sliceName || null, ...boardBody() };
  body.db_reset_consent = document.getElementById('np-consent').checked;

  // Which of the board's slices to scaffold — checked boxes only. With
  // candidates on screen but none ticked, that's a mistake, not "all".
  if (npCandidates.length) {
    const picked = [...document.querySelectorAll('.np-slice-check:checked')].map(el => el.dataset.bc);
    if (!picked.length) { status.textContent = 'select at least one slice (or clear the board to use a blank template)'; return; }
    body.slices = picked;
  }

  status.textContent = 'creating…';
  try {
    const res = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { status.textContent = `${res.status}: ${data.detail || 'failed'}`; return; }
    toggleNewProjectForm(false);
    ['np-slug', 'np-board-path', 'np-board-json', 'np-slice-name'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('np-board-file').value = '';
    document.getElementById('np-consent').checked = false;
    npCandidates = [];
    setNpBoardFile(null);
    state.projects = [];  // force switcher refresh
    await fetchOverview();
    if (data.slices && data.slices.length) goDetail(data.slug, data.slices[0].id);
  } catch (e) {
    status.textContent = `network error: ${e.message}`;
  }
}

// ------------------------------------------------------------ specs editor
async function loadSpecs(slug) {
  state.specFiles = [];
  state.specCurrent = null;
  state.specDirty = false;
  renderSpecTabs();
  document.getElementById('spec-editor').value = '';
  document.getElementById('spec-save').disabled = true;
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(slug)}/specs`);
    if (!res.ok) return;
    state.specFiles = await res.json();
    renderSpecTabs();
    // Auto-open the scenarios file — it's the one humans author.
    const scen = state.specFiles.find(f => f.name.includes('.scenarios.'));
    if (scen) openSpec(scen.name);
  } catch (e) { /* specs card just stays empty */ }
}

function renderSpecTabs() {
  document.getElementById('spec-tabs').innerHTML = state.specFiles.map(f => `
    <button class="spec-tab ${f.name === state.specCurrent ? 'active' : ''}" onclick="openSpec('${escapeAttr(f.name)}')">
      ${escapeHtml(f.name)}${f.name === state.specCurrent && state.specDirty ? ' <span class="dirty">●</span>' : ''}
    </button>`).join('');
}

async function openSpec(name) {
  if (state.specDirty && !window.confirm('Discard unsaved changes to the current file?')) return;
  const res = await fetch(`/api/projects/${encodeURIComponent(state.slug)}/specs/${encodeURIComponent(name)}`);
  if (!res.ok) return;
  const d = await res.json();
  state.specCurrent = name;
  state.specDirty = false;
  document.getElementById('spec-editor').value = d.content;
  document.getElementById('spec-save').disabled = true;
  document.getElementById('spec-status').textContent = '';
  renderSpecTabs();
}

async function saveSpec() {
  if (!state.specCurrent) return;
  const statusEl = document.getElementById('spec-status');
  statusEl.textContent = 'saving…';
  const res = await fetch(`/api/projects/${encodeURIComponent(state.slug)}/specs/${encodeURIComponent(state.specCurrent)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: document.getElementById('spec-editor').value }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    statusEl.textContent = `✗ ${data.detail || 'save failed'}`;
    return;
  }
  state.specDirty = false;
  document.getElementById('spec-save').disabled = true;
  statusEl.textContent = `✓ saved (${data.bytes} bytes)`;
  renderSpecTabs();
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('new-project-btn').addEventListener('click', () => toggleNewProjectForm(true));
  document.getElementById('np-cancel').addEventListener('click', () => toggleNewProjectForm(false));
  document.getElementById('np-create').addEventListener('click', createProject);
  document.getElementById('np-board-pick').addEventListener('click', () => document.getElementById('np-board-file').click());
  document.getElementById('np-board-file').addEventListener('change', onBoardFilePicked);
  document.getElementById('np-board-clear').addEventListener('click', () => {
    document.getElementById('np-board-file').value = '';
    setNpBoardFile(null);
  });
  // Typed path / pasted JSON also drive the slice-candidate list — inspect
  // on blur rather than per keystroke.
  document.getElementById('np-board-path').addEventListener('change', inspectBoard);
  document.getElementById('np-board-json').addEventListener('change', inspectBoard);
  document.getElementById('spec-save').addEventListener('click', saveSpec);
  document.getElementById('spec-editor').addEventListener('input', () => {
    if (!state.specCurrent) return;
    state.specDirty = true;
    document.getElementById('spec-save').disabled = false;
    renderSpecTabs();
  });
});

// ------------------------------------------------------------------- boot
document.getElementById('theme-btn').addEventListener('click', toggleTheme);
document.getElementById('notify-btn').addEventListener('click', toggleNotify);
document.getElementById('overview-btn').addEventListener('click', goOverview);
loadWorkspaceNote();

initTheme();
initNotify();
fetchHealth();
state.healthTimer = setInterval(fetchHealth, 15000);
applyRoute(parseHash());
if (!location.hash) goOverview();
