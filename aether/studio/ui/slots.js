// Slots tab: one card per provider slot (a signed-in CLI account), with enable switch, limit state and an editor.
// Pure helpers (formatCountdown, clockLabel, slotState, slotPayload, slotsBody) are exported for the node logic tests.
import { api, h, clear, loading, failure, emptyCard, errorText, ApiError, labelled, uid, confirmButton } from './lib.js';
import { icon } from './icons.js';
import { validatePath, cliNotice, isNotFound } from './workspace.js';

const POLL_MS = 10000;
const EDITABLE = ['id', 'kind', 'label', 'config_dir', 'model', 'enabled', 'order'];

// Remaining time as H:MM:SS (or M:SS under an hour); empty once it has passed.
export function formatCountdown(untilIso, nowMs = Date.now()) {
  const until = Date.parse(untilIso || '');
  if (Number.isNaN(until)) return '';
  const total = Math.ceil((until - nowMs) / 1000);
  if (total <= 0) return '';
  const hours = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const two = (n) => String(n).padStart(2, '0');
  return hours ? `${hours}:${two(mins)}:${two(secs)}` : `${mins}:${two(secs)}`;
}

// Local HH:MM for an ISO time.
export function clockLabel(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// -> {key, text}: key is one of available | running | blocked | disabled
export function slotState(slot, nowMs = Date.now()) {
  if (slot.blocked_until && Date.parse(slot.blocked_until) > nowMs) return { key: 'blocked', text: `blocked until ${clockLabel(slot.blocked_until)}` };
  if (!slot.enabled) return { key: 'disabled', text: 'turned off' };
  if (slot.active_count > 0) return { key: 'running', text: 'running' };
  return { key: 'available', text: 'available' };
}

// Only the fields the server accepts (it refuses unknown fields); null/empty model is sent as null.
export function slotPayload(slot) {
  const out = {};
  for (const key of EDITABLE) out[key] = slot[key];
  out.model = out.model ? out.model : null;
  return out;
}

// Body for PUT slots after replacing (next object), removing (next === null) or adding the slot with this id.
export function slotsBody(slots, id, next) {
  const list = slots.map(slotPayload);
  const at = list.findIndex((s) => s.id === id);
  if (next === null) { if (at >= 0) list.splice(at, 1); } else if (at >= 0) list[at] = slotPayload(next); else list.push(slotPayload(next));
  return { slots: list };
}

const KIND_LABEL = { claude: 'Claude Code', codex: 'Codex' };

export function mountSlots(panel) {
  let alive = true;
  let slots = null;
  let loadFailed = null;
  const board = h('div', { class: 'slot-board' });
  const note = h('div', { class: 'status-line', role: 'status' });
  const addButton = h('button', { type: 'button', class: 'primary', onclick: () => openEditor(null, addButton) }, icon('plus'), ' Add slot');
  clear(panel).append(h('h2', null, 'Provider slots'),
    h('p', { class: 'lead' }, 'A slot is one signed-in coding CLI account. Studio tries enabled slots in order and rests a slot when it hits its usage limit.'),
    h('p', { class: 'hint' }, 'Each slot points at a config folder. That folder is where the CLI keeps its own sign-in, so you log in with the CLI itself. Studio never asks for, shows or stores a password or token.'),
    h('div', { class: 'actions' }, addButton), note, board);
  board.append(loading());
  let ticks = [];

  const say = (text, kind = '') => { note.textContent = text; note.className = 'status-line ' + kind; };

  async function load() {
    try { slots = (await api('GET', '/api/studio/slots')).slots; loadFailed = null; } catch (e) { loadFailed = e; }
    if (alive) draw();
  }

  async function save(body, okText) {
    const result = await api('PUT', '/api/studio/slots', { body });
    slots = result.slots;
    say(okText, 'ok');
    draw();
  }

  function draw() {
    clear(board);
    ticks = [];
    addButton.hidden = Boolean(loadFailed && isNotFound(loadFailed));
    if (loadFailed) { board.append(isNotFound(loadFailed) ? cliNotice() : failure(loadFailed, load)); return; }
    if (!slots.length) {
      board.append(emptyCard('slots', 'No slots yet', 'Add a slot for each signed-in Claude Code or Codex account you want Studio to use.',
        h('button', { type: 'button', onclick: () => openEditor(null, addButton) }, icon('plus'), ' Add a slot')));
      return;
    }
    const grid = h('div', { class: 'slot-grid' });
    [...slots].sort((a, b) => a.order - b.order).forEach((slot) => grid.append(card(slot)));
    board.append(grid);
    tick();
  }

  function card(slot) {
    const state = h('span', { class: 'slot-state' });
    const count = h('span', { class: 'slot-count' });
    ticks.push(() => {
      const s = slotState(slot);
      state.textContent = s.text;
      const left = s.key === 'blocked' ? formatCountdown(slot.blocked_until) : '';
      count.textContent = left ? `back in ${left}` : '';
      if (s.key === 'blocked' && !left) load();
    });
    const toggle = h('input', { type: 'checkbox', role: 'switch', checked: slot.enabled, 'aria-label': `Enable ${slot.label}`,
      onchange: async () => {
        try { await save(slotsBody(slots, slot.id, { ...slot, enabled: toggle.checked }), `${slot.label} ${toggle.checked ? 'turned on' : 'turned off'}.`); }
        catch (e) { toggle.checked = !toggle.checked; say(errorText(e), 'error'); }
      } });
    const blocked = Boolean(slot.blocked_until && Date.parse(slot.blocked_until) > Date.now());
    return h('article', { class: 'card slot-card', 'data-slot': slot.id },
      h('div', { class: 'slot-head' }, h('h3', null, slot.label), h('span', { class: 'chip st-queued' }, KIND_LABEL[slot.kind] || slot.kind),
        h('label', { class: 'switch' }, toggle, h('span', null, 'Enabled'))),
      h('p', { class: 'slot-line' }, icon('clock'), state, count),
      slot.cli_found === false ? h('p', { class: 'hint' }, `The ${KIND_LABEL[slot.kind] || slot.kind} command was not found on this computer, so this slot cannot start work.`) : null,
      h('dl', { class: 'facts' }, h('dt', null, 'Config folder'), h('dd', { class: 'qpath' }, slot.config_dir),
        h('dt', null, 'Model'), h('dd', null, slot.model || 'CLI default'), h('dt', null, 'Order'), h('dd', null, String(slot.order))),
      h('div', { class: 'actions' },
        blocked ? h('button', { type: 'button', onclick: async () => {
          try { slots = (await api('POST', `/api/studio/slots/${slot.id}/clear-limit`, { body: {} })).slots; say('Limit cleared.', 'ok'); draw(); } catch (e) { say(errorText(e), 'error'); }
        } }, 'Clear limit') : null,
        h('button', { type: 'button', onclick: (e) => openEditor(slot, e.currentTarget) }, 'Edit')));
  }

  function tick() { if (alive) ticks.forEach((fn) => fn()); }

  // ---- editor dialog (add / edit / remove)
  function openEditor(slot, opener) {
    const editing = Boolean(slot);
    const id = h('input', { id: uid('sid'), type: 'text', value: slot ? slot.id : '', readonly: editing, autocomplete: 'off', spellcheck: 'false' });
    const kind = h('select', { id: uid('sk') }, Object.entries(KIND_LABEL).map(([v, t]) => h('option', { value: v, selected: slot && slot.kind === v }, t)));
    const label = h('input', { id: uid('sl'), type: 'text', value: slot ? slot.label : '', maxlength: 80 });
    const dir = h('input', { id: uid('sd'), type: 'text', value: slot ? slot.config_dir : '', autocomplete: 'off', spellcheck: 'false' });
    const dirHint = h('p', { class: 'hint', 'aria-live': 'polite' });
    const model = h('input', { id: uid('sm'), type: 'text', value: slot && slot.model ? slot.model : '', autocomplete: 'off', spellcheck: 'false' });
    const nextOrder = slots && slots.length ? Math.min(999, Math.max(...slots.map((s) => s.order)) + 1) : 0;
    const order = h('input', { id: uid('so'), type: 'number', min: 0, max: 999, value: slot ? slot.order : nextOrder });
    const err = h('p', { class: 'field-error', role: 'alert', hidden: true });
    const controls = { id, kind, label, config_dir: dir, model, order };
    const dialog = h('dialog', { 'aria-labelledby': 'slot-title', class: 'slot-dialog' });
    const submit = h('button', { type: 'submit', class: 'primary' }, editing ? 'Save slot' : 'Add slot');
    const remove = editing ? confirmButton('Remove slot', 'Confirm remove', async () => {
      try { await save(slotsBody(slots, slot.id, null), 'Slot removed.'); dialog.close(); } catch (e) { show(e); }
    }, 'danger') : null;
    const form = h('form', { method: 'dialog', novalidate: true },
      h('h3', { id: 'slot-title' }, editing ? 'Edit slot' : 'Add a slot'),
      h('p', { class: 'hint' }, 'The config folder is where this CLI keeps its own sign-in. Log in with the CLI using that folder; Studio only needs the folder and never asks for a token or password.'),
      labelled('Short id', id, 'Lowercase letters, digits, - and _. Cannot change later.'), labelled('Kind', kind), labelled('Label', label),
      h('div', { class: 'field' }, h('label', { for: dir.id }, 'Config folder (absolute path)'), dir, dirHint),
      labelled('Model (optional)', model, 'Leave empty for the CLI default.'), labelled('Order (0 to 999, lower goes first)', order), err,
      h('div', { class: 'dialog-actions' }, remove, h('button', { type: 'button', onclick: () => dialog.close() }, 'Close'), submit));
    dialog.append(form);
    if (editing) kind.disabled = true;

    const checkDir = () => {
      const r = validatePath(dir.value);
      dirHint.textContent = dir.value ? r.message : '';
      dirHint.className = 'hint' + (dir.value ? (r.ok ? ' ok' : ' bad') : '');
      if (dir.value && !r.ok) dir.setAttribute('aria-invalid', 'true'); else dir.removeAttribute('aria-invalid');
      return r;
    };
    dir.addEventListener('input', checkDir);
    function show(e) {
      const name = e instanceof ApiError && e.field ? e.field.split('/').pop() : '';
      const target = controls[name];
      err.textContent = errorText(e).replace(/ \[[^\]]*\]$/, '');
      err.hidden = false;
      if (target) { target.setAttribute('aria-invalid', 'true'); target.focus(); }
    }
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.hidden = true;
      dialog.querySelectorAll('[aria-invalid]').forEach((n) => n.removeAttribute('aria-invalid'));
      if (!checkDir().ok) { dir.focus(); return; }
      submit.disabled = true;
      const next = { id: id.value.trim(), kind: kind.value, label: label.value.trim(), config_dir: dir.value.trim(), model: model.value.trim() || null,
        enabled: slot ? slot.enabled : true, order: Number(order.value) };
      try { await save(slotsBody(slots, slot ? slot.id : next.id, next), editing ? 'Slot saved.' : 'Slot added.'); dialog.close(); } catch (ex) { show(ex); } finally { submit.disabled = false; }
    });
    dialog.addEventListener('close', () => { dialog.remove(); if (opener && opener.isConnected) opener.focus(); });
    document.body.append(dialog);
    checkDir();
    dialog.showModal();
    (editing ? label : id).focus();
  }

  const clock = setInterval(tick, 1000);
  const poll = setInterval(() => { if (alive && !document.hidden) load(); }, POLL_MS);
  load();
  return () => {
    alive = false;
    clearInterval(clock);
    clearInterval(poll);
    document.querySelectorAll('dialog.slot-dialog').forEach((d) => d.remove());
  };
}
