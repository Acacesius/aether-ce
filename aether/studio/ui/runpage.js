// Run page (#run/<queue id>): header, stage graph, live log (long-poll by byte offset), attempt timeline, evidence.
// Pure helpers are exported for tests; the DOM is built with h()/textContent only (log text is never markup).
import { h, clear, api, errorText, ApiError, emptyCard, when, confirmButton } from './lib.js';
import { icon } from './icons.js';

const QUEUE_ID = /^que_[0-9A-HJKMNP-TV-Z]{26}$/;
const STUDIO_RUN_ID = /^run_[0-9A-HJKMNP-TV-Z]{26}$/;
export const MAX_NODES = 2000;        // log nodes kept in the DOM
export const RESULT_PREVIEW = 600;    // characters of a tool result shown before "show more"
export const WAIT_SECONDS = 20;       // long-poll wait
export const BACKOFF_BASE_MS = 500;
export const BACKOFF_CAP_MS = 10000;
const TERMINAL = ['done', 'failed', 'cancelled'];

// ---- pure helpers
// Exponential backoff for failed polls: 0 -> 500ms, 1 -> 1s, 2 -> 2s ... capped at 10s.
export function backoff(failures) {
  const n = Math.max(0, Math.floor(Number(failures) || 0));
  return Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.min(n, 16));
}

// Merge one log chunk into the view state without duplicating lines. State: { events, nextOffset }.
// Events carry the byte offset of their source line; anything starting before nextOffset was already shown
// (a replayed chunk after a reconnect, or an offset the server stepped back to). Never mutates its input.
export function mergeEvents(state, view, cap = MAX_NODES) {
  const have = Number.isFinite(state.nextOffset) && state.nextOffset > 0 ? state.nextOffset : 0;
  const next = Number(view && view.next_offset);
  const incoming = Array.isArray(view && view.events) ? view.events : [];
  const fresh = incoming.filter((e) => !(Number.isFinite(e.offset) && e.offset < have));
  const nextOffset = Number.isFinite(next) && next > have ? next : have;
  let events = state.events.concat(fresh);
  let dropped = 0;
  if (events.length > cap) { dropped = events.length - cap; events = events.slice(dropped); }
  return { events, nextOffset, added: fresh, dropped };
}

// "1h 02m 03s" style duration between two ISO times (end defaults to now). Unknown start -> "–".
export function elapsedText(startIso, endIso, now = Date.now()) {
  const start = Date.parse(startIso || '');
  if (!Number.isFinite(start)) return '–';
  const parsedEnd = endIso ? Date.parse(endIso) : now;
  const end = Number.isFinite(parsedEnd) ? parsedEnd : now;
  const total = Math.max(0, Math.floor((end - start) / 1000));
  const hours = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const two = (v) => String(v).padStart(2, '0');
  if (hours) return `${hours}h ${two(mins)}m ${two(secs)}s`;
  if (mins) return `${mins}m ${two(secs)}s`;
  return `${secs}s`;
}

// Cut text to at most max characters (on a code point boundary). Reports how much was hidden.
export function truncateText(text, max = RESULT_PREVIEW) {
  const value = text === null || text === undefined ? '' : String(text);
  const chars = Array.from(value);
  if (chars.length <= max) return { text: value, truncated: false, hidden: 0 };
  return { text: chars.slice(0, Math.max(0, max)).join(''), truncated: true, hidden: chars.length - Math.max(0, max) };
}

// Attempts as timeline segments, with provider switches attached to the attempt they lead into.
export function timelineSegments(attempts, switches) {
  const list = Array.isArray(attempts) ? attempts : [];
  const pending = (switches || []).slice();
  return list.map((a, i) => {
    const at = i > 0 ? pending.findIndex((s) => s.to === a.slot) : -1;
    const sw = at >= 0 ? pending.splice(at, 1)[0] : null;
    return { attempt: a.attempt, slot: a.slot || '–', kind: a.kind || '', outcome: a.outcome || 'running', reason: a.reason || '',
             start: a.start || null, end: a.end || null,
             switched: sw ? { from: sw.from || '–', to: sw.to || '–', reason: sw.reason || 'no reason recorded' } : null };
  });
}

