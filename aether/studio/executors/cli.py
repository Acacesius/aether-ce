"""CliExecutor (CLI_EXECUTOR.md): one stage = one run of a signed-in coding CLI (Claude Code or Codex) in an
owner-chosen workspace folder. Registered only when ``AETHER_EXEC=cli``.

The prompt goes on stdin; argv holds fixed flags (plus a validated model name) only. The environment is built from
scratch. Output lines become run events (``cli_events``) and are kept verbatim in a per-run log; a usage-limit exit
blocks the slot (``limits``) and ends the attempt with the ``limit_stop`` outcome instead of a failure.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from aether.studio import cli_events, fsperm, limits, proc, scanner
from aether.studio.executors.base import ExecutorUnavailable, Handle
from aether.studio.slots import SlotStore, slot_env, under, utc_iso

CLAUDE_ALLOWED = ('Read', 'Edit', 'MultiEdit', 'Write', 'Glob', 'Grep', 'LS', 'NotebookEdit', 'TodoWrite')
CLAUDE_DISALLOWED = ('Bash', 'BashOutput', 'KillShell', 'WebFetch', 'WebSearch', 'Task')
ENV_KEYS = ('PATH', 'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'SystemRoot',
            'SystemDrive', 'windir', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL', 'USER',
            'USERNAME', 'LOGNAME')
STEP_PREFIX = 'cli:'
PROMPT_MAX = 200_000
STDERR_TAIL = 50
READER_JOIN_SECONDS = 5.0
WAIT_TICK = 0.1


class WorkspaceError(ValueError):
    pass


def _utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def validate_workspace(raw, data_dir) -> Path:
    """The owner-picked folder a CLI works in: existing directory, not a filesystem root, never the data dir, inside
    it, or containing it. Returns the resolved path."""
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise WorkspaceError('workspace folder required')
    if not os.path.isabs(str(raw)):
        raise WorkspaceError('workspace must be an absolute path')
    try:
        path = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkspaceError('workspace does not exist') from None
    if not path.is_dir():
        raise WorkspaceError('workspace must be a directory')
    if path.parent == path:
        raise WorkspaceError('workspace must not be a filesystem root')
    data = Path(data_dir).resolve()
    if under(path, data):
        raise WorkspaceError('workspace must not be the data dir or inside it')
    if under(data, path):
        raise WorkspaceError('workspace must not contain the data dir')
    return path


def build_env(slot, base=None) -> dict:
    """Minimal environment: a fixed allowlist from ``base`` plus the slot's config-folder variable."""
    base = os.environ if base is None else base
    env = {key: base[key] for key in ENV_KEYS if key in base}
    env['NO_COLOR'] = '1'
    env.update(slot_env(slot))
    return env


def build_argv(slot, command, workspace) -> list:
    argv = list(command)
    if slot['kind'] == 'claude':
        argv += ['-p', '--output-format', 'stream-json', '--verbose', '--permission-mode', 'acceptEdits',
                 '--allowedTools', ','.join(CLAUDE_ALLOWED), '--disallowedTools', ','.join(CLAUDE_DISALLOWED)]
        if slot.get('model'):
            argv += ['--model', slot['model']]
    else:
        argv += ['exec', '--json', '--sandbox', 'workspace-write', '--skip-git-repo-check', '-C', str(workspace)]
        if slot.get('model'):
            argv += ['-m', slot['model']]
        argv.append('-')   # prompt from stdin
    return argv


def _safe_name(text) -> str:
    return re.sub(r'[^A-Za-z0-9_-]', '_', str(text))[:80] or 'run'


class Attempt:
    """State of one CLI process. ``outcome`` is set once, by the waiter thread (or at start for a blocked slot)."""

    def __init__(self, slot, log, emit, deadline):
        self.slot = slot
        self.log = log
        self.emit = emit
        self.deadline = deadline
        self.proc = None
        self.outcome = None
        self.cancelled = False
        self.timed_out = False
        self.final = None
        self.stderr_tail = deque(maxlen=STDERR_TAIL)
        self.lock = threading.Lock()
        self.emit_lock = threading.Lock()
        self.done = threading.Event()

    def send(self, type_, data):
        """Forward to the runner. A failing emit must not kill a reader (a blocked pipe would hang the CLI)."""
        with self.emit_lock:
            try:
                self.emit(type_, data)
            except Exception:
                pass

    def finish(self, outcome):
        with self.lock:
            if self.outcome is None:
                self.outcome = outcome
        self.done.set()


