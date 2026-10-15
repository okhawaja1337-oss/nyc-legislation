/* D49 workspace. Plain ES modules-free JavaScript: no build step, no CDN.
   The server sends a strict CSP with no inline script, so everything lives
   here and every handler is attached, never written into markup. */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
/*
 * The access token, from the link or from last time.
 *
 * Staff are sent one URL with ?token= on the end. The token is stored and then
 * stripped from the address bar, so it does not sit in browser history, get
 * copied out of the URL bar into an email, or ride along in a Referer header
 * to anything the page links to. After the first visit the link works without
 * it, because the browser remembers.
 */
function firstToken() {
  let stored = '';
  try { stored = localStorage.getItem('d49tok') || ''; } catch (e) { /* private window */ }
  const url = new URL(location.href);
  const fromLink = url.searchParams.get('token') || url.searchParams.get('t') || '';
  if (fromLink) {
    try { localStorage.setItem('d49tok', fromLink); } catch (e) { /* ignore */ }
    url.searchParams.delete('token');
    url.searchParams.delete('t');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
    return fromLink;
  }
  return stored;
}

const state = {
  route: 'home', params: {}, token: firstToken(),
  csrf: '', me: localStorage.getItem('d49me') || '', status: null,
  programs: [], people: [], cursor: 0, es: null, cache: {},
};

/* ---------------------------------------------------------------- utils */
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = n => n == null ? '—' : '$' + Math.round(n).toLocaleString();
const num = n => n == null ? '—' : Number(n).toLocaleString();
const NY = { timeZone: 'America/New_York' };
const fmtDate = v => { if (!v) return '—'; const d = new Date(v);
  return isNaN(d) ? String(v).slice(0, 10)
    : d.toLocaleDateString('en-US', { ...NY, month: 'short', day: 'numeric' }); };
