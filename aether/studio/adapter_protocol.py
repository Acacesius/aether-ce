"""Adapter protocol ``aether.studio.adapter/v1`` (ADAPTER_PROTOCOL.md): message codec, negotiation, event hash chain.

Pure functions, no I/O. Every violation raises ``ProtocolError(code)``; callers record it as a run error and end the
session. Nothing is silently dropped or repaired.
"""
from __future__ import annotations

import json
import re

from aether.studio import capabilities, schema
from aether.studio.schema import canonical_json, sha256_hex

PROTOCOL = 'aether.studio.adapter/v1'
MAX_LINE = 64 * 1024                 # bytes per JSON line, newline included
MAX_EVENTS = 10_000                  # adapter events per session
LOG_TEXT_MAX = 4000
REF_RE = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
SESSION_RE = re.compile(r'^[0-9a-f]{32}$')
HASH_RE = re.compile(r'^[0-9a-f]{64}$')
KNOWN_CAPABILITIES = ('cancel', 'observe', 'submit')   # adapter scope -> Studio capability 'adapter.<scope>'
EVENT_KINDS = ('log', 'progress', 'status', 'artifact', 'result')
STATUS_STATES = ('accepted', 'running', 'waiting', 'finishing')
TEXT_MEDIA = ('text/plain', 'application/json', 'text/markdown')

# Adapter -> Studio: exact field sets (``content`` is optional and only for artifact events).
INBOUND = {
    'hello': {'type', 'protocol', 'name', 'capabilities'},
    'accepted': {'type', 'ref'},
    'pong': {'type', 'n'},
    'event': {'type', 'seq', 'prev', 'kind', 'data'},
}


