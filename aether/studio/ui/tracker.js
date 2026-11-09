// Tracker tab: filterable, sortable history of queued work with a detail drawer, totals and CSV export.
// Pure helpers (toCsv, csvCell, sortRows, successRate, providerTotals, fmtDuration, csvFilename ...) are exported for node tests.
import { api, h, clear, loading, failure, emptyCard, errorText, uid, when, short, labelled } from './lib.js';
import { icon } from './icons.js';
import { cliNotice, isNotFound } from './workspace.js';

const PAGE = 25;
const API_MAX = 200;          // server cap per request
const EXPORT_MAX_PAGES = 50;  // 10000 rows
const STATUSES = ['queued', 'running', 'blocked', 'done', 'failed', 'cancelled'];
const NS = 'http://www.w3.org/2000/svg'; // SVG namespace identifier; nothing is fetched from it
const UNREPORTED = 'not reported';

// ---- pure helpers
export function itemTitle(item) {
  const text = item.title || item.prompt || '';
  if (text) return String(text).replace(/\s+/g, ' ').trim().slice(0, 80);
  const base = String(item.workspace || '').split(/[\\/]/).filter(Boolean).pop();
  return base ? `${base} (${short(item.run_id)})` : short(item.run_id);
}

export function providerKey(item) {
  if (item.provider && item.provider.slot) return item.provider.slot;
  const last = (item.attempts || []).slice(-1)[0];
  return last && last.slot ? last.slot : '';
}

export function providerKind(item) {
  if (item.provider && item.provider.kind) return item.provider.kind;
  const last = (item.attempts || []).slice(-1)[0];
  return last && last.kind ? last.kind : '';
}

export function durationSeconds(item) {
  if (typeof item.wall_seconds === 'number' && Number.isFinite(item.wall_seconds)) return item.wall_seconds;
  const a = Date.parse(item.started_at || ''), b = Date.parse(item.ended_at || '');
  return Number.isNaN(a) || Number.isNaN(b) || b < a ? null : (b - a) / 1000;
}

