// Workspace tab: the queue board (list, reorder, pause, cancel, resume) and the add-run dialog.
// Pure helpers (validatePath, reorderIds, moveId, ageText, queuedIds, buildPrompt) are exported for the node logic tests.
import { api, h, clear, loading, failure, emptyCard, errorText, ApiError, labelled, uid, confirmButton, short } from './lib.js';
import { icon } from './icons.js';

const POLL_MS = 4000;
const STATES = ['queued', 'running', 'blocked', 'done', 'failed', 'cancelled'];
const STATE_LABEL = { queued: 'queued', running: 'running', blocked: 'blocked on limit', done: 'done', failed: 'failed', cancelled: 'cancelled' };
const REASON_TEXT = { owner_action_required: 'Waiting for you to resume it.', limit_stop: 'The account hit its usage limit.' };

// ---- pure helpers
// Client-side check only; the server re-validates (existing folder, not the Studio data folder, ...).
export function validatePath(value) {
  const text = typeof value === 'string' ? value : '';
  if (!text.trim()) return { ok: false, message: 'Type the full path of the project folder.' };
  if (text.includes('\u0000')) return { ok: false, message: 'The path contains an invalid character.' };
  const path = text.trim();
  const drive = /^[A-Za-z]:[\\/]/.test(path);
  const unc = /^\\\\[^\\/]+[\\/]+[^\\/]+/.test(path);
  const posix = path.startsWith('/') && !path.startsWith('\\\\');
  if (/^[A-Za-z]:(?![\\/])/.test(path)) return { ok: false, message: 'Include a slash after the drive, for example C:\\Users\\you\\project.' };
  if (!(drive || unc || posix)) return { ok: false, message: 'Use an absolute path, such as /work/my-project or C:\\Users\\you\\project.' };
  return { ok: true, message: 'Looks like an absolute path. Studio checks the folder when you add the run.' };
}

// Move `fromId` to the place `toId` holds; returns a new array (the input is untouched).
export function reorderIds(ids, fromId, toId) {
  const from = ids.indexOf(fromId);
  const to = ids.indexOf(toId);
  if (from < 0 || to < 0 || from === to) return ids.slice();
  const out = ids.slice();
  out.splice(from, 1);
  out.splice(to, 0, fromId);
  return out;
}

// Move one id up (-1) or down (+1) by a step, clamped to the list.
export function moveId(ids, id, delta) {
  const at = ids.indexOf(id);
  const to = Math.max(0, Math.min(ids.length - 1, at + delta));
  return at < 0 ? ids.slice() : reorderIds(ids, id, ids[to]);
}

export const queuedIds = (items) => (items || []).filter((item) => item.state === 'queued').map((item) => item.run_id);

