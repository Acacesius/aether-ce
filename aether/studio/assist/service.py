"""Assist service: status, bounded/rate-limited drafting, proposed-draft storage, acknowledged save."""
from __future__ import annotations

import json
import threading
import time

from aether.studio import schema
from aether.studio.assist import ceiling as ceiling_module, pipeline, review as review_module
from aether.studio.assist.provider import ProviderError
from aether.studio.schema import SchemaError, canonical_json, new_id, sha256_hex
from aether.studio.store import NotFound

RATE_WINDOW_SECONDS = 60.0
RATE_MAX = 6
KEEP_PROPOSED = 50
AID_PREFIX = 'aid_'
SAVE_NOTE = ' [AI-assisted draft {id}, saved after human permissions review]'


class AssistRejected(Exception):
    """HTTP-shaped refusal (rate limit, busy, unavailable)."""

    def __init__(self, status, code, detail):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


class Assist:
    def __init__(self, store, mode, provider, reason, *, clock=time.monotonic):
        assert (provider is None) == bool(reason), 'reason is set exactly when there is no provider'
        self.store, self.mode, self.provider, self.reason, self.clock = store, mode, provider, reason, clock
        self._lock = threading.Lock()       # one model call at a time, bounded request rate
        self._stamps: list[float] = []

    def status(self) -> dict:
        if self.provider is None:
            return {'available': False, 'detail': 'unavailable: ' + self.reason}
        return {'available': True, 'provider': self.provider.name, 'model': self.provider.model,
                'detail': f'local model {self.provider.model} on a loopback address', 'max_text': pipeline.MAX_TEXT}

    # ------------------------------------------------------------ draft
    def draft(self, body) -> tuple[int, dict]:
        if set(body) - {'text', 'context'} or 'text' not in body:
            raise SchemaError('invalid_request', '', 'body must be {"text": str, "context": {...}?}')
        if self.provider is None:
            raise AssistRejected(503, 'assist_unavailable', 'unavailable: ' + self.reason)
        pipeline.check_text(body['text'])
        ceiling = ceiling_module.build(body.get('context', {}), provider=self.mode.provider,
                                       enrolled_adapters={a['name'] for a in self.store.list_adapters()})
        if not self._lock.acquire(blocking=False):
            raise AssistRejected(429, 'assist_busy', 'another draft is in progress')
        try:
            now = self.clock()
            self._stamps = [t for t in self._stamps if now - t < RATE_WINDOW_SECONDS]
            if len(self._stamps) >= RATE_MAX:
                raise AssistRejected(429, 'rate_limited', f'at most {RATE_MAX} drafts per {int(RATE_WINDOW_SECONDS)} seconds')
            self._stamps.append(now)
            try:
                result = pipeline.draft(self.provider, body['text'], ceiling, mode_provider=self.mode.provider)
            except ProviderError as error:
                raise AssistRejected(502, 'provider_' + error.code, error.detail) from None
        finally:
            self._lock.release()
        if not result['ok']:
            return 422, {'error': 'draft_invalid', 'detail': 'the model output failed validation; nothing was stored',
                         'errors': result['errors'], 'attempts': result['attempts'], 'label': 'AI-PROPOSED, not yet reviewed'}
        return 201, self._store(result)

    def _store(self, result) -> dict:
        draft_id = new_id('aid', now=self.store.clock())
        spec = result['spec']
        with self.store.tx() as db:
            db.execute('INSERT INTO assist_drafts(id, kind, status, provider, model, prompt_sha256, created_utc, name, '
                       'description, spec_json, spec_sha256, review_json, review_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (draft_id, 'ai_draft', 'proposed', self.provider.name, self.provider.model, result['prompt_sha256'],
                        self.store.now(), result['name'], result['description'], canonical_json(spec), schema.spec_sha256(spec),
                        canonical_json(result['review']), result['review']['review_sha256']))
            db.execute("DELETE FROM assist_drafts WHERE status='proposed' AND id NOT IN "
                       "(SELECT id FROM assist_drafts WHERE status='proposed' ORDER BY id DESC LIMIT ?)", (KEEP_PROPOSED,))
        return self.get(draft_id)

    # ------------------------------------------------------------ read / guards
    def get(self, draft_id: str) -> dict:
        with self.store.tx() as db:
            row = db.execute('SELECT * FROM assist_drafts WHERE id=?', (draft_id,)).fetchone()
        if row is None:
            raise NotFound(draft_id)
        return {'id': row['id'], 'kind': row['kind'], 'status': row['status'], 'provider': row['provider'],
                'model': row['model'], 'prompt_sha256': row['prompt_sha256'], 'created_utc': row['created_utc'],
                'name': row['name'], 'description': row['description'], 'spec': json.loads(row['spec_json']),
                'spec_sha256': row['spec_sha256'], 'review': json.loads(row['review_json']),
                'definition_id': row['definition_id'], 'runnable': False}

    def guard_unsaved(self, spec: dict) -> None:
        """A validated spec identical to a still-proposed AI draft is not a blueprint: it cannot be run or saved
        through the ordinary routes, only through the acknowledged save."""
        with self.store.tx() as db:
            row = db.execute("SELECT id FROM assist_drafts WHERE spec_sha256=? AND status='proposed'",
                             (schema.spec_sha256(spec),)).fetchone()
        if row is not None:
            raise SchemaError('ai_draft_not_runnable', '/spec', f'{row["id"]} is an unreviewed AI draft; '
                              'use "Save as blueprint" with the permissions acknowledgement first')

    # ------------------------------------------------------------ save
    def save(self, draft_id: str, body) -> dict:
        if set(body) != {'name', 'description', 'spec', 'acknowledge_review', 'confirm'}:
            raise SchemaError('invalid_request', '', 'body must be {name, description, spec, acknowledge_review, confirm}')
        draft = self.get(draft_id)
        if body['confirm'] != draft_id:
            raise SchemaError('invalid_request', '/confirm', 'must equal the draft id')
        if body['acknowledge_review'] != draft['review']['review_sha256']:
            raise SchemaError('acknowledgement_required', '/acknowledge_review',
                              'acknowledge the permissions review by echoing its review_sha256')
        if draft['status'] != 'proposed':
            raise SchemaError('invalid_request', '', 'draft was already saved')
        schema._str(body['name'], '/name', 1, 80)
        schema._str(body['description'], '/description', 0, 2000 - len(SAVE_NOTE.format(id=draft_id)))
        spec = schema.validate_spec(body['spec'], provider=self.mode.provider)
        note = SAVE_NOTE.format(id=draft_id)
        with self.store.tx() as db:
            definition_id, _ = self.store._insert_definition(db, body['name'], body['description'] + note, [spec])
            db.execute("UPDATE assist_drafts SET status='saved', definition_id=? WHERE id=? AND status='proposed'",
                       (definition_id, draft_id))
            assert db.execute('SELECT changes()').fetchone()[0] == 1, 'draft was not proposed'
        saved = self.store.get_definition(definition_id)
        return {'definition': saved, 'draft_id': draft_id,
                'review_of_saved_spec': {'risk': review_module.risk(spec), 'permissions': review_module.permissions(spec)}}
