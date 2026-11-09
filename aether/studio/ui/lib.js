// Shared helpers for the Studio UI: DOM builder (text only, text nodes only), API client, error mapping.
import { icon } from './icons.js';
const TOKEN_KEY = 'aether.studio.owner';

export const session = {
  get token() { try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; } },
  set(token) { sessionStorage.setItem(TOKEN_KEY, token); },
  clear() { try { sessionStorage.removeItem(TOKEN_KEY); } catch { /* storage unavailable: nothing to clear */ } },
};

export function h(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (['value', 'checked', 'disabled', 'hidden', 'selected', 'open'].includes(key)) node[key] = value;
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  const add = (kid) => {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) kid.forEach(add);
    else node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  };
  kids.forEach(add);
  return node;
}

export const clear = (node) => { node.replaceChildren(); return node; };

let counter = 0;
export const uid = (prefix = 'f') => `${prefix}${++counter}`;

export class ApiError extends Error {
  constructor(status, body) {
    super(body.detail || body.error || `HTTP ${status}`);
    this.status = status;
    this.code = body.error || 'error';
    this.detail = body.detail || '';
    this.field = body.field || null;
    this.payload = body;
  }
}

export async function api(method, path, { body, headers } = {}) {
  assert(session.token, 'api called without a token');
  const options = { method, credentials: 'omit', cache: 'no-store',
                    headers: { Authorization: 'Bearer ' + session.token, ...(headers || {}) } };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); } catch {
    throw new ApiError(0, { error: 'network', detail: 'Cannot reach the Studio server.' });
  }
  let payload = {};
  try { payload = await response.json(); } catch { /* non-JSON error body: reported below from the status */ }
  if (response.status === 401) window.dispatchEvent(new Event('studio:unauthorized'));
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

export function assert(condition, message) { if (!condition) throw new Error('Studio UI assertion: ' + message); }

export const idempotencyKey = () => { assert(crypto.randomUUID, 'crypto.randomUUID missing'); return 'ui-' + crypto.randomUUID(); };

// ---- presentation helpers
export const badge = (text, kind = '') => h('span', { class: 'badge ' + kind }, text);
export const demoBadge = (demo) => demo ? h('span', { class: 'badge demo', title: 'Simulated: no real action was taken' }, 'DEMO') : null;
export const statusBadge = (status) => h('span', { class: 'badge st-' + status }, status);
export const when = (utc) => utc ? utc.replace('T', ' ').replace(/\.\d+Z$/, 'Z') : '–';
export const short = (id) => id ? id.slice(0, 8) + '…' + id.slice(-4) : '–';

export function status(root, text, kind = '') {
  root.textContent = text;
  root.className = 'status-line ' + kind;
}

export function loading(text = 'Loading…') { return h('p', { class: 'state', role: 'status' }, text); }
// Calm empty-state card (docs/studio/UI_DESIGN.md "card"): icon, heading, one paragraph, optional actions.
export function emptyCard(iconName, title, text, ...actions) {
  const headingId = uid('empty');
  return h('section', { class: 'card empty-card', 'aria-labelledby': headingId },
    icon(iconName), h('h2', { id: headingId }, title), h('p', null, text),
    actions.length ? h('div', { class: 'actions' }, actions) : null);
}
export function empty(text) { return h('p', { class: 'state empty' }, text); }
export function failure(error, retry) {
  return h('div', { class: 'state error', role: 'alert' }, h('p', null, errorText(error)),
    retry ? h('button', { type: 'button', onclick: retry }, 'Retry') : null);
}

export function errorText(error) {
  if (!(error instanceof ApiError)) return String(error && error.message || error);
  const where = error.field ? ` (${error.field})` : '';
  return `${error.detail || error.code}${where} [${error.code}]`;
}

export function labelled(label, control, hint) {
  const id = control.id || (control.id = uid('c'));
  const wrap = h('div', { class: 'field' }, h('label', { for: id }, label), control);
  if (hint) {
    const hintId = id + '-hint';
    wrap.append(h('p', { class: 'hint', id: hintId }, hint));
    control.setAttribute('aria-describedby', hintId);
  }
  return wrap;
}