class ProtocolError(ValueError):
    def __init__(self, code: str, detail: str = ''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code = code
        self.detail = detail[:200]


def studio_capability(scope: str) -> str:
    return 'adapter.' + scope


def allowed_scopes(provider: str = 'none') -> frozenset[str]:
    """Adapter scopes whose Studio capability is on the allowlist in this mode."""
    available = capabilities.available(provider)
    return frozenset(s for s in KNOWN_CAPABILITIES if studio_capability(s) in available)


# ---------------------------------------------------------------- codec

def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError('duplicate_field', key[:40])
        result[key] = value
    return result


def encode(message: dict) -> bytes:
    line = (canonical_json(message) + '\n').encode('utf-8')
    assert len(line) <= MAX_LINE, 'outbound message exceeds MAX_LINE'
    return line


def decode(line: bytes) -> dict:
    """Parse one adapter line. Enforces size, UTF-8, JSON object, known type and exact field set."""
    if len(line) > MAX_LINE:
        raise ProtocolError('oversized', f'{len(line)} bytes > {MAX_LINE}')
    if not line.endswith(b'\n'):
        raise ProtocolError('truncated_line')
    try:
        message = json.loads(line.decode('utf-8'), object_pairs_hook=_unique)
    except ProtocolError:
        raise
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError('invalid_json') from None
    if not isinstance(message, dict) or message.get('type') not in INBOUND:
        raise ProtocolError('unknown_message', str(message.get('type') if isinstance(message, dict) else '')[:40])
    fields = set(message)
    expected = INBOUND[message['type']]
    extra = fields - expected - ({'content'} if message['type'] == 'event' else set())
    if extra or expected - fields:
        raise ProtocolError('bad_fields', ','.join(sorted(extra or expected - fields))[:80])
    return message


# ---------------------------------------------------------------- negotiation

def negotiate(hello: dict, granted, provider: str = 'none') -> list[str]:
    """Effective scopes = declared ∩ enrollment grants ∩ allowlist. Version mismatch and unknown scopes refuse."""
    if hello['protocol'] != PROTOCOL:
        raise ProtocolError('version_mismatch', f'adapter speaks {str(hello["protocol"])[:60]}, Studio speaks {PROTOCOL}')
    name, declared = hello['name'], hello['capabilities']
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        raise ProtocolError('bad_hello', 'name')
    if not isinstance(declared, list) or len(declared) > 8 or not all(isinstance(c, str) for c in declared) \
            or len(set(declared)) != len(declared):
        raise ProtocolError('bad_hello', 'capabilities')
    unknown = sorted(set(declared) - set(KNOWN_CAPABILITIES))
    if unknown:
        raise ProtocolError('unknown_capability', ','.join(c[:20] for c in unknown[:4]))
    return sorted(set(declared) & set(granted) & allowed_scopes(provider))


# ---------------------------------------------------------------- event chain

def chain_seed(session: str) -> str:
    assert SESSION_RE.match(session), 'session nonce must be 32 hex chars'
    return sha256_hex(f'{PROTOCOL}|{session}')


def link_hash(seq: int, prev: str, kind: str, data: dict) -> str:
    """h_n = sha256(canonical({seq, prev, kind, data})); ``content`` is bound through data.sha256."""
    return sha256_hex(canonical_json({'seq': seq, 'prev': prev, 'kind': kind, 'data': data}))


def _int(value, lo, hi, field):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ProtocolError('bad_event', field)
    return value


def _text(value, lo, hi, field):
    if not isinstance(value, str) or '\x00' in value or not lo <= len(value) <= hi:
        raise ProtocolError('bad_event', field)
    return value


def _fields(data, required, field):
    if not isinstance(data, dict) or set(data) != set(required):
        raise ProtocolError('bad_event', field)


def check_event(message: dict, *, expected_seq: int, head: str, outputs=()) -> str:
    """Validate one event against sequence, chain and per-kind schema. Returns the new chain head."""
    seq, prev, kind, data = message['seq'], message['prev'], message['kind'], message['data']
    if isinstance(seq, bool) or not isinstance(seq, int) or seq != expected_seq:
        raise ProtocolError('bad_sequence', f'expected {expected_seq}, got {str(seq)[:20]}')
    if seq > MAX_EVENTS:
        raise ProtocolError('event_limit', f'more than {MAX_EVENTS} events')
    if not isinstance(prev, str) or prev != head:
        raise ProtocolError('bad_chain', f'event {seq} prev does not match chain head')
    if kind not in EVENT_KINDS:
        raise ProtocolError('bad_event', f'kind {str(kind)[:20]}')
    if ('content' in message) != (kind == 'artifact'):
        raise ProtocolError('bad_event', 'content only on artifact events')
    if kind == 'log':
        _fields(data, ('stream', 'text'), 'log')
        if data['stream'] not in ('stdout', 'stderr'):
            raise ProtocolError('bad_event', 'log/stream')
        _text(data['text'], 0, LOG_TEXT_MAX, 'log/text')
    elif kind == 'progress':
        _fields(data, ('done', 'total'), 'progress')
        total = _int(data['total'], 1, 10**9, 'progress/total')
        _int(data['done'], 0, total, 'progress/done')
    elif kind == 'status':
        _fields(data, ('state',), 'status')
        if data['state'] not in STATUS_STATES:
            raise ProtocolError('bad_event', 'status/state')
    elif kind == 'artifact':
        _fields(data, ('name', 'media_type', 'sha256', 'size'), 'artifact')
        if not isinstance(data['name'], str) or not schema.KEY_RE.match(data['name']) or data['name'] not in outputs:
            raise ProtocolError('artifact_undeclared', str(data['name'])[:40])
        if data['media_type'] not in TEXT_MEDIA:
            raise ProtocolError('bad_event', 'artifact/media_type')
        content = message['content']
        if not isinstance(content, str):
            raise ProtocolError('bad_event', 'artifact/content')
        raw = content.encode('utf-8')
        if data['size'] != len(raw) or data['sha256'] != sha256_hex(raw):
            raise ProtocolError('artifact_mismatch', data['name'])
    else:
        _fields(data, ('exit_code',), 'result')
        _int(data['exit_code'], 0, 255, 'result/exit_code')
    return link_hash(seq, prev, kind, data)


def verify_stored(events: list[dict]) -> dict:
    """Offline verification of stored run events (API objects, seq order).

    Each ``adapter.session`` starts a chain at ``chain_seed(session)``; its ``adapter.event`` rows must have
    i = 1, 2, ... and prev = previous sha256, and sha256 must recompute from the stored payload unless the payload
    was redacted at ingestion (``redacted: true``: continuity still checked). Returns
    ``{sessions, events, recomputed, redacted}``; raises ``ProtocolError`` on the first break.
    """
    head, expected, session = None, 1, None
    counts = {'sessions': 0, 'events': 0, 'recomputed': 0, 'redacted': 0}
    for event in events:
        if event['type'] == 'adapter.session':
            session = event['data']['session']
            head, expected = chain_seed(session), 1
            if event['data']['chain_seed'] != head:
                raise ProtocolError('bad_chain', f'seed of run event {event["seq"]}')
            counts['sessions'] += 1
        elif event['type'] == 'adapter.event':
            data = event['data']
            if session is None or data['session'] != session or data['i'] != expected or data['prev'] != head:
                raise ProtocolError('bad_chain', f'run event {event["seq"]}')
            if data['redacted']:
                counts['redacted'] += 1
            else:
                if link_hash(data['i'], data['prev'], data['kind'], data['data']) != data['sha256']:
                    raise ProtocolError('bad_chain', f'hash of run event {event["seq"]}')
                counts['recomputed'] += 1
            head, expected = data['sha256'], expected + 1
            counts['events'] += 1
    return counts
