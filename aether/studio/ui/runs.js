// Runs tab: list + filter, and the live run detail reused by Quick Run.
import { api, h, clear, loading, empty, failure, errorText, statusBadge, demoBadge, badge, when, short, fmtJson,
         confirmButton, overridesForm, idempotencyKey, showFieldError, ApiError, labelled } from './lib.js';

const STATUSES = ['queued', 'running', 'blocked', 'succeeded', 'failed', 'cancelled'];
const TERMINAL = ['succeeded', 'failed', 'cancelled'];
const MAX_EVENTS = 500;

export function eventRow(event) {
  const data = event.data || {};
  let body;
  if (event.type === 'stage.log') body = h('pre', { class: 'log' }, `${data.stream || 'out'}: ${data.text}${data.redacted ? '  [redacted]' : ''}`);
  else if (event.type === 'adapter.event' && data.kind === 'log') body = h('pre', { class: 'log' }, `adapter ${data.adapter} #${data.i} ${data.data.stream}: ${data.data.text}${data.redacted ? '  [redacted]' : ''}`);
  else body = h('span', { class: 'ev-data' }, Object.entries(data).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join('  '));
  return h('li', { class: 'event' + (event.type.startsWith('executor.error') ? ' bad' : '') },
    h('span', { class: 'ev-seq' }, '#' + event.seq), h('time', null, when(event.utc)), h('strong', null, event.type),
    event.stage_key ? h('span', { class: 'ev-stage' }, event.stage_key) : null, demoBadge(event.demo), body);
}

