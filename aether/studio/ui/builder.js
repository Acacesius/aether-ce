// Builder: guided stepper Spec -> Scope -> Permissions -> Review -> Validate -> Launch.
// The client only shapes the document and shows server answers; every rule is enforced by the API.
import { api, h, clear, loading, failure, labelled, errorText, badge, fmtJson, idempotencyKey, showFieldError,
         clearFieldErrors, overridesForm, ApiError, assert } from './lib.js';
import { assistPanel, reviewView } from './assist.js';

export const STEPS = ['Spec', 'Scope', 'Permissions', 'Review', 'Validate', 'Launch'];
const STEP_OF = [[/^\/(name|description|mission|executor|stages|inputs)/, 0], [/^\/(workspace|limits|retry|cancellation|evidence)/, 1],
                 [/^\/(capabilities|network_allowlist|approvals)/, 2]];
export const stepFor = (field) => { const m = STEP_OF.find(([re]) => re.test(field || '')); return m ? m[1] : 0; };

// Non-integers are passed through as text so the server, not the client, rejects them.
const num = (value) => (/^-?\d+$/.test(String(value).trim()) ? Number(value) : value);
const lines = (text) => text.split('\n').map((l) => l.trim()).filter(Boolean);
const keys = (text) => text.split(/[\s,]+/).filter(Boolean);

export function newDraft() {
  return { name: '', description: '', mission: '', executor: { kind: 'mock', ref: 'mock:demo' },
    workspace: { kind: 'scratch', mount: '', access: 'ro' }, inputs: [], capabilities: ['demo.run'], network: '',
    limits: { wall_seconds: '120', tokens: '0', cost_usd_cents: '0', max_workers: '1' },
    stages: [{ key: 'work', title: 'Work', executor_step: 'mock:success', timeout_seconds: '60', retry_max: '0', needs: [], capabilities: ['demo.run'], outputs: '' }],
    retry: { max_attempts: '1', backoff_seconds: '0' }, cancellation: { grace_seconds: '5' }, approvals: [],
    evidence: { outputs: '', require_tests: false } };
}

export function toSpec(d) {
  const ws = d.workspace.kind === 'mounted' ? { kind: 'mounted', mount: d.workspace.mount, access: d.workspace.access }
    : { kind: 'scratch', access: d.workspace.access };
  return { mission: d.mission, executor: { kind: d.executor.kind, ref: d.executor.ref }, workspace: ws,
    inputs: d.inputs.map((i) => { const o = { name: i.name, type: i.type, required: i.required };
      if (i.default !== '') o.default = i.type === 'integer' ? num(i.default) : i.type === 'boolean' ? i.default === 'true' : i.default;
      if (i.type === 'enum') o.enum = keys(i.enum); return o; }),
    capabilities: d.capabilities, network_allowlist: lines(d.network),
    limits: Object.fromEntries(Object.entries(d.limits).map(([k, v]) => [k, num(v)])),
    stages: d.stages.map((s) => ({ key: s.key, title: s.title, executor_step: s.executor_step, timeout_seconds: num(s.timeout_seconds),
      retry_max: num(s.retry_max), needs: s.needs, capabilities: s.capabilities, outputs: keys(s.outputs) })),
    retry: { max_attempts: num(d.retry.max_attempts), backoff_seconds: num(d.retry.backoff_seconds) },
    cancellation: { grace_seconds: num(d.cancellation.grace_seconds) }, approvals: d.approvals.map((a) => ({ ...a })),
    evidence: { outputs: keys(d.evidence.outputs), require_tests: d.evidence.require_tests } };
}

export function fromSpec(spec) {
  const d = newDraft();
  d.mission = spec.mission; d.executor = { ...spec.executor };
  d.workspace = { kind: spec.workspace.kind, mount: spec.workspace.mount || '', access: spec.workspace.access };
  d.inputs = spec.inputs.map((i) => ({ name: i.name, type: i.type, required: i.required, default: i.default === null ? '' : String(i.default), enum: i.enum.join(', ') }));
  d.capabilities = [...spec.capabilities]; d.network = spec.network_allowlist.join('\n');
  d.limits = Object.fromEntries(Object.entries(spec.limits).map(([k, v]) => [k, String(v)]));
  d.stages = spec.stages.map((s) => ({ key: s.key, title: s.title, executor_step: s.executor_step, timeout_seconds: String(s.timeout_seconds),
    retry_max: String(s.retry_max), needs: [...s.needs], capabilities: [...s.capabilities], outputs: s.outputs.join(' ') }));
  d.retry = { max_attempts: String(spec.retry.max_attempts), backoff_seconds: String(spec.retry.backoff_seconds) };
  d.cancellation = { grace_seconds: String(spec.cancellation.grace_seconds) };
  d.approvals = spec.approvals.map((a) => ({ ...a }));
  d.evidence = { outputs: spec.evidence.outputs.join(' '), require_tests: spec.evidence.require_tests };
  return d;
}

