"""The ceiling: the most a draft may ask for. Anything beyond it is DROPPED and listed, never silently kept.

Base ceiling = the Builder defaults (low-risk, no-approval capabilities, no network, default limits, DEMO executor).
Only the *user* (request context) can raise it: extra capabilities, network hosts, an enrolled adapter, a workspace id.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import re

from aether.studio import capabilities
from aether.studio.schema import SchemaError

BASE_CAPABILITIES = ('demo.run', 'workspace.read', 'artifact.write')
MAX_WORKERS = 2
HOST_RE = re.compile(r'^[A-Za-z0-9.-]{1,253}:[0-9]{1,5}$')
DEMO_EXECUTOR = {'kind': 'mock', 'ref': 'mock:demo'}


@dataclass(frozen=True)
class Ceiling:
    capabilities: tuple[str, ...]
    network: tuple[str, ...] = ()
    adapter: str | None = None
    mount: str | None = None
    wall_seconds: int = 120
    tokens: int = 0
    cost_usd_cents: int = 0
    max_workers: int = MAX_WORKERS

    def as_dict(self) -> dict:
        return {'capabilities': list(self.capabilities), 'network_allowlist': list(self.network),
                'executor': {'kind': 'adapter', 'ref': self.adapter} if self.adapter else dict(DEMO_EXECUTOR),
                'workspace': {'kind': 'mounted', 'mount': self.mount, 'access': 'ro'} if self.mount else {'kind': 'scratch', 'access': 'ro'},
                'limits': {'wall_seconds': self.wall_seconds, 'tokens': self.tokens,
                           'cost_usd_cents': self.cost_usd_cents, 'max_workers': self.max_workers}}


def build(context, *, provider: str, enrolled_adapters) -> Ceiling:
    """Validate the user's ``context`` and build the ceiling. Unknown keys and unavailable grants are errors."""
    if not isinstance(context, dict):
        raise SchemaError('invalid_request', '/context', 'object required')
    unknown = set(context) - {'capabilities', 'network_allowlist', 'adapter', 'workspace_mount'}
    if unknown:
        raise SchemaError('unknown_field', '/context/' + sorted(unknown)[0])
    extra = context.get('capabilities', [])
    if not isinstance(extra, list) or len(extra) > 32:
        raise SchemaError('invalid_request', '/context/capabilities', 'list of capability ids required')
    try:
        capabilities.check(extra, provider)
    except capabilities.CapabilityError as error:
        raise SchemaError('capability_not_allowed', '/context/capabilities', f'{error.capability}: {error}') from None
    network = context.get('network_allowlist', [])
    if not isinstance(network, list) or len(network) > 32 or not all(isinstance(h, str) and HOST_RE.match(h) for h in network):
        raise SchemaError('invalid_request', '/context/network_allowlist', 'list of host:port required')
    if network and 'net.egress.allowlist' not in extra:
        raise SchemaError('capability_not_allowed', '/context/network_allowlist', 'requires net.egress.allowlist')
    adapter = context.get('adapter')
    caps = list(BASE_CAPABILITIES) + list(extra)
    if adapter is not None:
        if not isinstance(adapter, str) or adapter not in enrolled_adapters:
            raise SchemaError('invalid_request', '/context/adapter', 'must name an enrolled, non-revoked adapter')
        caps.append('adapter.observe')
    mount = context.get('workspace_mount')
    if mount is not None and not (isinstance(mount, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', mount)):
        raise SchemaError('invalid_request', '/context/workspace_mount', 'workspace key required')
    ceiling = Ceiling(tuple(dict.fromkeys(caps)), tuple(network), adapter, mount)
    capabilities.check(ceiling.capabilities, provider)
    return ceiling


def _drop(removed, field, requested, applied, reason):
    action = 'removed' if applied is None else 'added' if requested is None else \
        'lowered' if isinstance(applied, int) and isinstance(requested, int) else 'replaced'
    removed.append({'field': field, 'action': action, 'requested': requested, 'applied': applied, 'reason': reason})


def clamp(raw, ceiling: Ceiling) -> tuple[object, list[dict]]:
    """Return ``(clamped copy, removed)``. Structure that is not the expected type is left for the validator to
    reject; this function only ever narrows, or adds approval entries that capability policy requires."""
    if not isinstance(raw, dict):
        return raw, []
    spec, removed = copy.deepcopy(raw), []
    allowed = set(ceiling.capabilities)

    caps = spec.get('capabilities')
    if isinstance(caps, list):
        kept = [c for c in caps if not isinstance(c, str) or c in allowed]
        for c in caps:
            if isinstance(c, str) and c not in allowed:
                _drop(removed, '/capabilities', c, None, 'above the ceiling: not granted by the Builder defaults or your request')
        spec['capabilities'] = caps = kept
    keep = {c for c in caps if isinstance(c, str)} if isinstance(caps, list) else set()

    net = spec.get('network_allowlist')
    if isinstance(net, list):
        kept = [h for h in net if not isinstance(h, str) or h in ceiling.network]
        for h in net:
            if isinstance(h, str) and h not in ceiling.network:
                _drop(removed, '/network_allowlist', h, None, 'network access is off unless you add the host yourself')
        spec['network_allowlist'] = kept

    ex = spec.get('executor')
    wanted = {'kind': 'adapter', 'ref': ceiling.adapter} if ceiling.adapter else DEMO_EXECUTOR
    if isinstance(ex, dict) and (ex.get('kind'), ex.get('ref')) != (wanted['kind'], wanted['ref']):
        _drop(removed, '/executor', f'{ex.get("kind")}:{ex.get("ref")}'[:80], f'{wanted["kind"]}:{wanted["ref"]}',
              'executor is fixed to the DEMO executor unless you pick an enrolled adapter')
        spec['executor'] = dict(wanted)

    ws = spec.get('workspace')
    if isinstance(ws, dict):
        if ws.get('kind') == 'mounted' and ws.get('mount') != ceiling.mount:
            _drop(removed, '/workspace', f'mounted:{ws.get("mount")}'[:80], 'scratch', 'mounted workspaces are not granted unless you name one')
            ws = spec['workspace'] = {'kind': 'scratch', 'access': 'ro'}
        if ws.get('access') == 'rw' and 'workspace.write' not in allowed:
            _drop(removed, '/workspace/access', 'rw', 'ro', 'workspace.write is not granted')
            ws['access'] = 'ro'

    limits = spec.get('limits')
    if isinstance(limits, dict):
        for name, cap in (('wall_seconds', ceiling.wall_seconds), ('tokens', ceiling.tokens),
                          ('cost_usd_cents', ceiling.cost_usd_cents), ('max_workers', ceiling.max_workers)):
            value = limits.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value > cap:
                _drop(removed, f'/limits/{name}', value, cap, 'limits cannot exceed the defaults')
                limits[name] = cap

    stages = spec.get('stages')
    if isinstance(stages, list):
        for i, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            scaps = stage.get('capabilities')
            if isinstance(scaps, list):
                stage['capabilities'] = [c for c in scaps if not isinstance(c, str) or c in keep]
                for c in scaps:
                    if isinstance(c, str) and c not in keep:
                        _drop(removed, f'/stages/{i}/capabilities', c, None, 'not in the clamped spec capabilities')
            timeout = stage.get('timeout_seconds')
            if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > ceiling.wall_seconds:
                _drop(removed, f'/stages/{i}/timeout_seconds', timeout, ceiling.wall_seconds, 'stage timeout cannot exceed the wall limit')
                stage['timeout_seconds'] = ceiling.wall_seconds
            workers = stage.get('max_workers')
            if isinstance(workers, int) and not isinstance(workers, bool) and workers > ceiling.max_workers:
                _drop(removed, f'/stages/{i}/max_workers', workers, ceiling.max_workers, 'fan-out is capped')
                stage['max_workers'] = ceiling.max_workers
            if stage.get('role') == 'planner':
                _drop(removed, f'/stages/{i}/role', 'planner', None, 'planner stages (which add stages at run time) are not drafted by AI')
                del stage['role']

    approvals = spec.get('approvals')
    if isinstance(approvals, list):
        kept = []
        for i, item in enumerate(approvals):
            if isinstance(item, dict) and isinstance(item.get('capability'), str) and item['capability'] not in keep:
                _drop(removed, f'/approvals/{i}', item['capability'], None, 'capability was removed')
            else:
                kept.append(item)
        spec['approvals'] = approvals = kept
        _require_approvals(spec, approvals, removed)
    elif approvals is None and isinstance(stages, list):
        spec['approvals'] = approvals = []
        _require_approvals(spec, approvals, removed)
    return spec, removed


def _require_approvals(spec, approvals, removed):
    """A draft can never skip an approval: every approval-requiring capability on a stage gets its approval entry."""
    have = {(a.get('stage'), a.get('capability')) for a in approvals if isinstance(a, dict)}
    for stage in spec.get('stages') or []:
        if not isinstance(stage, dict) or not isinstance(stage.get('capabilities'), list):
            continue
        for cap in stage['capabilities']:
            registered = capabilities.REGISTRY.get(cap) if isinstance(cap, str) else None
            if registered and registered.requires_approval and (stage.get('key'), cap) not in have:
                approvals.append({'stage': stage.get('key'), 'capability': cap, 'reason': 'approval required by capability policy'})
                _drop(removed, '/approvals', None, f'{stage.get("key")}:{cap}', 'this capability always needs a human approval')
                have.add((stage.get('key'), cap))


def assert_within(spec: dict, ceiling: Ceiling) -> None:
    """Defence in depth on the validated spec; failing here is a bug, never an input problem."""
    assert set(spec['capabilities']) <= set(ceiling.capabilities), 'capabilities exceed ceiling'
    assert set(spec['network_allowlist']) <= set(ceiling.network), 'network exceeds ceiling'
    assert spec['limits']['wall_seconds'] <= ceiling.wall_seconds and spec['limits']['tokens'] <= ceiling.tokens
    assert spec['limits']['cost_usd_cents'] <= ceiling.cost_usd_cents
    assert spec['limits']['max_workers'] <= ceiling.max_workers, 'max_workers exceeds ceiling'
    assert (spec['executor']['kind'] == 'adapter') == (ceiling.adapter is not None), 'executor exceeds ceiling'
    if ceiling.adapter:
        assert spec['executor']['ref'] == ceiling.adapter
    assert spec['workspace']['kind'] == 'scratch' or spec['workspace']['mount'] == ceiling.mount
    for stage in spec['stages']:
        assert set(stage['capabilities']) <= set(spec['capabilities']), 'stage capabilities exceed spec'
        assert stage.get('role') != 'planner' and stage.get('max_workers', 1) <= ceiling.max_workers