export function ageText(iso, nowMs = Date.now()) {
  const then = Date.parse(iso || '');
  if (Number.isNaN(then)) return '–';
  const s = Math.max(0, Math.floor((nowMs - then) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} d`;
}

// The prompt sent for a blueprint run is its mission plus any extra instructions the owner typed.
export function buildPrompt(mission, extra) {
  const base = (mission || '').trim();
  const more = (extra || '').trim();
  return base && more ? `${base}\n\n${more}` : base || more;
}

// Notice shown when the queue API answers 404 (CLI executor not enabled).
export function cliNotice() {
  return emptyCard('queue', 'The CLI executor is off',
    'The queue and provider slots only exist when Studio is started with the CLI executor. Restart Studio with the environment variable AETHER_EXEC=cli to turn it on. Until then, Quick Run still works as a DEMO.',
    h('a', { class: 'button', href: '#quick' }, 'Open Quick Run (DEMO)'));
}

export const isNotFound = (error) => error instanceof ApiError && error.status === 404;

function chip(state) {
  const known = STATES.includes(state) ? state : 'queued';
  return h('span', { class: 'chip st-' + known }, STATE_LABEL[known] || state);
}

// ---- view
export function mountWorkspace(panel) {
  let alive = true;
  let data = null;
  let dragging = null;
  let focusAfter = null;
  let loadFailed = null;
  const board = h('div', { class: 'queue-board' });
  const note = h('div', { class: 'status-line', role: 'status' });
  const addButton = h('button', { type: 'button', class: 'primary', onclick: () => openAddDialog(addButton) }, icon('plus'), ' Add run');
  clear(panel).append(h('h2', null, 'Workspace'), h('p', { class: 'lead' }, 'Queued work for your signed-in coding CLIs. Studio runs one item at a time per slot, in the order shown.'),
    h('div', { class: 'actions' }, addButton), note, board);
  board.append(loading());

  const say = (text, kind = '') => { note.textContent = text; note.className = 'status-line ' + kind; };

  async function load() {
    try {
      data = await api('GET', '/api/studio/queue');
      loadFailed = null;
    } catch (e) { loadFailed = e; }
    if (alive && !dragging) draw();
  }

  async function act(label, call) {
    try { await call(); say(label, 'ok'); } catch (e) { say(errorText(e), 'error'); }
    await load();
  }

  const pauseToggle = (paused) => act(paused ? 'Queue resumed.' : 'Queue paused. Running items finish; nothing new starts.',
    () => api('POST', '/api/studio/queue/' + (paused ? 'resume' : 'pause'), { body: {} }));

  async function reorder(ids, focusId, focusKind) {
    focusAfter = focusId ? { id: focusId, kind: focusKind } : null;
    const rest = (data.items || []).filter((item) => item.state !== 'queued');
    data.items = ids.map((id) => data.items.find((item) => item.run_id === id)).concat(rest);  // optimistic
    draw();
    await act('Order saved.', () => api('POST', '/api/studio/queue/reorder', { body: { ids } }));
  }

  function draw() {
    clear(board);
    if (loadFailed) {
      if (isNotFound(loadFailed)) board.append(cliNotice());
      else board.append(failure(loadFailed, load));
      return;
    }
    const items = data.items || [];
    if (data.paused) {
      board.append(h('div', { class: 'paused-banner', role: 'status' }, icon('pause'),
        h('span', null, 'The queue is paused. Running items finish, but nothing new starts.'),
        h('button', { type: 'button', onclick: () => pauseToggle(true) }, icon('play'), ' Resume queue')));
    } else if (items.length) {
      board.append(h('div', { class: 'actions queue-tools' }, h('button', { type: 'button', onclick: () => pauseToggle(false) }, icon('pause'), ' Pause queue')));
    }
    if (!data.driver_alive) board.append(h('p', { class: 'hint' }, 'The queue driver is not running, so queued items will wait.'));
    if (!items.length) {
      board.append(emptyCard('queue', 'Nothing queued', 'Add a run to give a signed-in coding CLI some work. It will start as soon as a slot is free.'));
      return;
    }
    const ids = queuedIds(items);
    const list = h('ul', { class: 'queue-list', 'aria-label': 'Queue' });
    items.forEach((item) => list.append(row(item, ids)));
    board.append(list);
    if (focusAfter) {
      const target = list.querySelector(`[data-id="${focusAfter.id}"] [data-kind="${focusAfter.kind}"]`);
      if (target) target.focus();
      focusAfter = null;
    }
  }

  function row(item, ids) {
    const queued = item.state === 'queued';
    const at = ids.indexOf(item.run_id);
    const label = `run ${short(item.run_id)}`;
    const li = h('li', { class: 'qrow', 'data-id': item.run_id, draggable: queued ? 'true' : null });
    const grip = queued ? h('button', { type: 'button', class: 'icon-btn grip', 'data-kind': 'grip', 'aria-label': `Reorder ${label}. Arrow up or down moves it.`,
      onkeydown: (e) => {
        const delta = { ArrowUp: -1, ArrowDown: 1 }[e.key];
        if (!delta) return;
        e.preventDefault();
        if (ids[at + delta] !== undefined) reorder(moveId(ids, item.run_id, delta), item.run_id, 'grip');
      } }, icon('grip')) : h('span', { class: 'grip-gap', 'aria-hidden': 'true' });
    const info = h('a', { class: 'qinfo', href: '#run/' + item.run_id, 'aria-label': `Open ${label}, ${STATE_LABEL[item.state] || item.state}` },
      h('span', { class: 'rid' }, short(item.run_id)), chip(item.state),
      h('span', { class: 'qmeta' }, `Slot ${item.requested_slot || 'auto'} · ${ageText(item.created_at)} ago`),
      h('span', { class: 'qpath' }, item.workspace),
      item.blueprint_ref ? h('span', { class: 'qmeta' }, 'Blueprint ' + item.blueprint_ref) : null,
      item.state === 'blocked' ? h('span', { class: 'qmeta' }, REASON_TEXT[item.reason] || 'Waiting for you.') : null);
    const tools = h('div', { class: 'qtools' });
    if (queued) {
      tools.append(
        h('button', { type: 'button', class: 'icon-btn', 'data-kind': 'up', 'aria-label': `Move ${label} up`, disabled: at <= 0,
          onclick: () => reorder(moveId(ids, item.run_id, -1), item.run_id, 'up') }, icon('up')),
        h('button', { type: 'button', class: 'icon-btn', 'data-kind': 'down', 'aria-label': `Move ${label} down`, disabled: at >= ids.length - 1,
          onclick: () => reorder(moveId(ids, item.run_id, 1), item.run_id, 'down') }, icon('down')));
    }
    if (item.state === 'blocked') {
      tools.append(h('button', { type: 'button', 'aria-label': `Resume ${label}`,
        onclick: () => act('Resumed.', () => api('POST', `/api/studio/queue/${item.run_id}/resume`, { body: {} })) }, icon('play'), ' Resume'));
    }
    if (['queued', 'running', 'blocked'].includes(item.state)) {
      const cancel = confirmButton('Cancel', 'Confirm cancel', () => act('Cancelled.', () => api('DELETE', '/api/studio/queue/' + item.run_id)), 'danger');
      cancel.setAttribute('aria-label', `Cancel ${label}`);
      tools.append(cancel);
    }
    li.append(grip, info, tools);
    li.addEventListener('click', (e) => { if (!e.target.closest('button, a')) location.hash = '#run/' + item.run_id; });
    if (queued) {
      li.addEventListener('dragstart', (e) => { dragging = item.run_id; li.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', item.run_id); });
      li.addEventListener('dragend', () => { dragging = null; li.classList.remove('dragging'); board.querySelectorAll('.drop-target').forEach((n) => n.classList.remove('drop-target')); draw(); });
      li.addEventListener('dragover', (e) => { if (dragging && dragging !== item.run_id) { e.preventDefault(); li.classList.add('drop-target'); } });
      li.addEventListener('dragleave', () => li.classList.remove('drop-target'));
      li.addEventListener('drop', (e) => {
        e.preventDefault();
        const from = dragging;
        dragging = null;
        if (from) reorder(reorderIds(ids, from, item.run_id), null);
      });
    }
    return li;
  }

  // ---- add-run dialog
  async function openAddDialog(opener) {
    let defs = [];
    let slots = [];
    try { defs = (await api('GET', '/api/studio/definitions?archived=0')).definitions || []; } catch { defs = []; }
    try { slots = (await api('GET', '/api/studio/slots')).slots || []; } catch { slots = []; }
    const mode = h('select', { id: uid('mode') },
      h('option', { value: 'oneoff' }, 'One-off prompt'), h('option', { value: 'blueprint', disabled: !defs.length }, defs.length ? 'Blueprint from the library' : 'Blueprint (library is empty)'));
    const blueprint = h('select', { id: uid('bp') }, defs.map((d) => h('option', { value: d.id }, d.name)));
    const prompt = h('textarea', { id: uid('prompt'), rows: 5, maxlength: 20000 });
    const pathInput = h('input', { id: uid('ws'), type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: '/work/my-project  or  C:\\Users\\you\\project' });
    const pathHint = h('p', { class: 'hint', id: uid('wsh'), 'aria-live': 'polite' }, 'Type the full path of the project folder the CLI should work in.');
    pathInput.setAttribute('aria-describedby', pathHint.id);
    const slot = h('select', { id: uid('slot') },
      h('option', { value: 'auto' }, 'Auto (first free slot)'), h('option', { value: 'auto:claude' }, 'Auto Claude'), h('option', { value: 'auto:codex' }, 'Auto Codex'),
      slots.map((s) => h('option', { value: s.id }, `${s.label} (${s.kind})`)));
    const err = h('p', { class: 'field-error', role: 'alert', hidden: true });
    const bpField = labelled('Blueprint', blueprint, 'Its mission becomes the prompt.');
    const promptField = labelled('Prompt', prompt);
    const submit = h('button', { type: 'submit', class: 'primary' }, 'Add to queue');
    const dialog = h('dialog', { 'aria-labelledby': 'add-title', class: 'add-dialog' });
    const form = h('form', { method: 'dialog', novalidate: true },
      h('h3', { id: 'add-title' }, 'Add a run'),
      labelled('What to run', mode), bpField, promptField,
      h('div', { class: 'field' }, h('label', { for: pathInput.id }, 'Workspace folder'), pathInput, pathHint),
      labelled('Slot', slot), err,
      h('div', { class: 'dialog-actions' }, h('button', { type: 'button', onclick: () => dialog.close() }, 'Close'), submit));
    dialog.append(form);

    const sync = () => {
      const bp = mode.value === 'blueprint';
      bpField.hidden = !bp;
      promptField.querySelector('label').textContent = bp ? 'Extra instructions (optional)' : 'Prompt';
    };
    const check = () => {
      const r = validatePath(pathInput.value);
      pathHint.textContent = pathInput.value ? r.message : 'Type the full path of the project folder the CLI should work in.';
      pathHint.className = 'hint' + (pathInput.value ? (r.ok ? ' ok' : ' bad') : '');
      if (pathInput.value && !r.ok) pathInput.setAttribute('aria-invalid', 'true'); else pathInput.removeAttribute('aria-invalid');
      return r;
    };
    const fail = (text, control) => {
      err.textContent = text; err.hidden = false;
      if (control) { control.setAttribute('aria-invalid', 'true'); control.focus(); }
    };
    mode.addEventListener('change', sync);
    pathInput.addEventListener('input', check);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.hidden = true; err.textContent = '';
      prompt.removeAttribute('aria-invalid');
      const where = validatePath(pathInput.value);
      check();
      if (!where.ok) { pathInput.focus(); return; }
      submit.disabled = true;
      try {
        let text = prompt.value;
        let ref;
        if (mode.value === 'blueprint') {
          const full = await api('GET', '/api/studio/definitions/' + blueprint.value);
          const latest = full.versions[full.versions.length - 1];
          const spec = (await api('GET', '/api/studio/versions/' + latest.id)).spec;
          text = buildPrompt(spec.mission, prompt.value);
          ref = blueprint.value;
        } else if (!text.trim()) { fail('Write what the CLI should do.', prompt); return; }
        const body = { prompt: text, workspace: pathInput.value.trim(), slot: slot.value };
        if (ref) body.blueprint_ref = ref;
        await api('POST', '/api/studio/queue', { body });
        dialog.close();
        say('Added to the queue.', 'ok');
        load();
      } catch (ex) {
        if (isNotFound(ex)) fail('The queue is not available. Start Studio with AETHER_EXEC=cli.');
        else if (ex instanceof ApiError && ex.code === 'workspace_invalid') fail(`${ex.detail || 'That folder cannot be used.'}`, pathInput);
        else if (ex instanceof ApiError && ex.code === 'secret_detected') fail(ex.detail || 'The prompt looks like it contains a secret. Remove it.', prompt);
        else fail(errorText(ex));
      } finally { submit.disabled = false; }
    });
    dialog.addEventListener('close', () => { dialog.remove(); if (opener && opener.isConnected) opener.focus(); });
    document.body.append(dialog);
    sync();
    dialog.showModal();
    (mode.value === 'blueprint' ? blueprint : prompt).focus();
  }

  const refresh = () => { if (alive && !document.hidden) load(); };
  const timer = setInterval(refresh, POLL_MS);
  const onVisible = () => { if (!document.hidden) refresh(); };
  document.addEventListener('visibilitychange', onVisible);
  load();
  return () => {
    alive = false;
    clearInterval(timer);
    document.removeEventListener('visibilitychange', onVisible);
    document.querySelectorAll('dialog.add-dialog').forEach((d) => d.remove());
  };
}