// Which actions apply to an item. Cancel: open items. Resume: a blocked item. Approve: blocked waiting on the owner.
export function actionsFor(state, reason) {
  const open = state === 'queued' || state === 'running' || state === 'blocked';
  return { cancel: open, resume: state === 'blocked' && reason !== 'owner_action_required',
           approve: state === 'blocked' && reason === 'owner_action_required' };
}

export const shortHash = (hash) => (hash ? hash.slice(0, 12) : '');
export const byteSize = (n) => (!Number.isFinite(n) ? '' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KiB' : (n / 1048576).toFixed(1) + ' MiB');

// ---- rendering of log events (text only)
function eventNode(e) {
  const text = e.text === undefined || e.text === null ? '' : String(e.text);
  switch (e.kind) {
    case 'assistant_text':
      return h('div', { class: 'lg lg-prose' }, text);
    case 'tool_use':
      return h('details', { class: 'lg lg-tool' },
        h('summary', null, h('strong', null, e.name || 'tool'), ' ', h('span', { class: 'lg-args' }, truncateText(e.input || '', 100).text)),
        h('pre', null, e.input || ''));
    case 'tool_result': {
      const cut = truncateText(text);
      const body = h('pre', null, cut.text);
      const more = cut.truncated ? h('button', { type: 'button', class: 'ghost', onclick: () => {
        const open = more.dataset.open === '1';
        body.textContent = open ? cut.text : text;
        more.textContent = open ? `Show ${cut.hidden} more characters` : 'Show less';
        more.dataset.open = open ? '0' : '1';
      } }, `Show ${cut.hidden} more characters`) : null;
      const first = text.split('\n', 1)[0].slice(0, 90);
      return h('details', { class: 'lg lg-result' + (e.is_error ? ' lg-error' : '') },
        h('summary', null, e.is_error ? 'tool error' : 'tool result', first ? ' · ' : '', h('span', { class: 'lg-args' }, first)), body, more);
    }
    case 'error':
      return h('div', { class: 'lg lg-error', role: 'alert' }, h('strong', null, 'Error '), text);
    case 'final_result':
      return h('div', { class: 'lg lg-final' + (e.is_error ? ' lg-error' : '') }, h('strong', null, e.is_error ? 'Finished with an error ' : 'Final result '), text);
    case 'usage':
      return h('div', { class: 'lg lg-raw' }, `usage: ${e.input_tokens} in, ${e.output_tokens} out, ${e.cached_tokens} cached`
        + (e.cost_usd !== undefined ? `, ~$${e.cost_usd}` : ''));
    case 'header':
      return h('div', { class: 'lg lg-header' }, '[aether] ' + text);
    case 'stderr':
      return h('div', { class: 'lg lg-raw' }, '[stderr] ' + text);
    default:
      return h('div', { class: 'lg lg-raw' }, text);
  }
}

// ---- stage graph (inline SVG-free flow of boxes; status colour comes from the chip classes)
function stageGraph(nodes) {
  return h('ol', { class: 'rp-flow', 'aria-label': 'Stages' }, nodes.map((n, i) => h('li', { class: 'rp-stage' },
    h('span', { class: 'rp-node' }, h('strong', null, n.key), h('span', { class: 'chip st-' + n.status }, n.status),
      n.note ? h('span', { class: 'hint' }, n.note) : null),
    i < nodes.length - 1 ? icon('chevron', { size: 16, className: 'rp-arrow' }) : null)));
}

function stagesFrom(run, itemStatus) {
  const specs = [...(run.snapshot && run.snapshot.stages || []), ...(run.added_stages || [])];
  const latest = new Map((run.stages || []).map((s) => [s.key, s]));
  return specs.map((spec) => {
    const s = latest.get(spec.key);
    return { key: spec.key, status: s ? s.status : (TERMINAL.includes(itemStatus) ? 'skipped' : 'queued'),
             note: spec.needs && spec.needs.length ? 'needs ' + spec.needs.join(', ') : '' };
  });
}

export function mountRunPage(panel, ctx) {
  const id = ctx && ctx.args ? ctx.args[0] || '' : '';
  if (!QUEUE_ID.test(id)) {
    clear(panel).append(emptyCard('run', 'Run not found', 'This link does not point to a queued run.',
      h('a', { class: 'button', href: '#workspace' }, 'Go to Workspace')));
    return () => {};
  }

  let stopped = false;
  let item = null;
  let track = null;
  let paused = false;
  let polling = false;
  let failures = 0;
  let state = { events: [], nextOffset: 0 };
  let logSize = null;
  let pollTimer = 0;
  let itemTimer = 0;
  let clockTimer = 0;
  let removedTotal = 0;
  let stickToBottom = true;
  let stageNodes = null;
  const timers = () => { clearTimeout(pollTimer); clearInterval(itemTimer); clearInterval(clockTimer); };

  const head = h('section', { class: 'card', 'aria-label': 'Run summary' });
  const graph = h('section', { class: 'card', 'aria-labelledby': 'rp-stages-h' });
  const timeline = h('section', { class: 'card', 'aria-labelledby': 'rp-tl-h' });
  const logCard = h('section', { class: 'card', 'aria-labelledby': 'rp-log-h' });
  const evidence = h('section', { class: 'card', 'aria-labelledby': 'rp-ev-h' });
  const banner = h('p', { class: 'state', role: 'status', 'aria-live': 'polite', hidden: true });
  const trimNote = h('p', { class: 'hint', hidden: true });
  const logBox = h('div', { class: 'rp-log', tabindex: '0', role: 'log', 'aria-label': 'Run log', 'aria-live': 'off' });
  const toggle = h('button', { type: 'button', 'aria-pressed': 'false', onclick: () => setPaused(!paused) }, icon('pause'), h('span', null, 'Pause'));
  const elapsed = h('span', { class: 'rp-elapsed', 'aria-label': 'Elapsed time' }, '–');
  const logEmpty = h('p', { class: 'state empty' }, 'Waiting for the first output…');
  logBox.append(logEmpty);

  logCard.append(h('div', { class: 'slot-line' }, h('h2', { id: 'rp-log-h' }, 'Log'), toggle), banner, trimNote, logBox);
  graph.append(h('h2', { id: 'rp-stages-h' }, 'Stages'), h('p', { class: 'state' }, 'Loading…'));
  timeline.append(h('h2', { id: 'rp-tl-h' }, 'Attempts'));
  evidence.append(h('h2', { id: 'rp-ev-h' }, 'Evidence'));
  clear(panel).append(h('a', { class: 'back', href: '#workspace' }, 'Back to Workspace'), head, graph, logCard, timeline, evidence);

  logBox.addEventListener('scroll', () => { stickToBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 32; });

  function setPaused(value) {
    paused = value;
    toggle.setAttribute('aria-pressed', String(paused));
    toggle.replaceChildren(icon(paused ? 'play' : 'pause'), h('span', null, paused ? 'Follow' : 'Pause'));
    if (paused) clearTimeout(pollTimer); else { stickToBottom = true; schedulePoll(0); }
  }

  const isTerminal = () => !!item && TERMINAL.includes(item.state);

  // ---- header
  function renderHead() {
    if (!item) return;
    const t = track || {};
    const provider = t.provider ? `${t.provider.kind} · ${t.provider.slot}` : (item.requested_slot ? 'requested: ' + item.requested_slot : '–');
    const acts = actionsFor(item.state, item.reason);
    const buttons = [];
    if (acts.approve) buttons.push(h('button', { type: 'button', class: 'primary', onclick: () => act('POST', 'resume') }, icon('check'), 'Approve and continue'));
    if (acts.resume) buttons.push(h('button', { type: 'button', class: 'primary', onclick: () => act('POST', 'resume') }, icon('play'), 'Resume'));
    if (acts.cancel) buttons.push(confirmButton('Cancel run', 'Press again to cancel', () => act('DELETE', ''), 'danger'));
    const title = (item.blueprint_ref ? 'Blueprint run' : 'Prompt run') + ' · ' + item.run_id.slice(4, 12);
    head.replaceChildren(
      h('div', { class: 'slot-head' }, h('h1', null, title),
        h('div', { class: 'badges' }, h('span', { class: 'chip', title: 'Provider' }, provider),
          h('span', { class: 'chip st-' + item.state, role: 'status' }, item.state))),
      h('dl', { class: 'facts' },
        h('dt', null, 'Elapsed'), h('dd', null, elapsed),
        h('dt', null, 'Workspace'), h('dd', { class: 'rid' }, item.workspace),
        item.reason ? h('dt', null, 'Reason') : null, item.reason ? h('dd', null, item.reason) : null,
        h('dt', null, 'Created'), h('dd', null, when(item.created_at)),
        t.tokens !== null && t.tokens !== undefined ? h('dt', null, 'Tokens') : null, t.tokens !== null && t.tokens !== undefined ? h('dd', null, String(t.tokens)) : null,
        t.cost_usd_cents !== null && t.cost_usd_cents !== undefined ? h('dt', null, 'Cost') : null,
        t.cost_usd_cents !== null && t.cost_usd_cents !== undefined ? h('dd', null, '$' + (t.cost_usd_cents / 100).toFixed(2)) : null),
      buttons.length ? h('div', { class: 'actions' }, buttons) : null,
      h('p', { class: 'hint', role: 'status', id: 'rp-action' }));
    tickClock();
  }

  async function act(method, suffix) {
    const note = head.querySelector('#rp-action');
    try {
      await api(method, '/api/studio/queue/' + id + (suffix ? '/' + suffix : ''), method === 'POST' ? { body: {} } : {});
      await refresh();
      if (!isTerminal()) schedulePoll(0);
    } catch (error) {
      if (note) note.textContent = errorText(error);
    }
  }

  function tickClock() {
    if (!item) return;
    const t = track || {};
    const start = t.started_at || (item.state === 'queued' ? null : item.created_at);
    elapsed.textContent = start ? elapsedText(start, isTerminal() ? (t.ended_at || item.updated_at) : null) : 'not started';
  }

  // ---- timeline and evidence
  function renderTimeline() {
    const segs = timelineSegments(track && track.attempts, track && track.provider_switches);
    timeline.replaceChildren(h('h2', { id: 'rp-tl-h' }, 'Attempts'));
    if (!segs.length) { timeline.append(h('p', { class: 'state empty' }, 'No attempts yet.')); return; }
    timeline.append(h('ol', { class: 'rp-timeline' }, segs.map((s) => h('li', { class: 'rp-seg out-' + s.outcome },
      s.switched ? h('p', { class: 'rp-switch', title: `Provider switch: ${s.switched.reason}` },
        icon('alert', { size: 14 }), ` Switched ${s.switched.from} to ${s.switched.to}: ${s.switched.reason}`) : null,
      h('span', { class: 'rp-seg-bar', 'aria-hidden': 'true' }),
      h('strong', null, `#${s.attempt} ${s.slot}`), h('span', { class: 'chip st-' + chipFor(s.outcome) }, s.outcome),
      h('span', { class: 'hint' }, s.start ? `${elapsedText(s.start, s.end)}${s.reason ? ' · ' + s.reason : ''}` : s.reason)))));
  }
  const chipFor = (outcome) => ({ ok: 'done', failed: 'failed', limit_stop: 'blocked', cancelled: 'cancelled' }[outcome] || 'running');

  async function copy(button, text) {
    try { await navigator.clipboard.writeText(text); button.textContent = 'Copied'; } catch { button.textContent = 'Copy failed'; }
    setTimeout(() => { button.textContent = 'Copy'; }, 1500);
  }
  function renderEvidence() {
    const ev = track && track.evidence || {};
    const rows = [['Raw log', ev.log_sha256, logSize], ['Final result text', ev.result_sha256, null]].filter((r) => r[1]);
    evidence.replaceChildren(h('h2', { id: 'rp-ev-h' }, 'Evidence'));
    if (!rows.length) { evidence.append(h('p', { class: 'state empty' }, 'Evidence hashes appear when the run finishes.')); return; }
    evidence.append(h('ul', { class: 'artifacts' }, rows.map(([name, hash, size]) => {
      const button = h('button', { type: 'button', class: 'ghost', 'aria-label': `Copy full sha256 of ${name}`, onclick: () => copy(button, hash) }, 'Copy');
      return h('li', null, h('strong', null, name), h('span', { class: 'hash', title: hash }, 'sha256 ' + shortHash(hash)),
        size !== null ? h('span', { class: 'hint' }, byteSize(size)) : null, button);
    })));
  }

  // ---- stage graph
  async function loadStages() {
    if (stageNodes) return;
    const ref = item && item.blueprint_ref;
    stageNodes = [{ key: item.blueprint_ref ? 'blueprint' : 'prompt', status: 'queued', note: '' }];
    if (ref && STUDIO_RUN_ID.test(ref)) {
      try {
        const run = await api('GET', '/api/studio/runs/' + ref);
        const nodes = stagesFrom(run, item.state);
        if (nodes.length) stageNodes = nodes; else stageNodes[0].note = 'no stage data';
        stageNodes.live = true;
      } catch { stageNodes[0].note = 'stage data unavailable'; }
    }
  }
  function renderGraph() {
    if (!stageNodes) return;
    const nodes = stageNodes.live ? stageNodes : stageNodes.map((n) => ({ ...n, status: item.state === 'done' ? 'done' : item.state }));
    graph.replaceChildren(h('h2', { id: 'rp-stages-h' }, 'Stages'), stageGraph(nodes),
      stageNodes.live ? null : h('p', { class: 'hint' }, item.blueprint_ref ? 'Blueprint: ' + item.blueprint_ref : 'A one-off prompt runs as a single stage.'));
  }

  async function refresh() {
    try {
      const view = await api('GET', '/api/studio/queue/' + id);
      item = view;
      track = view.track;
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) { stop(); showMissing(); return; }
      return;                       // transient: the log poll shows the reconnecting state
    }
    await loadStages();
    renderHead(); renderGraph(); renderTimeline(); renderEvidence();
    if (isTerminal()) { clearInterval(itemTimer); clearInterval(clockTimer); }
  }
  function showMissing() {
    clear(panel).append(emptyCard('run', 'Run not found', 'It may have been removed, or CLI mode is off.', h('a', { class: 'button', href: '#workspace' }, 'Go to Workspace')));
  }
  function stop() { stopped = true; timers(); }

  // ---- log polling
  function append(view) {
    const merged = mergeEvents(state, view, Number.MAX_SAFE_INTEGER);   // DOM cap is applied on nodes below
    state = { events: [], nextOffset: merged.nextOffset };               // events live in the DOM, not twice in memory
    logSize = Number.isFinite(view.size) ? view.size : logSize;
    if (!merged.added.length) return;
    logEmpty.remove();
    const frag = document.createDocumentFragment();
    merged.added.forEach((e) => frag.append(eventNode(e)));
    logBox.append(frag);
    while (logBox.childElementCount > MAX_NODES) { logBox.firstElementChild.remove(); removedTotal++; }
    if (removedTotal) { trimNote.hidden = false; trimNote.textContent = `Showing the last ${MAX_NODES} entries; ${removedTotal} older entries were removed from view.`; }
    if (stickToBottom && !paused) logBox.scrollTop = logBox.scrollHeight;
  }

  function schedulePoll(delay) {
    clearTimeout(pollTimer);
    if (stopped || paused) return;
    pollTimer = setTimeout(poll, delay);
  }

  async function poll() {
    if (stopped || paused || polling) return;
    polling = true;
    try {
      const view = await api('GET', `/api/studio/runs/${id}/log?after=${state.nextOffset}&wait=${WAIT_SECONDS}`);
      if (stopped) return;
      failures = 0;
      banner.hidden = true;
      append(view);
      if (view.done) {
        await refresh();
        renderEvidence();
        return;                                   // finished: no more polling
      }
      if (view.status !== (item && item.state)) await refresh();
      polling = false;
      schedulePoll(view.raw ? 0 : 200);
    } catch (error) {
      if (stopped) return;
      if (error instanceof ApiError && error.status === 404) { stop(); showMissing(); return; }
      failures += 1;
      const wait = backoff(failures - 1);
      banner.hidden = false;
      banner.textContent = `Reconnecting… retrying in ${Math.round(wait / 100) / 10}s. The log resumes from where it stopped.`;
      polling = false;
      schedulePoll(wait);
    } finally {
      polling = false;
    }
  }

  refresh().then(() => {
    if (stopped) return;
    itemTimer = setInterval(() => { if (!document.hidden) refresh(); }, 5000);
    clockTimer = setInterval(tickClock, 1000);
    schedulePoll(0);
  });

  return stop;
}