const fmtFull = v => { if (!v) return '—'; const d = new Date(v);
  return isNaN(d) ? String(v) : d.toLocaleString('en-US',
    { ...NY, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); };
const initials = n => (n || '?').split(/\s+/).slice(0, 2).map(w => w[0] || '').join('').toUpperCase();
const isLate = t => t.due && new Date(t.due) < new Date() && t.status !== 'Completed';
const safeURL = u => { try { const x = new URL(u, location.origin);
  return ['http:', 'https:'].includes(x.protocol) ? x.href : ''; } catch { return ''; } };

function toast(msg, ms = 4200) {
  const el = $('#toast'); el.textContent = msg; el.style.display = 'block';
  clearTimeout(window._t); window._t = setTimeout(() => el.style.display = 'none', ms);
}

async function api(path, body, method) {
  const res = await fetch(path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: { ...(state.token ? { Authorization: 'Bearer ' + state.token } : {}),
      ...(body ? { 'Content-Type': 'application/json', 'X-Workspace-CSRF': state.csrf } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (res.status === 401) { if (!$('#login').open) $('#login').showModal();
    throw new Error('Enter the workspace access token.'); }
  const ctype = res.headers.get('Content-Type') || '';
  if (!ctype.includes('json')) { if (!res.ok) throw new Error('Request failed'); return res; }
  const out = await res.json();
  if (!res.ok) throw new Error(out.error || 'Request failed');
  return out;
}

/* --------------------------------------------------------- live updates */
function connectStream() {
  if (state.es) state.es.close();
  // EventSource cannot send an Authorization header, so the token rides the
  // query string on this one read-only endpoint over a local connection.
  const es = new EventSource(`/api/stream?cursor=${state.cursor}&t=${encodeURIComponent(state.token)}`);
  state.es = es;
  es.onopen = () => { $('#live-dot').innerHTML = '<i class="dot"></i>live'; };
  es.onerror = () => { $('#live-dot').innerHTML = '<i class="dot off"></i>reconnecting'; };
  es.onmessage = ev => handleEvent(ev);
  ['task.created', 'task.updated', 'task.moved', 'task.completed', 'comment.added',
   'project.saved', 'project.status', 'program.saved', 'job.complete', 'job.error',
   'sync.legistar', 'index.rebuilt'].forEach(k => es.addEventListener(k, handleEvent));
}

let refreshTimer = null;
function handleEvent(ev) {
  let d; try { d = JSON.parse(ev.data); } catch { return; }
  if (d.id) state.cursor = d.id;
  if (d.kind === 'job.complete' || d.kind === 'job.error') {
    $('#job').textContent = d.summary || ''; toast(d.summary || 'Job finished');
  }
  // Someone else changed something: re-render soon, but coalesce a burst.
  if (d.actor !== state.me) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => { loadShell(); render(); }, 400);
  }
  if (d.kind === 'comment.added' && (d.payload?.mentions || []).includes(state.me))
    toast(`${d.actor} mentioned you`);
}

/* ------------------------------------------------------------- shell */
async function loadShell() {
  state.status = await api('/api/status');
  state.csrf = state.status.csrf;
  state.cursor = Math.max(state.cursor, state.status.cursor || 0);
  state.people = state.status.people || [];
  if (!state.me && state.people.length) state.me = state.people[0].name;
  state.programs = await api('/api/programs');
  renderPeople(); renderTree();
  const mine = await api('/api/mywork?person=' + encodeURIComponent(state.me));
  $('#mywork-count').textContent = mine.total || '';
  $('#inbox-count').textContent = mine.unread || '';
  $('#me-avatar').textContent = initials(state.me);
  const job = state.status.job || {};
  $('#job').textContent = job.status === 'Running' ? '↻ ' + job.name : '';

  // The two live counters in the sidebar. A failure here must not stop the
  // shell from loading -- the workspace still works with no counts on it.
  try {
    const [changes, meetings] = await Promise.all([
      api('/api/changes/status'), api('/api/meetings?days=7')]);
    $('#change-count').textContent = changes.unacknowledged_high || '';
    const ahead = meetings.filter(m =>
      ['oversight', 'hearing', 'stated'].includes(m.packet_kind) && !m.has_packet).length;
    $('#meeting-count').textContent = ahead || '';
  } catch (e) { /* counters are a convenience, not the page */ }
}

function renderPeople() {
  $('#me').innerHTML = state.people.map(p =>
    `<option ${p.name === state.me ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
}

function renderTree() {
  // Programs stay expanded unless the user closes one. A tree that opens
  // collapsed hides the whole workspace behind a second click.
  let closed = {};
  try { closed = JSON.parse(localStorage.getItem('d49closed') || '{}'); } catch {}
  $('#program-tree').innerHTML = state.programs.map(p => {
    const onRoute = state.params.program === p.id || (p.projects || [])
      .some(x => x.id === state.params.project);
    const open = onRoute || !closed[p.id];
    return `<details ${open ? 'open' : ''}>
      <summary>${esc(p.icon || '▸')} ${esc(p.name)}<span class="n">${p.task_counts.done}/${p.task_counts.total}</span></summary>
      ${(p.projects || []).map(pr => `<a href="#project?project=${pr.id}"
        class="${state.params.project === pr.id ? 'active' : ''}">${esc(pr.name)}
        <span class="n">${pr.counts.total || ''}</span></a>`).join('')}
    </details>`;
  }).join('') || '<p class="muted small pad-x" >No programs yet.</p>';

  $$('#program-tree details').forEach((d, i) => d.ontoggle = () => {
    const id = state.programs[i]?.id; if (!id) return;
    let map = {}; try { map = JSON.parse(localStorage.getItem('d49closed') || '{}'); } catch {}
    if (d.open) delete map[id]; else map[id] = 1;
    localStorage.setItem('d49closed', JSON.stringify(map));
  });
}



/* ------------------------------------------------------------ routing */
const TITLES = { home: 'Home', mywork: 'My work', inbox: 'Inbox', calendar: 'Calendar',
  workload: 'Workload', search: 'Search', assistant: 'Assistant', media: 'Media & press',
  sources: 'Sources', project: 'Project', meetings: 'Meetings', changes: 'What changed',
  breakdown: 'Breakdowns', connections: 'Connections', briefs: 'Run a brief',
  position: 'The district position', repos: 'Source repositories',
  signon: 'Sign-on decisions', law: 'The code' };

async function route() {
  const [name, qs] = (location.hash.slice(1) || 'home').split('?');
  state.route = TITLES[name] ? name : 'home';
  state.params = Object.fromEntries(new URLSearchParams(qs || ''));
  $('#crumb').textContent = TITLES[state.route];
  $$('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === state.route));
  $('#sidebar').classList.remove('open');
  // A modal left open across a navigation covers the page it navigated to.
  if ($('#drawer').open) $('#drawer').close();
  if ($('#palette').open) $('#palette').close();
  renderTree();
  await render();
}

/* Every view is async, so two navigations close together race each other and
   whichever server round-trip finishes last wins the page. That is not a rare
   edge: clicking Meetings while Home is still loading rendered Home under the
   Meetings heading, and the breadcrumb — set synchronously — said Meetings, so
   the page looked merely wrong rather than stale. A render only gets to write
   if it is still the current one. */
let renderSeq = 0;

async function render() {
  const main = $('#main');
  const mine = ++renderSeq;
  const route = state.route;
  try {
    const view = VIEWS[route];
    const html = view ? await view() : '';
    if (mine !== renderSeq) return;   // a newer navigation has taken over
    if (view) main.innerHTML = html;
    wire();
  } catch (e) {
    if (mine !== renderSeq) return;
    main.innerHTML = `<div class="empty"><h3>Could not load this view</h3>
      <p>${esc(e.message)}</p><button class="btn" data-act="reload">Try again</button></div>`;
    wire();
  }
}

const head = (eyebrow, title, sub) => `<div class="page-head"><div>
  <div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1>
  <p class="sub">${sub}</p></div></div>`;
const metric = (label, n, note) => `<div class="card metric"><div class="label">${esc(label)}</div>
  <div class="n">${n}</div><div class="sublabel">${note || ''}</div></div>`;

/* -------------------------------------------------------------- views */
const VIEWS = {};

VIEWS.home = async () => {
  const s = state.status, cov = s.coverage || [];
  const mine = await api('/api/mywork?person=' + encodeURIComponent(state.me));
  const b = mine.buckets;
  const acts = await api('/api/activity?limit=14');
  const totalTasks = state.programs.reduce((a, p) => a + p.task_counts.total, 0);
  const overdue = state.programs.reduce((a, p) => a + p.task_counts.overdue, 0);
  return head('OPERATIONS HUB', `Good ${greeting()}, ${esc(state.me.split(' ')[0] || 'team')}.`,
    'Everything the office is carrying, and what the record says about it.') + `
  <div class="grid">
    ${metric('Due today', b.today.length, b.overdue.length ? `<span class="overdue">${b.overdue.length} overdue</span>` : 'Nothing overdue')}
    ${metric('My open work', mine.total, `${b.blocked.length} blocked by others`)}
    ${metric('Office tasks', totalTasks, `${overdue} overdue across all programs`)}
    ${metric('Searchable records', num(s.indexed), 'Bills, funding, orgs, people, media')}
  </div>
  <h2>Programs</h2>
  <div class="grid">${state.programs.map(p => `
    <div class="card"><div class="row-icon">
      <span class="prog-icon">${esc(p.icon || '▸')}</span><h3>${esc(p.name)}</h3></div>
      <p class="small muted my-xs" >${esc((p.purpose || '').slice(0, 110))}…</p>
      <div class="bar"><i data-w="${p.task_counts.pct}"></i></div>
      <div class="small muted mt-xs" >${p.task_counts.done}/${p.task_counts.total} done
      ${p.task_counts.overdue ? `· <span class="overdue">${p.task_counts.overdue} overdue</span>` : ''}</div>
    </div>`).join('')}</div>
  <h2>What the record holds</h2>
  <div class="tablewrap"><table><thead><tr><th>Kind</th><th class="num">Records</th>
    <th>Years</th><th class="num">Value</th></tr></thead><tbody>
    ${cov.map(c => `<tr><td>${esc(c.kind)}</td><td class="num">${num(c.n)}</td>
      <td>${c.first_fy ? `FY${c.first_fy}–FY${c.last_fy}` : '—'}</td>
      <td class="num">${c.total ? money(c.total) : '—'}</td></tr>`).join('')}
  </tbody></table></div>
  <h2>Team activity</h2>
  ${acts.map(a => `<div class="activity"><b>${esc(a.actor || 'workspace')}</b> ${esc(a.summary)}
     <time>· ${fmtFull(a.at)}</time></div>`).join('') || '<p class="muted">No activity yet.</p>'}`;
};

const greeting = () => { const h = new Date().getHours();
  return h < 12 ? 'morning' : h < 18 ? 'afternoon' : 'evening'; };

VIEWS.mywork = async () => {
  const m = await api('/api/mywork?person=' + encodeURIComponent(state.me));
  const group = (key, label, note) => {
    const rows = m.buckets[key];
    if (!rows.length) return '';
    return `<h2>${label} <span class="muted small">(${rows.length})</span></h2>
      ${note ? `<p class="small muted">${note}</p>` : ''}
      <div class="tablewrap"><table><tbody>
      ${rows.map(t => `<tr><td class="w-check">
        <input type="checkbox" data-complete="${esc(t.id)}" aria-label="Complete"></td>
        <td><button class="linkish" data-task="${esc(t.id)}">${esc(t.title)}</button>
        ${t.milestone == 1 ? ' <span class="tag warn">milestone</span>' : ''}</td>
        <td>${priorityTag(t.priority)}</td>
        <td class="${isLate(t) ? 'overdue' : 'muted'}">${fmtDate(t.due)}</td>
        <td class="muted small">${esc(t.status)}</td></tr>`).join('')}
      </tbody></table></div>`;
  };
  return head('MY WORK', 'What is actually yours today.',
    'Assigned to you, ordered by when it is due. Blocked work is separated so it does not look like a backlog you are ignoring.')
    + group('overdue', 'Overdue')
    + group('today', 'Today')
    + group('week', 'This week')
    + group('later', 'Later')
    + group('blocked', 'Blocked', 'Waiting on another task to finish first.')
    + group('someday', 'No date set')
    + (m.total ? '' : '<div class="empty"><h3>Nothing assigned</h3><p>Work assigned to you appears here.</p></div>');
};

const priorityTag = p => p === 'Urgent' ? '<span class="tag urgent">Urgent</span>'
  : p === 'High' ? '<span class="tag high">High</span>' : '';

VIEWS.inbox = async () => {
  const rows = await api('/api/inbox?person=' + encodeURIComponent(state.me));
  return head('INBOX', 'Mentions, assignments and what changed.',
    'Everything addressed to you, newest first.') + `
  <div class="toolbar"><button class="btn" data-act="read-all">Mark all read</button></div>
  ${rows.map(n => `<div class="activity${n.read ? ' dim' : ''}">
    <span class="tag">${esc(n.kind)}</span>
    ${n.task_id ? `<button class="linkish" data-task="${esc(n.task_id)}">${esc(n.body)}</button>`
      : esc(n.body)}
    <time>· ${fmtFull(n.created)}</time></div>`).join('')
    || '<div class="empty"><h3>Inbox clear</h3><p>Mentions and assignments land here.</p></div>'}`;
};

VIEWS.project = async () => {
  const id = state.params.project;
  if (!id) return '<div class="empty"><h3>Pick a project</h3><p>Choose one from the sidebar.</p></div>';
  const d = await api('/api/project?id=' + encodeURIComponent(id));
  if (!d.project) return '<div class="empty"><h3>Project not found</h3></div>';
  state.cache.project = d;
  const view = state.params.view || d.project.default_view || 'board';
  const tabs = ['board', 'list', 'timeline', 'updates'].map(v =>
    `<button data-view="${v}" aria-selected="${v === view}">${v[0].toUpperCase() + v.slice(1)}</button>`).join('');
  const body = view === 'list' ? listView(d) : view === 'timeline' ? timelineView(d)
    : view === 'updates' ? updatesView(d) : boardView(d);
  return `<div class="page-head"><div><div class="eyebrow">PROJECT</div>
      <h1>${esc(d.project.name)}</h1><p class="sub">${esc(d.project.purpose || '')}</p></div></div>
    <div class="toolbar">${healthTag(d.project.health)}
      <span class="muted small">${d.tasks.length} tasks · lead ${esc(d.project.lead || '—')}</span>
      <div class="spacer grow" ></div>
      <button class="btn sm" data-act="new-task" data-project="${esc(id)}">＋ Task</button>
      <button class="btn sm" data-act="post-status">Post status</button></div>
    <div class="tabs">${tabs}</div>${body}`;
};

const healthTag = h => `<span class="tag ${h === 'On track' ? 'good' : h === 'At risk' ? 'warn'
  : h === 'Off track' ? 'urgent' : ''}">${esc(h || 'On track')}</span>`;

function boardView(d) {
  const cols = d.sections.length ? d.sections : [{ id: '', name: 'All tasks' }];
  const first = cols[0].id || '';
  return `<div class="board">${cols.map((sec, i) => {
    // A task with no section would otherwise be invisible on the board.
    // Unfiled work belongs in the first column, where it gets triaged.
    const rows = d.tasks.filter(t => {
      const s = t.section_id || '';
      return s === (sec.id || '') || (i === 0 && !d.sections.some(x => x.id === s));
    });
    return `<div class="col" data-section="${esc(sec.id)}">
      <div class="col-head">${esc(sec.name)}<span class="n">${rows.length}</span></div>
      ${rows.map(taskCard).join('')}
      <button class="btn subtle sm" data-act="new-task" data-project="${esc(d.project.id)}"
        data-section="${esc(sec.id)}">＋ Add task</button></div>`;
  }).join('')}</div>`;
}

const taskCard = t => `<button class="tcard" draggable="true" data-task="${esc(t.id)}">
  <h4>${esc(t.title)}</h4>
  ${t.milestone == 1 ? '<span class="tag warn">milestone</span> ' : ''}${priorityTag(t.priority)}
  ${t.description ? `<div class="small muted">${esc(t.description.slice(0, 80))}…</div>` : ''}
  <div class="meta"><span class="${isLate(t) ? 'overdue' : ''}">${fmtDate(t.due)}</span>
    <span class="avatar" title="${esc(t.owner || 'Unassigned')}">${esc(initials(t.owner))}</span></div>
</button>`;

function listView(d) {
  return `<div class="tablewrap"><table><thead><tr><th></th><th>Task</th><th>Owner</th>
    <th>Status</th><th>Priority</th><th>Due</th></tr></thead><tbody>
    ${d.tasks.map(t => `<tr><td><input type="checkbox" data-complete="${esc(t.id)}"
      ${t.status === 'Completed' ? 'checked disabled' : ''} aria-label="Complete"></td>
      <td><button class="linkish" data-task="${esc(t.id)}">${esc(t.title)}</button></td>
      <td>${esc(t.owner || '—')}</td><td>${esc(t.status)}</td>
      <td>${priorityTag(t.priority) || '<span class="muted">Normal</span>'}</td>
      <td class="${isLate(t) ? 'overdue' : ''}">${fmtDate(t.due)}</td></tr>`).join('')
      || '<tr><td colspan="6" class="muted">No tasks yet.</td></tr>'}
  </tbody></table></div>`;
}

function timelineView(d) {
  const dated = d.tasks.filter(t => t.due);
  if (!dated.length) return '<div class="empty"><h3>No dated work</h3><p>Give tasks a due date to see them on a timeline.</p></div>';
  const times = dated.map(t => new Date(t.due).getTime());
  const min = Math.min(...times, Date.now()), max = Math.max(...times, Date.now());
  const span = Math.max(max - min, 86400000);
  const pos = v => ((new Date(v).getTime() - min) / span) * 92;
  return `<div class="timeline">${dated.map(t => {
    const start = t.starts ? pos(t.starts) : Math.max(pos(t.due) - 6, 0);
    const width = Math.max(pos(t.due) - start, t.milestone == 1 ? 2 : 5);
    const cls = t.status === 'Completed' ? 'done' : isLate(t) ? 'late'
      : t.milestone == 1 ? 'milestone' : '';
    return `<div class="tl-row"><div class="tl-name">
      <button class="linkish" data-task="${esc(t.id)}">${esc(t.title.slice(0, 34))}</button></div>
      <div class="tl-track"><div class="tl-today" data-l="${pos(new Date().toISOString())}"></div>
      <div class="tl-bar ${cls}" data-l="${start}" data-w="${width}"
        title="${esc(t.title)} — ${fmtDate(t.due)}">${fmtDate(t.due)}</div></div></div>`;
  }).join('')}</div>
  <p class="small muted">The red line is today. Bars run from start date, or a short lead-in where none is set.</p>`;
}

function updatesView(d) {
  return `<div class="card"><h3>Post a status update</h3>
    <form id="status-form"><label>Health<select name="health">
      ${['On track', 'At risk', 'Off track', 'On hold', 'Complete'].map(h =>
        `<option ${h === d.project.health ? 'selected' : ''}>${h}</option>`).join('')}
      </select></label>
      <label>What changed<textarea name="body" required
        placeholder="One paragraph: what moved, what is blocked, what you need."></textarea></label>
      <button class="btn primary">Post update</button></form></div>
    ${d.updates.map(u => `<div class="card mt-md" >
      ${healthTag(u.health)} <b>${esc(u.author || 'Staff')}</b>
      <time class="muted small">· ${fmtFull(u.created)}</time>
      <p class="prewrap mt-xs">${esc(u.body)}</p></div>`).join('')}`;
}

VIEWS.workload = async () => {
  const rows = await api('/api/workload?days=14');
  return head('WORKLOAD', 'Who is carrying what, over the next fortnight.',
    'Estimated hours against each person’s stated capacity. Tasks without an estimate count toward the task total but not the hours, so a low bar with many tasks means estimates are missing, not that someone is free.')
    + `<div class="tablewrap"><table><thead><tr><th>Person</th><th>Role</th>
      <th class="num">Open</th><th class="num">Due ≤14d</th><th class="num">Overdue</th>
      <th>Load</th><th class="num">Hours</th></tr></thead><tbody>
      ${rows.map(p => `<tr><td><span class="avatar avatar-inline" >${esc(p.initials)}</span>
        ${esc(p.name)}</td><td class="muted small">${esc(p.role || '')}</td>
        <td class="num">${p.open}</td><td class="num">${p.soon}</td>
        <td class="num ${p.overdue ? 'overdue' : ''}">${p.overdue}</td>
        <td><div class="bar"><i class="${p.load_pct > 100 ? 'hot' : p.load_pct > 75 ? 'warm' : ''}"
          data-w="${Math.min(p.load_pct, 100)}"></i></div>
          <span class="small muted">${p.load_pct}%</span></td>
        <td class="num">${p.hours} / ${p.capacity}</td></tr>`).join('')}
    </tbody></table></div>`;
};

VIEWS.calendar = async () => {
  const [events, tasks] = await Promise.all([api('/api/calendar'),
    api('/api/tasks?top_level=1')]);
  const items = [...events.map(e => ({ when: e.start, title: e.summary,
      where: e.location || e.kind, url: e.link })),
    ...tasks.filter(t => t.due && t.status !== 'Completed').map(t => ({
      when: t.due, title: t.title, where: 'Due · ' + (t.owner || 'Unassigned'), task: t.id }))]
    .sort((a, b) => String(a.when).localeCompare(String(b.when)));
  return head('CALENDAR', 'Hearings and deadlines together.',
    'Committee meetings from Legistar and the office calendar, alongside what is due.')
    + (items.map(i => `<div class="activity"><b>${fmtFull(i.when)}</b> —
      ${i.task ? `<button class="linkish" data-task="${esc(i.task)}">${esc(i.title)}</button>`
        : esc(i.title)}<br><span class="muted small">${esc(i.where || '')}
      ${i.url ? `· <a href="${esc(safeURL(i.url))}" target="_blank" rel="noopener">source ↗</a>` : ''}</span></div>`).join('')
      || '<div class="empty"><h3>Nothing scheduled</h3><p>Refresh sources to pull committee hearings.</p></div>');
};

VIEWS.search = async () => {
  const q = state.params.q || '';
  const res = q ? await api('/api/search?limit=60&q=' + encodeURIComponent(q)) : null;
  return head('SEARCH', 'One box over the whole record.',
    'Bills, funding lines, organizations, officials, hearings, media and your own tasks.') + `
  <form id="search-form" class="toolbar">
    <input name="q" value="${esc(q)}" placeholder="Try: kind:funding fy:2027 org:Snug"
      class="grow search-input"><button class="btn primary">Search</button>
    ${q ? `<a class="btn" href="/api/export?q=${encodeURIComponent(q)}">Export CSV</a>` : ''}
  </form>
  <p class="small muted">Fields: <code>kind</code> <code>fy</code> <code>org</code>
    <code>sponsor</code> <code>agency</code> <code>pillar</code> <code>status</code>
    <code>committee</code> <code>amount</code>. Ranges <code>fy:2022..2027</code>,
    comparisons <code>amount:&gt;50000</code>, exclusion <code>-kind:matter</code>,
    phrases <code>"Snug Harbor"</code>.</p>
  ${res ? renderResults(res) : ''}`;
};

function renderResults(res) {
  if (res.error) return `<div class="note">${esc(res.error)}</div>`;
  const facets = Object.entries(res.facets || {}).filter(([k]) => !k.startsWith('_'));
  return `<p class="small muted">${num(res.total)} results in ${res.ms} ms
    ${res.strategy && res.strategy !== 'exact' ? `· <b>${esc(res.strategy)}</b> match` : ''}</p>
  ${res.note ? `<div class="note">${esc(res.note)}</div>` : ''}
  ${facets.length ? `<div class="toolbar">${facets.map(([k, vals]) =>
    `<select data-facet="${esc(k)}"><option value="">${esc(k)}</option>
      ${vals.map(v => `<option value="${esc(v.value)}">${esc(v.value)} (${num(v.n)})</option>`).join('')}
    </select>`).join('')}</div>` : ''}
  <div class="tablewrap"><table><thead><tr><th>Kind</th><th>Record</th><th>FY</th>
    <th>Who</th><th class="num">Amount</th><th></th></tr></thead><tbody>
    ${res.rows.map(r => `<tr><td><span class="tag">${esc(r.kind)}</span></td>
      <td><button class="linkish" data-record="${esc(r.key)}">${esc(r.title)}</button>
      ${r.snip ? `<div class="small muted">${r.snip}</div>` : ''}
      ${r.citation && r.citation.inline
        ? `<div class="small muted cite" data-cite="${esc(r.citation.inline)}"
             title="Click to copy">${esc(r.citation.inline)}</div>` : ''}</td>
      <td>${r.fy || r.year || '—'}</td><td class="small">${esc(r.sponsor || r.org || '—')}</td>
      <td class="num">${r.amount ? money(r.amount) : '—'}</td>
      <td>${BRIEFABLE.has(String(r.key || '').split(':')[0])
        ? `<button class="btn sm" data-act="brief-record"
             data-key="${esc(r.key)}">Brief this</button>` : ''}</td></tr>`).join('')
      || '<tr><td colspan="6" class="muted">No matches.</td></tr>'}
  </tbody></table></div>
  ${(res.bibliography || []).length ? `<div class="card">
    <h3>Sources for these results</h3>
    <ol class="small muted">${res.bibliography.map(b =>
      `<li>${esc(b.replace(/^\[\d+\]\s*/, ''))}</li>`).join('')}</ol>
    <p class="small muted">Every row above carries its own citation. Click one
      to copy it.</p></div>` : ''}`;
}

/* Which record kinds the office can brief. Anything else shows no button
   rather than a button that fails: a control that does nothing teaches people
   to distrust the ones that work. */
const BRIEFABLE = new Set(['matter', 'funding', 'org', 'ledger', 'fy']);

VIEWS.assistant = async () => {
  const prompts = await api('/api/assistant/prompts');
  const ai = state.status.ai || {};
  return head('ASSISTANT', 'Ask, and get the evidence with the answer.',
    'Every answer is built from the loaded record first, then written. Quotes come only from stored transcripts — the assistant cannot invent something the Member never said.') + `
  ${ai.mode === 'scaffold' ? `<div class="note">No language model is configured, so answers
    return the evidence, the figures and the structure — not prose. Set
    <code>ANTHROPIC_API_KEY</code> before starting the workspace to enable drafting.</div>` : ''}
  <form id="ask-form" class="card">
    <label>What do you need?<textarea name="question" required
      placeholder="What did we fund for seniors in District 49 last year?"></textarea></label>
    <div class="row">
      <label>Deliverable<select name="kind">
        <option value="answer">Answer a question</option>
        <option value="talking_points">Talking points</option>
        <option value="quote">Quote (from the record)</option>
        <option value="press_statement">Press statement</option>
        <option value="press_release">Press release</option>
        <option value="hearing_questions">Hearing questions</option>
        <option value="constituent_reply">Constituent reply</option>
        <option value="newsletter">Newsletter item</option>
        <option value="memo">Internal memo</option>
        <option value="social">Social posts</option></select></label>
      <label>Register<select name="register">
        <option>measured</option><option>firm</option><option>urgent</option><option>warm</option>
      </select></label>
    </div>
    <label class="small"><input type="checkbox" name="council" class="inline-check">
      Run the LLM Council (five perspectives, peer-reviewed)</label>
    <button class="btn primary">Generate</button>
  </form>
  <div class="toolbar">${prompts.map(p =>
    `<button class="btn sm" data-prompt="${esc(p.label)}" data-kind="${esc(p.kind)}">${esc(p.label)}</button>`).join('')}</div>
  <div id="ask-out"></div>`;
};

VIEWS.media = async () => {
  const [lib, cov] = await Promise.all([api('/api/media?limit=80'), api('/api/media/coverage')]);
  return head('MEDIA & PRESS', 'The public record, collected.',
    'Council hearing video, the Council YouTube channel, news coverage and press releases. A clip can only be quoted once a real transcript is attached.') + `
  <div class="toolbar"><button class="btn" data-act="refresh-media">↻ Collect media</button>
    <span class="muted small">${cov.total} items · ${cov.quotable} quotable</span></div>
  <div class="note">${esc(cov.note)}</div>
  ${lib.length ? `<div class="tablewrap"><table><thead><tr><th>Kind</th><th>Title</th>
    <th>Published</th><th>Transcript</th></tr></thead><tbody>
    ${lib.map(m => `<tr><td><span class="tag">${esc(m.kind)}</span></td>
      <td>${m.url ? `<a href="${esc(safeURL(m.url))}" target="_blank" rel="noopener">${esc(m.title)} ↗</a>`
        : esc(m.title)}${m.mentions_member ? ' <span class="tag good">names the CM</span>' : ''}</td>
      <td class="small muted">${esc((m.published || '').slice(0, 16))}</td>
      <td>${m.transcript ? '<span class="tag good">yes</span>'
        : `<button class="btn sm" data-transcript="${esc(m.id)}">Attach</button>`}</td></tr>`).join('')}
  </tbody></table></div>` : '<div class="empty"><h3>No media collected</h3><p>Collect media to pull hearings, video, news and press releases.</p></div>'}`;
};

VIEWS.sources = async () => {
  const s = state.status;
  return head('SOURCES', 'What is loaded, and what is reachable.',
    'A failed refresh never deletes good data — the last successful pull stays, and the failure is recorded here.') + `
  <div class="grid">
    ${metric('Indexed records', num(s.indexed), 'Across every kind')}
    ${metric('Funding lines', num(s.counts.funding), 'FY2022–FY2027')}
    ${metric('Legislative matters', num(s.counts.matters), 'From the Council record')}
    ${metric('Media items', num((s.media || {}).total || 0), `${(s.media || {}).quotable || 0} with transcripts`)}
  </div>
  <div class="toolbar"><button class="btn" data-act="refresh">↻ Refresh live sources</button>
    <button class="btn" data-act="reindex">Rebuild search index</button></div>
  <h2>Coverage by kind</h2>
  <div class="tablewrap"><table><thead><tr><th>Kind</th><th class="num">Records</th>
    <th>Years</th><th class="num">Value</th></tr></thead><tbody>
    ${(s.coverage || []).map(c => `<tr><td>${esc(c.kind)}</td><td class="num">${num(c.n)}</td>
      <td>${c.first_fy ? `FY${c.first_fy}–FY${c.last_fy}` : '—'}</td>
      <td class="num">${c.total ? money(c.total) : '—'}</td></tr>`).join('')}
  </tbody></table></div>`;
};

/* --------------------------------------------------------- task drawer */
async function openTask(id) {
  const t = await api('/api/task?id=' + encodeURIComponent(id));
  const opts = (list, val) => list.map(v =>
    `<option ${v === val ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const body = `
  <div class="eyebrow">TASK</div><h2 class="head-tight">${esc(t.title)}</h2>
  ${t.blocked_by.filter(b => b.status !== 'Completed').length
    ? `<div class="blocked">Blocked by ${t.blocked_by.filter(b => b.status !== 'Completed')
        .map(b => esc(b.title)).join(', ')}</div>` : ''}
  <form id="task-form">
    <input type="hidden" name="id" value="${esc(t.id)}">
    <input type="hidden" name="revision" value="${esc(t.revision)}">
    <label>Title<input name="title" value="${esc(t.title)}" required></label>
    <label>Description<textarea name="description">${esc(t.description || '')}</textarea></label>
    <div class="row">
      <label>Owner<select name="owner"><option value=""></option>
        ${opts(state.people.map(p => p.name), t.owner)}</select></label>
      <label>Status<select name="status">${opts(
        ['Intake','Research','Drafting','Messaging review','COS review','Ready','Completed'],
        t.status)}</select></label>
      <label>Priority<select name="priority">${opts(['Low','Normal','High','Urgent'], t.priority)}</select></label>
      <label>Due<input type="datetime-local" name="due" value="${esc((t.due || '').slice(0,16))}"></label>
      <label>Estimate (hours)<input name="estimate_hours" type="number" step="0.5"
        value="${esc(t.estimate_hours ?? '')}"></label>
      <label>Milestone<select name="milestone"><option value="">No</option>
        <option value="1" ${t.milestone == 1 ? 'selected' : ''}>Yes</option></select></label>
    </div>
    <button class="btn primary">Save task</button>
    ${t.status !== 'Completed' ? '<button type="button" class="btn" data-act="complete">Mark complete</button>' : ''}
  </form>

  <h3 class="mt-lg">Subtasks (${t.subtasks.length})</h3>
  ${t.subtasks.map(s => `<div class="activity">
    <input type="checkbox" data-complete="${esc(s.id)}" ${s.status === 'Completed' ? 'checked disabled' : ''}>
    <button class="linkish" data-task="${esc(s.id)}">${esc(s.title)}</button></div>`).join('')}
  <form id="subtask-form" class="toolbar"><input name="title" placeholder="Add a subtask"
    class="grow"><button class="btn sm">Add</button></form>

  <h3 class="mt-lg">Checklist</h3>
  ${t.checklist.map(c => `<div class="activity">
    <input type="checkbox" data-check="${c.id}" ${c.done ? 'checked' : ''}>
    ${esc(c.title)}</div>`).join('') || '<p class="muted small">No steps yet.</p>'}
  <form id="check-form" class="toolbar"><input name="title" placeholder="Add a step" class="grow">
    <button class="btn sm">Add</button></form>

  ${t.links.length ? `<h3 class="mt-lg">Attached evidence</h3>
    ${t.links.map(l => `<div class="activity"><span class="tag">${esc(l.kind || 'record')}</span>
      <button class="linkish" data-record="${esc(l.record_key)}">${esc(l.title || l.record_key)}</button></div>`).join('')}` : ''}

  <h3 class="mt-lg">Discussion</h3>
  ${t.comments.map(c => `<div class="comment"><b>${esc(c.author)}</b>
    <small>${fmtFull(c.created)}</small><p>${mentionise(c.body)}</p></div>`).join('')
    || '<p class="muted small">No comments yet.</p>'}
  <form id="comment-form"><label>Comment
    <textarea name="body" required placeholder="Use @name to notify a teammate."></textarea></label>
    <button class="btn primary">Post</button></form>

  <details class="mt-lg"><summary class="muted small">History (${t.log.length})</summary>
    ${t.log.map(l => `<div class="activity small"><b>${esc(l.actor)}</b>
      <time>${fmtFull(l.at)}</time></div>`).join('')}</details>`;
  $('#drawer-body').innerHTML = body;
  if (!$('#drawer').open) $('#drawer').showModal();
  wireDrawer(t);
}

const mentionise = s => esc(s).replace(/@([A-Za-z][\w.-]*)/g, '<span class="mention">@$1</span>');

/* Minimal markdown for generated drafts. Escapes first, formats second, so
   model output can never inject markup. */
function md(src) {
  const lines = esc(src || '').split('\n');
  const out = []; let list = false;
  const inline = t => t
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|\s)\*([^*]+)\*/g, '$1<i>$2</i>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
  for (const raw of lines) {
    const l = raw.trimEnd();
    const bullet = /^\s*[-*]\s+(.*)$/.exec(l);
    if (bullet) { if (!list) { out.push('<ul>'); list = true; }
      out.push('<li>' + inline(bullet[1]) + '</li>'); continue; }
    if (list) { out.push('</ul>'); list = false; }
    const h = /^(#{1,4})\s+(.*)$/.exec(l);
    if (h) { const n = Math.min(h[1].length + 1, 4);
      out.push(`<h${n}>${inline(h[2])}</h${n}>`); continue; }
    if (/^>\s?/.test(l)) { out.push('<blockquote>' + inline(l.replace(/^>\s?/, '')) + '</blockquote>'); continue; }
    if (/^(---|\*\*\*)$/.test(l)) { out.push('<hr>'); continue; }
    if (!l.trim()) { out.push(''); continue; }
    out.push('<p>' + inline(l) + '</p>');
  }
  if (list) out.push('</ul>');
  return out.join('\n');
}

async function openRecord(key) {
  const r = await api('/api/record?key=' + encodeURIComponent(key));
  const skip = new Set(['key', 'body', 'detail', 'notes', 'changes', 'tasks', 'snip', 'score']);
  let detail = {}; try { detail = JSON.parse(r.detail || '{}'); } catch {}
  $('#drawer-body').innerHTML = `<div class="eyebrow">${esc(r.kind)} · SOURCE RECORD</div>
    <h2 class="head-tight">${esc(r.title)}</h2>
    ${r.url ? `<p><a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener">Open the primary record ↗</a></p>` : ''}
    <div class="tablewrap"><table><tbody>
    ${Object.entries(r).filter(([k, v]) => !skip.has(k) && v !== null && v !== '')
      .map(([k, v]) => `<tr><th>${esc(k.replace(/_/g, ' '))}</th>
        <td>${k === 'amount' ? money(v) : esc(v)}</td></tr>`).join('')}
    ${Object.entries(detail).filter(([, v]) => v !== null && v !== '')
      .map(([k, v]) => `<tr><th>${esc(k.replace(/_/g, ' '))}</th><td>${esc(v)}</td></tr>`).join('')}
    </tbody></table></div>
    ${r.tasks.length ? `<h3 class="mt-lg">Linked work</h3>
      ${r.tasks.map(t => `<div class="activity"><button class="linkish" data-task="${esc(t.id)}">${esc(t.title)}</button>
        <span class="muted small">· ${esc(t.status)}</span></div>`).join('')}` : ''}
    <div class="toolbar"><button class="btn" data-act="task-from-record" data-key="${esc(key)}">
      ＋ Create a task from this record</button></div>`;
  if (!$('#drawer').open) $('#drawer').showModal();
  wire();
}

/* ------------------------------------------------------------- wiring */
function applyMetrics() {
  // A strict Content-Security-Policy refuses style attributes written into
  // markup, so anything positional is carried as data-* and set through the
  // CSSOM here, which the policy does allow.
  $$('[data-w]').forEach(el => { el.style.width = Number(el.dataset.w) + '%'; });
  $$('[data-l]').forEach(el => { el.style.left = Number(el.dataset.l) + '%'; });
}

function wire() {
  applyMetrics();
  $$('[data-task]').forEach(el => el.onclick = e => {
    if (e.target.matches('input[type=checkbox]')) return;
    e.preventDefault(); openTask(el.dataset.task);
  });
  $$('[data-record]').forEach(el => el.onclick = e => {
    e.preventDefault(); openRecord(el.dataset.record);
  });
  // A citation is only useful if it reaches the memo. One click copies it.
  $$('[data-cite]').forEach(el => {
    el.style.cursor = 'copy';
    el.onclick = async () => {
      try { await navigator.clipboard.writeText(el.dataset.cite);
        toast('Citation copied'); }
      catch (err) { toast('Select and copy — the browser blocked the clipboard'); }
    };
  });
  $$('[data-complete]').forEach(el => el.onchange = async () => {
    try { const r = await api('/api/task/complete',
        { id: el.dataset.complete, actor: state.me });
      toast(r.unblocked?.length ? `Completed — unblocked ${r.unblocked.length} task(s)`
        : r.note || 'Completed');
      loadShell(); render();
    } catch (e) { toast(e.message); }
  });
  $$('[data-view]').forEach(b => b.onclick = () => {
    location.hash = `#project?project=${state.params.project}&view=${b.dataset.view}`;
  });
  $$('[data-facet]').forEach(sel => sel.onchange = () => {
    const q = (state.params.q || '') + ` ${sel.dataset.facet}:"${sel.value}"`;
    location.hash = '#search?q=' + encodeURIComponent(q.trim());
  });
  $$('[data-prompt]').forEach(b => b.onclick = () => {
    const f = $('#ask-form'); if (!f) return;
    f.question.value = b.dataset.prompt; f.kind.value = b.dataset.kind;
    f.requestSubmit();
  });
  $$('[data-transcript]').forEach(b => b.onclick = () => promptTranscript(b.dataset.transcript));
  $$('[data-act]').forEach(b => b.onclick = () => action(b.dataset.act, b));

  // A select that simply narrows the current view: change one parameter and
  // stay put, rather than resetting every other choice the user has made.
  $$('[data-param]').forEach(sel => sel.onchange = () => {
    const params = { ...state.params, [sel.dataset.param]: sel.value };
    if (!sel.value) delete params[sel.dataset.param];
    delete params.packet;
    location.hash = `#${state.route}?` + new URLSearchParams(params);
  });
  $$('[data-ack]').forEach(b => b.onclick = async () => {
    try { await api('/api/changes/ack', { id: b.dataset.ack, who: state.me });
      toast('Marked seen'); render();
    } catch (e) { toast(e.message); }
  });
  $$('[data-packet]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = 'Preparing…';
    try { await api('/api/meetings/build', { event_id: b.dataset.packet, who: state.me });
      toast('Building the packet — it will appear when the evidence is assembled.');
    } catch (e) { toast(e.message); b.disabled = false; b.textContent = 'Prepare'; }
  });
  $$('[data-sync]').forEach(b => b.onclick = async () => {
    try { await api('/api/connect/sync', { what: b.dataset.sync });
      toast(`Syncing ${b.dataset.sync} in the background…`);
    } catch (e) { toast(e.message); }
  });

  const sf = $('#search-form');
  if (sf) sf.onsubmit = e => { e.preventDefault();
    location.hash = '#search?q=' + encodeURIComponent(sf.q.value); };
  const af = $('#ask-form'); if (af) af.onsubmit = onAsk;
  const bf = $('#brief-form'); if (bf) bf.onsubmit = onBrief;
  const stf = $('#status-form'); if (stf) stf.onsubmit = onStatus;
  wireDrag();
}

function wireDrag() {
  let dragged = null;
  $$('.tcard').forEach(card => {
    card.ondragstart = e => { dragged = card; card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move'; };
    card.ondragend = () => { card.classList.remove('dragging'); dragged = null; };
  });
  $$('.col').forEach(col => {
    col.ondragover = e => { e.preventDefault(); col.classList.add('over'); };
    col.ondragleave = () => col.classList.remove('over');
    col.ondrop = async e => {
      e.preventDefault(); col.classList.remove('over');
      if (!dragged) return;
      const id = dragged.dataset.task;
      try {
        await api('/api/task/move', { id, section_id: col.dataset.section,
          position: [...col.querySelectorAll('.tcard')].length, actor: state.me });
        render();
      } catch (err) { toast(err.message); }
    };
  });
}

function wireDrawer(t) {
  wire();
  $('#task-form').onsubmit = async e => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(e.target));
    d.actor = state.me;
    if (d.due) d.due = d.due + ':00';
    try { await api('/api/tasks', d); toast('Saved'); $('#drawer').close();
      loadShell(); render(); } catch (err) { toast(err.message); }
  };
  $('#subtask-form').onsubmit = async e => {
    e.preventDefault();
    const title = e.target.title.value.trim(); if (!title) return;
    await api('/api/tasks', { title, parent_id: t.id, project_id: t.project_id,
      owner: t.owner, actor: state.me });
    openTask(t.id);
  };
  $('#check-form').onsubmit = async e => {
    e.preventDefault();
    const title = e.target.title.value.trim(); if (!title) return;
    await api('/api/checklist', { task_id: t.id, title, actor: state.me });
    openTask(t.id);
  };
  $('#comment-form').onsubmit = async e => {
    e.preventDefault();
    const body = e.target.body.value.trim(); if (!body) return;
    const r = await api('/api/comments', { task_id: t.id, body, author: state.me });
    if (r.mentions?.length) toast('Notified ' + r.mentions.join(', '));
    openTask(t.id);
  };
  $$('[data-check]').forEach(cb => cb.onchange = async () => {
    await api('/api/checklist', { id: cb.dataset.check, task_id: t.id,
      done: cb.checked, actor: state.me });
  });
}

