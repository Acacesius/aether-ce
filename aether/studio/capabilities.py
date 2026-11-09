"""Capability allowlist (INTERFACE_SPEC §7). Nothing here grants host command execution."""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch


@dataclass(frozen=True)
class Capability:
    id: str
    description: str
    risk: str               # low | medium | high
    ce_available: bool
    requires_approval: bool
    needs_provider: str | None = None


REGISTRY = {c.id: c for c in (
    Capability('demo.run', 'mock executor steps (DEMO, no real action)', 'low', True, False),
    Capability('workspace.read', 'read the mounted workspace', 'low', True, False),
    Capability('workspace.write', 'write the mounted workspace', 'medium', True, True),
    Capability('artifact.write', 'emit artifacts', 'low', True, False),
    Capability('net.egress.allowlist', 'outbound to network_allowlist only', 'high', False, True),
    Capability('provider.local', 'call a user-configured local model endpoint', 'medium', True, True, 'local'),
    Capability('adapter.submit', 'submit work to an enrolled adapter (needs an explicit enrollment grant)', 'medium', True, True),
    Capability('adapter.observe', 'observe an enrolled adapter and ingest its verified events', 'low', True, False),
    Capability('adapter.cancel', 'ask an enrolled adapter to cancel (needs an explicit enrollment grant)', 'medium', True, True),
)}

FORBIDDEN = ('shell.*', 'shell', 'ssh.*', 'ssh', 'wake.*', 'docker.socket', 'systemd.*', 'tmux.*', 'provider.cloud')


class CapabilityError(ValueError):
    def __init__(self, capability: str, detail: str):
        super().__init__(detail)
        self.capability = capability
        self.code = 'capability_not_allowed'


def forbidden(cap_id: str) -> bool:
    return any(fnmatch.fnmatchcase(cap_id, pattern) for pattern in FORBIDDEN)


def available(provider: str = 'none') -> frozenset[str]:
    return frozenset(c.id for c in REGISTRY.values()
                     if c.ce_available and (c.needs_provider is None or c.needs_provider == provider))


def check(cap_ids, provider: str = 'none') -> None:
    allowed = available(provider)
    for cap_id in cap_ids:
        if not isinstance(cap_id, str) or forbidden(cap_id) or cap_id not in REGISTRY:
            raise CapabilityError(str(cap_id)[:64], 'unknown or forbidden capability')
        if cap_id not in allowed:
            raise CapabilityError(cap_id, 'capability unavailable in this mode')


def describe(provider: str = 'none') -> list[dict]:
    allowed = available(provider)
    return [{'id': c.id, 'description': c.description, 'risk': c.risk, 'available': c.id in allowed,
             'requires_approval': c.requires_approval} for c in sorted(REGISTRY.values(), key=lambda c: c.id)]