// Inline display only: returns a message when the stage graph has an unknown dependency or a cycle.
export function graphProblem(stages) {
  const by = new Map(stages.map((s) => [s.key, s]));
  for (const s of stages) for (const n of s.needs) if (!by.has(n)) return `${s.key} depends on unknown stage ${n}`;
  const state = new Map();
  let cycle = null;
  const visit = (key, path) => {
    if (cycle) return;
    if (state.get(key) === 1) { cycle = [...path.slice(path.indexOf(key)), key].join(' → '); return; }
    if (state.get(key) === 2) return;
    state.set(key, 1);
    for (const n of by.get(key).needs) visit(n, [...path, key]);
    state.set(key, 2);
  };
  stages.forEach((s) => visit(s.key, []));
  const dup = stages.map((s) => s.key).find((k, i, a) => a.indexOf(k) !== i);
  return cycle ? 'Cycle: ' + cycle : dup ? `Duplicate stage key ${dup}` : '';
}

const text = (id, d, path, field, extra) => {
  let o = d; for (const p of path.slice(0, -1)) o = o[p];
  const last = path[path.length - 1];
  return h('input', { id, type: 'text', value: o[last], 'data-field': field, oninput: (e) => { o[last] = e.target.value; changed(); }, ...extra });
};
let changed = () => {};

export function mountBuilder(host, route) {
  let alive = true;
  clear(host).append(loading('Preparing Builder…'));
  (async () => {
    let caps, mode, assist, existing = null;
    try {
      [caps, mode, assist] = await Promise.all([api('GET', '/api/studio/capabilities'), api('GET', '/api/studio/mode'), api('GET', '/api/studio/assist/status')]);
      if (route[0]) {
        const def = await api('GET', '/api/studio/definitions/' + route[0]);
        const latest = def.versions[def.versions.length - 1];
        existing = { def, spec: (await api('GET', '/api/studio/versions/' + latest.id)).spec };
      }
    } catch (e) { if (alive) clear(host).append(failure(e, () => mountBuilder(host, route))); return; }
    if (alive) run(host, caps.capabilities, mode, existing, assist);
  })();
  return () => { alive = false; changed = () => {}; };
}

