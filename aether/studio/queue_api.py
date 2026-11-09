"""HTTP routes for the CLI queue and slots (CLI_EXECUTOR.md). Only built when ``AETHER_EXEC=cli``; transport-free.

Paths live under ``/api/studio/`` like every other Studio route (same guard, bearer auth and Origin rules)."""
from __future__ import annotations

import re
import time

from aether.studio import run_log, slots as slots_module
from aether.studio.executors.cli import WorkspaceError, validate_workspace
from aether.studio.exec_queue import TERMINAL, TRACKER_MAX, QueueError, clean_prompt

QUEUE = '/api/studio/queue'
ITEM = re.compile(r'^/api/studio/queue/(que_[0-9A-HJKMNP-TV-Z]{26})(/resume)?$')
CLEAR = re.compile(r'^/api/studio/slots/([a-z][a-z0-9_-]{0,39})/clear-limit$')
TRACKER = '/api/studio/tracker'
LOG = re.compile(r'^/api/studio/runs/(que_[0-9A-HJKMNP-TV-Z]{26})/log(/stream)?$')
MAX_WAIT = 25.0
BLUEPRINT_REF = re.compile(r'^[A-Za-z0-9._:-]{1,80}$')
ROUTES = {('GET', QUEUE), ('POST', QUEUE), ('GET', QUEUE + '/<id>'), ('DELETE', QUEUE + '/<id>'),
          ('POST', QUEUE + '/<id>/resume'), ('POST', QUEUE + '/reorder'), ('POST', QUEUE + '/pause'),
          ('POST', QUEUE + '/resume'), ('GET', QUEUE + '/audit'), ('GET', '/api/studio/slots'),
          ('PUT', '/api/studio/slots'), ('POST', '/api/studio/slots/<id>/clear-limit'),
          ('GET', TRACKER), ('GET', '/api/studio/runs/<id>/log'), ('GET', '/api/studio/runs/<id>/log/stream')}


def _err(status, code, detail='', field=None):
    return status, {'error': code, 'detail': detail[:300], 'field': field}


