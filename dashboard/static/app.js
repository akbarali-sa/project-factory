// Project Factory dashboard — overview grid + live per-slice detail view.
// Talks only to the read-only /api/* endpoints in app.py. No polling loop
// hand-rolled per view: the overview grid polls (cheap, summary-only), the
// detail view subscribes to a server-sent-events stream so N open tabs don't
// turn into N independent poll loops hammering Postgres.

const STATUS_LABEL = { done: 'DONE', retrying: 'RETRYING', parked: 'PARKED', pending: 'PENDING', current: 'RUNNING' };
const PILL_LABEL = { running: 'Running', gate: 'Gate pending', parked: 'Parked', done: 'Done', idle: 'Not started', paused: 'Paused', planned: 'Planned' };

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
  timelineSig: '',
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
  cardHtml: {},          // slug -> last-rendered card markup (grid diffing)
  lastOverviewText: null, // last /api/overview body — unchanged => skip render
  deleteArmed: null,
  deleting: null,
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

// /api/overview can take seconds (it probes every slice's stack), so the 4s
// poll overlaps itself and responses can arrive out of order — a stale one
// landing after a fresh one used to erase a just-created project from the
// grid until the next poll. Render monotonically: a response may paint only
// if it was issued after the last one painted ("newest wins" would be wrong
// here — with responses slower than the poll interval, every response is
// already superseded when it lands and the grid would never paint at all).
let overviewSeq = 0, overviewRendered = 0;
async function fetchOverview() {
  if (document.hidden) return;   // background tabs neither fetch nor render
  const seq = ++overviewSeq;
  try {
    const res = await fetch('/api/overview');
    const text = await res.text();
    if (seq <= overviewRendered) return;
    overviewRendered = seq;
    // Identical body => nothing to do; skipping the render (not just the
    // patch) keeps an idle grid completely still between polls.
    if (text !== state.lastOverviewText) {
      state.lastOverviewText = text;
      renderOverview(JSON.parse(text));
    }
    setConn(true);
  } catch (e) {
    if (seq > overviewRendered) setConn(false);
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

  // Fluid grid: rebuild the whole grid only when the SET or ORDER of
  // projects changed; otherwise patch just the cards whose markup differs.
  // A full innerHTML rewrite every poll tears buttons and links out from
  // under the cursor mid-click and restarts CSS transitions — the grid
  // visibly "blinks" every 4 seconds.
  const html = projects.map(projectCardHtml);
  const cards = [...el.children];
  const sameShape = cards.length === projects.length &&
    projects.every((p, i) => cards[i].dataset.slug === p.slug);
  if (!sameShape) {
    el.innerHTML = html.join('');
    state.cardHtml = Object.fromEntries(projects.map((p, i) => [p.slug, html[i]]));
    return;
  }
  projects.forEach((p, i) => {
    if (state.cardHtml[p.slug] !== html[i]) {
      state.cardHtml[p.slug] = html[i];
      cards[i].outerHTML = html[i];
    }
  });
}

function projectCardHtml(p) {
  const armed = state.deleteArmed === p.slug;
  const deleting = state.deleting === p.slug;
  const dbName = p.slug.replace(/-/g, '_');
  const deleteBar = deleting ? `
      <div class="delete-bar">wiping project — processes, database, checkpoints, files…</div>`
    : armed ? `
      <div class="delete-bar" onclick="event.stopPropagation()">
        <span>Permanently delete <b>${escapeHtml(p.slug)}</b>? Stops its runs and wipes
        specs, repo &amp; branches, database <code>${escapeHtml(dbName)}</code>,
        checkpoint history and logs. This cannot be undone.</span>
        <span class="delete-bar-actions">
          <button class="btn btn-danger" onclick="deleteProject('${escapeAttr(p.slug)}')">Delete forever</button>
          <button class="btn" onclick="cancelDelete()">Cancel</button>
        </span>
      </div>` : '';
  return `
    <div class="project-card" data-slug="${escapeAttr(p.slug)}">
      <div class="project-card-head">
        <div class="name">${escapeHtml(p.slug)}</div>
        <div class="head-right">
          <div class="count">${p.slices.length} slice${p.slices.length === 1 ? '' : 's'}</div>
          <button class="pc-delete" title="Delete project…" ${deleting ? 'disabled' : ''}
            onclick="event.stopPropagation(); armDelete('${escapeAttr(p.slug)}')">🗑</button>
        </div>
      </div>
      ${deleteBar}
      ${p.project ? renderProjectBanner(p.slug, p.project) : ''}
      ${p.error ? `<div class="empty-note">${escapeHtml(p.error)}</div>` : p.slices.map(s => `
        <div class="slice-row${s.planned_only ? ' planned-only' : ''}"${s.planned_only ? '' : ` onclick="goDetail('${escapeAttr(p.slug)}','${escapeAttr(s.id)}')"`}>
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
  `;
}
function escapeAttr(s) { return String(s).replace(/'/g, "\\'"); }

// --------------------------------------------------------------- delete flow
function armDelete(slug) {
  state.deleteArmed = slug;
  state.cardHtml = {};              // force the card to re-render its confirm bar
  renderOverview(state.projects);
}
function cancelDelete() {
  state.deleteArmed = null;
  state.cardHtml = {};
  renderOverview(state.projects);
}
async function deleteProject(slug) {
  state.deleteArmed = null;
  state.deleting = slug;
  state.cardHtml = {};
  renderOverview(state.projects);
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(slug)}?confirm=${encodeURIComponent(slug)}`,
                            { method: 'DELETE' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      window.alert(`delete failed — ${res.status}: ${data.detail || 'unknown error'}`);
    } else if (data.errors && Object.keys(data.errors).length) {
      // The directory is gone but a side effect failed (e.g. Postgres down):
      // say WHICH residue survived rather than pretending the wipe was total.
      window.alert('project deleted, but some cleanup failed:\n'
        + Object.entries(data.errors).map(([k, v]) => `  ${k}: ${v}`).join('\n'));
    }
  } catch (e) {
    window.alert(`delete failed — network error: ${e.message}`);
  }
  state.deleting = null;
  state.lastOverviewText = null;    // the grid genuinely changed — force a render
  state.projects = state.projects.filter(p => p.slug !== slug);
  renderOverview(state.projects);
  await fetchOverview();
}