async function onAsk(e) {
  e.preventDefault();
  const f = new FormData(e.target);
  const out = $('#ask-out');
  out.innerHTML = '<div class="loading">Searching the record, then writing…</div>';
  try {
    const r = await api('/api/assistant', {
      question: f.get('question'), kind: f.get('kind'),
      register: f.get('register'), council: !!f.get('council'), actor: state.me });
    const ev = r.evidence || {};
    out.innerHTML = `<div class="card mt-md" >
      <div class="toolbar"><span class="tag">${esc(r.kind)}</span>
        <span class="tag ${r.mode === 'written' ? 'good' : 'warn'}">${esc(r.mode)}</span>
        <span class="muted small">${num(ev.hit_count)} records · ${ev.search_ms} ms
        ${ev.strategy && ev.strategy !== 'exact' ? `· ${esc(ev.strategy)} match` : ''}</span>
        <button class="btn sm" data-act="copy-answer">Copy</button></div>
      ${r.blocked ? `<div class="note">${esc(r.why)}</div>` : ''}
      ${r.quotes ? r.quotes.map(q => `<blockquote class="card my-sm" >
        “${esc(q.text)}”<div class="small muted">— ${esc(q.source)}
        ${q.url ? `· <a href="${esc(safeURL(q.url))}" target="_blank" rel="noopener">clip ↗</a>` : ''}</div>
        </blockquote>`).join('') : ''}
      <div id="answer-body" class="doc" data-raw="${esc(r.body || '')}">${md(r.body || '')}</div>
      ${(ev.gaps || []).length ? `<div class="note"><b>Gaps.</b><br>${ev.gaps.map(esc).join('<br>')}</div>` : ''}
    </div>`;
    wire();
  } catch (err) { out.innerHTML = `<div class="note">${esc(err.message)}</div>`; }
}

