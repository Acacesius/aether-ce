"""Studio v1 structures and validation (INTERFACE_SPEC §1-5): spec, stage DAG, overrides, ids, hashing.

``validate_spec`` returns a normalized copy (defaults filled) and is idempotent, so re-validating a stored
snapshot reproduces its ``spec_sha256``.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time
import unicodedata

from aether.studio import capabilities

SCHEMA_VERSION = 'aether.studio/v1'
SCHEMA_V2 = 'aether.studio/v2'
STATUSES = ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'blocked')
TERMINAL = frozenset(('succeeded', 'failed', 'cancelled'))
REASONS = ('timeout', 'cancelled_by_owner', 'executor_error', 'exit_nonzero', 'lease_lost', 'approval_denied',
           'approval_pending', 'capability_unavailable', 'adapter_unavailable', 'dependency_failed',
           'limit_exceeded', 'invalid_snapshot', 'budget_exceeded', 'owner_action_required', 'expansion_rejected',
           'review_rejected')
ROLES = ('planner', 'worker', 'reviewer')
ON_FAILURE = ('continue_independent', 'stop')
MAX_PROPOSALS = 16
KEY_RE = re.compile(r'^[a-z][a-z0-9_-]{0,39}$')
CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
ID_RE = re.compile(r'^(hd|hv|run|stg|apr|art|adp)_[0-9A-HJKMNP-TV-Z]{26}$')
ADAPTER_STEP_RE = re.compile(r'^(?P<op>submit|observe):(?P<arg>[^\x00-\x1f]{1,190})$')
ADAPTER_REF_RE = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
MAX_STAGES = 64
MAX_DEPTH = 16


class SchemaError(ValueError):
    def __init__(self, code: str, field: str | None = None, detail: str = ''):
        super().__init__(f'{code} at {field}: {detail}' if field else code)
        self.code = code
        self.field = field
        self.detail = detail[:300]

    def as_dict(self):
        return {'error': self.code, 'field': self.field, 'detail': self.detail}


# ---------------------------------------------------------------- primitives

def new_id(prefix: str, *, now: float | None = None) -> str:
    ms = int((time.time() if now is None else now) * 1000)
    value = (ms << 80) | int.from_bytes(os.urandom(10), 'big')
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD[value & 31])
        value >>= 5
    return f'{prefix}_' + ''.join(reversed(chars))


def utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256_hex(text: str | bytes) -> str:
    return hashlib.sha256(text.encode('utf-8') if isinstance(text, str) else text).hexdigest()


def spec_sha256(spec: dict) -> str:
    return sha256_hex(canonical_json(spec))


def _obj(value, field, required: set, optional: dict, sparse=()):
    """Validate an object: reject unknown fields, require ``required``, fill ``optional`` defaults.

    ``sparse`` fields are allowed but never filled in, so specs stored before they existed keep their hash."""
    if not isinstance(value, dict):
        raise SchemaError('schema_invalid', field, 'object required')
    unknown = set(value) - required - set(optional) - set(sparse)
    if unknown:
        raise SchemaError('unknown_field', f'{field}/{sorted(unknown)[0]}', 'unknown field')
    missing = required - set(value)
    if missing:
        raise SchemaError('schema_invalid', f'{field}/{sorted(missing)[0]}', 'required field missing')
    result = {k: copy.deepcopy(v) for k, v in optional.items()}
    result.update(value)
    return result


def _str(value, field, lo=0, hi=4000):
    if not isinstance(value, str) or '\x00' in value or unicodedata.normalize('NFC', value) != value:
        raise SchemaError('schema_invalid', field, 'NFC string without NUL required')
    if not lo <= len(value) <= hi:
        raise SchemaError('schema_invalid', field, f'length must be {lo}..{hi}')
    return value


def _int(value, field, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise SchemaError('schema_invalid', field, f'integer {lo}..{hi} required')
    return value


def _bool(value, field):
    if not isinstance(value, bool):
        raise SchemaError('schema_invalid', field, 'boolean required')
    return value


def _enum(value, field, allowed):
    if value not in allowed:
        raise SchemaError('schema_invalid', field, 'value must be one of ' + '|'.join(map(str, allowed)))
    return value


def _key(value, field):
    if not isinstance(value, str) or not KEY_RE.match(value):
        raise SchemaError('schema_invalid', field, 'key must match ^[a-z][a-z0-9_-]{0,39}$')
    return value


def _list(value, field, hi):
    if not isinstance(value, list) or len(value) > hi:
        raise SchemaError('schema_invalid', field, f'list of at most {hi} items required')
    return value


def _keys(value, field, hi=64, *, unique=True):
    _list(value, field, hi)
    for i, item in enumerate(value):
        _key(item, f'{field}/{i}')
    if unique and len(set(value)) != len(value):
        raise SchemaError('schema_invalid', field, 'duplicate entries')
    return value


def _caps(value, field, provider):
    _list(value, field, 32)
    if len(set(value)) != len(value):
        raise SchemaError('schema_invalid', field, 'duplicate capability')
    try:
        capabilities.check(value, provider)
    except capabilities.CapabilityError as error:
        raise SchemaError('capability_not_allowed', field, f'{error.capability}: {error}') from None
    return value


# ---------------------------------------------------------------- spec

def _limits(value, field, provider):
    limits = _obj(value, field, {'wall_seconds'}, {'tokens': 0, 'cost_usd_cents': 0, 'max_workers': 1})
    _int(limits['wall_seconds'], f'{field}/wall_seconds', 1, 86400)
    _int(limits['tokens'], f'{field}/tokens', 0, 10_000_000)
    _int(limits['cost_usd_cents'], f'{field}/cost_usd_cents', 0, 100000)
    _int(limits['max_workers'], f'{field}/max_workers', 1, 4)
    if provider == 'none' and limits['cost_usd_cents'] != 0:
        raise SchemaError('schema_invalid', f'{field}/cost_usd_cents', 'must be 0 when provider=none')
    if provider == 'none' and limits['tokens'] != 0:
        raise SchemaError('schema_invalid', f'{field}/tokens', 'must be 0 when provider=none')
    return limits


def _input(value, field):
    item = _obj(value, field, {'name', 'type'}, {'required': False, 'default': None, 'enum': [],
                                                  'max_length': 4000, 'secret': False})
    _key(item['name'], f'{field}/name')
    _enum(item['type'], f'{field}/type', ('string', 'integer', 'boolean', 'enum'))
    _bool(item['required'], f'{field}/required')
    if _bool(item['secret'], f'{field}/secret'):
        raise SchemaError('secret_inputs_unsupported', f'{field}/secret', 'credentials belong to adapters')
    _int(item['max_length'], f'{field}/max_length', 1, 4000)
    _list(item['enum'], f'{field}/enum', 64)
    for i, choice in enumerate(item['enum']):
        _str(choice, f'{field}/enum/{i}', 1, 200)
    if item['type'] == 'enum' and not item['enum']:
        raise SchemaError('schema_invalid', f'{field}/enum', 'enum input needs choices')
    if item['default'] is not None:
        input_value(item, item['default'], f'{field}/default')
    return item


def input_value(item: dict, value, field):
    kind = item['type']
    if kind == 'string':
        _str(value, field, 0, item['max_length'])
    elif kind == 'integer':
        _int(value, field, -2**53, 2**53)
    elif kind == 'boolean':
        _bool(value, field)
    else:
        _enum(value, field, item['enum'])
    return value


def _stage(value, field, spec_caps, provider):
    stage = _obj(value, field, {'key', 'title', 'executor_step', 'timeout_seconds'},
                 {'needs': [], 'retry_max': 0, 'capabilities': [], 'outputs': []},
                 sparse=('role', 'optional', 'restartable', 'max_workers'))
    if 'role' in stage:
        _enum(stage['role'], f'{field}/role', ROLES)
    for flag in ('optional', 'restartable'):
        if flag in stage:
            _bool(stage[flag], f'{field}/{flag}')
    if 'max_workers' in stage:
        _int(stage['max_workers'], f'{field}/max_workers', 1, 4)
    _key(stage['key'], f'{field}/key')
    _str(stage['title'], f'{field}/title', 1, 120)
    _str(stage['executor_step'], f'{field}/executor_step', 1, 200)
    _int(stage['timeout_seconds'], f'{field}/timeout_seconds', 1, 86400)
    _int(stage['retry_max'], f'{field}/retry_max', 0, 3)
    _keys(stage['needs'], f'{field}/needs')
    _keys(stage['outputs'], f'{field}/outputs', 32)
    _caps(stage['capabilities'], f'{field}/capabilities', provider)
    extra = set(stage['capabilities']) - set(spec_caps)
    if extra:
        raise SchemaError('capability_not_allowed', f'{field}/capabilities', 'stage capability not in spec capabilities')
    return stage


def stage_role(stage: dict) -> str:
    return stage.get('role', 'worker')


def max_attempts(spec: dict, stage: dict) -> int:
    """Attempts allowed for a stage: its own ``1 + retry_max``, raised to the blueprint ``retry.max_attempts`` floor."""
    return max(1 + stage['retry_max'], spec['retry']['max_attempts'])


def validate_expansion(spec: dict, planner: dict, existing: list[dict], proposals, *, provider: str = 'none') -> list[dict]:
    """Validate a planner's follow-up stage proposals through the stage schema; returns the normalized stages.

    Rules: bounded list; capabilities within the blueprint; no planner role (no recursion, no agent spawning
    agents); new keys only, never edits to existing stages; every new stage descends from the planner; combined
    graph stays acyclic with at most MAX_STAGES stages and depth MAX_DEPTH. Same executor, so no new executors."""
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= MAX_PROPOSALS:
        raise SchemaError('expansion_rejected', '/stages', f'1..{MAX_PROPOSALS} proposals required')
    if stage_role(planner) != 'planner':
        raise SchemaError('expansion_rejected', '/stage', 'only a planner stage may propose stages')
    if len(existing) + len(proposals) > MAX_STAGES:
        raise SchemaError('expansion_rejected', '/stages', f'total stages would exceed {MAX_STAGES}')
    added = [_stage(item, f'/stages/{i}', spec['capabilities'], provider) for i, item in enumerate(proposals)]
    taken = {s['key'] for s in existing}
    for i, stage in enumerate(added):
        if stage['key'] in taken:
            raise SchemaError('expansion_rejected', f'/stages/{i}/key', 'key already exists')
        taken.add(stage['key'])
        if stage_role(stage) == 'planner':
            raise SchemaError('expansion_rejected', f'/stages/{i}/role', 'proposed stages cannot be planners')
        if spec['executor']['kind'] == 'adapter':
            adapter_step(stage, f'/stages/{i}')
    combined = list(existing) + added
    try:
        topo_order(combined)
    except SchemaError as error:
        raise SchemaError('expansion_rejected', error.field, error.detail) from None
    parents = {s['key']: s['needs'] for s in combined}

    def descends(key, seen=()):
        return planner['key'] in parents[key] or any(descends(n, seen + (key,)) for n in parents[key] if n not in seen)
    for i, stage in enumerate(added):
        if not descends(stage['key']):
            raise SchemaError('expansion_rejected', f'/stages/{i}/needs', f'must descend from planner {planner["key"]}')
    return added


def adapter_step(stage: dict, field: str = '/stage') -> tuple[str, str]:
    """``submit:<task>`` (needs stage capability adapter.submit) or ``observe:<external ref>``."""
    match = ADAPTER_STEP_RE.match(stage['executor_step'])
    if not match:
        raise SchemaError('schema_invalid', f'{field}/executor_step', 'adapter step must be submit:<task> or observe:<ref>')
    op, arg = match.group('op'), match.group('arg')
    if op == 'observe' and not ADAPTER_REF_RE.match(arg):
        raise SchemaError('schema_invalid', f'{field}/executor_step', 'observe ref must match [A-Za-z0-9._:-]{1,128}')
    needed = 'adapter.' + op
    if needed not in stage['capabilities']:
        raise SchemaError('capability_not_allowed', f'{field}/capabilities', f'{op} step requires {needed}')
    return op, arg


def topo_order(stages: list[dict]) -> list[str]:
    """Kahn's algorithm, stable by declaration order. Raises on unknown deps, cycles, or depth > 16."""
    keys = [s['key'] for s in stages]
    if len(set(keys)) != len(keys):
        raise SchemaError('schema_invalid', '/stages', 'duplicate stage key')
    index = {k: i for i, k in enumerate(keys)}
    for i, stage in enumerate(stages):
        for dep in stage['needs']:
            if dep not in index:
                raise SchemaError('schema_invalid', f'/stages/{i}/needs', f'unknown dependency {dep}')
            if dep == stage['key']:
                raise SchemaError('cycle_detected', f'/stages/{i}/needs', 'stage depends on itself')
    remaining = {s['key']: set(s['needs']) for s in stages}
    depth = {}
    order = []
    while remaining:
        ready = [k for k in keys if k in remaining and not remaining[k]]
        if not ready:
            raise SchemaError('cycle_detected', '/stages', 'stage graph has a cycle')
        for k in ready:
            needs = stages[index[k]]['needs']
            depth[k] = 1 + max((depth[d] for d in needs), default=0)
            if depth[k] > MAX_DEPTH:
                raise SchemaError('schema_invalid', '/stages', f'depth exceeds {MAX_DEPTH}')
            order.append(k)
            del remaining[k]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