// ---- per-field server errors. Controls carry data-field="/json/pointer"; the longest matching prefix wins.
export function clearFieldErrors(root) {
  root.querySelectorAll('.field-error').forEach((node) => node.remove());
  root.querySelectorAll('[aria-invalid]').forEach((node) => {
    node.removeAttribute('aria-invalid');
    const keep = (node.getAttribute('aria-describedby') || '').split(' ').filter((id) => id && !id.endsWith('-err'));
    if (keep.length) node.setAttribute('aria-describedby', keep.join(' ')); else node.removeAttribute('aria-describedby');
  });
}

export function showFieldError(root, error) {
  clearFieldErrors(root);
  if (!(error instanceof ApiError) || !error.field) return null;
  const parts = error.field.split('/');
  while (parts.length) {
    const pointer = parts.join('/');
    const target = [...root.querySelectorAll('[data-field]')].find((node) => node.dataset.field === pointer);
    if (target) {
      const id = (target.id || (target.id = uid('c'))) + '-err';
      const note = h('p', { class: 'field-error', id }, errorText(error));
      (target.closest('.field') || target).append(note);
      target.setAttribute('aria-invalid', 'true');
      target.setAttribute('aria-describedby', [target.getAttribute('aria-describedby'), id].filter(Boolean).join(' '));
      return target;
    }
    parts.pop();
  }
  return null;
}

export function fmtJson(value) { return JSON.stringify(value, null, 2); }

export function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  const link = h('a', { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Two-step confirm for destructive/irreversible actions: first click arms, second click runs.
export function confirmButton(label, armedLabel, action, cls = '') {
  let timer = 0;
  const button = h('button', { type: 'button', class: cls, onclick: async () => {
    if (!button.dataset.armed) {
      button.dataset.armed = '1';
      button.textContent = armedLabel;
      timer = setTimeout(disarm, 6000);
      return;
    }
    disarm();
    await action();
  } }, label);
  function disarm() { clearTimeout(timer); delete button.dataset.armed; button.textContent = label; }
  return button;
}

// ---- overrides: declared inputs plus lowered limits, relative to a baseline spec
export function overridesForm(baseSpec, prior = {}) {
  const root = h('div', { class: 'overrides' });
  const controls = [];
  const inputs = baseSpec.inputs || [];
  if (inputs.length) root.append(h('h4', null, 'Inputs'));
  for (const item of inputs) {
    const current = item.name in prior ? prior[item.name] : item.default;
    let control;
    if (item.type === 'boolean') control = h('input', { type: 'checkbox', checked: current === true });
    else if (item.type === 'enum') control = h('select', null, item.enum.map((c) => h('option', { value: c, selected: c === current }, c)));
    else control = h('input', { type: item.type === 'integer' ? 'number' : 'text', value: current ?? '' });
    control.dataset.field = '/overrides/' + item.name;
    controls.push({ kind: 'input', item, control });
    const wrap = labelled(`${item.name}${item.required ? ' (required)' : ''}`, control, `type ${item.type}; default ${item.default ?? 'none'}`);
    root.append(wrap);
  }
  root.append(h('h4', null, 'Limits (may only be lowered)'));
  for (const name of ['wall_seconds', 'tokens', 'cost_usd_cents', 'max_workers']) {
    const base = baseSpec.limits[name];
    const control = h('input', { type: 'number', min: 0, max: base, value: prior.limits && name in prior.limits ? prior.limits[name] : base });
    control.dataset.field = '/overrides/limits/' + name;
    controls.push({ kind: 'limit', name, base, control });
    root.append(labelled(name, control, `blueprint value ${base}`));
  }
  root.read = () => {
    const out = {};
    const limits = {};
    for (const c of controls) {
      if (c.kind === 'input') {
        const { item, control } = c;
        const value = item.type === 'boolean' ? control.checked
          : item.type === 'integer' ? (control.value === '' ? null : Number(control.value)) : control.value;
        if (value !== item.default && !(value === '' && item.default === null) && value !== null) out[item.name] = value;
      } else if (c.control.value !== '' && Number(c.control.value) !== c.base) {
        limits[c.name] = Number(c.control.value);
      }
    }
    if (Object.keys(limits).length) out.limits = limits;
    return out;
  };
  return root;
}