export function fmtDuration(sec) {
  if (sec === null || sec === undefined || !Number.isFinite(sec) || sec < 0) return '–';
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

// Tokens/cost are only shown when the CLI reported them; null/undefined is never rendered as 0.
export const fmtTokens = (n) => (typeof n === 'number' ? n.toLocaleString('en-US') : UNREPORTED);
export const fmtCost = (cents) => (typeof cents === 'number' ? `$${(cents / 100).toFixed(2)}` : UNREPORTED);

// done / (done + failed); cancelled and unfinished work are not counted. null when nothing has finished.
export function successRate(counts) {
  const done = Number(counts && counts.done) || 0, failed = Number(counts && counts.failed) || 0;
  return done + failed === 0 ? null : done / (done + failed);
}
export const fmtRate = (rate) => (rate === null || rate === undefined ? '–' : `${Math.round(rate * 100)}%`);

// Per provider slot: runs, counts by status, seconds spent, success rate.
export function providerTotals(items) {
  const map = new Map();
  for (const item of items || []) {
    const key = providerKey(item) || 'unassigned';
    if (!map.has(key)) map.set(key, { provider: key, runs: 0, seconds: 0, counts: {} });
    const row = map.get(key);
    row.runs += 1;
    row.counts[item.status] = (row.counts[item.status] || 0) + 1;
    row.seconds += durationSeconds(item) || 0;
  }
  return [...map.values()].map((r) => ({ ...r, rate: successRate(r.counts) })).sort((a, b) => b.runs - a.runs || a.provider.localeCompare(b.provider));
}

const SORT_KEYS = {
  status: (i) => i.status,
  title: (i) => itemTitle(i).toLowerCase(),
  provider: (i) => providerKey(i).toLowerCase() || null,
  duration: durationSeconds,
  switches: (i) => (i.provider_switches || []).length,
  started: (i) => i.started_at || i.created_at || null,
};
export const SORT_COLUMNS = Object.keys(SORT_KEYS);

// Stable, non-mutating. Missing values sort last in both directions.
export function sortRows(rows, key, dir = 'asc') {
  const get = SORT_KEYS[key];
  if (!get) return [...rows];
  const sign = dir === 'desc' ? -1 : 1;
  return rows.map((row, at) => ({ row, at, v: get(row) })).sort((a, b) => {
    const an = a.v === null || a.v === undefined, bn = b.v === null || b.v === undefined;
    if (an || bn) return an && bn ? a.at - b.at : an ? 1 : -1;
    const c = typeof a.v === 'number' && typeof b.v === 'number' ? a.v - b.v : String(a.v) < String(b.v) ? -1 : String(a.v) > String(b.v) ? 1 : 0;
    return c ? c * sign : a.at - b.at;
  }).map((x) => x.row);
}

// One CSV cell: formula-injection neutralised (strings starting with = + - @ tab CR get a leading apostrophe),
// then RFC 4180 quoting (quotes doubled; quoted when it holds comma, quote, CR or LF).
export function csvCell(value) {
  if (value === null || value === undefined) return '';
  let text = String(value);
  if (typeof value !== 'number' && /^[=+\-@\t\r]/.test(text)) text = "'" + text;
  return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
}

export function toCsv(header, rows) {
  return [header, ...rows].map((row) => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
}

export const CSV_HEADER = ['run_id', 'status', 'title', 'provider_slot', 'provider_kind', 'started_at', 'ended_at', 'duration_seconds',
  'attempts', 'provider_switches', 'tokens', 'cost_usd_cents', 'log_sha256', 'result_sha256', 'workspace'];

// Unreported tokens/cost stay empty cells (never 0).
export function csvRows(items) {
  return items.map((i) => [i.run_id, i.status, itemTitle(i), providerKey(i), providerKind(i), i.started_at, i.ended_at, durationSeconds(i),
    (i.attempts || []).length, (i.provider_switches || []).length, i.tokens, i.cost_usd_cents,
    i.evidence && i.evidence.log_sha256, i.evidence && i.evidence.result_sha256, i.workspace]);
}

export function csvFilename(date = new Date()) {
  const two = (n) => String(n).padStart(2, '0');
  return `tracker-${date.getFullYear()}${two(date.getMonth() + 1)}${two(date.getDate())}.csv`;
}

// Query string for the tracker route from the filter state (empty values omitted).
export function trackerQuery(filters, limit, offset) {
  const p = new URLSearchParams();
  const provider = filters.provider || '';
  if (filters.status) p.set('status', filters.status);
  if (provider.startsWith('slot:')) p.set('slot', provider.slice(5));
  if (provider.startsWith('kind:')) p.set('kind', provider.slice(5));
  if (filters.q && filters.q.trim()) p.set('q', filters.q.trim());
  if (filters.since) p.set('since', filters.since);
  if (filters.until) p.set('until', filters.until);
  p.set('limit', String(limit));
  p.set('offset', String(offset));
  return p.toString();
}

// ---- view
const KIND_LABEL = { claude: 'Claude Code', codex: 'Codex' };
const statusChip = (s) => h('span', { class: 'chip st-' + s }, s);
const providerText = (i) => { const slot = providerKey(i), kind = providerKind(i); return slot ? (kind ? `${slot} (${kind})` : slot) : '–'; };

function svgEl(tag, attrs, text) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
  if (text !== undefined) node.textContent = text;
  return node;
}

// Horizontal bars with visible text labels. Decorative for AT (the data table next to it carries the values).
function barChart(rows, cls) {
  const rowH = 28, labelW = 110, valueW = 64, width = 360, barMax = width - labelW - valueW;
  const max = Math.max(...rows.map((r) => r.value), 0);
  const svg = svgEl('svg', { viewBox: `0 0 ${width} ${Math.max(rows.length, 1) * rowH}`, class: 'tk-svg', 'aria-hidden': 'true', focusable: 'false' });
  rows.forEach((r, n) => {
    const y = n * rowH, w = max > 0 ? Math.max(r.value > 0 ? 2 : 0, (r.value / max) * barMax) : 0;
    svg.append(svgEl('text', { x: 0, y: y + 18, class: 'tk-label' }, r.label.length > 16 ? r.label.slice(0, 15) + '…' : r.label),
      svgEl('rect', { x: labelW, y: y + 6, width: w, height: 14, rx: 3, class: 'tk-bar ' + cls }),
      svgEl('text', { x: labelW + w + 6, y: y + 18, class: 'tk-value' }, r.text));
  });
  return svg;
}

export function mountTracker(panel) {
  let alive = true;
  let seq = 0, totalsSeq = 0;
  const filters = { status: '', provider: '', q: '', since: '', until: '' };
  const state = { offset: 0, total: 0, items: [], sort: { key: 'started', dir: 'desc' }, summary: null, error: null, loaded: false };
  let debounce = 0;
  let drawerClose = null;

  const note = h('div', { class: 'status-line', role: 'status' });
  const body = h('div', { class: 'tk-body' });
  const totalsBox = h('section', { class: 'card tk-totals', 'aria-labelledby': 'tk-totals-h' });
  const pager = h('div', { class: 'tk-pager' });

  // ---- filter bar
  const status = h('select', { id: uid('tks') }, h('option', { value: '' }, 'All statuses'), STATUSES.map((s) => h('option', { value: s }, s)));
  const provider = h('select', { id: uid('tkp') }, h('option', { value: '' }, 'All providers'));
  const search = h('input', { id: uid('tkq'), type: 'search', placeholder: 'Run id, folder, slot or reason', autocomplete: 'off', spellcheck: 'false' });
  const since = h('input', { id: uid('tkf'), type: 'date' });
  const until = h('input', { id: uid('tku'), type: 'date' });
  const sortKey = h('select', { id: uid('tko') }, SORT_COLUMNS.map((k) => h('option', { value: k }, k === 'started' ? 'Started' : k[0].toUpperCase() + k.slice(1))));
  const sortDir = h('button', { type: 'button', 'aria-label': 'Reverse sort order' });
  const exportBtn = h('button', { type: 'button', onclick: () => exportCsv() }, icon('download'), ' Export CSV');
  const refreshBtn = h('button', { type: 'button', onclick: () => load() }, 'Refresh');
  const clearBtn = h('button', { type: 'button', class: 'ghost', onclick: () => {
    Object.assign(filters, { status: '', provider: '', q: '', since: '', until: '' });
    status.value = provider.value = search.value = since.value = until.value = '';
    state.offset = 0; load();
  } }, 'Clear filters');

  const changed = () => { state.offset = 0; load(); };
  status.addEventListener('change', () => { filters.status = status.value; changed(); });
  provider.addEventListener('change', () => { filters.provider = provider.value; changed(); });
  since.addEventListener('change', () => { filters.since = since.value; changed(); });
  until.addEventListener('change', () => { filters.until = until.value; changed(); });
  search.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(() => { filters.q = search.value; changed(); }, 300); });
  sortKey.addEventListener('change', () => { state.sort.key = sortKey.value; draw(); });
  sortDir.addEventListener('click', () => { state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc'; draw(); });

  const bar = h('div', { class: 'card tk-filters', role: 'search' },
    h('div', { class: 'tk-fields' },
      labelled('Status', status), labelled('Provider', provider), labelled('Search', search),
      labelled('From (date)', since), labelled('To (date)', until)),
    h('div', { class: 'actions' }, clearBtn, refreshBtn, exportBtn));
  const sortbar = h('div', { class: 'tk-sortbar' }, labelled('Sort by', sortKey), sortDir);

  clear(panel).append(h('h2', null, 'Tracker'),
    h('p', { class: 'lead' }, 'Every queued run: what ran, on which provider, how long it took and where it switched.'),
    bar, note, totalsBox, sortbar, body, pager);
  body.append(loading());

  // ---- data
  const say = (text, kind = '') => { note.textContent = text; note.className = 'status-line ' + kind; };

  async function loadProviders() {
    try {
      const { slots } = await api('GET', '/api/studio/slots');
      const group = h('optgroup', { label: 'Slots' }, slots.map((s) => h('option', { value: 'slot:' + s.id }, s.label || s.id)));
      const kinds = h('optgroup', { label: 'Kinds' }, Object.entries(KIND_LABEL).map(([k, t]) => h('option', { value: 'kind:' + k }, t)));
      if (alive) provider.append(kinds, slots.length ? group : null);
    } catch { if (alive) provider.append(h('optgroup', { label: 'Kinds' }, Object.entries(KIND_LABEL).map(([k, t]) => h('option', { value: 'kind:' + k }, t)))); }
  }

  async function load() {
    const mine = ++seq;
    try {
      const res = await api('GET', '/api/studio/tracker?' + trackerQuery(filters, PAGE, state.offset));
      if (!alive || mine !== seq) return;
      Object.assign(state, { items: res.items, total: res.total, summary: res.summary, error: null, loaded: true });
    } catch (e) { if (!alive || mine !== seq) return; state.error = e; }
    draw();
    loadTotals();
  }

  async function loadTotals() {
    const mine = ++totalsSeq;
    if (state.error) { clear(totalsBox); return; }
    try {
      const res = await api('GET', '/api/studio/tracker?' + trackerQuery(filters, API_MAX, 0));
      if (alive && mine === totalsSeq) drawTotals(res);
    } catch { if (alive && mine === totalsSeq) clear(totalsBox).append(h('p', { class: 'hint' }, 'Totals are unavailable right now.')); }
  }

  // ---- totals panel
  function drawTotals(res) {
    const rows = providerTotals(res.items);
    const sum = res.summary || {};
    const counts = sum.counts_by_status || {};
    clear(totalsBox);
    totalsBox.append(h('h3', { id: 'tk-totals-h' }, 'Totals'));
    if (!rows.length) { totalsBox.append(h('p', { class: 'hint' }, 'No runs match these filters.')); return; }
    const tok = sum.tokens || {}, cost = sum.cost_usd_cents || {};
    const reported = (o, fmt) => (o.reported_count ? `${fmt(o.sum)} (reported by ${o.reported_count} of ${res.total} runs)` : UNREPORTED);
    totalsBox.append(
      h('dl', { class: 'facts tk-facts' },
        h('dt', null, 'Runs'), h('dd', null, String(res.total)),
        h('dt', null, 'Success rate'), h('dd', null, fmtRate(successRate(counts))),
        h('dt', null, 'Time spent'), h('dd', null, fmtDuration(sum.total_wall_seconds)),
        h('dt', null, 'Tokens'), h('dd', null, reported(tok, fmtTokens)),
        h('dt', null, 'Cost'), h('dd', null, reported(cost, fmtCost))),
      res.total > res.items.length ? h('p', { class: 'hint' }, `Per-provider charts use the latest ${res.items.length} of ${res.total} matching runs.`) : null);
    const charts = [
      ['Runs per provider', 'runs', rows.map((r) => ({ label: r.provider, value: r.runs, text: String(r.runs) }))],
      ['Success rate', 'rate', rows.map((r) => ({ label: r.provider, value: r.rate === null ? 0 : r.rate, text: fmtRate(r.rate) }))],
      ['Time spent', 'time', rows.map((r) => ({ label: r.provider, value: r.seconds, text: fmtDuration(r.seconds) }))],
    ];
    const grid = h('div', { class: 'tk-charts' });
    for (const [title, cls, data] of charts) grid.append(h('figure', { class: 'tk-chart' }, h('figcaption', null, title), barChart(data, cls)));
    totalsBox.append(grid,
      h('div', { class: 'sr-only' }, h('table', null, h('caption', null, 'Totals per provider'),
        h('thead', null, h('tr', null, ['Provider', 'Runs', 'Success rate', 'Time spent'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', null, rows.map((r) => h('tr', null, h('th', { scope: 'row' }, r.provider), h('td', null, String(r.runs)), h('td', null, fmtRate(r.rate)), h('td', null, fmtDuration(r.seconds))))))));
  }

  // ---- table
  const COLS = [['status', 'Status'], ['title', 'Title'], ['provider', 'Provider'], ['duration', 'Duration'], ['switches', 'Switches'], ['started', 'Started']];

  function draw() {
    clear(body);
    clear(pager);
    sortKey.value = state.sort.key;
    sortDir.textContent = state.sort.dir === 'asc' ? 'Ascending' : 'Descending';
    exportBtn.disabled = Boolean(state.error) || state.total === 0;
    if (state.error) { body.append(isNotFound(state.error) ? cliNotice() : failure(state.error, load)); clear(totalsBox); return; }
    if (!state.items.length) {
      const filtered = Object.values(filters).some(Boolean);
      body.append(emptyCard('tracker', filtered ? 'No matching runs' : 'No runs yet',
        filtered ? 'Nothing matches these filters. Try widening the dates or clearing the search.' : 'Runs appear here once work has been queued and started.'));
      return;
    }
    const rows = sortRows(state.items, state.sort.key, state.sort.dir);
    const head = h('tr', null, COLS.map(([key, label]) => {
      const active = state.sort.key === key;
      const th = h('th', { scope: 'col', 'aria-sort': active ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none' });
      th.append(h('button', { type: 'button', class: 'tk-sort', onclick: () => {
        state.sort = { key, dir: active && state.sort.dir === 'asc' ? 'desc' : 'asc' };
        draw();
        body.querySelector(`th:nth-child(${COLS.findIndex((c) => c[0] === key) + 1}) button`).focus();
      } }, label, active ? h('span', { 'aria-hidden': 'true' }, state.sort.dir === 'asc' ? ' ▲' : ' ▼') : null));
      return th;
    }));
    const tbody = h('tbody', null, rows.map(row));
    body.append(h('div', { class: 'table-wrap tk-wrap' }, h('table', { class: 'table tk-table' },
      h('caption', { class: 'sr-only' }, 'Run history. Use the column header buttons to sort the rows on this page.'), h('thead', null, head), tbody)));
    const first = state.offset + 1, last = state.offset + state.items.length;
    const prev = h('button', { type: 'button', disabled: state.offset === 0, onclick: () => { state.offset = Math.max(0, state.offset - PAGE); load(); } }, 'Previous');
    const next = h('button', { type: 'button', disabled: last >= state.total, onclick: () => { state.offset += PAGE; load(); } }, 'Next');
    pager.append(prev, h('span', { class: 'tk-range', role: 'status' }, `${first} to ${last} of ${state.total}`), next);
  }

  function row(item) {
    const opener = h('button', { type: 'button', class: 'tk-open', 'aria-haspopup': 'dialog', onclick: (e) => openDrawer(item, e.currentTarget) }, itemTitle(item));
    const switches = (item.provider_switches || []).length;
    const cell = (label, ...kids) => h('td', { 'data-label': label }, kids);
    return h('tr', { 'data-run': item.run_id },
      cell('Status', statusChip(item.status)), cell('Title', opener), cell('Provider', providerText(item)),
      cell('Duration', fmtDuration(durationSeconds(item))), cell('Switches', String(switches)),
      cell('Started', when(item.started_at || item.created_at)));
  }

  // ---- detail drawer
  function openDrawer(item, opener) {
    if (drawerClose) drawerClose();
    const titleId = uid('tkd');
    const scrim = h('div', { class: 'scrim tk-scrim' });
    const closeBtn = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Close details', onclick: () => close() }, icon('cancel'));
    const attempts = item.attempts || [];
    const switches = item.provider_switches || [];
    const ev = item.evidence || {};
    const drawer = h('aside', { class: 'drawer tk-drawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabindex: '-1' },
      h('div', { class: 'tk-drawer-head' }, h('h3', { id: titleId }, itemTitle(item)), closeBtn),
      h('p', null, statusChip(item.status), item.reason ? h('span', { class: 'hint' }, ' ' + item.reason) : null),
      h('dl', { class: 'facts' },
        h('dt', null, 'Run'), h('dd', { class: 'qpath' }, item.run_id),
        h('dt', null, 'Folder'), h('dd', { class: 'qpath' }, item.workspace || '–'),
        h('dt', null, 'Provider'), h('dd', null, providerText(item)),
        h('dt', null, 'Started'), h('dd', null, when(item.started_at)),
        h('dt', null, 'Ended'), h('dd', null, when(item.ended_at)),
        h('dt', null, 'Duration'), h('dd', null, fmtDuration(durationSeconds(item))),
        h('dt', null, 'Tokens'), h('dd', null, fmtTokens(item.tokens)),
        h('dt', null, 'Cost'), h('dd', null, fmtCost(item.cost_usd_cents))),
      h('h4', null, `Attempts (${attempts.length})`),
      attempts.length ? h('ol', { class: 'tk-list' }, attempts.map((a) => h('li', null,
        h('strong', null, `#${a.attempt} ${a.slot || '–'}`), ` ${a.kind ? '(' + a.kind + ') ' : ''}${a.outcome || 'in progress'}`,
        a.reason ? ` – ${a.reason}` : '', h('br'),
        h('span', { class: 'hint' }, `${when(a.start)} to ${when(a.end)}; tokens ${fmtTokens(a.tokens)}; cost ${fmtCost(a.cost_usd_cents)}`))))
        : h('p', { class: 'hint' }, 'No attempts recorded.'),
      h('h4', null, `Provider switches (${switches.length})`),
      switches.length ? h('ul', { class: 'tk-list' }, switches.map((s) => h('li', null, `${s.from || '–'} to ${s.to || '–'}`, s.reason ? ` – ${s.reason}` : '', h('br'), h('span', { class: 'hint' }, when(s.at)))))
        : h('p', { class: 'hint' }, 'None.'),
      h('h4', null, 'Evidence (SHA-256)'),
      h('dl', { class: 'facts' },
        h('dt', null, 'Log'), h('dd', { class: 'qpath tk-hash' }, ev.log_sha256 || UNREPORTED),
        h('dt', null, 'Result'), h('dd', { class: 'qpath tk-hash' }, ev.result_sha256 || UNREPORTED)),
      h('div', { class: 'actions' }, h('a', { class: 'button primary', href: '#run/' + encodeURIComponent(item.run_id) }, 'Open run page')));

    function trap(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key !== 'Tab') return;
      const focusable = [...drawer.querySelectorAll('button:not([disabled]), a[href]')];
      if (!focusable.length) return;
      const firstEl = focusable[0], lastEl = focusable[focusable.length - 1];
      if (e.shiftKey && (document.activeElement === firstEl || document.activeElement === drawer)) { e.preventDefault(); lastEl.focus(); }
      else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); firstEl.focus(); }
    }
    function close() {
      if (!drawerClose) return;
      drawerClose = null;
      drawer.removeEventListener('keydown', trap);
      drawer.remove();
      scrim.remove();
      if (opener && opener.isConnected) opener.focus();
    }
    drawer.addEventListener('keydown', trap);
    scrim.addEventListener('click', close);
    drawer.querySelector('a.button').addEventListener('click', () => close());
    drawerClose = close;
    document.body.append(scrim, drawer);
    closeBtn.focus();
  }

  // ---- CSV export: fetches every page of the current filter (up to EXPORT_MAX_PAGES), so it is not limited to the visible page
  async function exportCsv() {
    exportBtn.disabled = true;
    say('Preparing the CSV…');
    try {
      const all = [];
      for (let page = 0; page < EXPORT_MAX_PAGES; page++) {
        const res = await api('GET', '/api/studio/tracker?' + trackerQuery(filters, API_MAX, page * API_MAX));
        all.push(...res.items);
        if (!res.items.length || all.length >= res.total) break;
      }
      const csv = '\ufeff' + toCsv(CSV_HEADER, csvRows(sortRows(all, state.sort.key, state.sort.dir)));
      const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
      const link = h('a', { href: url, download: csvFilename() });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      say(`Exported ${all.length} run${all.length === 1 ? '' : 's'}.`, 'ok');
    } catch (e) { say(errorText(e), 'error'); } finally { exportBtn.disabled = state.total === 0; }
  }

  loadProviders();
  load();
  return () => {
    alive = false;
    clearTimeout(debounce);
    if (drawerClose) drawerClose();
    document.querySelectorAll('.tk-drawer, .tk-scrim').forEach((n) => n.remove());
  };
}
