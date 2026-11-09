// Library tab: blueprints (HarnessDefinition + immutable versions), archive, export/import, run with overrides.
import { api, h, clear, loading, empty, failure, errorText, badge, when, fmtJson, download, labelled, overridesForm,
         idempotencyKey, showFieldError, confirmButton } from './lib.js';

export function mountLibrary(host) {
  let alive = true;
  const list = h('div', { class: 'lib-list', 'aria-live': 'polite' });
  const msg = h('div', { class: 'status-line', role: 'status' });
  const search = h('input', { id: 'lib-search', type: 'search', placeholder: 'name or description' });
  const archived = h('input', { id: 'lib-archived', type: 'checkbox' });
  let defs = [];

  const say = (text, kind = '') => { msg.textContent = text; msg.className = 'status-line ' + kind; };

  async function load() {
    try {
      defs = (await api('GET', '/api/studio/definitions?archived=' + (archived.checked ? '1' : '0'))).definitions;
      if (alive) draw();
    } catch (e) { if (alive) clear(list).append(failure(e, load)); }
  }

  function draw() {
    const q = search.value.trim().toLowerCase();
    const rows = defs.filter((d) => !q || (d.name + ' ' + d.description).toLowerCase().includes(q));
    clear(list);
    if (!defs.length) list.append(empty('The library is empty. Build a blueprint in Builder or import a bundle.'));
    else if (!rows.length) list.append(empty('No blueprint matches your search.'));
    for (const d of rows) list.append(card(d));
  }

  function card(d) {
    const slot = h('div', { class: 'slot' });
    const toggle = async () => {
      try { await api('POST', `/api/studio/definitions/${d.id}/${d.archived ? 'unarchive' : 'archive'}`, { body: {}, headers: { 'If-Match': String(d.rev) } }); say(`${d.name} ${d.archived ? 'unarchived' : 'archived'}.`); load(); }
      catch (e) { say(errorText(e), 'error'); }
    };
    return h('article', { class: 'card lib-card' },
      h('h3', null, d.name, ' ', d.archived ? badge('archived') : null),
      h('p', null, d.description || h('em', null, 'No description.')),
      h('p', { class: 'meta' }, `${d.id} · v${d.latest_version} · rev ${d.rev} · updated ${when(d.updated_utc)}`),
      h('div', { class: 'actions' },
        h('button', { type: 'button', onclick: () => versions(d, slot) }, 'Versions'),
        h('a', { class: 'button', href: '#builder/' + d.id }, 'Edit (new version)'),
        d.archived ? null : h('button', { type: 'button', class: 'primary', onclick: () => runPanel(d, slot) }, 'Run…'),
        h('button', { type: 'button', onclick: async () => { try { exportBundle([d.id], d.name); } catch (e) { say(errorText(e), 'error'); } } }, 'Export'),
        d.archived ? h('button', { type: 'button', onclick: toggle }, 'Unarchive') : confirmButton('Archive', 'Confirm archive', toggle)),
      slot);
  }

  async function versions(d, slot) {
    clear(slot).append(loading());
    try {
      const full = await api('GET', '/api/studio/definitions/' + d.id);
      clear(slot).append(h('h4', null, 'Versions (immutable)'), h('ul', { class: 'versions' }, full.versions.slice().reverse().map((v) => {
        const body = h('div');
        return h('li', null, h('strong', null, 'v' + v.version), ' ', h('span', { class: 'meta' }, when(v.created_utc)),
          h('div', { class: 'hash' }, 'sha256 ' + v.spec_sha256), h('div', { class: 'actions' },
            h('button', { type: 'button', onclick: async () => {
              if (body.firstChild) { clear(body); return; }
              try { body.append(h('pre', { class: 'json' }, fmtJson((await api('GET', '/api/studio/versions/' + v.id)).spec))); } catch (e) { body.append(failure(e)); }
            } }, 'View spec')), body);
      })));
    } catch (e) { clear(slot).append(failure(e)); }
  }

  async function runPanel(d, slot) {
    clear(slot).append(loading());
    try {
      const full = await api('GET', '/api/studio/definitions/' + d.id);
      const latest = full.versions[full.versions.length - 1];
      const spec = (await api('GET', '/api/studio/versions/' + latest.id)).spec;
      const form = overridesForm(spec);
      const err = h('div', { class: 'status-line', role: 'alert' });
      clear(slot).append(h('h4', null, `Run v${latest.version} with overrides`), form, err,
        h('button', { type: 'button', class: 'primary', onclick: async () => {
          err.textContent = '';
          try {
            const run = await api('POST', '/api/studio/runs', { headers: { 'Idempotency-Key': idempotencyKey() },
              body: { version_id: latest.id, overrides: form.read() } });
            location.hash = '#runs/' + run.id;
          } catch (e) { showFieldError(form, e); err.textContent = errorText(e); err.className = 'status-line error'; }
        } }, 'Start run'));
    } catch (e) { clear(slot).append(failure(e)); }
  }

  async function exportBundle(ids, label) {
    const bundle = await api('POST', '/api/studio/export', { body: ids ? { definition_ids: ids } : {} });
    download(`studio-bundle-${(label || 'all').replace(/[^A-Za-z0-9._-]+/g, '_')}.json`, fmtJson(bundle));
    say(`Exported ${bundle.definitions.length} blueprint(s). The bundle contains no runs, tokens or host paths.`);
  }

  const importText = h('textarea', { id: 'lib-import', rows: 4, 'data-field': '', placeholder: 'Paste a bundle JSON, or choose a file' });
  const importFile = h('input', { id: 'lib-file', type: 'file', accept: 'application/json,.json', onchange: async () => {
    const file = importFile.files[0];
    if (file) importText.value = await file.text();
  } });
  const importBox = h('details', { class: 'card' }, h('summary', null, 'Import bundle'),
    labelled('Bundle file', importFile), labelled('Bundle JSON', importText),
    h('button', { type: 'button', onclick: async () => {
      let bundle;
      try { bundle = JSON.parse(importText.value); } catch { say('That is not valid JSON.', 'error'); return; }
      try {
        const r = await api('POST', '/api/studio/import', { body: bundle });
        say(`Imported: ${r.created.length} created, ${r.skipped.length} skipped (already present).`);
        importText.value = ''; load();
      } catch (e) { say(errorText(e), 'error'); }
    } }, 'Import'));

  archived.addEventListener('change', load);
  search.addEventListener('input', draw);
  host.append(h('h2', null, 'Library'),
    h('div', { class: 'toolbar' }, labelled('Search', search),
      h('label', { class: 'check', for: 'lib-archived' }, archived, ' Show archived'),
      h('button', { type: 'button', onclick: load }, 'Refresh'),
      h('button', { type: 'button', onclick: () => exportBundle(null, 'all').catch((e) => say(errorText(e), 'error')) }, 'Export all'),
      h('a', { class: 'button primary', href: '#builder' }, 'New blueprint')),
    msg, importBox, list);
  list.append(loading());
  load();
  return () => { alive = false; };
}