async function onStatus(e) {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  try {
    await api('/api/project/status', { ...f, project_id: state.params.project, author: state.me });
    toast('Status posted'); render();
  } catch (err) { toast(err.message); }
}

function promptTranscript(id) {
  const text = window.prompt('Paste the transcript for this clip. Quotes may only be drawn from real recordings.');
  if (!text) return;
  api('/api/media/transcript', { id, transcript: text, actor: state.me })
    .then(() => { toast('Transcript attached'); render(); })
    .catch(e => toast(e.message));
}

async function action(act, el) {
  try {
    if (act === 'reload') return render();
    if (act === 'close-drawer') return $('#drawer').close();
    if (act === 'refresh') { await api('/api/refresh', { full: false });
      return toast('Refreshing sources in the background…'); }
    if (act === 'refresh-media') { await api('/api/media/refresh', {});
      return toast('Collecting media in the background…'); }
    if (act === 'reindex') { await api('/api/reindex', {});
      return toast('Rebuilding the search index…'); }
    if (act === 'scan') { await api('/api/watch/scan', {});
      return toast('Scanning the budget and the docket for changes…'); }
    if (act === 'week') { await api('/api/meetings/week', { days: state.params.days || 7 });
      return toast('Preparing every meeting in the window…'); }
    if (act === 'brief-record') {
      const key = el.dataset.key;
      toast('Writing the brief…');
      await api('/api/brief/run', { kind: 'record', subject: key });
      return toast('Brief queued — it appears under Run a brief when done.');
    }
    if (act === 'law-go') {
      const q = ($('#law-q')?.value || '').trim();
      if (!q) return;
      return location.hash = '#law?mode=' + (q.includes('-') ? 'section' : 'find')
        + '&ref=' + encodeURIComponent(q);
    }
    if (act === 'position-find') {
      const q = ($('#pos-q')?.value || '').trim();
      const box = $('#pos-hits');
      if (!q) { box.innerHTML = '<p class="muted">Type something to look for.</p>'; return; }
      box.innerHTML = '<p class="muted">Looking…</p>';
      const hits = await api('/api/position/find?q=' + encodeURIComponent(q));
      box.innerHTML = hits.length
        ? `<table class="grid"><thead><tr><th class="num">Amount</th><th>What</th>
             <th>Kind</th><th>Where it is written</th></tr></thead><tbody>
           ${hits.map(h => `<tr><td class="num">${fmtMoney(h.amount)}</td>
             <td>${esc(h.label)}</td><td>${esc(h.kind)}</td>
             <td class="muted">${esc(h.locator)}</td></tr>`).join('')}</tbody></table>`
        : '<p class="muted">Nothing in the reconciliation mentions that.</p>';
      return;
    }
    if (act === 'breakdown-go') {
      const params = { ...state.params, q: $('#bd-q')?.value || '' };
      if (!params.q) delete params.q;
      delete params.packet;
      return location.hash = '#breakdown?' + new URLSearchParams(params);
    }
    if (act === 'read-all') { await api('/api/inbox/read', { person: state.me });
      loadShell(); return render(); }
    if (act === 'new-task') return newTask(el?.dataset.project, el?.dataset.section);
    if (act === 'post-status') return location.hash =
      `#project?project=${state.params.project}&view=updates`;
    if (act === 'complete') {
      const id = $('#task-form')?.id?.value || $('[name=id]')?.value;
      await api('/api/task/complete', { id, actor: state.me });
      $('#drawer').close(); loadShell(); return render();
    }
    if (act === 'copy-answer') {
      await navigator.clipboard.writeText($('#answer-body').dataset.raw || '');
      return toast('Copied');
    }
    if (act === 'task-from-record') {
      const title = window.prompt('Task title'); if (!title) return;
      const t = await api('/api/tasks', { title, owner: state.me, actor: state.me });
      await api('/api/task/link', { task_id: t.id, record_key: el.dataset.key });
      $('#drawer').close(); toast('Task created and linked'); return render();
    }
  } catch (e) { toast(e.message); }
}

