// Quick Run: workspace + agent chosen only from what /mode and /capabilities report; submits an inline DEMO spec.
import { api, h, clear, loading, failure, labelled, errorText, showFieldError, idempotencyKey, demoBadge, badge } from './lib.js';
import { runDetail } from './runs.js';

// Scenario = fixed mock step lists. Every one is simulated and labelled DEMO by the server.
export const SCENARIOS = {
  success: { label: 'Succeeds quickly', stages: [['work', 'DEMO work', 'mock:success', []]] },
  artifact: { label: 'Produces an evidence artifact', stages: [['work', 'DEMO work', 'mock:sleep:1', []], ['report', 'DEMO report', 'mock:artifact:report', ['report']]] },
  fail: { label: 'Fails (exit 3)', stages: [['work', 'DEMO work', 'mock:fail:3', []]] },
  slow: { label: 'Runs 30 s so you can cancel it', stages: [['work', 'DEMO work', 'mock:sleep:30', []]] },
};

export function quickSpec(mission, scenario) {
  const stages = SCENARIOS[scenario].stages;
  const outputs = stages.flatMap((s) => s[3]);
  const caps = outputs.length ? ['demo.run', 'artifact.write'] : ['demo.run'];
  return {
    mission, executor: { kind: 'mock', ref: 'mock:demo' }, capabilities: caps, limits: { wall_seconds: 120 },
    workspace: { kind: 'scratch' },
    stages: stages.map(([key, title, step, outs], i) => ({ key, title, executor_step: step, timeout_seconds: 60,
      needs: i ? [stages[i - 1][0]] : [], capabilities: caps, outputs: outs })),
    evidence: { outputs, require_tests: false },
  };
}

export function mountQuick(host) {
  clear(host).append(loading('Reading mode and capabilities…'));
  let detail = null, alive = true;
  (async () => {
    let mode, caps;
    try { [mode, caps] = await Promise.all([api('GET', '/api/studio/mode'), api('GET', '/api/studio/capabilities')]); } catch (e) {
      if (alive) clear(host).append(failure(e, () => mountQuick(host))); return;
    }
    if (!alive) return;
    const demoRun = caps.capabilities.find((c) => c.id === 'demo.run');
    const agent = h('select', { id: 'qr-agent', 'data-field': '/executor' });
    for (const [key, ex] of Object.entries(mode.executors)) {
      const ok = ex.status === 'ready';
      agent.append(h('option', { value: key, disabled: !ok || key !== 'mock' }, key === 'mock' ? 'DEMO executor (simulated)' : `Adapter (${ok ? 'ready' : 'unavailable'})`));
    }
    const adapter = mode.executors.adapter;
    const workspace = h('select', { id: 'qr-ws', 'data-field': '/workspace' },
      h('option', { value: 'scratch' }, 'Scratch (empty, no host files)'));
    const scenario = h('select', { id: 'qr-scenario' }, Object.entries(SCENARIOS).map(([k, s]) => h('option', { value: k }, s.label)));
    const mission = h('textarea', { id: 'qr-mission', rows: 3, maxlength: 4000, 'data-field': '/mission' }, 'DEMO: simulated run, no real action is taken');
    const msg = h('div', { class: 'status-line', role: 'alert' });
    const out = h('div', { class: 'qr-out' });
    const form = h('form', { class: 'card', novalidate: true, onsubmit: async (e) => {
      e.preventDefault(); msg.textContent = ''; msg.className = 'status-line';
      if (detail) { detail.stop(); detail = null; clear(out); }
      try {
        const run = await api('POST', '/api/studio/runs', { headers: { 'Idempotency-Key': idempotencyKey() },
          body: { name: 'Quick Run DEMO', spec: quickSpec(mission.value, scenario.value) } });
        detail = runDetail(run.id);
        out.append(h('h3', null, 'Live run'), h('p', null, h('a', { href: '#runs/' + run.id }, 'Open in Runs')), detail.node);
        out.scrollIntoView({ block: 'nearest' });
      } catch (err) { showFieldError(form, err); msg.textContent = errorText(err); msg.className = 'status-line error'; }
    } },
      h('h2', null, 'Quick Run'),
      h('p', { class: 'lead' }, 'Run a simulated harness in one step. ', demoBadge(true), ' Nothing here touches your files or the network.'),
      labelled('Workspace', workspace, 'This install reports no mounted workspaces; only a scratch workspace is offered.'),
      labelled('Agent', agent, adapter.status === 'ready' ? 'Quick Run uses the DEMO executor; run enrolled adapters from the Builder.' : `Adapter unavailable: ${adapter.detail || 'no adapter configured'}.`),
      labelled('DEMO scenario', scenario),
      labelled('Mission', mission),
      demoRun && demoRun.available ? null : h('p', { class: 'field-error' }, 'demo.run is not available in this mode; runs will be rejected.'),
      h('div', { class: 'badges' }, badge(`max workers ${mode.max_workers}`), badge(`provider ${mode.provider}`)),
      msg, h('button', { type: 'submit', class: 'primary' }, 'Run DEMO'));
    clear(host).append(form, out);
  })();
  return () => { alive = false; if (detail) detail.stop(); };
}