// Live detail. Returns {node, stop}. Polls events by cursor, honouring poll_after_ms; stops itself at terminal state.
export function runDetail(runId, { onTerminal } = {}) {
  const root = h('section', { class: 'run-detail', 'aria-label': 'Run ' + runId });
  const head = h('div', { class: 'run-head' });
  const actions = h('div', { class: 'actions' });
  const live = h('p', { class: 'sr-only', role: 'status', 'aria-live': 'polite' });
  const stagesBox = h('div', { class: 'stages' });
  const approvalsBox = h('div', { class: 'approvals' });
  const auditBox = h('div', { class: 'audit' });
  const eventList = h('ol', { class: 'events', 'aria-label': 'Event timeline' });
  const evidenceBox = h('div', { class: 'evidence' });
  const snapBox = h('div', { class: 'snap' });
  const relaunchBox = h('div', { class: 'relaunch' });
  const errBox = h('div', { class: 'status-line', role: 'alert' });
  root.append(head, live, errBox, actions, h('h3', null, 'Stages'), stagesBox, approvalsBox,
    h('h3', null, 'Events'), eventList, auditBox, h('h3', null, 'Evidence'), evidenceBox, snapBox, relaunchBox);
  let alive = true, cursor = 0, timer = 0, lastStatus = null, shown = 0, finished = false, baseSpec = null;

  const fail = (error) => { errBox.textContent = errorText(error); errBox.classList.add('error'); };

  function renderRun(run) {
    clear(head).append(
      h('div', { class: 'badges' }, statusBadge(run.status), demoBadge(run.demo), run.reason ? badge(run.reason, 'reason') : null,
        run.cancel_requested && !TERMINAL.includes(run.status) ? badge('cancel requested', 'reason') : null),
      h('dl', { class: 'facts' },
        h('dt', null, 'Run'), h('dd', null, run.id), h('dt', null, 'Blueprint version'), h('dd', null, run.version_id),
        h('dt', null, 'Snapshot hash'), h('dd', { class: 'hash' }, run.snapshot_sha256),
        h('dt', null, 'Created'), h('dd', null, when(run.created_utc)), h('dt', null, 'Started'), h('dd', null, when(run.started_utc)),
        h('dt', null, 'Ended'), h('dd', null, when(run.ended_utc)),
        run.relaunch_of ? [h('dt', null, 'Relaunch of'), h('dd', null, h('a', { href: '#runs/' + run.relaunch_of }, run.relaunch_of))] : null,
        run.exit_code !== null ? [h('dt', null, 'Exit code'), h('dd', null, String(run.exit_code))] : null));
    renderStages(run);
    clear(actions);
    if (!TERMINAL.includes(run.status)) {
      actions.append(confirmButton('Cancel run', 'Confirm cancel', async () => {
        try { await api('POST', `/api/studio/runs/${runId}/cancel`, { body: { confirm: runId } }); errBox.textContent = ''; tick(0); } catch (e) { fail(e); }
      }, 'danger'));
    }
    if (run.status !== lastStatus) {
      live.textContent = `Run ${run.status}${run.reason ? ' (' + run.reason + ')' : ''}`;
      lastStatus = run.status;
    }
    clear(evidenceBox).append(
      h('p', null, run.evidence.events_sha256 ? ['Events hash ', h('code', { class: 'hash' }, run.evidence.events_sha256)] : 'Events are not sealed yet (run still active).'),
      run.artifacts.length ? h('ul', { class: 'artifacts' }, run.artifacts.map((a) => h('li', null,
        h('strong', null, a.name), ' ', demoBadge(a.demo), a.flagged ? badge('flagged: secret-shaped content redacted', 'bad') : null,
        h('div', { class: 'meta' }, `${a.media_type} · ${a.size} bytes · stage ${a.stage_key}`),
        h('div', { class: 'hash' }, 'sha256 ' + a.sha256))))
        : empty(TERMINAL.includes(run.status) ? 'This run produced no artifacts.' : 'No artifacts yet.'),
      run.artifacts.length ? h('p', { class: 'hint' }, 'Metadata only: the CE API does not serve artifact bytes yet.') : null);
    clear(snapBox).append(h('details', null, h('summary', null, 'Immutable snapshot and overrides'),
      h('p', { class: 'hint' }, 'Frozen at submit. Hash ' + run.snapshot_sha256), h('pre', { class: 'json' }, fmtJson({ overrides: run.overrides, snapshot: run.snapshot }))));
  }

  // Stage graph: every stage of the (expanded) DAG with its latest attempt, role and needs.
  function renderStages(run) {
    clear(stagesBox);
    const specs = [...run.snapshot.stages, ...(run.added_stages || [])];
    const latest = new Map(run.stages.map((s) => [s.key, s]));
    const allowed = (spec) => Math.max(1 + spec.retry_max, run.snapshot.retry.max_attempts);
    for (const spec of specs) {
      const s = latest.get(spec.key);
      const status = s ? s.status : 'queued';
      stagesBox.append(h('div', { class: 'stage', role: 'group', 'aria-label': `Stage ${spec.key}: ${status}` },
        h('strong', null, spec.key), statusBadge(status), badge(spec.role || 'worker', ''),
        s ? h('span', null, `attempt ${s.attempt}/${allowed(spec)}`) : null,
        spec.optional ? badge('optional', '') : null,
        s && s.reason ? badge(s.reason, 'reason') : null,
        s && s.exit_code !== null ? h('span', null, 'exit ' + s.exit_code) : null,
        spec.needs.length ? h('span', { class: 'meta' }, 'needs ' + spec.needs.join(', ')) : null));
    }
  }

  // A blocked run with no pending approval continues from its checkpoint when the owner resumes it.
  function renderResume(run, pending) {
    if (run.status !== 'blocked' || pending) return;
    actions.prepend(h('button', { type: 'button', class: 'primary', onclick: async () => {
      try { await api('POST', `/api/studio/runs/${runId}/resume`, { body: { confirm: runId } }); errBox.textContent = ''; tick(0); } catch (e) { fail(e); }
    } }, 'Resume run'));
    if (run.reason === 'owner_action_required') {
      actions.append(h('p', { class: 'status-line warn', role: 'status' },
        'A stage was running when the runner stopped and is not marked restartable. Resuming re-runs it; check its side effects first.'));
    }
  }

  function renderAudit(audit) {
    clear(auditBox).append(h('details', null,
      h('summary', null, `Audit trail (${audit.entries.length})`),
      h('p', { class: audit.verify.ok ? 'hint' : 'status-line error', role: audit.verify.ok ? null : 'alert' },
        audit.verify.ok ? `Hash chain verified, ${audit.verify.entries} entries.` : `Hash chain BROKEN at entry ${audit.verify.broken_seq}.`),
      h('ol', { class: 'events', 'aria-label': 'Audit trail' }, audit.entries.map((e) => h('li', { class: 'event' },
        h('span', { class: 'ev-seq' }, '#' + e.seq), h('time', null, when(e.utc)), h('strong', null, e.action),
        h('span', { class: 'ev-stage' }, e.actor), e.stage_key ? h('span', { class: 'ev-stage' }, e.stage_key) : null,
        h('span', { class: 'ev-data' }, Object.entries(e.detail).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join('  ')))))));
  }

  async function renderApprovals(run) {
    const all = (await api('GET', '/api/studio/approvals')).approvals.filter((a) => a.run_id === runId);
    clear(approvalsBox);
    if (!all.length) return false;
    approvalsBox.append(h('h3', null, 'Approvals'));
    for (const a of all) {
      const row = h('div', { class: 'approval' }, h('strong', null, `${a.capability} @ ${a.stage_key}`), badge(a.status, 'ap-' + a.status));
      if (a.status === 'pending') {
        for (const [decision, label, cls] of [['grant', 'Approve', ''], ['deny', 'Deny', 'danger']]) {
          row.append(confirmButton(label, 'Confirm ' + label.toLowerCase(), async () => {
            try { await api('POST', `/api/studio/approvals/${a.id}`, { body: { decision, confirm: runId } }); tick(0); } catch (e) { fail(e); }
          }, cls));
        }
      }
      approvalsBox.append(row);
    }
    return all.some((a) => a.status === 'pending');
  }

  async function renderRelaunch(run) {
    if (!TERMINAL.includes(run.status) || relaunchBox.dataset.ready) return;
    relaunchBox.dataset.ready = '1';
    baseSpec = (await api('GET', '/api/studio/versions/' + run.version_id)).spec;
    const form = overridesForm(baseSpec, run.overrides);
    const msg = h('div', { class: 'status-line', role: 'alert' });
    const go = h('button', { type: 'button', class: 'primary', onclick: async () => {
      msg.textContent = '';
      try {
        const next = await api('POST', '/api/studio/runs', { headers: { 'Idempotency-Key': idempotencyKey() },
          body: { version_id: run.version_id, overrides: form.read(), relaunch_of: run.id } });
        location.hash = '#runs/' + next.id;
      } catch (e) { showFieldError(form, e); msg.textContent = errorText(e); msg.className = 'status-line error'; }
    } }, 'Relaunch');
    relaunchBox.append(h('details', null, h('summary', null, 'Relaunch with overrides'),
      h('p', { class: 'hint' }, 'Creates a new run on the same blueprint version. This run and its snapshot stay unchanged.'), form, msg, go));
  }

  async function tick(delay) {
    clearTimeout(timer);
    if (!alive || finished) return;
    timer = setTimeout(async () => {
      try {
        let more = true, nextDelay = 500, terminal = false;
        while (more && alive) {
          const page = await api('GET', `/api/studio/runs/${runId}/events?cursor=${cursor}&limit=200`);
          for (const ev of page.events) {
            if (shown < MAX_EVENTS) { eventList.append(eventRow(ev)); shown++; }
            else if (shown === MAX_EVENTS) { eventList.append(h('li', { class: 'event' }, `Only the first ${MAX_EVENTS} events are shown.`)); shown++; }
          }
          cursor = page.next_cursor; more = page.has_more; nextDelay = page.poll_after_ms;
          terminal = nextDelay === 0 && !more;
        }
        if (!alive) return;
        const run = await api('GET', '/api/studio/runs/' + runId);
        errBox.textContent = ''; errBox.className = 'status-line';
        renderRun(run);
        renderResume(run, await renderApprovals(run));
        renderAudit(await api('GET', `/api/studio/runs/${runId}/audit`));
        if (!eventList.children.length) eventList.append(h('li', { class: 'state empty' }, 'No events yet.'));
        if (terminal && TERMINAL.includes(run.status)) {
          finished = true;
          await renderRelaunch(run);
          if (onTerminal) onTerminal(run);
          return;
        }
        tick(Math.max(nextDelay, 300));
      } catch (e) { fail(e); if (!(e instanceof ApiError && e.status === 404)) tick(2000); }
    }, delay);
  }
  head.append(loading('Loading run…'));
  tick(0);
  return { node: root, stop() { alive = false; clearTimeout(timer); } };
}