async function newTask(projectId, sectionId) {
  const title = window.prompt('Task title');
  if (!title) return;
  try {
    await api('/api/tasks', { title, project_id: projectId || state.params.project || '',
      section_id: sectionId || '', owner: state.me, actor: state.me });
    toast('Task created'); loadShell(); render();
  } catch (e) { toast(e.message); }
}

/* ------------------------------------------------------------ palette */
let paletteTimer = null;
function openPalette() {
  $('#palette').showModal();
  const input = $('#palette-input'); input.value = ''; input.focus();
  $('#palette-results').innerHTML = '';
  input.oninput = () => {
    clearTimeout(paletteTimer);
    paletteTimer = setTimeout(async () => {
      const q = input.value.trim();
      if (q.length < 2) return $('#palette-results').innerHTML = '';
      const rows = await api('/api/suggest?q=' + encodeURIComponent(q));
      $('#palette-results').innerHTML = rows.map(r =>
        `<button class="pres" data-record="${esc(r.key)}">
          <span class="k">${esc(r.kind)}</span><br>${esc(r.title)}</button>`).join('')
        + `<button class="pres" data-search="${esc(q)}"><span class="k">full search</span><br>
           Search everything for “${esc(q)}”</button>`;
      $$('#palette-results [data-record]').forEach(b => b.onclick = () => {
        $('#palette').close(); openRecord(b.dataset.record); });
      $$('#palette-results [data-search]').forEach(b => b.onclick = () => {
        $('#palette').close();
        location.hash = '#search?q=' + encodeURIComponent(b.dataset.search); });
    }, 120);
  };
}