// -------------------------------------------------------------- run-project
function renderProjectBanner(slug, pj) {
  const pct = pj.project_budget_usd ? Math.min(pj.spent_usd / pj.project_budget_usd * 100, 100) : 0;
  const running = pj.runner && pj.runner.running;
  // Three states: no plan yet (planner hasn't run) → Plan project;
  // plan exists unapproved → Approve plan; approved → Run/Continue.
  // Project-level gates between plan approval and slice work, in pipeline
  // order: decisions (open start-blockers hold the run), then UI/UX
  // (a drafted preview awaiting approval). Without these states the card
  // says "Run project" while a gate silently holds everything.
  const decBlockers = pj.approved && pj.decisions ? pj.decisions.open_start_blockers : 0;
  const uiuxPending = pj.approved && !decBlockers && pj.uiux
    && pj.uiux.preview_exists && !pj.uiux.approved;
  const planState = !pj.total_slices ? 'not planned yet'
    : !pj.approved ? 'plan awaiting approval'
    : decBlockers ? `decision gate — ${decBlockers} start-blocker${decBlockers === 1 ? '' : 's'} open`
    : uiuxPending ? 'UI/UX preview awaiting approval'
    : `plan approved by ${escapeHtml(pj.approved_by || '?')}`;
  const button = !pj.total_slices
    ? `<button class="btn btn-primary" ${running ? 'disabled' : ''} onclick="runProject('${escapeAttr(slug)}')">${running ? 'planning…' : 'Plan project ▶'}</button>`
    : !pj.approved
      ? `<button class="btn btn-primary" onclick="approvePlanUI('${escapeAttr(slug)}')">Approve plan ✓</button>`
    : decBlockers
      ? `<a class="btn btn-primary" href="/p/${encodeURIComponent(slug)}/decisions">Review decisions →</a>`
    : uiuxPending
      ? `<a class="btn btn-primary" href="/p/${encodeURIComponent(slug)}/uiux">Review UI/UX →</a>`
      : `<button class="btn" ${running ? 'disabled' : ''} onclick="runProject('${escapeAttr(slug)}')">${running ? 'project running…' : (pj.completed ? 'Continue project ▶' : 'Run project ▶')}</button>`;
  // Live phase + last log line while the runner is working, so a
  // minutes-long agent never reads as a hang.
  const activityRow = running && pj.phase ? `
      <div class="pb-row" style="margin-top:6px;">
        <span class="pb-progress" style="color:var(--muted);font-size:12px;">
          <span class="spinner-dot">●</span> ${escapeHtml(pj.phase)}${pj.activity ? ` — ${escapeHtml(pj.activity)}` : ''}
        </span>
      </div>` : '';
  return `
    <div class="project-banner" onclick="event.stopPropagation()">
      <div class="pb-row">
        <span class="pb-plan ${pj.approved ? 'ok' : 'pending'}">${planState}</span>
        ${pj.total_slices ? `<span class="pb-progress">${pj.completed}/${pj.total_slices} slices done</span>` : ''}
        <span class="pb-spacer"></span>
        ${button}
      </div>
      ${activityRow}
      <div class="pb-budget">
        <div class="pb-budget-bar"><div class="pb-budget-fill${pct > 85 ? ' hot' : ''}" style="width:${pct.toFixed(1)}%"></div></div>
        <span class="pb-budget-label">$${pj.spent_usd.toFixed(2)} / $${pj.project_budget_usd.toFixed(0)} project budget</span>
      </div>
    </div>`;
}

