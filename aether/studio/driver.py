"""Queue driver (CLI_EXECUTOR.md): one thread that feeds queued CLI runs to ``CliExecutor`` slots.

Started with the Studio app only when ``AETHER_EXEC=cli``; ``stop()`` ends it (tests, shutdown). Rules:

* The next queued run is taken in queue order (position); a paused queue starts nothing (running runs continue).
* A run asking for ``auto`` (or ``auto:claude`` / ``auto:codex``) gets the first enabled slot, in ``slots.json``
  order, that is not blocked by a usage limit and has fewer active runs than its cap. A run that names a slot id is
  pinned to it: it waits for that slot and is never moved.
* ``limit_stop``: the attempt is recorded as ``limit_stop``, the slot stays blocked (slots.json is the single source
  of truth for ``blocked_until``; the ``slot_state`` table is a mirror), the run goes back to the FRONT of the queue
  and the next attempt uses another available slot; a ``provider_switch {from, to, reason}`` event records the move.
  With no slot available the run stays queued and the driver wakes when the earliest ``blocked_until`` passes.
* Concurrency: global cap (default 1, clamp 1..4) and a per-slot cap (default 1, clamp 1..4).
* Every action is appended to the queue audit chain (exec_queue.QueueStore.audit).
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import threading
import time

from aether.studio import slots as slots_module
from aether.studio.executors.base import ExecutorUnavailable
from aether.studio.exec_queue import QueueError

MAX_CAP = 4


def clamp_cap(value, default=1) -> int:
    try:
        return max(1, min(MAX_CAP, int(value)))
    except (TypeError, ValueError):
        return default


class Driver:
    def __init__(self, queue, slots, executor, *, global_cap=1, slot_cap=1, tick_seconds=0.1, idle_seconds=5.0,
                 timeout_seconds=3600, grace_seconds=5, clock=lambda: datetime.now(timezone.utc)):
        self.queue, self.slots, self.executor = queue, slots, executor
        self.global_cap = clamp_cap(global_cap)
        self.slot_cap = {'*': clamp_cap(slot_cap)} if not isinstance(slot_cap, dict) else \
            {k: clamp_cap(v) for k, v in slot_cap.items()} | {'*': clamp_cap(slot_cap.get('*', 1))}
        self.tick_seconds, self.idle_seconds = tick_seconds, idle_seconds
        self.timeout_seconds, self.grace_seconds = timeout_seconds, grace_seconds
        self.clock = clock
        self.running = {}            # run_id -> {handle, slot, attempt, errors}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ lifecycle
    def start(self, *, recover=True):
        if self._thread is not None:
            return
        if recover:
            self.recover()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='exec-queue-driver')
        self._thread.start()

    def stop(self, timeout=5.0):
        """Stop the thread and end any CLI process it started (queue rows stay as they are; recovery handles them)."""
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            for entry in list(self.running.values()):
                try:
                    self.executor.cancel(entry['handle'], 0)
                except Exception:
                    pass
            self.running.clear()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wake(self):
        self._wake.set()

    def recover(self) -> dict:
        result = self.queue.recover()
        self.queue.audit('driver', 'driver.started', None, {'recovered_blocked': result['blocked']})
        return result

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:   # the driver must outlive one bad tick
                try:
                    self.queue.audit('driver', 'driver.error', None, {'error': type(error).__name__})
                except Exception:
                    pass
            self._wake.wait(self._wait_seconds())
            self._wake.clear()

    def _wait_seconds(self) -> float:
        if self.running:
            return self.tick_seconds
        if self.queue.paused() or not self.queue.queued():
            return self.idle_seconds
        until = self._earliest_block()
        if until is None:
            return self.idle_seconds
        return max(0.05, min(self.idle_seconds, (until - self.clock()).total_seconds() + 0.05))

    def _earliest_block(self):
        times = [t for t in (self.slots.blocked_until(s['id']) for s in self.slots.load() if s['enabled']) if t]
        return min(times) if times else None

    # ------------------------------------------------------------ one pass
    def tick(self):
        with self._lock:
            slots = self.slots.load()
            self.queue.mirror_slots([s['id'] for s in slots], self.slots.state())
            self._poll()
            if not self.queue.paused():
                self._dispatch(slots)

    def _poll(self):
        for run_id, entry in list(self.running.items()):
            try:
                result = self.executor.poll(entry['handle'])
            except Exception:
                result = {'state': 'exited', 'exit_code': None, 'reason': 'executor_error'}
            if result.get('state') == 'running':
                continue
            del self.running[run_id]
            self._finish(run_id, entry, result)

    def _evidence(self, run_id, info):
        """sha256 of the raw log file as it is now and of the final-result text (None when the CLI gave none)."""
        digest = hashlib.sha256()
        try:
            with open(self.executor.log_path(run_id), 'rb') as stream:
                for block in iter(lambda: stream.read(65536), b''):
                    digest.update(block)
        except OSError:
            digest = None
        final = info.get('final')
        self.queue.set_evidence(run_id, digest.hexdigest() if digest else None,
                                hashlib.sha256(final.encode('utf-8')).hexdigest() if final is not None else None)

    def _finish(self, run_id, entry, result):
        self._finish_state(run_id, entry, result)
        self._evidence(run_id, entry['info'])

    def _finish_state(self, run_id, entry, result):
        attempt, slot = entry['attempt'], entry['slot']
        item = self.queue.get(run_id)
        if item['state'] != 'running':      # cancelled meanwhile; cancel() already closed the row
            self.queue.end_attempt(run_id, attempt, 'cancelled', reason='cancelled_by_owner')
            return
        if result.get('state') == 'limit_stop':
            until = result.get('blocked_until')
            self.queue.end_attempt(run_id, attempt, 'limit_stop', reason='limit_stop', blocked_until=until)
            self.queue.note_limit(result.get('slot') or slot, until)
            self.queue.set_state(run_id, 'queued', 'limit_stop', expect=('running',), front=True, audit='driver.requeue',
                                 detail={'slot': slot, 'blocked_until': until})
            return
        code, reason = result.get('exit_code'), result.get('reason')
        if code == 0:
            self.queue.end_attempt(run_id, attempt, 'succeeded', exit_code=0)
            self.queue.set_state(run_id, 'done', None, expect=('running',), audit='driver.finish')
        elif reason == 'cancelled_by_owner':
            self.queue.end_attempt(run_id, attempt, 'cancelled', reason=reason)
            self.queue.set_state(run_id, 'cancelled', reason, expect=('running',), audit='driver.finish')
        else:
            reason = reason or 'exit_nonzero'
            self.queue.end_attempt(run_id, attempt, 'failed', reason=reason, exit_code=code)
            self.queue.set_state(run_id, 'failed', reason, expect=('running',), audit='driver.finish')

    # ------------------------------------------------------------ dispatch
    def _cap(self, slot_id) -> int:
        return self.slot_cap.get(slot_id, self.slot_cap['*'])

    def choose_slot(self, requested, slots):
        """First enabled slot in slots.json order that is unblocked, under its cap and whose CLI is installed."""
        kind = requested.split(':', 1)[1] if requested.startswith('auto:') else None
        pinned = None if requested.startswith('auto') else requested
        rows = self.queue.slot_rows()
        for slot in slots:
            if not slot['enabled'] or (kind and slot['kind'] != kind) or (pinned and slot['id'] != pinned):
                continue
            if self.slots.blocked_until(slot['id']) is not None:
                continue
            if rows.get(slot['id'], {}).get('active_count', 0) >= self._cap(slot['id']):
                continue
            if self.executor.command(slot['kind']) is None:
                continue
            return slot
        return None

    def _dispatch(self, slots):
        skip = set()
        while len(self.running) < self.global_cap:
            placed = None
            for item in self.queue.queued():
                if item['run_id'] in skip:
                    continue
                slot = self.choose_slot(item['requested_slot'], slots)
                if slot is not None:
                    placed = (item, slot)
                    break
                skip.add(item['run_id'])
            if placed is None:
                return
            self._launch(*placed)

    def _launch(self, item, slot):
        run_id = item['run_id']
        last = self.queue.last_attempt(run_id)
        try:
            prompt = self.queue.prompt(item)
        except OSError:
            self.queue.set_state(run_id, 'failed', 'prompt_missing', expect=('queued',), audit='driver.finish')
            return
        try:
            attempt = self.queue.start_attempt(run_id, slot['id'], slot['kind'])
        except QueueError:
            return
        if last and last['state'] == 'limit_stop' and last['slot_id'] != slot['id']:
            data = {'from': last['slot_id'], 'to': slot['id'], 'reason': 'limit_stop'}
            self.queue.event(run_id, 'provider_switch', data)
            self.queue.audit('driver', 'driver.provider_switch', run_id, data)
        errors = []
        info = {'final': None}     # text of the CLI's final result (hashed as evidence, never stored)

        def emit(type_, data):
            if type_ == 'executor.error':
                errors.append(str(data.get('code', 'executor_error')))
            elif type_ == 'stage.usage':      # only figures the CLI reported; the latest report of the attempt wins
                tokens, cents = data.get('tokens'), data.get('cost_usd_cents')
                self.queue.record_usage(run_id, attempt, tokens if isinstance(tokens, int) else None,
                                        cents if isinstance(cents, int) else None)
            elif type_ == 'stage.log' and data.get('event') == 'final_result':
                text = str(data.get('text', ''))      # rendered as "final_result[ (error)] <text>"; hash the text only
                for prefix in ('final_result (error) ', 'final_result '):
                    if text.startswith(prefix):
                        text = text[len(prefix):]
                        break
                info['final'] = text
        try:
            handle = self.executor.launch(slot['id'], item['workspace'], prompt, emit=emit, run_id=run_id,
                                          stage_key='queue', attempt=attempt, timeout_seconds=self.timeout_seconds)
        except (ExecutorUnavailable, ValueError) as error:
            reason = errors[-1] if errors else (str(error) or 'executor_error')[:60]
            self.queue.end_attempt(run_id, attempt, 'failed', reason=reason)
            self.queue.set_state(run_id, 'failed', reason, expect=('running',), audit='driver.finish')
            self._evidence(run_id, info)
            return
        self.running[run_id] = {'handle': handle, 'slot': slot['id'], 'attempt': attempt, 'errors': errors, 'info': info}
        self._wake.set()      # an immediate limit_stop (race with a block) should be seen without waiting

    # ------------------------------------------------------------ owner actions
    def cancel(self, run_id, actor='owner') -> str:
        with self._lock:
            item = self.queue.get(run_id)
            if item['state'] in ('done', 'cancelled', 'failed'):
                raise QueueError('terminal_state', f'item is {item["state"]}', 409)
            entry = self.running.pop(run_id, None)
            if entry is not None:
                try:
                    self.executor.cancel(entry['handle'], self.grace_seconds)   # kills the process tree via proc.py
                except Exception:
                    pass
                self.queue.end_attempt(run_id, entry['attempt'], 'cancelled', reason='cancelled_by_owner')
            self.queue.set_state(run_id, 'cancelled', 'cancelled_by_owner', expect=('queued', 'running', 'blocked'),
                                 audit='queue.cancel', detail={'was': item['state']}, actor=actor)
            self._evidence(run_id, entry['info'] if entry is not None else {})
        self.wake()
        return 'cancelled'

    def resume_item(self, run_id, actor='owner') -> str:
        if not self.queue.set_state(run_id, 'queued', None, expect=('blocked',), audit='queue.resume_item', actor=actor):
            raise QueueError('conflict', 'only a blocked item can be resumed', 409)
        self.wake()
        return 'queued'