def validate_spec(spec, *, provider: str = 'none') -> dict:
    top = {'mission', 'executor', 'limits', 'stages'}
    optional = {'workspace': {'kind': 'scratch', 'mount': None, 'access': 'ro'}, 'inputs': [],
                'capabilities': [], 'network_allowlist': [], 'retry': {'max_attempts': 1, 'backoff_seconds': 0},
                'cancellation': {'grace_seconds': 5}, 'approvals': [],
                'evidence': {'outputs': [], 'require_tests': False}}
    out = _obj(spec, '', top, optional, sparse=('on_failure',))
    if 'on_failure' in out:
        _enum(out['on_failure'], '/on_failure', ON_FAILURE)
    _str(out['mission'], '/mission', 1, 4000)

    ws = out['workspace'] = _obj(out['workspace'], '/workspace', {'kind'}, {'mount': None, 'access': 'ro'})
    _enum(ws['kind'], '/workspace/kind', ('scratch', 'mounted'))
    _enum(ws['access'], '/workspace/access', ('ro', 'rw'))
    if ws['kind'] == 'scratch' and ws['mount'] is not None:
        raise SchemaError('schema_invalid', '/workspace/mount', 'scratch workspace has no mount')
    if ws['kind'] == 'mounted':
        _key(ws['mount'], '/workspace/mount')

    ex = out['executor'] = _obj(out['executor'], '/executor', {'kind', 'ref'}, {'demo': None})
    _enum(ex['kind'], '/executor/kind', ('mock', 'adapter'))
    _str(ex['ref'], '/executor/ref', 1, 200)
    if ex['demo'] is None:
        ex['demo'] = ex['kind'] == 'mock'
    if _bool(ex['demo'], '/executor/demo') != (ex['kind'] == 'mock'):
        raise SchemaError('schema_invalid', '/executor/demo', 'demo is true iff executor kind is mock')

    caps = _caps(out['capabilities'], '/capabilities', provider)
    if ex['kind'] == 'mock' and 'demo.run' not in caps:
        raise SchemaError('schema_invalid', '/capabilities', 'mock executor requires demo.run')
    if ex['kind'] == 'adapter':
        _key(ex['ref'], '/executor/ref')
        if 'adapter.observe' not in caps:
            raise SchemaError('capability_not_allowed', '/capabilities', 'adapter executor requires adapter.observe')
    if ws['access'] == 'rw' and 'workspace.write' not in caps:
        raise SchemaError('capability_not_allowed', '/workspace/access', 'rw workspace requires workspace.write')

    _list(out['network_allowlist'], '/network_allowlist', 32)
    if out['network_allowlist'] and 'net.egress.allowlist' not in caps:
        raise SchemaError('capability_not_allowed', '/network_allowlist', 'requires net.egress.allowlist')
    for i, host in enumerate(out['network_allowlist']):
        if not isinstance(host, str) or not re.match(r'^[A-Za-z0-9.-]{1,253}:[0-9]{1,5}$', host):
            raise SchemaError('schema_invalid', f'/network_allowlist/{i}', 'host:port required')

    out['limits'] = _limits(out['limits'], '/limits', provider)

    _list(out['inputs'], '/inputs', 32)
    out['inputs'] = [_input(item, f'/inputs/{i}') for i, item in enumerate(out['inputs'])]
    names = [i['name'] for i in out['inputs']]
    if len(set(names)) != len(names):
        raise SchemaError('schema_invalid', '/inputs', 'duplicate input name')

    stages = out['stages']
    if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
        raise SchemaError('schema_invalid', '/stages', f'1..{MAX_STAGES} stages required')
    out['stages'] = [_stage(s, f'/stages/{i}', caps, provider) for i, s in enumerate(stages)]
    topo_order(out['stages'])
    if ex['kind'] == 'adapter':
        for i, stage in enumerate(out['stages']):
            adapter_step(stage, f'/stages/{i}')

    retry = out['retry'] = _obj(out['retry'], '/retry', set(), {'max_attempts': 1, 'backoff_seconds': 0})
    _int(retry['max_attempts'], '/retry/max_attempts', 1, 3)
    _int(retry['backoff_seconds'], '/retry/backoff_seconds', 0, 600)
    canc = out['cancellation'] = _obj(out['cancellation'], '/cancellation', set(), {'grace_seconds': 5})
    _int(canc['grace_seconds'], '/cancellation/grace_seconds', 0, 60)

    stage_keys = {s['key'] for s in out['stages']}
    _list(out['approvals'], '/approvals', 64)
    approvals = []
    for i, item in enumerate(out['approvals']):
        a = _obj(item, f'/approvals/{i}', {'stage', 'capability'}, {'reason': ''})
        if a['stage'] not in stage_keys:
            raise SchemaError('schema_invalid', f'/approvals/{i}/stage', 'unknown stage')
        if a['capability'] not in caps:
            raise SchemaError('capability_not_allowed', f'/approvals/{i}/capability', 'not in spec capabilities')
        _str(a['reason'], f'/approvals/{i}/reason', 0, 500)
        approvals.append(a)
    out['approvals'] = approvals

    ev = out['evidence'] = _obj(out['evidence'], '/evidence', set(), {'outputs': [], 'require_tests': False})
    _keys(ev['outputs'], '/evidence/outputs', 32)
    _bool(ev['require_tests'], '/evidence/require_tests')
    declared = {o for s in out['stages'] for o in s['outputs']}
    for i, name in enumerate(ev['outputs']):
        if name not in declared:
            raise SchemaError('schema_invalid', f'/evidence/outputs/{i}', 'not produced by any stage')
    return out


