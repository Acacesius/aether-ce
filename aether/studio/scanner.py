"""Secret-shape scanner (CE_EXTRACTION_AUDIT §7 secret rules; INTERFACE_SPEC §5, §10).

Used for artifact content on write and for bundle import. Findings carry rule names and offsets only, never the
matched text. ``redact`` replaces each match with ``[REDACTED:<rule>]``.
"""
from __future__ import annotations

import re

RULES = (
    ('pem_private_key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('openssh_key', re.compile(r'-----BEGIN OPENSSH PRIVATE KE[Y]-----|ssh-(?:ed25519|rsa) AAAA')),
    ('vendor_key_shape', re.compile(r'\b(?:sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|'
                                  r'github_pat_[A-Za-z0-9_]{20,}|xox[bap]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|'
                                  r'tskey-[A-Za-z0-9-]{10,})')),
    ('bearer_literal', re.compile(r'Bearer [A-Za-z0-9._~+/-]{20,}')),
    ('token_shape_43', re.compile(r'(?<![A-Za-z0-9_-])(?!def\b)[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])')),
    ('hex_secret', re.compile(r'(?i)(?:secret|token|key|hmac)\s*[:=]\s*[\'"]?[0-9a-f]{32,}')),
)

# Field names that never belong in a bundle (value-checked on import: a falsy value such as ``secret: false`` is fine).
SECRET_FIELD_RE = re.compile(r'(?i)token|secret|password|passwd|credential|api[_-]?key|private[_-]?key|authorization')


def scan(text: str) -> list[dict]:
    """Return ``[{'rule', 'start', 'end'}]`` for every secret-shaped match (never the text itself)."""
    findings = []
    for name, pattern in RULES:
        for match in pattern.finditer(text):
            findings.append({'rule': name, 'start': match.start(), 'end': match.end()})
    return sorted(findings, key=lambda f: (f['start'], f['rule']))


def redact(text: str) -> tuple[str, list[str]]:
    """Redact matches; returns (clean text, sorted unique rule names)."""
    findings = scan(text)
    if not findings:
        return text, []
    out, pos = [], 0
    for f in findings:
        if f['start'] < pos:
            continue
        out.append(text[pos:f['start']])
        out.append(f'[REDACTED:{f["rule"]}]')
        pos = f['end']
    out.append(text[pos:])
    return ''.join(out), sorted({f['rule'] for f in findings})


def find_in_value(value, path='') -> dict | None:
    """Walk JSON-like data; return ``{'field', 'rule'}`` for the first secret-shaped string or secret-named field."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and SECRET_FIELD_RE.search(key) and item not in (False, None, '', [], {}, 0):
                return {'field': f'{path}/{key}'[:120], 'rule': 'secret_field'}
            hit = find_in_value(item, f'{path}/{key}')
            if hit:
                return hit
    elif isinstance(value, list):
        for i, item in enumerate(value):
            hit = find_in_value(item, f'{path}/{i}')
            if hit:
                return hit
    elif isinstance(value, str):
        found = scan(value)
        if found:
            return {'field': path[:120], 'rule': found[0]['rule']}
    return None


def strip_secret_fields(value):
    """Deep copy without secret-named fields."""
    if isinstance(value, dict):
        return {k: strip_secret_fields(v) for k, v in value.items()
                if not (isinstance(k, str) and SECRET_FIELD_RE.search(k))}
    if isinstance(value, list):
        return [strip_secret_fields(v) for v in value]
    return value