class QueueAPI:
    def __init__(self, queue, driver, slots, executor, data_dir):
        self.queue, self.driver, self.slots, self.executor, self.data_dir = queue, driver, slots, executor, data_dir

    def owns(self, path) -> bool:
        return (path == QUEUE or path.startswith(QUEUE + '/') or path == '/api/studio/slots' or path.startswith('/api/studio/slots/')
                or path == TRACKER or bool(LOG.match(path)))

    def handle(self, method, path, body, query=None):
        try:
            return self._dispatch(method, path, body, query or {})
        except QueueError as error:
            return _err(error.status, error.code, error.detail)
        except slots_module.SlotError as error:
            return _err(404 if error.code == 'not_found' else 400, error.code, error.detail, error.field or None)

    # ------------------------------------------------------------ views
    def _queue_view(self):
        return {'items': self.queue.list(), 'paused': self.queue.paused(), 'running': sorted(self.driver.running),
                'global_cap': self.driver.global_cap, 'driver_alive': self.driver.alive}

    def _slots_view(self):
        rows = self.queue.slot_rows()
        state = self.slots.state()
        views = []
        for slot in self.slots.load():
            blocked = self.slots.blocked_until(slot['id'])
            views.append({**slot, 'cli_found': self.executor.command(slot['kind']) is not None,
                          'blocked_until': slot_state_until(state, slot['id']) if blocked else None,
                          'last_limit_at': rows.get(slot['id'], {}).get('last_limit_at'),
                          'active_count': rows.get(slot['id'], {}).get('active_count', 0),
                          'cap': self.driver._cap(slot['id'])})
        return {'slots': views}

    # ------------------------------------------------------------ routes
    # ------------------------------------------------------------ tracking and live log
    @staticmethod
    def _param(query, name):
        values = query.get(name) or []
        return values[0] if values else None

    def _int(self, query, name, default, low, high):
        raw = self._param(query, name)
        if raw in (None, ''):
            return default
        try:
            return max(low, min(high, int(raw)))
        except ValueError:
            raise QueueError('invalid_request', f'{name} must be an integer') from None

    def tracker(self, query):
        slot = self._param(query, 'slot') or self._param(query, 'provider') or None
        return self.queue.tracker(
            status=self._param(query, 'status') or None, slot=slot, kind=self._param(query, 'kind') or None,
            since=self._param(query, 'since') or None, until=self._param(query, 'until') or None,
            q=self._param(query, 'q') or None, limit=self._int(query, 'limit', 50, 1, TRACKER_MAX),
            offset=self._int(query, 'offset', 0, 0, 10**9))

    def log_view(self, run_id, after=0, limit=run_log.DEFAULT_LIMIT, wait=0.0) -> dict:
        """Events and raw text after byte offset ``after``; with ``wait`` > 0 blocks (polling) until something new,
        the run is over, or the time is up."""
        deadline = time.monotonic() + max(0.0, min(MAX_WAIT, wait))
        path = self.executor.log_path(run_id)
        while True:
            item = self.queue.get(run_id)       # 404 for an unknown id
            final = item['state'] in TERMINAL
            last = self.queue.last_attempt(run_id)
            kind = (self.slots.get(last['slot_id']) or {}).get('kind', 'claude') if last else 'claude'
            chunk = run_log.read_chunk(path, after, limit, final=final, kind=kind)
            if chunk['raw'] or final or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        return {'run_id': run_id, 'status': item['state'], 'offset': chunk['offset'], 'next_offset': chunk['next_offset'],
                'size': chunk['size'], 'raw': chunk['raw'], 'events': chunk['events'],
                'done': final and chunk['next_offset'] >= chunk['size']}

    def stream(self, run_id, after=0, *, heartbeat=15.0, max_seconds=600.0):
        """Generator for SSE: ``('log', view)`` for new data, ``None`` for a heartbeat, ``('done', view)`` last."""
        self.queue.get(run_id)      # unknown id raises now, before any generator work
        return self._stream(run_id, run_log.clamp_offset(after), heartbeat, max_seconds)

    def _stream(self, run_id, after, heartbeat, max_seconds):
        end = time.monotonic() + max_seconds
        while time.monotonic() < end:
            view = self.log_view(run_id, after, wait=heartbeat)
            after = view['next_offset']
            if view['done']:
                yield 'done', view
                return
            yield ('log', view) if view['raw'] else None

    def _log_route(self, match, method, query):
        if method != 'GET':
            return _err(405, 'method_not_allowed')
        if match.group(2):      # SSE is served by the HTTP handler (needs the socket); the JSON route does not exist
            return _err(404, 'not_found')
        return 200, self.log_view(match.group(1), self._param(query, 'after'),
                                  run_log.clamp_limit(self._param(query, 'limit') or run_log.DEFAULT_LIMIT),
                                  self._wait(query))

    @staticmethod
    def _wait(query):
        try:
            raw = float((query.get('wait') or ['0'])[0] or 0)
        except ValueError:
            return 0.0
        return raw if 0 <= raw <= MAX_WAIT else (MAX_WAIT if raw > MAX_WAIT else 0.0)

    def _dispatch(self, method, path, body, query):
        if path == TRACKER:
            return (200, self.tracker(query)) if method == 'GET' else _err(405, 'method_not_allowed')
        match = LOG.match(path)
        if match:
            return self._log_route(match, method, query)
        if path == '/api/studio/slots':
            if method == 'GET':
                return 200, self._slots_view()
            if method == 'PUT':
                if not isinstance(body, dict) or set(body) != {'slots'}:
                    return _err(400, 'invalid_request', 'body must be {"slots": [...]}')
                saved = self.slots.save(body['slots'])
                self.queue.audit('owner', 'slots.update', None, {'slots': [s['id'] for s in saved]})
                self.driver.wake()
                return 200, self._slots_view()
            return _err(405, 'method_not_allowed')
        match = CLEAR.match(path)
        if match:
            if method != 'POST' or body not in ({}, None):
                return _err(405 if method != 'POST' else 400, 'invalid_request', 'POST with body {}')
            if self.slots.get(match.group(1)) is None:
                return _err(404, 'not_found')
            self.slots.unblock(match.group(1))
            self.queue.mirror_slots([match.group(1)], {})
            self.queue.audit('owner', 'slots.clear_limit', None, {'slot': match.group(1)})
            self.driver.wake()
            return 200, self._slots_view()
        if path.startswith('/api/studio/slots/'):
            return _err(404, 'not_found')
        if path == QUEUE:
            if method == 'GET':
                return 200, self._queue_view()
            if method == 'POST':
                return 201, self._add(body)
            return _err(405, 'method_not_allowed')
        if path == QUEUE + '/audit' and method == 'GET':
            return 200, {'entries': self.queue.audit_entries(), 'verify': self.queue.verify_audit()}
        if path in (QUEUE + '/pause', QUEUE + '/resume', QUEUE + '/reorder'):
            if method != 'POST':
                return _err(405, 'method_not_allowed')
            if path.endswith('/reorder'):
                ids = (body or {}).get('ids') if isinstance(body, dict) and set(body) == {'ids'} else None
                if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                    return _err(400, 'invalid_request', 'body must be {"ids": [<every queued id in the new order>]}')
                self.queue.reorder(ids)
            else:
                if body not in ({}, None):
                    return _err(400, 'invalid_request', 'body must be {}')
                self.queue.set_paused(path.endswith('/pause'))
            self.driver.wake()
            return 200, self._queue_view()
        match = ITEM.match(path)
        if match:
            run_id, resume = match.groups()
            if resume:
                if method != 'POST' or body not in ({}, None):
                    return _err(405 if method != 'POST' else 400, 'invalid_request', 'POST with body {}')
                return 200, {'result': self.driver.resume_item(run_id), 'run_id': run_id}
            if method == 'GET':
                return 200, dict(self.queue.get(run_id), track=self.queue.track(run_id))
            if method == 'DELETE':
                return 200, {'result': self.driver.cancel(run_id), 'run_id': run_id}
            return _err(405, 'method_not_allowed')
        return _err(404, 'not_found')

    def _add(self, body):
        allowed = {'prompt', 'workspace', 'slot', 'blueprint_ref'}
        if not isinstance(body, dict) or not {'prompt', 'workspace'} <= set(body) or set(body) - allowed:
            raise QueueError('invalid_request', 'body fields: prompt, workspace, slot?, blueprint_ref?')
        prompt = clean_prompt(body['prompt'])
        try:
            workspace = str(validate_workspace(body['workspace'], self.data_dir))
        except WorkspaceError as error:
            raise QueueError('workspace_invalid', str(error)) from None
        slot = body.get('slot', 'auto')
        if slot not in ('auto', 'auto:claude', 'auto:codex') and (
                not isinstance(slot, str) or self.slots.get(slot) is None):
            raise QueueError('invalid_request', 'slot must be auto, auto:claude, auto:codex or a configured slot id')
        ref = body.get('blueprint_ref')
        if ref is not None and (not isinstance(ref, str) or not BLUEPRINT_REF.match(ref)):
            raise QueueError('invalid_request', 'blueprint_ref format')
        item = self.queue.add(workspace, prompt, slot, ref)
        self.driver.wake()
        return item


def slot_state_until(state, slot_id):
    return (state.get(slot_id) or {}).get('blocked_until')