/* --------------------------------------------------------------- boot */
function boot() {
  $('#login-form').onsubmit = async e => {
    e.preventDefault();
    state.token = $('#token').value.trim();
    localStorage.setItem('d49tok', state.token);
    $('#login').close();
    await start();
  };
  $('#omni').onclick = openPalette;
  $('#menu').onclick = () => $('#sidebar').classList.toggle('open');
  $('#me').onchange = e => { state.me = e.target.value;
    localStorage.setItem('d49me', state.me); loadShell(); render(); };
  $('#add-program').onclick = async () => {
    const name = window.prompt('Program name'); if (!name) return;
    await api('/api/programs', { name, actor: state.me });
    loadShell(); toast('Program created');
  };
  document.addEventListener('keydown', e => {
    if (e.key === '/' && !/input|textarea|select/i.test(e.target.tagName)) {
      e.preventDefault(); openPalette();
    }
    if (e.key === 'Escape' && $('#palette').open) $('#palette').close();
  });
  window.addEventListener('hashchange', route);
  start();
}

async function start() {
  try {
    await loadShell();
    connectStream();
    await route();
  } catch (e) {
    if (!$('#login').open) $('#login').showModal();
  }
}

boot();

/* --------------------------------------------------- what changed */
const SEV = { high: 'alert', medium: 'warn', low: '' };

