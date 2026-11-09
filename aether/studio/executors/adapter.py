"""AdapterExecutor (AHS-04): runs enrolled third-party adapters over ``aether.studio.adapter/v1`` (ADAPTER_PROTOCOL.md).

Transport: JSON lines on the stdin/stdout pipes of a subprocess that Studio starts itself, from the enrollment's
fixed argv (no shell), with a minimal environment, cwd = a fresh scratch dir, its own process group, and confined by
``adapter_sandbox.py`` (Landlock + seccomp: no fork/exec, no sockets, filesystem = stdlib + scratch). Nothing an
adapter sends can make Studio run a command or open a connection: the inbound message set is closed and data-only.

One process per stage attempt (plus short-lived health probes). Every protocol violation is recorded as a run
``executor.error`` and ends the session; nothing is dropped silently.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time

from aether.studio import adapter_protocol as proto
from aether.studio import adapter_sandbox
from aether.studio import proc
from aether.studio import scanner, schema
from aether.studio.executors.base import ExecutorUnavailable, Handle
from aether.studio.schema import SchemaError

SANDBOX = Path(__file__).resolve().parents[1] / 'adapter_sandbox.py'
SANDBOX_EXIT = 70
EXIT_GRACE_SECONDS = 2.0
MAX_ARGS = 16
STDERR_TAIL = 1024


def _utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def script_sha256(path) -> str:
    return schema.sha256_hex(Path(path).read_bytes())


def validate_enrollment(body, data_dir) -> dict:
    """Validate an enrollment request ``{name, script, args}``. Returns ``{name, script (resolved), args, sha256}``.

    The script must be a regular ``.py`` file, not group/world-writable, in a directory that neither contains nor lies
    inside the Studio data dir (the sandbox grants read access to the script's directory). Arguments must not look
    like secrets: enrollments hold no credentials, so nothing secret can leak into storage or exports.
    """
    if not isinstance(body, dict):
        raise SchemaError('invalid_request', '', 'object required')
    unknown = set(body) - {'name', 'script', 'args'}
    if unknown:
        raise SchemaError('unknown_field', '/' + sorted(unknown)[0])
    if 'name' not in body or 'script' not in body:
        raise SchemaError('schema_invalid', '/name' if 'name' not in body else '/script', 'required field missing')
    name = schema._key(body['name'], '/name')
    raw = schema._str(body['script'], '/script', 1, 4000)
    if not os.path.isabs(raw):
        raise SchemaError('schema_invalid', '/script', 'absolute path required')
    try:
        script = Path(raw).resolve(strict=True)
        info = script.stat()
        parent = script.parent.stat()
    except OSError:
        raise SchemaError('schema_invalid', '/script', 'script not found') from None
    if not stat.S_ISREG(info.st_mode) or script.suffix != '.py':
        raise SchemaError('schema_invalid', '/script', 'regular .py file required')
    posix = os.name == 'posix' and hasattr(os, 'getuid')
    if posix and (info.st_mode & 0o022 or parent.st_mode & 0o022):
        raise SchemaError('schema_invalid', '/script', 'script and its directory must not be group/world-writable')
    if posix and info.st_uid not in (0, os.getuid()):
        raise SchemaError('schema_invalid', '/script', 'script must be owned by root or the Studio user')
    data = Path(data_dir).resolve()
    if _under(script.parent, data) or _under(data, script.parent):
        raise SchemaError('schema_invalid', '/script', 'script directory must not contain or lie inside the data dir')
    args = body.get('args', [])
    schema._list(args, '/args', MAX_ARGS)
    for i, arg in enumerate(args):
        schema._str(arg, f'/args/{i}', 1, 200)
        if any(ord(ch) < 32 for ch in arg):
            raise SchemaError('schema_invalid', f'/args/{i}', 'control characters not allowed')
        if scanner.scan(arg) or scanner.SECRET_FIELD_RE.search(arg):
            raise SchemaError('adapter_secret_detected', f'/args/{i}', 'secret-like argument refused; value not shown')
    return {'name': name, 'script': str(script), 'args': list(args), 'sha256': script_sha256(script)}


def sandbox_status():
    """``(usable, message)``: adapters run only inside the Linux sandbox; elsewhere they are disabled with a message."""
    return adapter_sandbox.availability()


class Refused(ExecutorUnavailable):
    """Session could not be established. ``code`` is a protocol/launch code, ``detail`` is bounded and secret-free."""

    def __init__(self, code, detail=''):
        super().__init__(code)
        self.code = code
        self.detail = detail[:200]


class Session:
    """One adapter process. Reader/writer/stderr threads; state guarded by ``cond``."""

    def __init__(self, executor, adapter, scratch: Path, *, emit=None, outputs=()):
        self.executor = executor
        self.adapter = adapter
        self.scratch = scratch
        self.emit = emit
        self.outputs = tuple(outputs)
        self.session = os.urandom(16).hex()
        self.head = proto.chain_seed(self.session)
        self.seq = 1
        self.cond = threading.Condition()
        # Lock order: emit_lock -> cond; emit_lock -> store (via emit). Nothing takes store -> emit_lock, so a runner
        # pre-flight holding the store lock may still read session state under ``cond``.
        self.emit_lock = threading.Lock()
        self.phase = 'handshake'          # handshake -> welcomed -> requested -> accepted
        self.hello = None
        self.effective = []
        self.ref = None
        self.ping_sent = 0
        self.pong_seen = 0
        self.result = None
        self.result_at = None
        self.failure = None               # (run reason, code, detail)
        self.closing = False              # cancel/bye issued: an exit without result is expected
        self.exited = False
        self.returncode = None
        self.last_seen = time.monotonic()
        self.last_ping = time.monotonic()
        self._stderr = bytearray()
        self._out = queue.Queue()
        usable, message = sandbox_status()
        if not usable:
            raise Refused('sandbox_unavailable', message)
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'HOME': str(scratch),
               'TMPDIR': str(scratch), 'AETHER_ADAPTER_PROTOCOL': proto.PROTOCOL}
        argv = [executor.python, '-I', '-S', '-B', str(SANDBOX), str(scratch), adapter['script'], *adapter['args']]
        self.proc = proc.popen_group(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=scratch, env=env, close_fds=True)
        threads = [threading.Thread(target=target, name=f'adapter-{adapter["name"]}-{name}', daemon=True)
                   for target, name in ((self._read, 'out'), (self._write, 'in'), (self._drain, 'err'))]
        for thread in threads:
            thread.start()
        self._drain_thread = threads[2]

    # ------------------------------------------------------------ threads
    def _write(self):
        stream = self.proc.stdin
        while True:
            item = self._out.get()
            if item is None:
                break
            try:
                stream.write(item)
                stream.flush()
            except (OSError, ValueError):
                break
        try:
            stream.close()
        except OSError:
            pass

    def _drain(self):
        with self.proc.stderr as stream:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                self._stderr += chunk
                del self._stderr[:-STDERR_TAIL]

    def _read(self):
        stream = self.proc.stdout
        try:
            while True:
                line = stream.readline(proto.MAX_LINE + 1)
                if not line:
                    break
                try:
                    if len(line) > proto.MAX_LINE:
                        raise proto.ProtocolError('oversized', f'line exceeds {proto.MAX_LINE} bytes')
                    self._handle(proto.decode(line))
                except proto.ProtocolError as error:
                    self.fail('executor_error', 'adapter_' + error.code, error.detail)
                    break
                if self.failure:
                    break
        finally:
            self._out.put(None)
            self.kill()
            returncode = self.proc.wait()
            stream.close()
            with self.cond:
                self.returncode = returncode
                lost = self.phase != 'handshake' and self.result is None and not self.closing and not self.failure
            if lost:
                self.fail('executor_error', 'adapter_disconnected', f'adapter exited {returncode} before a result')
            with self.cond:
                self.exited = True
                self.cond.notify_all()
            self.executor._forget(self)
            shutil.rmtree(self.scratch, ignore_errors=True)

    # ------------------------------------------------------------ inbound
    def _handle(self, message):
        with self.emit_lock:
            self._handle_locked(message)

    def _handle_locked(self, message):
        kind = message['type']
        with self.cond:
            if self.failure:
                return
            self.last_seen = time.monotonic()
            allowed = {'handshake': ('hello',), 'welcomed': ('pong',), 'requested': ('accepted', 'pong'),
                       'accepted': ('event', 'pong')}[self.phase]
            if kind not in allowed or (kind == 'event' and self.result is not None):
                raise proto.ProtocolError('unexpected_message', f'{kind} during {self.phase}')
            if kind == 'hello':
                self.hello = message
            elif kind == 'pong':
                n = message['n']
                if isinstance(n, bool) or not isinstance(n, int) or not self.pong_seen < n <= self.ping_sent:
                    raise proto.ProtocolError('bad_pong')
                self.pong_seen = n
            elif kind == 'accepted':
                if not isinstance(message['ref'], str) or not proto.REF_RE.match(message['ref']):
                    raise proto.ProtocolError('bad_ref')
                self.ref = message['ref']
                self.phase = 'accepted'
            self.cond.notify_all()
            if kind != 'event':
                return
            head = proto.check_event(message, expected_seq=self.seq, head=self.head, outputs=self.outputs)
            record = {'source': 'adapter', 'protocol_version': proto.PROTOCOL, 'adapter': self.adapter['name'],
                      'session': self.session, 'ref': self.ref, 'kind': message['kind'], 'i': self.seq,
                      'prev': self.head, 'sha256': head, 'data': message['data'], 'redacted': False}
            self.seq, self.head = self.seq + 1, head
            if message['kind'] == 'result':
                self.result, self.result_at = message['data']['exit_code'], time.monotonic()
        if message['kind'] == 'log':
            clean, flagged = scanner.redact(message['data']['text'])
            if flagged:
                record['data'] = dict(message['data'], text=clean)
                record['redacted'] = True
        self.emit('adapter.event', record)
        if message['kind'] == 'artifact':
            self.emit('artifact.write', {'name': message['data']['name'], 'media_type': message['data']['media_type'],
                                         'content': message['content']})

    # ------------------------------------------------------------ outbound / control
    def send(self, message):
        self._out.put(proto.encode(message))

    def _wait(self, predicate, seconds, what):
        with self.cond:
            if not self.cond.wait_for(lambda: predicate() or self.failure or self.exited, seconds):
                raise Refused(f'{what}_timeout', f'no {what} within {seconds}s')
            if predicate():
                return                      # a later failure is reported by poll()
            if self.failure:
                raise Refused(self.failure[1], self.failure[2])
            raise Refused(*self._exit_reason())

    def _exit_reason(self):
        self._drain_thread.join(1.0)      # stderr closes when the process exits
        tail = bytes(self._stderr).decode('utf-8', 'replace').strip().splitlines()
        last = scanner.redact(tail[-1])[0][:200] if tail else ''
        if self.returncode == SANDBOX_EXIT:
            return 'sandbox_unavailable', last
        return 'adapter_exited', f'exit {self.returncode}'

    def handshake(self, granted, provider):
        offered = sorted(set(granted) & proto.allowed_scopes(provider))
        self.send({'type': 'hello', 'protocol': proto.PROTOCOL, 'session': self.session, 'offered': offered})
        self._wait(lambda: self.hello is not None, self.executor.handshake_seconds, 'hello')
        try:
            effective = proto.negotiate(self.hello, granted, provider)
        except proto.ProtocolError as error:
            self.send({'type': 'refused', 'code': error.code})
            self.close()
            raise Refused('adapter_' + error.code, error.detail) from None
        with self.cond:
            self.effective, self.phase = effective, 'welcomed'
        self.send({'type': 'welcome', 'session': self.session, 'capabilities': effective})
        return effective

    def ping(self):
        with self.cond:
            self.ping_sent += 1
            n = self.ping_sent
            self.last_ping = time.monotonic()
        self.send({'type': 'ping', 'n': n})
        return n

    def ping_wait(self):
        n = self.ping()
        self._wait(lambda: self.pong_seen >= n, self.executor.handshake_seconds, 'pong')

    def request(self, message):
        with self.cond:
            self.phase = 'requested'
        self.send(message)
        self._wait(lambda: self.ref is not None, self.executor.handshake_seconds, 'accepted')

    def fail(self, reason, code, detail=''):
        """Record the first failure as a run error (unless a cancel/bye made the exit expected), then kill the
        process group. Holding ``emit_lock`` guarantees no adapter event is emitted after the error."""
        assert reason in schema.REASONS, reason
        with self.emit_lock:      # serializes all failures and inbound handling
            with self.cond:
                recorded = not self.failure and self.result is None
                expected = self.closing
            # Persist the run error *before* publishing the failure: once poll() sees it the runner ends the run and
            # a later (lease-guarded) append would be refused.
            if recorded and self.emit and not expected:
                self.emit('executor.error', {'code': code[:64], 'message': detail[:300]})
            if recorded:
                with self.cond:
                    self.failure = (reason, code, detail[:200])
                    self.cond.notify_all()
            self.kill()

    def kill(self, force=True):
        if self.proc.poll() is None:
            proc.signal_group(self.proc.pid, force=force)

    def close(self, grace=1.0):
        """Polite end (probe/bye): send bye, wait briefly for exit, then kill the process group."""
        with self.cond:
            self.closing = True
        self.send({'type': 'bye'})
        with self.cond:
            self.cond.wait_for(lambda: self.exited, grace)
        self.kill()

    def poll(self):
        with self.cond:
            failure, result, exited = self.failure, self.result, self.exited
            idle = time.monotonic() - self.last_seen
            due = time.monotonic() - self.last_ping >= self.executor.heartbeat_seconds
            closing = self.closing
        if failure:
            return {'state': 'exited', 'exit_code': None, 'reason': failure[0]}
        if result is not None:
            if exited:
                return {'state': 'exited', 'exit_code': result}
            if time.monotonic() - self.result_at > EXIT_GRACE_SECONDS:
                self.kill()
            return {'state': 'running', 'exit_code': None}
        if exited:
            return {'state': 'exited', 'exit_code': None, 'reason': 'cancelled_by_owner' if closing else 'executor_error'}
        if idle > self.executor.dead_seconds:
            self.fail('adapter_unavailable', 'adapter_unresponsive', f'no message for {idle:.0f}s')
            return {'state': 'exited', 'exit_code': None, 'reason': 'adapter_unavailable'}
        if due and not closing:
            self.ping()
        return {'state': 'running', 'exit_code': None}

    def stale(self):
        with self.cond:
            return not self.exited and time.monotonic() - self.last_seen > 2 * self.executor.heartbeat_seconds


class AdapterExecutor:
    kind = 'adapter'
    demo = False

    def __init__(self, store, *, data_dir, scratch_root=None, provider='none', python=sys.executable,
                 heartbeat_seconds=2.0, dead_seconds=15.0, probe_ttl=30.0, handshake_seconds=5.0):
        assert os.path.isabs(python), 'adapter interpreter must be an absolute path'
        assert dead_seconds > 2 * heartbeat_seconds, 'dead_seconds must exceed two heartbeats'
        self.store = store
        self.data_dir = Path(data_dir)
        self.scratch_root = Path(scratch_root) if scratch_root else self.data_dir / 'adapter-scratch'
        self.scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for leftover in self.scratch_root.iterdir():   # no session exists yet: anything here is from a crash
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover)
            else:
                leftover.unlink()
        self.provider = provider
        self.python = python
        self.heartbeat_seconds = heartbeat_seconds
        self.dead_seconds = dead_seconds
        self.probe_ttl = probe_ttl
        self.handshake_seconds = handshake_seconds
        self._lock = threading.Lock()
        self._contact: dict[str, dict] = {}      # adapter id -> last contact {ok, at, utc, detail, scopes}
        self._live: dict[str, set] = {}          # adapter id -> live sessions

    def capabilities(self):
        return frozenset(proto.studio_capability(s) for s in proto.allowed_scopes(self.provider))

    # ------------------------------------------------------------ sessions
    def _open(self, adapter, label, *, emit=None, outputs=()):
        usable, message = sandbox_status()
        if not usable:      # never launch an adapter without its sandbox
            raise Refused('sandbox_unavailable', message)
        scratch = self.scratch_root / f'{label}-{os.urandom(6).hex()}'
        scratch.mkdir(mode=0o700)
        session = Session(self, adapter, scratch, emit=emit, outputs=outputs)
        with self._lock:
            self._live.setdefault(adapter['id'], set()).add(session)
        return session

    def _forget(self, session):
        with self._lock:
            self._live.get(session.adapter['id'], set()).discard(session)

    def _record(self, adapter, ok, detail, scopes=()):
        entry = {'ok': ok, 'at': time.monotonic(), 'utc': _utc(), 'detail': detail[:200], 'scopes': sorted(scopes)}
        with self._lock:
            self._contact[adapter['id']] = entry
        return entry

    # ------------------------------------------------------------ health
    def _static(self, adapter):
        """Enrollment-level reasons an adapter cannot be used, without contacting it."""
        if adapter is None:
            return 'not enrolled'
        usable, message = sandbox_status()
        if not usable:
            return message
        if adapter['revoked']:
            return 'revoked'
        if 'observe' not in adapter['granted']:
            return 'observe not granted'
        try:
            if script_sha256(adapter['script']) != adapter['script_sha256']:
                return 'script changed since enrollment; re-enroll'
        except OSError:
            return 'script missing'
        return None

    def probe(self, adapter):
        """Contact the adapter: launch, handshake, ping/pong, bye. Records and returns the contact entry."""
        problem = self._static(adapter)
        if problem:
            return self._record(adapter, False, problem)
        session = self._open(adapter, 'probe')
        try:
            scopes = session.handshake(adapter['granted'], self.provider)
            session.ping_wait()
            return self._record(adapter, True, f'handshake ok; scopes {",".join(scopes) or "none"}', scopes)
        except Refused as error:
            return self._record(adapter, False, f'{error.code}: {error.detail}'.rstrip(': '))
        finally:
            session.close()

    def _view(self, adapter, contact):
        """Honest status from cached contact only: stale contact is degraded, never ready."""
        problem = self._static(adapter)
        base = {'id': adapter['id'], 'name': adapter['name'], 'granted': adapter['granted']}
        if problem:
            return dict(base, status='unavailable', observed_utc=_utc(), detail=problem, scopes=[])
        if contact is None:
            return dict(base, status='unavailable', observed_utc=_utc(), detail='not yet contacted', scopes=[])
        age = time.monotonic() - contact['at']
        with self._lock:
            stale_session = any(s.stale() for s in self._live.get(adapter['id'], ()))
        status = 'unavailable' if not contact['ok'] else ('degraded' if age > self.probe_ttl or stale_session else 'ready')
        detail = contact['detail']
        if contact['ok'] and age > self.probe_ttl:
            detail = f'last contact {age:.0f}s ago'
        if stale_session:
            detail = 'a live session stopped answering heartbeats'
        return dict(base, status=status, observed_utc=contact['utc'], detail=detail, scopes=contact['scopes'])

    def health(self, ref=None):
        """Without ``ref``: aggregate from cached contact (never launches anything). With ``ref`` (a blueprint
        ``executor.ref`` = enrollment name): re-probe when the last contact is older than ``probe_ttl``."""
        if ref is None:
            adapters = [self._view(a, self._contact.get(a['id'])) for a in self.store.list_adapters()]
            statuses = {a['status'] for a in adapters}
            status = 'ready' if 'ready' in statuses else ('degraded' if 'degraded' in statuses else 'unavailable')
            detail = f'{len(adapters)} enrolled adapter(s)' if adapters else 'no adapter enrolled'
            return {'status': status, 'observed_utc': _utc(), 'detail': detail, 'adapters': adapters}
        adapter = self.store.active_adapter_named(ref)
        if adapter is None:
            return {'status': 'unavailable', 'observed_utc': _utc(), 'detail': 'not enrolled'}
        contact = self._contact.get(adapter['id'])
        if contact is None or time.monotonic() - contact['at'] > self.probe_ttl:
            contact = self.probe(adapter)
        return self._view(adapter, contact)

    def preflight(self, snapshot):
        """Runner pre-flight: None, or the reason the run is blocked before anything starts."""
        health = self.health(snapshot['executor']['ref'])
        if health['status'] != 'ready':
            return 'adapter_unavailable'
        needed = {schema.adapter_step(stage)[0] for stage in snapshot['stages']}
        return None if needed <= set(health['scopes']) else 'capability_unavailable'

    # ------------------------------------------------------------ executor interface
    def start(self, run, stage, emit):
        adapter = self.store.active_adapter_named(run.snapshot['executor']['ref'])
        problem = self._static(adapter)
        if problem:
            emit('executor.error', {'code': 'adapter_unavailable', 'message': problem})
            raise ExecutorUnavailable('adapter_unavailable')
        spec = next(s for s in run.snapshot['stages'] if s['key'] == stage.key)
        op, arg = schema.adapter_step(spec)
        session = self._open(adapter, f'{run.id}-{stage.key}-{stage.attempt}', emit=emit, outputs=stage.outputs)
        try:
            scopes = session.handshake(adapter['granted'], self.provider)
            self._record(adapter, True, 'session handshake ok', scopes)
            if op not in scopes:
                raise Refused('capability_not_negotiated', f'{op} not in negotiated scopes')
            emit('adapter.session', {'source': 'adapter', 'protocol_version': proto.PROTOCOL, 'adapter': adapter['name'],
                                     'adapter_id': adapter['id'], 'session': session.session,
                                     'chain_seed': session.head, 'capabilities': scopes})
            if op == 'submit':
                inputs = {i['name']: i['default'] for i in run.snapshot['inputs'] if i['default'] is not None}
                job = {'run_id': run.id, 'stage': stage.key, 'attempt': stage.attempt, 'task': arg,
                       'timeout_seconds': stage.timeout_seconds, 'inputs': inputs, 'outputs': list(stage.outputs)}
                session.request({'type': 'submit', 'job': job})
            else:
                session.request({'type': 'observe', 'ref': arg})
        except Refused as error:
            if session.failure:
                # A protocol violation (already recorded as a run error): the stage fails via poll() with
                # executor_error. Unavailability below fails it with adapter_unavailable instead.
                return Handle(run.id, stage.key, stage.attempt, state=session)
            emit('executor.error', {'code': error.code[:64], 'message': error.detail})
            if error.code != 'capability_not_negotiated':
                self._record(adapter, False, f'{error.code}: {error.detail}'.rstrip(': '))
            session.close(0)
            raise
        return Handle(run.id, stage.key, stage.attempt, state=session)

    def poll(self, handle):
        return handle.state.poll()

    def cancel(self, handle, grace_seconds):
        """Grace, then a forced kill of the process group. With the ``cancel`` scope the adapter is asked first; without it
        the group gets SIGTERM. ``grace_seconds == 0`` kills at once."""
        session = handle.state
        with session.cond:
            session.closing = True
            exited = session.exited
        if exited:
            return
        if grace_seconds <= 0:
            session.kill()
            return
        if 'cancel' in session.effective:
            session.send({'type': 'cancel', 'grace_seconds': grace_seconds})
        else:
            session.kill(force=False)
        timer = threading.Timer(grace_seconds, session.kill)
        timer.daemon = True
        timer.start()

    def revoke(self, adapter_id):
        """Called after the store revoked an enrollment: end its live sessions (recorded as run errors) at once."""
        with self._lock:
            sessions = list(self._live.get(adapter_id, ()))
            self._contact.pop(adapter_id, None)
        for session in sessions:
            session.fail('adapter_unavailable', 'adapter_revoked', 'enrollment revoked by owner')
        return len(sessions)

    def forget_contact(self, adapter_id):
        with self._lock:
            self._contact.pop(adapter_id, None)