class CliExecutor:
    kind = 'cli'
    demo = False

    def __init__(self, data_dir, *, slots: SlotStore | None = None, commands: dict | None = None,
                 clock=lambda: datetime.now(timezone.utc), base_env=None, log_dir=None):
        self.data_dir = Path(data_dir)
        self.slots = slots or SlotStore(self.data_dir, clock=clock)
        self.commands = dict(commands or {})     # kind -> argv prefix (tests: [python, fake_cli.py])
        self.clock = clock
        self.base_env = base_env
        self.log_dir = Path(log_dir) if log_dir else self.data_dir / 'cli-logs'

    def log_path(self, run_id) -> Path:
        return self.log_dir / f'{_safe_name(run_id)}.log'

    def capabilities(self):
        return frozenset({'cli.run'})

    def command(self, kind):
        if kind in self.commands:
            return list(self.commands[kind])
        found = proc.resolve_cli(kind)
        return [found] if found else None

    def health(self):
        try:
            slots = self.slots.load()
            state = self.slots.state()
        except ValueError as error:
            return {'status': 'unavailable', 'observed_utc': _utc(), 'detail': f'slots.json: {error}'[:200], 'slots': []}
        views, ready = [], False
        for slot in slots:
            found = self.command(slot['kind']) is not None
            blocked = self.slots.blocked_until(slot['id'])
            usable = slot['enabled'] and found and blocked is None
            ready = ready or usable
            views.append({'id': slot['id'], 'kind': slot['kind'], 'label': slot['label'], 'enabled': slot['enabled'],
                          'cli_found': found, 'blocked_until': state.get(slot['id'], {}).get('blocked_until') if blocked else None})
        detail = f'{len(slots)} slot(s); {"a slot is usable" if ready else "no usable slot"}' if slots else 'no slot configured'
        return {'status': 'ready' if ready else 'unavailable', 'observed_utc': _utc(), 'detail': detail, 'slots': views}

    # ------------------------------------------------------------ slot selection
    def _pick(self, ref):
        """``ref`` = slot id, or ``auto`` / ``auto:claude`` / ``auto:codex``. Returns (slot, blocked_until)."""
        if ref in ('auto', 'auto:claude', 'auto:codex'):
            kind = None if ref == 'auto' else ref.split(':', 1)[1]
            candidates = [s for s in self.slots.load() if s['enabled'] and (kind is None or s['kind'] == kind)]
            if not candidates:
                return None, None
            for slot in candidates:
                if self.slots.blocked_until(slot['id']) is None:
                    return slot, None
            soonest = min(candidates, key=lambda s: self.slots.blocked_until(s['id']))
            return soonest, self.slots.blocked_until(soonest['id'])
        slot = self.slots.get(ref)
        if slot is None or not slot['enabled']:
            return None, None
        return slot, self.slots.blocked_until(slot['id'])

    # ------------------------------------------------------------ executor interface
    def start(self, run, stage, emit):
        executor = run.snapshot.get('executor', {}) if isinstance(run.snapshot, dict) else {}
        spec = next((s for s in run.snapshot.get('stages', []) if s.get('key') == stage.key), {}) \
            if isinstance(run.snapshot, dict) else {}
        workspace = executor.get('workspace') or run.snapshot.get('workspace')
        prompt = spec.get('prompt') or stage.executor_step
        if prompt.startswith(STEP_PREFIX):
            prompt = prompt[len(STEP_PREFIX):]
        return self.launch(executor.get('ref', 'auto'), workspace, prompt, emit=emit, run_id=run.id,
                           stage_key=stage.key, attempt=stage.attempt, timeout_seconds=stage.timeout_seconds)

    def launch(self, slot_ref, workspace, prompt, *, emit, run_id='run', stage_key='stage', attempt=1,
               timeout_seconds=600) -> Handle:
        handle = Handle(run_id, stage_key, attempt)
        try:
            slot, blocked = self._pick(slot_ref)
        except ValueError as error:
            emit('executor.error', {'code': 'slots_invalid', 'message': str(error)[:300]})
            raise ExecutorUnavailable('slots_invalid') from None
        if slot is None:
            emit('executor.error', {'code': 'slot_unavailable', 'message': f'no enabled slot for {str(slot_ref)[:60]}'})
            raise ExecutorUnavailable('slot_unavailable')
        try:
            folder = validate_workspace(workspace, self.data_dir)
        except WorkspaceError as error:
            emit('executor.error', {'code': 'workspace_invalid', 'message': str(error)})
            raise
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_MAX:
            emit('executor.error', {'code': 'prompt_invalid', 'message': 'prompt must be 1..200000 characters'})
            raise ValueError('prompt_invalid')
        fsperm.private_dir(self.log_dir)
        log = cli_events.RawLog(self.log_path(run_id))
        state = Attempt(slot, log, emit, time.monotonic() + max(1, int(timeout_seconds)))
        handle.state = state
        if blocked is not None:
            log.header(stage=_safe_name(stage_key), attempt=attempt, slot=slot['id'], kind=slot['kind'], launched='no')
            until = utc_iso(blocked)
            state.send('stage.log', {'stream': 'system', 'text': f'slot {slot["id"]} is blocked by a usage limit until {until}; not started',
                                     'redacted': False, 'source': 'cli', 'event': 'limit_stop'})
            state.finish({'state': 'limit_stop', 'exit_code': None, 'outcome': 'limit_stop', 'slot': slot['id'],
                          'blocked_until': until})
            return handle
        command = self.command(slot['kind'])
        if command is None:
            emit('executor.error', {'code': 'cli_not_found', 'message': f'{slot["kind"]} CLI not found on PATH'})
            raise ExecutorUnavailable('cli_not_found')
        argv = build_argv(slot, command, folder)
        log.header(stage=_safe_name(stage_key), attempt=attempt, slot=slot['id'], kind=slot['kind'], launched='yes')
        try:
            state.proc = proc.start(argv, folder, build_env(slot, self.base_env), stdin=prompt)
        except OSError as error:
            emit('executor.error', {'code': 'cli_start_failed', 'message': type(error).__name__})
            raise ExecutorUnavailable('cli_start_failed') from None
        readers = [threading.Thread(target=self._read_stdout, args=(state,), daemon=True, name=f'cli-out-{run_id}'),
                   threading.Thread(target=self._read_stderr, args=(state,), daemon=True, name=f'cli-err-{run_id}')]
        for thread in readers:
            thread.start()
        threading.Thread(target=self._wait, args=(state, readers), daemon=True, name=f'cli-wait-{run_id}').start()
        return handle

    def poll(self, handle):
        state = handle.state
        if state.outcome is None:
            return {'state': 'running', 'exit_code': None}
        return dict(state.outcome)

    def cancel(self, handle, grace_seconds):
        state = handle.state
        with state.lock:
            if state.outcome is not None or state.proc is None:
                return
            state.cancelled = True
        proc.kill_tree(state.proc, grace_seconds)

    # ------------------------------------------------------------ threads
    def _read_stdout(self, state):
        kind = state.slot['kind']
        for raw in iter(state.proc.popen.stdout.readline, b''):
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            try:
                state.log.stdout(line)
            except OSError:
                pass
            for event in cli_events.parse_line(line, kind):
                self._forward(state, event)

    def _read_stderr(self, state):
        for raw in iter(state.proc.popen.stderr.readline, b''):
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            state.stderr_tail.append(line)
            try:
                state.log.stderr(line)
            except OSError:
                pass

    def _forward(self, state, event):
        kind = event['kind']
        if kind == 'final_result':
            state.final = event
        if kind == 'usage':
            tokens = event['input_tokens'] + event['output_tokens']
            data = {'tokens': tokens}
            if 'cost_usd' in event:      # only what the CLI reported; never an estimate
                data['cost_usd_cents'] = int(round(event['cost_usd'] * 100))
            state.send('stage.usage', data)
        text, rules = scanner.redact(cli_events.render(event))
        if not text and kind != 'final_result':
            return
        state.send('stage.log', {'stream': 'stderr' if kind == 'error' else 'stdout', 'text': text,
                                 'redacted': bool(rules), 'source': 'cli', 'event': kind})

    def _wait(self, state, readers):
        popen = state.proc.popen
        while True:
            try:
                code = popen.wait(timeout=WAIT_TICK)
                break
            except subprocess.TimeoutExpired:
                if not state.timed_out and not state.cancelled and time.monotonic() > state.deadline:
                    state.timed_out = True
                    proc.kill_tree(state.proc, 0)
        for thread in readers:
            thread.join(READER_JOIN_SECONDS)
        proc.kill_tree(state.proc, 0)   # reap stray children left in the group (POSIX); no-op once gone
        state.proc.close_pipes()
        state.finish(self._outcome(state, code))

    def _outcome(self, state, code):
        if state.cancelled:
            return {'state': 'exited', 'exit_code': None, 'reason': 'cancelled_by_owner'}
        if state.timed_out:
            return {'state': 'exited', 'exit_code': None, 'reason': 'timeout'}
        final_error = bool(state.final and state.final.get('is_error'))
        if code == 0 and not final_error:
            return {'state': 'exited', 'exit_code': 0}
        hit = limits.detect_in_log(state.log, state.slot['kind'], self.clock())
        if hit:
            until = hit['reset_utc']
            try:
                stored = self.slots.block(state.slot['id'], until, 'usage limit' + ('' if hit['parsed'] else ' (reset time assumed)'))
            except ValueError:
                stored = utc_iso(until)
            state.send('stage.log', {'stream': 'system', 'redacted': False, 'source': 'cli', 'event': 'limit_stop',
                                     'text': f'usage limit reached on slot {state.slot["id"]}; blocked until {stored}'
                                             + ('' if hit['parsed'] else ' (no reset time reported; default 5 h)')})
            return {'state': 'limit_stop', 'exit_code': None, 'outcome': 'limit_stop', 'slot': state.slot['id'],
                    'blocked_until': stored}
        if code is None or code == 0:
            return {'state': 'exited', 'exit_code': 1}
        return {'state': 'exited', 'exit_code': 128 - code if code < 0 else code}