def apply_overrides(spec: dict, overrides, *, provider: str = 'none') -> dict:
    """Freeze a run snapshot: declared input values become defaults; limits may only be lowered."""
    if not isinstance(overrides, dict):
        raise SchemaError('invalid_request', '/overrides', 'object required')
    snapshot = copy.deepcopy(spec)
    by_name = {i['name']: i for i in snapshot['inputs']}
    for name, value in overrides.items():
        if name == 'limits':
            if not isinstance(value, dict):
                raise SchemaError('override_not_allowed', '/overrides/limits', 'object required')
            for limit, lowered in value.items():
                current = snapshot['limits'].get(limit)
                if current is None or isinstance(lowered, bool) or not isinstance(lowered, int) or lowered > current:
                    raise SchemaError('override_not_allowed', f'/overrides/limits/{limit}', 'limits may only be lowered')
                snapshot['limits'][limit] = lowered
            continue
        if name not in by_name:
            raise SchemaError('override_not_allowed', f'/overrides/{name}'[:120], 'only declared inputs')
        input_value(by_name[name], value, f'/overrides/{name}')
        by_name[name]['default'] = value
    for item in snapshot['inputs']:
        if item['required'] and item['default'] is None:
            raise SchemaError('schema_invalid', f'/overrides/{item["name"]}', 'required input missing')
    return validate_spec(snapshot, provider=provider)


