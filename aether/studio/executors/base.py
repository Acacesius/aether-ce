"""Executor protocol (INTERFACE_SPEC §8). ``emit`` is the only output path; executors never touch the DB."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class ExecutorUnavailable(RuntimeError):
    """Executor cannot start (health != ready). Maps to blocked(adapter_unavailable)."""


@dataclass(frozen=True)
class RunView:
    id: str
    demo: bool
    snapshot: dict


@dataclass(frozen=True)
class StageView:
    key: str
    attempt: int
    executor_step: str
    timeout_seconds: int
    outputs: tuple = ()
    idempotency_key: str = ''
    role: str = 'worker'
    upstream: tuple = ()  # reviewer: per direct dependency {stage, status, verdict, artifacts[]}
    approved: tuple = ()  # capabilities the owner granted for this stage


@dataclass
class Handle:
    run_id: str
    stage_key: str
    attempt: int
    state: Any = None
    extra: dict = field(default_factory=dict)


Emit = Callable[[str, dict], None]


class Executor(Protocol):
    kind: str
    demo: bool

    def capabilities(self) -> frozenset: ...
    def health(self) -> dict: ...
    def start(self, run: RunView, stage: StageView, emit: Emit) -> Handle: ...
    def poll(self, handle: Handle) -> dict: ...  # {"state": "running"|"exited"|"blocked", "exit_code": int|None}
    def cancel(self, handle: Handle, grace_seconds: int) -> None: ...