VIEWS.changes = async () => {
  const hours = Number(state.params.hours || 168);
  const [digest, status] = await Promise.all([
    api('/api/changes/digest?hours=' + hours),
    api('/api/changes/status')]);
  const last = status.last_scan || {};
  const rows = digest.items.map(c => `
    <tr class="sev-${esc(c.severity)}">
      <td><span class="tag ${SEV[c.severity] || ''}">${esc(c.severity)}</span></td>
      <td><strong>${esc(c.label || '')}</strong>
          <div class="muted">${esc(c.field || c.change)}:
            <s>${esc(c.before ?? '—')}</s> → <b>${esc(c.after ?? '—')}</b></div>
          <div class="muted">${esc(c.why || '')}</div></td>
      <td class="nowrap muted">${esc((c.at || '').slice(0, 16).replace('T', ' '))}</td>
      <td class="nowrap">${esc(c.source_id || '')}</td>
      <td>${c.acknowledged ? '<span class="muted">seen</span>'
            : `<button class="btn tiny" data-ack="${esc(c.id)}">Mark seen</button>`}</td>
    </tr>`).join('');

  return head('Live', 'What changed',
      'Every watched record is fingerprinted on each scan. A diff is arithmetic, not a guess.')
    + `<div class="cards">
        ${metric('Changes', digest.total, `last ${Math.round(hours / 24)} days`)}
        ${metric('Worth attention', digest.high, 'high severity')}
        ${metric('Reversals', digest.reversals.length, 'money pulled back')}
        ${metric('Net movement', fmtMoney(digest.net_funding_delta), 'tracked awards')}
      </div>
      <div class="card"><p class="lead">${esc(digest.headline)}</p>
        <div class="row gap">
          <button class="btn" data-act="scan">Scan now</button>
          <select data-param="hours">
            ${[24, 72, 168, 720].map(h => `<option value="${h}"${h === hours ? ' selected' : ''}>
              last ${h < 48 ? h + ' hours' : Math.round(h / 24) + ' days'}</option>`).join('')}
          </select>
          <span class="muted">Last scan ${esc((last.at || 'never').slice(0, 16).replace('T', ' '))}
            · watching ${Object.values(status.watched || {}).reduce((a, b) => a + b, 0).toLocaleString()} records</span>
        </div></div>`
    + (rows ? `<div class="card table-wrap"><table class="grid">
        <thead><tr><th></th><th>What moved</th><th>When</th><th>Source</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
      : `<div class="empty"><h3>Nothing has moved</h3>
          <p>No tracked change in this window. Run a scan to check again.</p></div>`);
};

/* ----------------------------------------------------- breakdowns */
VIEWS.breakdown = async () => {
  const by = state.params.by || 'member';
  const q = state.params.q || '';
  const kind = state.params.kind || 'funding';
  const fy = state.params.fy || '';
  const dims = await api('/api/breakdown/dimensions');
  const qs = new URLSearchParams({ by, q, kind, top: '25' });
  if (fy) qs.set('fy', fy);
  const cut = await api('/api/breakdown?' + qs);

  const rows = cut.buckets.map(b => `
    <tr><td><strong>${esc(b.display || b.value)}</strong></td>
      <td class="num">${b.records.toLocaleString()}</td>
      <td class="num">${fmtMoney(b.amount)}</td>
      <td class="num">${fmtMoney(b.confirmed)}</td>
      <td class="num${b.pending ? ' warn-text' : ''}">${fmtMoney(b.pending)}</td>
      <td class="num${b.reversed ? ' alert-text' : ''}">${fmtMoney(b.reversed)}</td></tr>`).join('');

  return head('Research', 'Breakdowns',
      'Cut any legislation or budget search by member, initiative, committee, agency, year or tier.')
    + `<div class="card"><div class="row gap wrap">
        <input id="bd-q" class="grow" placeholder="Filter the records first (optional)"
               value="${esc(q)}">
        <select data-param="by">${dims.map(d =>
          `<option value="${esc(d.name)}"${d.name === by ? ' selected' : ''}>by ${esc(d.name)}</option>`).join('')}</select>
        <select data-param="kind">${['funding', 'matter', 'org', 'calendar'].map(k =>
          `<option value="${esc(k)}"${k === kind ? ' selected' : ''}>${esc(k)}</option>`).join('')}</select>
        <select data-param="fy"><option value="">all years</option>${
          [2027, 2026, 2025, 2024, 2023, 2022].map(y =>
          `<option value="${y}"${String(y) === String(fy) ? ' selected' : ''}>FY${y}</option>`).join('')}</select>
        <button class="btn primary" data-act="breakdown-go">Break it down</button>
      </div>
      <p class="muted">${esc((dims.find(d => d.name === by) || {}).means || '')}</p></div>`
    + `<div class="cards">
        ${metric('Records', cut.totals.records.toLocaleString(), `${cut.totals.buckets} buckets`)}
        ${metric('Tracked', fmtMoney(cut.totals.amount), 'all matching lines')}
        ${metric('Confirmed', fmtMoney(cut.totals.confirmed), 'nothing outstanding')}
        ${metric('Pending', fmtMoney(cut.totals.pending), 'needs a modification')}
      </div>`
    + `<div class="card table-wrap"><table class="grid">
        <thead><tr><th>${esc(by)}</th><th class="num">Records</th><th class="num">Amount</th>
        <th class="num">Confirmed</th><th class="num">Pending</th><th class="num">Reversed</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="6">Nothing matched.</td></tr>'}</tbody></table>
        ${cut.other ? `<p class="muted">${cut.other.buckets} further buckets hold
          ${cut.other.records.toLocaleString()} records and ${fmtMoney(cut.other.amount)}.</p>` : ''}
        <p class="note">Confirmed is money with nothing outstanding against it. Pending needs a
        budget modification before it can be announced. Reversed is the signed value of lines a
        later resolution undid — pair a reversal with the award it cancels before quoting a net figure.</p>
        <p class="muted">Sources: ${esc(cut.sources.join(', ') || '—')}</p></div>`;
};

/* ------------------------------------------------------- meetings */
VIEWS.meetings = async () => {
  const days = Number(state.params.days || 14);
  const [items, packets] = await Promise.all([
    api('/api/meetings?days=' + days), api('/api/meetings/packets?limit=40')]);
  const PREPARABLE = ['oversight', 'hearing', 'stated', 'caucus'];
  const live = items.filter(e => !['deferred', 'none'].includes(e.packet_kind));
  const off = items.filter(e => e.packet_kind === 'deferred');
  const byEvent = {};
  packets.forEach(p => { byEvent[p.event_id] = p; });

  const row = e => `<tr>
    <td class="nowrap">${esc((e.start || '').slice(0, 16).replace('T', ' '))}</td>
    <td><span class="tag${e.packet_kind === 'oversight' ? ' alert' : ''}">${esc(e.packet_kind)}</span></td>
    <td><strong>${esc(e.summary || '')}</strong>
        <div class="muted">${esc(e.committee || '')}${e.location ? ' · ' + esc(e.location) : ''}</div></td>
    <td>${(e.owners || []).map(o => `<span class="avatar tiny">${esc(o)}</span>`).join(' ')}</td>
    <td class="nowrap">${byEvent[e.event_id]
      ? `<a class="btn tiny" href="#meetings?packet=${encodeURIComponent(byEvent[e.event_id].id)}">Read packet</a>`
      : PREPARABLE.includes(e.packet_kind)
        ? `<button class="btn tiny" data-packet="${esc(e.event_id)}">Prepare</button>`
        : ''}</td></tr>`;

  if (state.params.packet) {
    const got = await api('/api/meetings/packet?id=' + encodeURIComponent(state.params.packet));
    return head('Meetings', got.title, `${esc(got.kind)} packet · ${esc(got.mode)}`)
      + `<div class="card"><a class="btn tiny" href="#meetings">← All meetings</a></div>`
      + `<div class="card doc">${md(got.body || '')}</div>`;
  }

  return head('Meetings', 'Meetings and packets',
      'Every hearing on the calendar, with the agenda, the district stake, and the questions ready.')
    + `<div class="cards">
        ${metric('Ahead', live.length, `next ${days} days`)}
        ${metric('Oversight', live.filter(e => e.packet_kind === 'oversight').length, 'question sets needed')}
        ${metric('Packets built', packets.length, 'saved')}
        ${metric('Deferred', off.length, 'no packet needed')}
      </div>
      <div class="card"><div class="row gap">
        <button class="btn primary" data-act="week">Prepare the week</button>
        <select data-param="days">${[7, 14, 30, 60].map(d =>
          `<option value="${d}"${d === days ? ' selected' : ''}>next ${d} days</option>`).join('')}</select>
      </div></div>`
    + `<div class="card table-wrap"><table class="grid">
        <thead><tr><th>When</th><th>Kind</th><th>Meeting</th><th>Owners</th><th></th></tr></thead>
        <tbody>${live.map(row).join('') || '<tr><td colspan="5">Nothing scheduled.</td></tr>'}</tbody>
      </table></div>`
    + (off.length ? `<div class="card"><h3>Deferred or cancelled</h3>
        <p class="muted">These stay on the calendar but are not happening, so no packet is built.</p>
        <ul>${off.map(e => `<li>${esc((e.start || '').slice(0, 10))} — ${esc(e.summary)}</li>`).join('')}</ul>
      </div>` : '');
};

/* ---------------------------------------------------- connections */
VIEWS.connections = async () => {
  const c = await api('/api/connect/status');
  const creds = Object.entries(c.credentials).map(([name, info]) => `
    <tr><td><code>${esc(name)}</code></td>
      <td>${info.configured ? `<span class="tag">yes · ${info.count}</span>`
                            : '<span class="muted">not set</span>'}</td>
      <td class="muted">${esc(info.source)}</td>
      <td class="muted">${esc((info.fingerprints || []).join(', '))}</td></tr>`).join('');
  const sheets = (c.sheets || []).map(sh => `
    <tr><td><strong>${esc(sh.label)}</strong><div class="muted">${esc(sh.role)}</div></td>
      <td>${sh.last_status === 'ok' ? '<span class="tag">ok</span>'
            : sh.last_status ? '<span class="tag alert">failed</span>'
            : '<span class="muted">never synced</span>'}</td>
      <td class="muted">${esc(sh.last_sync || '—')}</td>
      <td class="num">${sh.last_rows ? sh.last_rows.toLocaleString() : ''}</td></tr>`).join('');
  const cal = c.calendar || {};
  const tr = c.transcripts || {};

  return head('Live', 'Connections',
      'Where this system reaches out, what answered, and what is still missing.')
    + `<div class="cards">
        ${metric('AI', c.ai.mode === 'deliberated' ? 'ready' : 'scaffold',
                 `${c.ai.keys_configured || 0} key(s) · ${esc(c.ai.model)}`)}
        ${metric('Calendar', cal.events ? cal.events.toLocaleString() : '—',
                 cal.transport ? 'via ' + esc(cal.transport) : 'not synced')}
        ${metric('Sheets', (c.sheets || []).filter(x => x.last_status === 'ok').length + '/' + (c.sheets || []).length,
                 'books syncing')}
        ${metric('Transcripts', (tr.coverage_pct || 0) + '%', `${tr.with_transcript || 0} of ${tr.media || 0}`)}
      </div>
      <div class="card"><div class="row gap wrap">
        <button class="btn" data-sync="calendar">Sync calendar</button>
        <button class="btn" data-sync="sheets">Sync sheets</button>
        <button class="btn" data-sync="captions">Fetch transcripts</button>
      </div>
      <p class="note">Credentials live in your environment or <code>~/.d49/config.json</code>
      with owner-only permissions. Nothing here is ever written into the repository.
      Set one with <code>universe connect key set &lt;name&gt;</code>.</p></div>`
    + `<div class="card table-wrap"><h3>Credentials</h3><table class="grid">
        <thead><tr><th>Name</th><th>Configured</th><th>Read from</th><th>Fingerprint</th></tr></thead>
        <tbody>${creds}</tbody></table></div>`
    + `<div class="card table-wrap"><h3>Books</h3><table class="grid">
        <thead><tr><th>Sheet</th><th>Status</th><th>Last sync</th><th class="num">Rows</th></tr></thead>
        <tbody>${sheets || '<tr><td colspan="4">No sheets registered.</td></tr>'}</tbody></table></div>`
    + `<div class="card"><h3>Transcripts</h3><p>${esc(tr.note || '')}</p>
        <p class="muted">Quotes can only be drawn from a stored transcript. Where a transcript
        is missing the item is summarisable but not quotable.</p></div>`;
};

async function onBrief(e) {
  e.preventDefault();
  const f = e.target;
  const kind = f.kind.value;
  const subject = (f[`sub_${kind}`]?.value || '').trim();
  const note = $('#brief-status');
  const btn = f.querySelector('button[type=submit]');
  btn.disabled = true;
  note.textContent = 'Assembling the evidence…';
  try {
    await api('/api/brief/run', { kind, subject, ask: f.ask.value.trim(),
                                  council: f.council.checked });
    note.textContent = 'Writing. It will appear below when the checks have run.';
  } catch (err) {
    note.textContent = err.message;
    btn.disabled = false;
  }
}

function fmtMoney(n) {
  const v = Number(n || 0);
  if (!v) return '$0';
  const sign = v < 0 ? '-' : '';
  const a = Math.abs(v);
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${sign}$${Math.round(a / 1e3)}K`;
  return `${sign}$${Math.round(a)}`;
}


/* ------------------------------------------------------ run a brief */
const VERDICT_CLASS = { 'shippable': 'ok', 'shippable with notes': 'warn', 'blocked': 'alert' };

VIEWS.briefs = async () => {
  const [kinds, past] = await Promise.all([
    api('/api/brief/kinds'), api('/api/pipeline/receipts?limit=20')]);

  if (state.params.id) {
    const got = await api('/api/pipeline/receipt?id=' + encodeURIComponent(state.params.id));
    const r = got.receipt;
    const stages = (r?.stages || []).map(st => `
      <div class="stg ${st.passed ? 'ok' : 'bad'}">
        <div class="row between"><strong>${esc(st.name)}</strong>
          <span class="muted">${st.gates.filter(g => g.passed).length}/${st.gates.length} checks</span></div>
        ${st.gates.filter(g => !g.passed).map(g =>
          `<p class="muted">${g.severity === 'blocking' ? '✕' : '!'} ${esc(g.asks)} — ${esc(g.found)}</p>`).join('')}
      </div>`).join('');
    return head('Run a brief', got.subject || got.deliverable_id,
        `${esc(got.kind)} brief · ${esc(got.status)}`)
      + `<div class="card"><a class="btn tiny" href="#briefs">← All briefs</a></div>`
      + (r ? `<div class="card"><p class="lead">${esc(r.why)}</p>
          <div class="stages">${stages}</div>
          <p class="note">Every stage names what it consumed and what it produced.
          A blocking check that fails stops the brief from being filed.</p></div>` : '')
      + `<div class="card doc">${md(got.body || '*The brief text is on disk under this id.*')}</div>`;
  }

  const options = kinds.map((k, i) => `
    <label class="pick">
      <input type="radio" name="kind" value="${esc(k.kind)}" ${i === 0 ? 'checked' : ''}>
      <div><strong>${esc(k.label)}</strong><p class="muted">${esc(k.does)}</p>
        <input class="sub" name="sub_${esc(k.kind)}" placeholder="${esc(k.placeholder)}"
               aria-label="${esc(k.subject_label)}"></div>
    </label>`).join('');

  const rows = past.map(b => `
    <tr>
      <td class="nowrap"><a href="#briefs?id=${encodeURIComponent(b.deliverable_id)}">
        ${esc(b.deliverable_id)}</a></td>
      <td>${esc(b.subject || '')}<div class="muted">${esc(b.kind || '')}</div></td>
      <td><span class="tag ${VERDICT_CLASS[b.verdict] || ''}">${esc(b.verdict || b.status)}</span></td>
      <td class="muted">${esc((b.why || '').slice(0, 120))}</td>
      <td class="nowrap muted">${esc((b.created || '').slice(0, 16).replace('T', ' '))}</td>
    </tr>`).join('');

  // The live stream re-renders this view whenever anything is written, which
  // wiped the "writing…" message typed into the form. Read the running job
  // from shell state instead, so progress survives a re-render.
  const job = state.status?.job || {};
  const running = job.status === 'Running' && /brief/i.test(job.name || '');

  return head('Deliverables', 'Run a brief',
      'Pick what you need. The evidence is assembled from the record first, and every check is run before it is filed.')
    + (running ? `<div class="card banner"><strong>${esc(job.name)}</strong>
        <p class="muted">Assembling the evidence, writing it, then running every check.
        It appears below when it is done — you can leave this page.</p></div>` : '')
    + `<div class="card">
        <form id="brief-form">
          <div class="picks">${options}</div>
          <label class="fld"><span>What is the question? (optional, but it records who the answer is for)</span>
            <input name="ask" placeholder="What is our FY27 position going into the Finance hearing?"></label>
          <label class="chk"><input type="checkbox" name="council">
            <span>Run the LLM Council — five perspectives argue it, then review each other. Slower.</span></label>
          <div class="row gap">
            <button class="btn primary" type="submit">Write the brief</button>
            <span class="muted" id="brief-status"></span>
          </div>
        </form>
        <p class="note">Nothing is invented. Every figure must appear in the evidence or be
        flagged unverified, and a brief that fails a blocking check is saved as a draft rather
        than filed.</p>
      </div>`
    + `<div class="card table-wrap"><h3>Briefs already written</h3><table class="grid">
        <thead><tr><th>Id</th><th>Subject</th><th>Verdict</th><th>Why</th><th>When</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5">None yet.</td></tr>'}</tbody></table></div>`;
};

/* --------------------------------------------------- the district position */
/* Five true answers to five different questions, always shown together and
   always with the question attached. The office's recurring public error is
   not arithmetic -- it is quoting the $3.0M designation figure in a room that
   is asking about the $35.9M the district actually got. */
const tieBadge = t => t === true ? '<b class="pill good">ties ✓</b>'
  : t === false ? '<b class="pill alert">does not tie</b>'
    : '<b class="pill">no check</b>';

VIEWS.position = async () => {
  const fy = state.params.fy || '2027';
  const [pos, cats, chans] = await Promise.all([
    api('/api/position?fy=' + encodeURIComponent(fy)),
    api('/api/position/categories'),
    api('/api/position/channels')]);

  const bases = pos.bases.map(b => b.amount == null
    ? `<div class="card"><h3>${esc(b.question)}</h3>
        <p class="warn-text">Not loaded — sync the budget repository.</p></div>`
    : `<div class="card"><h3>${esc(b.question)} ${tieBadge(b.ties)}</h3>
        <p class="n">${fmtMoney(b.amount)}</p>
        <p class="muted">${b.lines ? num(b.lines) + ' lines · ' : ''}${esc(b.source || '')}</p>
        <p class="note">${esc(b.caution)}</p></div>`).join('');

  const catRows = cats.map(c => `<tr><td>${esc(c.category)}</td>
    <td class="num">${fmtMoney(c.amount)}</td>
    <td class="num">${c.share}%</td></tr>`).join('');
  const chanRows = chans.map(c => `<tr><td>${esc(c.channel)}</td>
    <td>${esc(c.section)}</td>
    <td class="num">${fmtMoney(c.total)}</td></tr>`).join('');

  const tc = pos.tie_checks || {};
  const failing = (tc.failing || []).map(c =>
    `<li class="alert-text"><strong>${esc(c.sheet)}</strong> — ${esc(c.check)}</li>`).join('');

  return head('Money', `The district position — FY${esc(String(pos.fy))}`,
      'Every defensible answer to "what did District 49 get", side by side. '
      + 'Quoting one without its question is how an office contradicts itself in public.')
    + `<div class="cards">${bases}</div>`
    + `<div class="card"><h3>Reconciliation checks</h3>
        <p>${tc.passing} of ${tc.total} passing. A tie-check is the row where the
        office proved its workbook reproduces the City's printed book, page by
        page. A figure standing on a sheet that ties has been checked against
        the City's own document; one without a check is an assertion.</p>
        ${failing ? `<ul>${failing}</ul>` : ''}
        ${(pos.notes || []).map(n => `<p class="warn-text">${esc(n)}</p>`).join('')}</div>`
    + `<div class="card table-wrap"><h3>By category — the itemised basis</h3>
        <table class="grid"><thead><tr><th>Category</th>
          <th class="num">Amount</th><th class="num">Share</th></tr></thead>
        <tbody>${catRows || '<tr><td colspan="3">Not loaded.</td></tr>'}</tbody>
        </table></div>`
    + `<div class="card table-wrap"><h3>By channel — which door the money came through</h3>
        <table class="grid"><thead><tr><th>Component</th>
          <th>Section</th><th class="num">Amount</th></tr></thead>
        <tbody>${chanRows || '<tr><td colspan="3">Not loaded.</td></tr>'}</tbody>
        </table>
        <p class="note">These components are non-overlapping and come from the
        reconciliation's own decomposition, not from adding up the workbook.
        The same $77,350,000 of capital is restated on eight different sheets —
        that is what a reconciliation does — so summing across them would report
        roughly eight times the island's real capital.</p></div>`
    + `<div class="card"><h3>Find a figure</h3>
        <div class="row gap wrap">
          <input id="pos-q" class="grow" placeholder="Snug Harbor, St. George Theatre, an EIN…"
                 value="${esc(state.params.q || '')}">
          <button class="btn primary" data-act="position-find">Look it up</button>
        </div>
        <p class="muted">Searches every row of the reconciliation. Each hit names
        the sheet and row it was read from, so any number can be walked back to
        its cell.</p>
        <div id="pos-hits"></div></div>`;
};

/* ----------------------------------------------------- source repositories */
/* What the office has pulled from GitHub and how old it is. A file that moved
   upstream and was never reloaded is the quiet failure this view makes loud. */
VIEWS.repos = async () => {
  const d = await api('/api/repos');
  const st = d.status || {};

  const cards = Object.entries(st.repos || {}).map(([key, r]) => {
    const pills = Object.entries(r.by_status || {}).map(([k, v]) =>
      `<b class="pill${k === 'stale' || k === 'failed' ? ' alert'
        : k === 'new' ? ' warn' : ''}">${esc(k)} ${v}</b>`).join(' ');
    return `<div class="card"><h3>${esc(key)}${r.cloned === false
        ? ' <b class="pill alert">not cloned</b>' : ''}${r.role === 'code'
        ? ' <b class="pill">code</b>' : ''}</h3>
      <p class="muted"><a href="${esc(r.url)}" target="_blank" rel="noopener"
         >${esc(r.url)}</a></p>
      <p class="note">${esc(r.purpose)}</p>
      <p class="muted">${r.role === 'code'
        ? 'Tracked for its version, never ingested.'
        : `${num(r.files)} files · ${(r.bytes / 1e6).toFixed(1)} MB · last loaded
           ${r.last_ingest ? fmtFull(r.last_ingest) : 'never'}`}</p>
      <p>${pills}</p></div>`;
  }).join('');

  const stale = (d.stale || []).map(f => `<tr><td>${esc(f.path)}</td>
    <td>${esc(f.handler || '—')}</td>
    <td class="${f.status === 'failed' ? 'alert-text' : 'warn-text'}">${esc(f.status)}</td>
    <td class="muted">${f.ingested ? fmtFull(f.ingested) : 'never'}</td></tr>`).join('');

  return head('Sources', 'Source repositories',
      'Where the office’s data actually comes from, and how old it is.')
    + `<div class="cards">${cards}</div>`
    + `<div class="card table-wrap"><h3>Not loaded from current bytes</h3>
        ${stale ? `<table class="grid"><thead><tr><th>File</th><th>Handler</th>
            <th>State</th><th>Last loaded</th></tr></thead>
            <tbody>${stale}</tbody></table>`
          : '<p>Every handled file is loaded from its current bytes.</p>'}
        <p class="note">Files are compared by content hash, never by timestamp.
        A clone rewrites every timestamp, so an mtime check would report the
        whole budget repository as changed on every single run — and a report
        that cries wolf gets muted within a week.</p>
        <p class="muted">To refresh, run <code>python3 -m universe repos sync</code>
        in the terminal. It pulls both repositories and reloads whatever moved.</p>
        </div>`;
};


/* ---------------------------------------------------- sign-on decisions */
/* A list of relevant bills is not a recommendation, and the difference is the
   whole job: forty candidates arrive every week and the scarce thing is not
   finding them, it is deciding. Every verdict shows its reasoning, because a
   recommender whose reasoning is hidden gets ignored the first time it is
   wrong — and this one will be, sometimes. */
const VERDICT_TONE = {
  SIGN: 'good', WATCH: 'warn', LETTER: '', DECLINE: '', ALREADY: '',
};

VIEWS.signon = async () => {
  const want = state.params.verdict || '';
  const q = await api('/api/signon?limit=30' + (want ? '&verdict=' + encodeURIComponent(want) : ''));
  const tallies = Object.entries(q.by_verdict || {})
    .map(([k, v]) => `<a class="btn sm${want === k ? ' primary' : ''}"
        href="#signon?verdict=${esc(k)}">${esc(k)} ${v}</a>`).join(' ');

  const cards = (q.items || []).map(a => `
    <div class="card">
      <div class="row gap wrap baseline">
        <b class="pill ${VERDICT_TONE[a.verdict] || ''}">${esc(a.verdict)}</b>
        <strong>${esc(a.file)}</strong>
        <span class="muted">${esc(a.committee || '')} · ${num(a.n_sponsors)} sponsors ·
          prime ${esc(a.prime || '—')}</span>
      </div>
      <h3 class="tight">${esc(a.name || '')}</h3>
      <p class="note">${esc(a.headline)}</p>
      <ul class="small">
        ${(a.for || []).map(r => `<li>+ ${esc(r)}</li>`).join('')}
        ${(a.against || []).map(r => `<li class="alert-text">− ${esc(r)}</li>`).join('')}
      </ul>
      ${(a.law || []).length ? `<p class="small muted">Amends ${
        a.law.map(x => '§ ' + esc(x.section)).join(', ')}</p>` : ''}
      <div class="row gap wrap">
        <a class="btn sm" href="${esc(a.url)}" target="_blank" rel="noopener">Legistar</a>
        <button class="btn sm" data-act="brief-record"
          data-key="signon:${esc(a.matter_id)}">Write the memo</button>
        <button class="btn sm" data-record="matter:${esc(a.matter_id)}">The bill</button>
      </div>
    </div>`).join('');

  return head('Legislative', 'Sign-on decisions',
      'Sign, watch, decline — with the reasoning, so she can overrule it in one read.')
    + `<div class="card"><p>${esc(q.says || '')}</p>
        <div class="row gap wrap">
          <a class="btn sm${want ? '' : ' primary'}" href="#signon">all</a>
          ${tallies}
        </div></div>`
    + (cards || '<div class="card"><p>Nothing live matches the office\u2019s pillars right now. That is a real answer, not an empty one.</p></div>');
};

/* ------------------------------------------------------------- the code */
/* Drafting starts with three questions the corpus could not answer: has anyone
   legislated on this section, what happened to them, and what else lives here.
   The base rate is the useful one — it is what nobody has, because computing it
   by hand means reading a decade of Legistar. */
VIEWS.law = async () => {
  const ref = state.params.ref || '';
  const mode = state.params.mode || (ref.includes('-') ? 'section' : ref ? 'find' : '');
  let body = '';

  if (mode === 'section' && ref) {
    const d = await api('/api/law/section?ref=' + encodeURIComponent(ref));
    body = `<div class="card"><h3>${esc(d.says)}</h3>
      ${d.found ? `<p class="note">${esc(d.precedent.says)}</p>` : ''}</div>`
      + (d.found ? `<div class="card table-wrap"><table class="grid">
        <thead><tr><th>Year</th><th>Bill</th><th>Action</th><th>Outcome</th>
          <th>What it did</th></tr></thead><tbody>
        ${d.bills.map(b => `<tr><td>${b.year || '—'}</td>
          <td><button class="linkish" data-record="matter:${esc(b.matter_id)}"
            >${esc(b.file)}</button></td>
          <td>${esc(b.action)}</td>
          <td>${esc(b.stage)}${b.local_law ? ' · LL ' + esc(b.local_law) : ''}</td>
          <td class="small">${esc((b.name || '').slice(0, 90))}</td></tr>`).join('')}
        </tbody></table></div>` : '');
  } else if (mode === 'find' && ref) {
    const rows = await api('/api/law/find?q=' + encodeURIComponent(ref));
    body = `<div class="card table-wrap"><h3>Sections governing “${esc(ref)}”</h3>
      <table class="grid"><thead><tr><th>Section</th><th class="num">Bills</th>
        <th class="num">Amended</th><th>Example</th></tr></thead><tbody>
      ${rows.map(r => `<tr>
        <td><a href="#law?mode=section&ref=${esc(r.section)}">§ ${esc(r.section)}</a></td>
        <td class="num">${num(r.bills)}</td><td class="num">${num(r.amended)}</td>
        <td class="small">${esc((r.example || '').slice(0, 70))}</td></tr>`).join('')
        || '<tr><td colspan="4" class="muted">No section matched.</td></tr>'}
      </tbody></table></div>`;
  } else {
    const d = await api('/api/law');
    body = `<div class="cards">${d.bodies.map(b => `<div class="card">
      <h3>${esc(b.body_of_law.replace('_', ' '))}</h3>
      <p class="n">${num(b.sections)}</p>
      <p class="muted">sections, across ${num(b.bills)} bills
        (${num(b.refs)} references)</p></div>`).join('')}</div>`;
  }

  return head('Legislative', 'The code',
      'What each bill touches, and what has been tried there before.')
    + `<div class="card"><div class="row gap wrap">
        <input id="law-q" class="grow" value="${esc(ref)}"
          placeholder="A section (27-2004), or words — street vending, noise, sidewalk">
        <button class="btn primary" data-act="law-go">Look it up</button>
      </div>
      <p class="small muted">A section number goes straight to its history.
        Words find the sections that govern that subject, by reading the bills
        that amend them.</p></div>` + body;
};