export function mountRuns(host, route) {
  clear(host);
  if (route[0]) {
    const back = h('a', { class: 'back', href: '#runs' }, '← All runs');
    const detail = runDetail(route[0]);
    host.append(back, detail.node);
    return detail.stop;
  }
  const list = h('div', { class: 'run-list', 'aria-live': 'polite' });
  const filter = h('select', { id: 'run-filter' }, [h('option', { value: '' }, 'all statuses'), ...STATUSES.map((s) => h('option', { value: s }, s))]);
  let alive = true, timer = 0;
  async function load() {
    clearTimeout(timer);
    try {
      const q = filter.value ? '?status=' + filter.value : '';
      const { runs } = await api('GET', '/api/studio/runs' + q);
      if (!alive) return;
      clear(list);
      if (!runs.length) list.append(empty(filter.value ? `No ${filter.value} runs.` : 'No runs yet. Start one from Quick Run or the Library.'));
      for (const r of runs) {
        list.append(h('a', { class: 'run-row', href: '#runs/' + r.id }, statusBadge(r.status), demoBadge(r.demo),
          h('span', { class: 'rid' }, short(r.id)), h('time', null, when(r.created_utc)),
          r.reason ? badge(r.reason, 'reason') : null, r.relaunch_of ? badge('relaunch', '') : null));
      }
      timer = setTimeout(load, 3000);
    } catch (e) { if (alive) clear(list).append(failure(e, load)); }
  }
  filter.addEventListener('change', load);
  host.append(h('h2', null, 'Runs'), h('div', { class: 'toolbar' }, labelled('Status', filter),
    h('button', { type: 'button', onclick: load }, 'Refresh')), list);
  list.append(loading());
  load();
  return () => { alive = false; clearTimeout(timer); };
}
