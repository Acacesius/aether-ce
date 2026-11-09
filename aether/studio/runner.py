"""CE built-in runner (AHS-01): one dispatcher thread, bounded workers, leases, heartbeat, cancel, recovery.

Imported only in custom mode. Executes ``runs.snapshot_json`` only. Each claimed run is driven by an
``orchestrator.RunDriver`` (stage DAG, budgets, retries, resume). A run whose lease expires is recovered from its
checkpoints: completed stages are never re-executed; see ``Store.recover_expired_leases``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import uuid

from aether.studio import fsperm, scanner, schema
from aether.studio.orchestrator import RunDriver, Slots
from aether.studio.store import CasConflict

MAX_WORKERS = 4
ARTIFACT_MAX = 16 * 1024 * 1024
RUN_ARTIFACT_MAX = 256 * 1024 * 1024


class Runner:
    def __init__(self, store, executors: dict, *, max_workers=1, artifacts_dir=None, heartbeat_seconds=5.0,
                 lease_seconds=30.0, poll_seconds=0.05, tick_seconds=0.2, owner=None, provider='none', max_stages=None):
        if not isinstance(max_workers, int) or not 1 <= max_workers <= MAX_WORKERS:
            raise ValueError('max_workers must be 1..4')
        if lease_seconds <= heartbeat_seconds:
            raise ValueError('lease must outlive the heartbeat interval')
        self.store = store
        self.slots = Slots(max_stages or max_workers * MAX_WORKERS)  # global cap on concurrently running stages
        self.provider = provider
        self.executors = executors
        self.max_workers = max_workers
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.tick_seconds = tick_seconds
        self.owner = owner or f'runner-{os.getpid()}-{uuid.uuid4().hex[:12]}'
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._dispatcher = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self.recover()
        self._dispatcher = threading.Thread(target=self._loop, name='studio-dispatcher', daemon=True)
        self._dispatcher.start()

    def stop(self, timeout=5.0):
        """Stop dispatching. In-flight runs are left running with their lease; recovery fails them later."""
        self._stop.set()
        self._wake.set()
        if self._dispatcher:
            self._dispatcher.join(timeout)
        for thread in list(self._workers.values()):
            thread.join(timeout)

    def notify(self):
        self._wake.set()

    def recover(self):
        return self.store.recover_expired_leases()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # keep dispatching; the failing run is handled by its worker/recovery
                pass
            self._wake.wait(self.tick_seconds)
            self._wake.clear()

    def active(self):
        with self._lock:
            for run_id, thread in list(self._workers.items()):
                if not thread.is_alive():
                    del self._workers[run_id]
            return len(self._workers)

    def tick(self):
        """One serialized scheduling pass: recover expired leases, then claim FIFO up to the bound."""
        self.store.recover_expired_leases()
        started = []
        while not self._stop.is_set() and self.active() < self.max_workers:
            try:
                row = self.store.claim_next(self.owner, lease_seconds=self.lease_seconds,
                                            max_running=self.max_workers, preflight=self._preflight)
            except CasConflict:
                continue
            if row is None:
                break
            thread = threading.Thread(target=self._execute_safe, args=(row,), name=f'studio-{row["id"]}', daemon=True)
            with self._lock:
                self._workers[row['id']] = thread
            thread.start()
            started.append(row['id'])
        return started

    def _preflight(self, row):
        try:
            snapshot = json.loads(row['snapshot_json'])
            schema.validate_spec(snapshot, provider=self.provider)
        except (ValueError, KeyError):
            return 'invalid_snapshot'
        executor = self.executors.get(snapshot['executor']['kind'])
        if executor is None:
            return 'adapter_unavailable' if snapshot['executor']['kind'] == 'adapter' else 'capability_unavailable'
        try:
            if snapshot['executor']['kind'] == 'adapter':
                return executor.preflight(snapshot)
            healthy = executor.health().get('status') == 'ready'
        except Exception:
            healthy = False
        if not healthy:
            return 'adapter_unavailable'
        return None

    # ------------------------------------------------------------ execution
    def _execute_safe(self, row):
        try:
            self._execute(row)
        except Exception as error:
            self.store.append_event(row['id'], 'executor.error', {'code': 'runner_exception',
                                                                  'message': type(error).__name__[:300]}, owner=self.owner)
            self.store.finish(row['id'], self.owner, 'failed', 'executor_error')
        finally:
            self._wake.set()

    def _execute(self, row):
        executor = self.executors[json.loads(row['snapshot_json'])['executor']['kind']]
        driver = RunDriver(self.store, row, executor, owner=self.owner, write_artifact=self._write_artifact,
                           slots=self.slots, lease_seconds=self.lease_seconds, heartbeat_seconds=self.heartbeat_seconds,
                           provider=self.provider)
        while not driver.step():
            if self._stop.is_set():  # shutdown: leave the lease to expire; recovery interrupts running stages
                return driver.shutdown()
            time.sleep(self.poll_seconds)

    def _write_artifact(self, run, stage, data, produced, artifact_bytes, system=False):
        name = data.get('name')
        if not isinstance(name, str) or not schema.KEY_RE.match(name) or name not in stage.outputs:
            self.store.append_event(run.id, 'executor.error', {'code': 'artifact_undeclared', 'message': str(name)[:64]},
                                    stage_key=stage.key, owner=self.owner)
            return None
        if not system and 'artifact.write' not in run.snapshot['capabilities']:
            self.store.append_event(run.id, 'executor.error', {'code': 'capability_not_allowed', 'message': 'artifact.write'},
                                    stage_key=stage.key, owner=self.owner)
            return None
        content = str(data.get('content', '')).encode('utf-8')
        media_type = data.get('media_type', 'text/plain')
        if media_type not in ('text/plain', 'application/json', 'text/markdown', 'image/png'):
            media_type = 'text/plain'
        if len(content) > ARTIFACT_MAX or artifact_bytes[0] + len(content) > RUN_ARTIFACT_MAX:
            self.store.append_event(run.id, 'limit.exceeded', {'limit': 'artifact_bytes', 'value': len(content)},
                                    stage_key=stage.key, owner=self.owner)
            return None
        flagged = []
        if media_type == 'image/png':
            if scanner.scan(content.decode('latin-1')):
                self.store.append_event(run.id, 'executor.error', {'code': 'artifact_secret_detected', 'message': name},
                                        stage_key=stage.key, owner=self.owner)
                return None
        else:
            clean, flagged = scanner.redact(content.decode('utf-8', 'replace'))
            if flagged:
                content = clean.encode('utf-8')
        digest = schema.sha256_hex(content)
        path = None
        if self.artifacts_dir is not None:
            directory = self.artifacts_dir / run.id
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                fsperm.restrict(directory, directory=True)
            path = directory / digest
            if not path.exists():
                tmp = directory / f'.{digest}.tmp'
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
        artifact_bytes[0] += len(content)
        art_id = self.store.add_artifact(run.id, self.owner, stage.key, name, media_type, path or f'memory:{digest}',
                                         len(content), digest, flagged)
        if art_id:
            produced.add(name)
        return art_id

    # ------------------------------------------------------------ owner actions
    def cancel(self, run_id):
        result = self.store.request_cancel(run_id)
        self._wake.set()
        return result

    def resume(self, run_id):
        result = self.store.resume_run(run_id)
        self._wake.set()
        return result