def demo_spec(step: str = 'mock:success', *, wall_seconds: int = 120, timeout_seconds: int = 60,
              outputs=(), stages=None) -> dict:
    """A minimal DEMO spec (tests, smoke, first-run examples)."""
    caps = ['demo.run'] + (['artifact.write'] if outputs else [])
    return {'mission': 'DEMO: simulated run, no real action is taken',
            'executor': {'kind': 'mock', 'ref': 'mock:demo', 'demo': True},
            'capabilities': caps, 'limits': {'wall_seconds': wall_seconds},
            'stages': stages or [{'key': 'demo', 'title': 'DEMO step', 'executor_step': step,
                                  'timeout_seconds': timeout_seconds, 'outputs': list(outputs),
                                  'capabilities': caps}],
            'evidence': {'outputs': list(outputs), 'require_tests': False}}


# ---------------------------------------------------------------- v1 -> v2 migration
# v2 = v1 + optional ``tags`` (list of Keys, default []). Nothing else changes, so the upgrade is lossless.

def upgrade_spec_v1_to_v2(spec: dict) -> dict:
    """Pure function: returns an upgraded deep copy; the input is never mutated."""
    if not isinstance(spec, dict) or 'tags' in spec:
        raise SchemaError('schema_invalid', '/tags', 'v1 spec required (no tags)')
    upgraded = copy.deepcopy(spec)
    upgraded['tags'] = []
    return upgraded


def validate_spec_v2(spec, *, provider: str = 'none') -> dict:
    if not isinstance(spec, dict):
        raise SchemaError('schema_invalid', '', 'object required')
    rest = {k: v for k, v in spec.items() if k != 'tags'}
    out = validate_spec(rest, provider=provider)
    out['tags'] = _keys(spec.get('tags', []), '/tags', 16)
    return out