async function runProject(slug) {
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(slug)}/run-project`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    if (!r.ok) alert((await r.json()).detail || `run-project failed (${r.status})`);
  } catch (e) { alert('run-project failed: ' + e); }
  fetchOverview();
}

async function approvePlanUI(slug) {
  const by = prompt('Plan approval is the project-level gate and must be attributable.\nYour name:');
  if (!by) return;
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(slug)}/plan/approve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ by }),
    });
    if (!r.ok) alert((await r.json()).detail || `approve failed (${r.status})`);
  } catch (e) { alert('approve failed: ' + e); }
  fetchOverview();
}

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
    if (livePending.length < 2000) livePending.push(line);  // beyond this a
    // full rebuild from liveLines covers it — see flushLive
    scheduleLiveRender();
    markActive();
  };
  es.onerror = () => setLiveBadge('idle');
  state.liveEs = es;
}

// Coalesce live-log rendering: agent bursts (and the initial file replay on
// connect) deliver hundreds of SSE lines per second — one full innerHTML
// rebuild per line froze the tab. Instead: batch per animation frame, and on
// the common append-only path insert ONLY the new lines, trimming from the
// top. Full rebuilds are reserved for filter/clear/reconnect.
let livePending = [];
let liveRenderQueued = false;

function scheduleLiveRender() {
  if (liveRenderQueued) return;
  liveRenderQueued = true;
  requestAnimationFrame(() => {
    liveRenderQueued = false;
    if (document.hidden) return;   // flushed by the visibilitychange handler
    flushLive();
  });
}

function liveLineHtml(l) {
  if (DAY_BANNER_RE.test(l.trim())) return `<div class="log-line day-banner">${escapeHtml(l.trim())}</div>`;
  return `<div class="log-line ${/FAIL|✗|failed/.test(l) ? 'fail' : ''}">${escapeHtml(l)}</div>`;
}

function flushLive() {
  const batch = livePending;
  livePending = [];
  if (!batch.length) return;
  // Filtered view, or a backlog bigger than the window itself (a tab left
  // hidden accumulates without rAF): one bounded full rebuild beats a
  // mega-append. state.liveLines is already capped at 2000.
  if (state.liveFilter.trim() || batch.length >= 500) { renderLive(); return; }
  const el = document.getElementById('live-log');
  if (el.dataset.empty === '1') { el.innerHTML = ''; el.dataset.empty = '0'; }
  el.insertAdjacentHTML('beforeend', batch.map(liveLineHtml).join(''));
  while (el.children.length > 2000) el.removeChild(el.firstChild);
  if (state.liveAutoscroll) el.scrollTop = el.scrollHeight;
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) flushLive();
});

function setLiveBadge(mode) {
  const b = document.getElementById('live-badge');
  if (!b) return;
  b.textContent = mode === 'active' ? '● streaming' : 'idle';
  b.classList.toggle('live', mode === 'active');
  b.classList.toggle('off', false);
}

const DAY_BANNER_RE = /^─+\s*\d{4}-\d{2}-\d{2}\s*─+$/;

function renderLive() {
  livePending = [];   // a full rebuild covers anything queued for append
  const filter = state.liveFilter.trim().toLowerCase();
  const lines = filter ? state.liveLines.filter(l => l.toLowerCase().includes(filter)) : state.liveLines;
  const el = document.getElementById('live-log');
  const html = lines.map(liveLineHtml).join('');
  el.dataset.empty = html ? '0' : '1';
  el.innerHTML = html || '<div class="log-line">(nothing yet — populated once a `run` process is actively working this slice)</div>';
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
  const tk = d.tokens;
  const fmtTok = (n) => n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(0) + 'K' : String(n);
  document.getElementById('cost-meta').textContent = tk && (tk.output || tk.cache_read)
    ? `of $${d.budget_usd} · ${fmtTok(tk.output)} out · ${fmtTok(tk.cache_read)} cache-read tokens`
    : `of $${d.budget_usd} budget`;
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
        ${p.active_s || p.cost_usd ? `<div class="phase-metrics">${p.active_s ? fmtDuration(p.active_s) : ''}${p.active_s && p.cost_usd ? ' · ' : ''}${p.cost_usd ? '$' + p.cost_usd.toFixed(2) : ''}</div>` : ''}
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
  if (!state.slug || !state.sliceId || document.hidden) return;
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(state.slug)}/slices/${encodeURIComponent(state.sliceId)}/timeline`);
    if (!res.ok) return;
    renderTimeline(await res.json());
  } catch (e) { /* stream badge already reflects connectivity */ }
}
function renderTimeline(t) {
  document.getElementById('started-stat').textContent = t.started_at ? localTime(t.started_at) : '—';
  document.getElementById('started-meta').textContent = t.started_at ? timeAgo(t.started_at) : 'not started yet';
  // Active time (work actually happening, from live-log activity spans) is
  // the headline; wall-clock (which includes gate waits / sleeps / crashes)
  // is demoted to the meta line so it can't masquerade as effort.
  document.getElementById('elapsed-stat').textContent = fmtDuration(t.active_s != null ? t.active_s : t.elapsed_s);
  const metaBits = [];
  if (t.active_s != null && t.elapsed_s != null && t.elapsed_s > t.active_s + 60)
    metaBits.push(`wall ${fmtDuration(t.elapsed_s)}`);
  if (t.last_update_at) metaBits.push(`last update ${timeAgo(t.last_update_at)}`);
  document.getElementById('elapsed-meta').textContent = metaBits.join(' · ');

  const events = t.events || [];
  // Skip the (expensive) full-history rebuild when nothing changed — the
  // 5s poll usually returns an identical list while a long agent step runs.
  const sig = events.length + ':' + (events.length ? events[events.length - 1].ts : '');
  const changed = sig !== state.timelineSig;
  state.timelineSig = sig;
  state.timelineEvents = events;
  if (changed) renderLog();
}
function renderLog() {
  const events = state.timelineEvents || [];
  const filter = state.logFilter.trim().toLowerCase();
  const filtered = filter ? events.filter(e => e.line.toLowerCase().includes(filter)) : events;
  const el = document.getElementById('activity-log');
  el.innerHTML = filtered.map(e => `
    <div class="log-line ${/FAIL/.test(e.line) ? 'fail' : ''} ${filter ? 'match' : ''}">
      <span class="ts">${localTime(e.ts)}</span><span>${escapeHtml(e.line)}</span>${e.dur_s != null && e.dur_s > 0 ? `<span class="dur">${fmtDuration(e.dur_s)}</span>` : ''}
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
let npInputFiles = null;   // uploaded engagement FOLDER: [{path, content}]
let npCandidates = [];

function setNpBoardFile(file) {
  npBoardFile = file;
  if (file) npInputFiles = null;
  document.getElementById('np-board-file-name').textContent = file ? `${file.name} (${Math.round(file.content.length / 1024)} KB)` : 'nothing selected';
  document.getElementById('np-board-clear').classList.toggle('hidden', !file && !npInputFiles);
  inspectBoard();
}

function setNpFolder(name, files) {
  npInputFiles = files;
  if (files) npBoardFile = null;
  document.getElementById('np-board-file-name').textContent =
    files ? `📁 ${name}/ (${files.length} file${files.length === 1 ? '' : 's'})` : 'nothing selected';
  document.getElementById('np-board-clear').classList.toggle('hidden', !files && !npBoardFile);
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

// Folder picker: the browser hands us every file's relative path + content
// (never the folder's real path). Only text artifacts the factory reads are
// sent — board/backlog JSON, markdown stories, yaml — the server
// reconstructs the folder and resolves the board out of it exactly as it
// would for a typed folder path.
const NP_FOLDER_EXTS = ['.json', '.md', '.txt', '.yaml', '.yml', '.csv'];
function onFolderPicked(ev) {
  const all = [...(ev.target.files || [])];
  if (!all.length) { setNpFolder(null, null); return; }
  const topDir = (all[0].webkitRelativePath || '').split('/')[0];
  const wanted = all.filter(f =>
    NP_FOLDER_EXTS.some(ext => f.name.toLowerCase().endsWith(ext))
    && f.size <= 5 * 1024 * 1024
    && !f.webkitRelativePath.split('/').some(seg => seg.startsWith('.') || seg === 'node_modules'));
  if (!wanted.length) {
    document.getElementById('np-status').textContent = 'no board/backlog/story files found in that folder';
    setNpFolder(null, null);
    return;
  }
  Promise.all(wanted.map(f => f.text().then(content => ({
    // strip the top folder segment so the board sits at the upload's root
    path: f.webkitRelativePath.split('/').slice(1).join('/') || f.name,
    content,
  })))).then(files => setNpFolder(topDir, files))
    .catch(() => { setNpFolder(null, null); document.getElementById('np-status').textContent = 'could not read the folder'; });
}

// Body for board-carrying requests, in the same priority order the create
// call uses: picked folder > picked file > typed path > pasted JSON.
function boardBody() {
  const boardPath = document.getElementById('np-board-path').value.trim();
  const boardJson = document.getElementById('np-board-json').value.trim();
  if (npInputFiles) return { input_files: npInputFiles };
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
  if (!npInputFiles && !npBoardFile && !boardPath && !boardJson) { status.textContent = 'choose a board file or engagement folder, type a path, or paste board JSON'; return; }

  const body = { slug, slice_name: sliceName || null, ...boardBody() };
  body.db_reset_consent = document.getElementById('np-consent').checked;
  const projectMode = document.getElementById('np-project-mode').checked;
  if (projectMode) body.project_mode = true;

  // Which of the board's slices to scaffold — checked boxes only. With
  // candidates on screen but none ticked, that's a mistake, not "all".
  // Irrelevant in project mode: the planner owns slicing there.
  if (!projectMode && npCandidates.length) {
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
    document.getElementById('np-project-mode').checked = false;
    npCandidates = [];
    setNpBoardFile(null);
    // Paint the new project NOW: the confirming /api/overview round-trip can
    // take seconds, and an unchanged grid after clicking Create reads as
    // "nothing happened". Marking every in-flight poll as already rendered
    // stops a response issued before the create from wiping this card.
    if (!state.projects.some(p => p.slug === data.slug)) {
      overviewRendered = overviewSeq;
      renderOverview([{
        slug: data.slug, error: null, slices: [],
        project: data.project_mode ? {
          phase: null, activity: null, approved: false, approved_by: null,
          total_slices: 0, completed: 0, project_budget_usd: 100, spent_usd: 0,
          runner: { running: false }, decisions: null,
          uiux: { preview_exists: false, approved: false },
        } : null,
      }, ...state.projects]);
    }
    state.projects = [];  // force switcher refresh
    state.lastOverviewText = null;  // grid changed for real — never skip this render
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
  document.getElementById('np-folder-pick').addEventListener('click', () => document.getElementById('np-board-folder').click());
  document.getElementById('np-board-folder').addEventListener('change', onFolderPicked);
  document.getElementById('np-board-clear').addEventListener('click', () => {
    document.getElementById('np-board-file').value = '';
    document.getElementById('np-board-folder').value = '';
    setNpFolder(null, null);
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