function run(host, capList, mode, existing, assist) {
  const d = existing ? fromSpec(existing.spec) : newDraft();
  const meta = { name: existing ? existing.def.name : '', description: existing ? existing.def.description : '' };
  let defId = existing ? existing.def.id : null, rev = existing ? existing.def.rev : null, savedVersionId = null;
  let step = 0, validated = null, lastError = null, dirty = true, savedFp = null;
  let ai = null;   // the AI draft the form was loaded from: { id, review, saved }; cleared never, so its review stays visible
  const loadDraft = (draft) => {
    Object.assign(d, fromSpec(draft.spec)); meta.name = draft.name; meta.description = draft.description;
    ai = { id: draft.id, review: draft.review, saved: false }; validated = null; changed(); go(0);
  };
  const fp = () => JSON.stringify([meta, d]);
  changed = () => { dirty = true; if (validated && validated.fp !== fp()) validated = null; };

  const nav = h('ol', { class: 'stepper', 'aria-label': 'Builder steps' });
  const panel = h('div', { class: 'step-panel', id: 'builder-panel', tabindex: '-1' });
  const summary = h('div', { class: 'status-line', role: 'alert', id: 'builder-summary' });
  const back = h('button', { type: 'button', onclick: () => go(step - 1) }, 'Back');
  const next = h('button', { type: 'button', class: 'primary', onclick: () => go(step + 1) }, 'Next');

  function go(n) {
    step = Math.max(0, Math.min(STEPS.length - 1, n));
    draw();
    panel.focus();
  }

  function draw() {
    clear(nav).append(...STEPS.map((s, i) => h('li', null, h('button', { type: 'button', 'aria-current': i === step ? 'step' : null,
      class: 'step' + (i === step ? ' on' : ''), onclick: () => go(i) }, h('span', { class: 'n' }, i + 1), ' ', s))));
    clear(panel).append(h('h2', null, `${step + 1}. ${STEPS[step]}`), [specStep, scopeStep, permsStep, reviewStep, validateStep, launchStep][step]());
    back.disabled = step === 0;
    next.hidden = step === STEPS.length - 1;
    if (lastError) showFieldError(panel, lastError);
  }

  // ---- 1 Spec
  function specStep() {
    const stageBox = h('div', { class: 'stage-editor' });
    const problem = h('p', { class: 'status-line', role: 'status', 'aria-live': 'polite' });
    const drawStages = () => {
      clear(stageBox);
      d.stages.forEach((s, i) => {
        const f = `/stages/${i}`;
        const others = d.stages.filter((_, j) => j !== i);
        stageBox.append(h('fieldset', { class: 'stage-card' }, h('legend', null, `Stage ${i + 1}: ${s.key || '(unnamed)'}`),
          labelled('Key', text(`st${i}-key`, d, ['stages', i, 'key'], f + '/key', { oninput: (e) => {
            const old = s.key; s.key = e.target.value;
            d.stages.forEach((o) => { o.needs = o.needs.map((n) => (n === old ? s.key : n)); });
            d.approvals.forEach((a) => { if (a.stage === old) a.stage = s.key; });
            changed(); showProblem(); }, onchange: drawStages }), 'lowercase letters, digits, - or _'),
          labelled('Title', text(`st${i}-title`, d, ['stages', i, 'title'], f + '/title')),
          labelled('Step', text(`st${i}-step`, d, ['stages', i, 'executor_step'], f + '/executor_step'),
            'DEMO steps: mock:success, mock:fail:<code>, mock:sleep:<s>, mock:hang, mock:artifact:<name>, mock:approval:<cap>, mock:verdict:pass|fail'),
          labelled('Timeout (seconds)', text(`st${i}-to`, d, ['stages', i, 'timeout_seconds'], f + '/timeout_seconds', { inputmode: 'numeric' })),
          labelled('Retries (0–3)', text(`st${i}-rt`, d, ['stages', i, 'retry_max'], f + '/retry_max', { inputmode: 'numeric' })),
          labelled('Output names (space separated)', text(`st${i}-out`, d, ['stages', i, 'outputs'], f + '/outputs')),
          h('fieldset', { class: 'checks', 'data-field': f + '/needs' }, h('legend', null, 'Depends on'),
            others.length ? others.map((o) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: s.needs.includes(o.key),
              onchange: (e) => { s.needs = e.target.checked ? [...s.needs, o.key] : s.needs.filter((n) => n !== o.key); changed(); showProblem(); } }), ' ', o.key || '(unnamed)'))
              : h('p', { class: 'hint' }, 'No other stages.')),
          h('button', { type: 'button', disabled: d.stages.length === 1, onclick: () => {
            d.stages.splice(i, 1); d.stages.forEach((o) => { o.needs = o.needs.filter((n) => n !== s.key); }); changed(); drawStages(); showProblem(); } }, 'Remove stage')));
      });
    };
    const showProblem = () => { const p = graphProblem(d.stages); problem.textContent = p; problem.className = 'status-line ' + (p ? 'error' : ''); };
    drawStages(); showProblem();
    return h('div', null,
      existing ? null : assistPanel(assist, loadDraft), ai ? reviewView(ai.review) : null,
      labelled('Name', text('b-name', meta, ['name'], '/name'), 'Used in the Library.'),
      labelled('Description', h('textarea', { id: 'b-desc', rows: 2, 'data-field': '/description', oninput: (e) => { meta.description = e.target.value; changed(); } }, meta.description)),
      labelled('Mission', h('textarea', { id: 'b-mission', rows: 4, 'data-field': '/mission', oninput: (e) => { d.mission = e.target.value; changed(); } }, d.mission), 'What the harness is for (1–4000 characters).'),
      agentField(), h('h3', null, 'Stages'), h('p', { class: 'hint' }, 'A stage runs after all stages it depends on succeed. The server rejects cycles; they are also flagged here.'),
      stageBox, problem,
      h('button', { type: 'button', onclick: () => {
        let n = d.stages.length + 1; while (d.stages.some((s) => s.key === 'stage-' + n)) n++;
        d.stages.push({ key: 'stage-' + n, title: 'Stage ' + n, executor_step: 'mock:success', timeout_seconds: '60', retry_max: '0', needs: [], capabilities: [...d.capabilities], outputs: '' });
        changed(); drawStages(); showProblem(); } }, 'Add stage'),
      inputsEditor());
  }

  function agentField() {
    const adapter = mode.executors.adapter;
    const select = h('select', { id: 'b-exec', 'data-field': '/executor/kind', onchange: (e) => { d.executor.kind = e.target.value; d.executor.ref = e.target.value === 'mock' ? 'mock:demo' : ''; changed(); draw(); } },
      h('option', { value: 'mock', selected: d.executor.kind === 'mock' }, 'DEMO executor (simulated)'),
      h('option', { value: 'adapter', selected: d.executor.kind === 'adapter', disabled: adapter.status !== 'ready' }, `Adapter${adapter.status === 'ready' ? '' : ' (unavailable)'}`));
    const enrolled = (adapter.adapters || []).map((a) => `${a.name} (${a.status}${a.granted.length ? ', grants ' + a.granted.join('/') : ', no grants'})`);
    return h('div', null, labelled('Agent', select, adapter.status === 'ready' ? '' : `Adapter unavailable: ${adapter.detail || 'not configured'}.`),
      labelled('Agent reference', text('b-ref', d, ['executor', 'ref'], '/executor/ref'),
        d.executor.kind === 'adapter' ? `Enrolled adapter name. Steps: submit:<task> or observe:<ref>. Enrolled: ${enrolled.join('; ') || 'none'}.` : ''));
  }

  function inputsEditor() {
    const box = h('div');
    const drawInputs = () => {
      clear(box);
      d.inputs.forEach((it, i) => {
        const f = `/inputs/${i}`;
        box.append(h('fieldset', { class: 'stage-card' }, h('legend', null, `Input ${i + 1}`),
          labelled('Name', text(`in${i}-n`, d, ['inputs', i, 'name'], f + '/name')),
          labelled('Type', h('select', { id: `in${i}-t`, 'data-field': f + '/type', onchange: (e) => { it.type = e.target.value; changed(); drawInputs(); } },
            ['string', 'integer', 'boolean', 'enum'].map((t) => h('option', { value: t, selected: it.type === t }, t)))),
          it.type === 'enum' ? labelled('Choices (comma separated)', text(`in${i}-e`, d, ['inputs', i, 'enum'], f + '/enum')) : null,
          labelled('Default', text(`in${i}-d`, d, ['inputs', i, 'default'], f + '/default'), it.type === 'boolean' ? 'true or false' : ''),
          h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: it.required, onchange: (e) => { it.required = e.target.checked; changed(); } }), ' Required'),
          h('button', { type: 'button', onclick: () => { d.inputs.splice(i, 1); changed(); drawInputs(); } }, 'Remove input')));
      });
    };
    drawInputs();
    return h('div', null, h('h3', null, 'Inputs (overridable per run)'), box,
      h('button', { type: 'button', onclick: () => { d.inputs.push({ name: '', type: 'string', required: false, default: '', enum: '' }); changed(); drawInputs(); } }, 'Add input'));
  }

  // ---- 2 Scope
  function scopeStep() {
    const mount = labelled('Workspace id (symbolic)', text('b-mount', d, ['workspace', 'mount'], '/workspace/mount'), 'Mounted workspaces are referenced by id, never by host path.');
    mount.hidden = d.workspace.kind !== 'mounted';
    const kind = h('select', { id: 'b-wskind', 'data-field': '/workspace/kind', onchange: (e) => { d.workspace.kind = e.target.value; mount.hidden = e.target.value !== 'mounted'; changed(); } },
      ['scratch', 'mounted'].map((k) => h('option', { value: k, selected: d.workspace.kind === k }, k === 'scratch' ? 'Scratch (empty)' : 'Mounted workspace')));
    return h('div', null, labelled('Workspace', kind), mount,
      h('h3', null, 'Limits'),
      labelled('Wall time (seconds)', text('b-wall', d, ['limits', 'wall_seconds'], '/limits/wall_seconds', { inputmode: 'numeric' })),
      labelled('Tokens', text('b-tok', d, ['limits', 'tokens'], '/limits/tokens', { inputmode: 'numeric' }), mode.provider === 'none' ? 'Must be 0 while no model provider is configured.' : ''),
      labelled('Money (US cents)', text('b-cost', d, ['limits', 'cost_usd_cents'], '/limits/cost_usd_cents', { inputmode: 'numeric' }), mode.provider === 'none' ? 'Must be 0 while no model provider is configured.' : ''),
      labelled('Max workers (1–4)', text('b-workers', d, ['limits', 'max_workers'], '/limits/max_workers', { inputmode: 'numeric' })),
      h('h3', null, 'Retry and cancellation'),
      labelled('Max attempts (1–3)', text('b-att', d, ['retry', 'max_attempts'], '/retry/max_attempts', { inputmode: 'numeric' })),
      labelled('Retry backoff (seconds)', text('b-back', d, ['retry', 'backoff_seconds'], '/retry/backoff_seconds', { inputmode: 'numeric' })),
      labelled('Cancellation grace (seconds, 0–60)', text('b-grace', d, ['cancellation', 'grace_seconds'], '/cancellation/grace_seconds', { inputmode: 'numeric' })),
      h('h3', null, 'Tests and evidence'),
      h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: d.evidence.require_tests, 'data-field': '/evidence/require_tests', onchange: (e) => { d.evidence.require_tests = e.target.checked; changed(); } }), ' Require tests to pass'),
      labelled('Evidence outputs (names produced by stages)', text('b-evid', d, ['evidence', 'outputs'], '/evidence/outputs'), 'Space separated.'));
  }

  // ---- 3 Permissions
  function permsStep() {
    const rows = capList.map((c) => {
      const box = h('input', { type: 'checkbox', id: 'cap-' + c.id, 'data-field': '/capabilities', checked: d.capabilities.includes(c.id), disabled: !c.available,
        onchange: (e) => { d.capabilities = e.target.checked ? [...d.capabilities, c.id] : d.capabilities.filter((x) => x !== c.id);
          if (!e.target.checked) { d.stages.forEach((s) => { s.capabilities = s.capabilities.filter((x) => x !== c.id); }); d.approvals = d.approvals.filter((a) => a.capability !== c.id); }
          changed(); draw(); } });
      return h('li', { class: 'cap' }, h('label', { class: 'check', for: box.id }, box, ' ', h('strong', null, c.id)), ' ',
        badge(c.risk + ' risk', 'risk-' + c.risk), c.requires_approval ? badge('needs approval', 'bad') : null,
        h('div', { class: 'hint' }, c.description, c.available ? '' : ' — unavailable in this install.'));
    });
    const net = h('textarea', { id: 'b-net', rows: 3, 'data-field': '/network_allowlist', disabled: !capList.find((c) => c.id === 'net.egress.allowlist').available,
      oninput: (e) => { d.network = e.target.value; changed(); } }, d.network);
    const netAvailable = capList.find((c) => c.id === 'net.egress.allowlist').available;
    const perStage = d.stages.map((s) => h('fieldset', { class: 'checks' }, h('legend', null, `Stage ${s.key} capabilities`),
      d.capabilities.length ? d.capabilities.map((c) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: s.capabilities.includes(c),
        onchange: (e) => { s.capabilities = e.target.checked ? [...s.capabilities, c] : s.capabilities.filter((x) => x !== c); changed(); } }), ' ', c)) : h('p', { class: 'hint' }, 'Select spec capabilities first.')));
    const approvals = h('div');
    const drawApprovals = () => {
      clear(approvals);
      d.approvals.forEach((a, i) => approvals.append(h('fieldset', { class: 'stage-card' }, h('legend', null, `Approval ${i + 1}`),
        labelled('Stage', h('select', { id: `ap${i}-s`, 'data-field': `/approvals/${i}/stage`, onchange: (e) => { a.stage = e.target.value; changed(); } },
          d.stages.map((s) => h('option', { value: s.key, selected: s.key === a.stage }, s.key)))),
        labelled('Capability', h('select', { id: `ap${i}-c`, 'data-field': `/approvals/${i}/capability`, onchange: (e) => { a.capability = e.target.value; changed(); } },
          d.capabilities.map((c) => h('option', { value: c, selected: c === a.capability }, c)))),
        labelled('Reason', text(`ap${i}-r`, d, ['approvals', i, 'reason'], `/approvals/${i}/reason`)),
        h('button', { type: 'button', onclick: () => { d.approvals.splice(i, 1); changed(); drawApprovals(); } }, 'Remove approval'))));
    };
    drawApprovals();
    return h('div', null,
      h('h3', null, 'Directory permissions'),
      labelled('Workspace access', h('select', { id: 'b-access', 'data-field': '/workspace/access', onchange: (e) => { d.workspace.access = e.target.value; changed(); } },
        ['ro', 'rw'].map((a) => h('option', { value: a, selected: d.workspace.access === a }, a === 'ro' ? 'Read only' : 'Read and write (needs workspace.write)'))),
        `Applies to ${d.workspace.kind === 'mounted' ? 'workspace ' + (d.workspace.mount || '(unset)') : 'the scratch workspace'}.`),
      h('h3', null, 'Tool capabilities'), h('p', { class: 'hint' }, 'Only allowlisted capabilities exist. High-risk and approval-required ones are flagged.'),
      h('ul', { class: 'caps' }, rows),
      h('h3', null, 'Network allowlist'),
      labelled('Hosts (host:port, one per line)', net, netAvailable ? '' : 'Disabled: net.egress.allowlist is not available in this install, so outbound network access is off.'),
      h('h3', null, 'Per-stage capabilities'), perStage,
      h('h3', null, 'Approvals'), h('p', { class: 'hint' }, 'A stage pauses until you approve the listed capability.'), approvals,
      h('button', { type: 'button', disabled: !d.capabilities.length, onclick: () => { d.approvals.push({ stage: d.stages[0].key, capability: d.capabilities[0], reason: '' }); changed(); drawApprovals(); } }, 'Add approval'));
  }

  // ---- 4 Review
  function reviewStep() {
    const spec = toSpec(d);
    const flagged = capList.filter((c) => d.capabilities.includes(c.id) && (c.risk === 'high' || c.requires_approval));
    return h('div', null, ai ? reviewView(ai.review) : null,
      h('dl', { class: 'facts' }, h('dt', null, 'Name'), h('dd', null, meta.name || '(not set)'), h('dt', null, 'Agent'), h('dd', null, `${d.executor.kind} · ${d.executor.ref}`),
        h('dt', null, 'Workspace'), h('dd', null, `${d.workspace.kind}${d.workspace.mount ? ' ' + d.workspace.mount : ''} · ${d.workspace.access}`),
        h('dt', null, 'Stages'), h('dd', null, d.stages.map((s) => s.key + (s.needs.length ? ` (after ${s.needs.join(', ')})` : '')).join(' · ')),
        h('dt', null, 'Capabilities'), h('dd', null, d.capabilities.join(', ') || 'none'),
        h('dt', null, 'Network'), h('dd', null, d.network.trim() ? lines(d.network).join(', ') : 'none (outbound disabled)')),
      flagged.length ? h('p', { class: 'status-line warn' }, 'Needs attention: ' + flagged.map((c) => `${c.id} (${c.risk}${c.requires_approval ? ', approval' : ''})`).join('; ')) : null,
      mode.demo_only ? h('p', null, badge('DEMO', 'demo'), ' This install only simulates runs.') : null,
      h('details', null, h('summary', null, 'Specification JSON'), h('pre', { class: 'json' }, fmtJson(spec))));
  }

  // ---- 5 Validate
  function validateStep() {
    const out = h('div', { class: 'validate-out', 'aria-live': 'polite' });
    const show = () => {
      clear(out);
      if (validated) out.append(h('p', { class: 'status-line ok' }, `Valid. Stage order: ${validated.order.join(' → ')}`), h('p', { class: 'hash' }, 'spec sha256 ' + validated.sha));
      else if (lastError) out.append(h('div', { class: 'status-line error' }, errorText(lastError),
        h('button', { type: 'button', onclick: () => go(stepFor(lastError.field)) }, 'Go to the field')));
      else out.append(h('p', { class: 'hint' }, 'Nothing validated yet. Validation runs the same server rules used at launch; nothing is stored.'));
    };
    show();
    return h('div', null, h('button', { type: 'button', class: 'primary', onclick: async () => {
      try {
        const r = await api('POST', '/api/studio/validate', { body: { spec: toSpec(d) } });
        validated = { sha: r.spec_sha256, order: r.stage_order, spec: r.spec, fp: fp() }; lastError = null; clearFieldErrors(panel);
      } catch (e) { validated = null; lastError = e; if (!(e instanceof ApiError)) throw e; }
      show();
    } }, 'Validate'), out);
  }

  // ---- 6 Launch
  function launchStep() {
    if (!validated) return h('p', { class: 'status-line warn' }, 'Validate the current blueprint before saving or launching. Your last edit invalidated any earlier result.');
    const msg = h('div', { class: 'status-line', role: 'alert' });
    const form = overridesForm(validated.spec);
    const say = (t, k = '') => { msg.textContent = t; msg.className = 'status-line ' + k; };
    const save = async () => {
      if (!meta.name) { lastError = new ApiError(400, { error: 'schema_invalid', detail: 'name is required to save', field: '/name' }); go(0); return null; }
      const body = { name: meta.name, description: meta.description, spec: toSpec(d) };
      if (ai && !ai.saved) {
        if (!ack.checked) { say('Tick the acknowledgement of the permissions review before saving an AI draft.', 'error'); return null; }
        const saved = await api('POST', `/api/studio/assist/drafts/${ai.id}/save`,
          { body: { ...body, acknowledge_review: ai.review.review_sha256, confirm: ai.id } });
        ai.saved = true; defId = saved.definition.id; rev = saved.definition.rev; savedFp = fp(); dirty = false;
        savedVersionId = saved.definition.versions.at(-1).id;
        return saved.definition;
      }
      const res = defId ? await api('PUT', '/api/studio/definitions/' + defId, { body, headers: { 'If-Match': String(rev) } })
        : await api('POST', '/api/studio/definitions', { body });
      defId = res.id; rev = res.rev; savedFp = fp(); dirty = false;
      savedVersionId = (await api('GET', '/api/studio/definitions/' + defId)).versions.at(-1).id;
      return res;
    };
    const where = h('p', null);
    const ack = h('input', { type: 'checkbox', id: 'assist-ack' });
    const unsavedAi = () => ai && !ai.saved;
    const runLabel = () => (unsavedAi() ? 'Save as blueprint to run' : savedVersionId && savedFp === fp() ? 'Run saved version' : 'Run (unsaved draft)');
    const runButton = h('button', { type: 'button', class: 'primary', onclick: async () => {
      try {
        const overrides = form.read();
        const body = savedVersionId && savedFp === fp() ? { version_id: savedVersionId, overrides } : { spec: toSpec(d), name: meta.name || 'Builder run', overrides };
        const run = await api('POST', '/api/studio/runs', { body, headers: { 'Idempotency-Key': idempotencyKey() } });
        location.hash = '#runs/' + run.id;
      } catch (e) { showFieldError(form, e); say(errorText(e), 'error'); if (!(e instanceof ApiError)) throw e; }
    } }, runLabel());
    runButton.disabled = unsavedAi();
    const refresh = () => {
      runButton.disabled = unsavedAi();
      where.textContent = defId ? `Library entry ${defId} (rev ${rev}). A changed spec is saved as a new immutable version; an identical one only updates the name and description.` : 'Not saved to the Library yet.';
      runButton.textContent = runLabel();
    };
    refresh();
    return h('div', null, where, msg,
      unsavedAi() ? h('label', { class: 'check', for: 'assist-ack' }, ack, ' I reviewed the permissions review for this AI-PROPOSED draft and want to save it as a blueprint.') : null,
      h('button', { type: 'button', class: 'primary', onclick: async () => {
        try { const r = await save(); if (r) { refresh(); say(`Saved as ${r.name}, version ${r.latest_version}.`, 'ok'); } }
        catch (e) { lastError = e; say(e instanceof ApiError && e.code === 'rev_conflict' ? 'The Library entry changed elsewhere. Reload the entry before saving again.' : errorText(e), 'error'); if (!(e instanceof ApiError)) throw e; }
      } }, unsavedAi() ? 'Save as blueprint' : defId ? 'Save new version' : 'Save to Library'),
      h('h3', null, 'Run'), form, runButton);
  }

  clear(host).append(h('h2', { class: 'sr-only' }, 'Builder'), nav, summary, panel, h('div', { class: 'actions step-actions' }, back, next));
  assert(STEPS.length === 6, 'six steps');
  draw();
}
