"""Studio CE route table (AHS-01.8). Transport-free: ``handle`` returns (status, payload)."""
from __future__ import annotations

import re

from aether.studio import adapter_protocol, capabilities, exportimport, schema
from aether.studio.assist.service import AID_PREFIX, AssistRejected
from aether.studio.executors.adapter import validate_enrollment
from aether.studio.schema import SchemaError, canonical_json, sha256_hex
from aether.studio.store import CasConflict, IdempotencyConflict, NotFound, QueueFull, StoreError, TerminalState

RUN_PATH = re.compile(r'^/api/studio/runs/(run_[0-9A-HJKMNP-TV-Z]{26})(/events|/evidence|/cancel|/audit|/resume)?$')
IDEMPOTENCY_RE = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
DEF_PATH = re.compile(r'^/api/studio/definitions/(hd_[0-9A-HJKMNP-TV-Z]{26})(/archive|/unarchive)?$')
VER_PATH = re.compile(r'^/api/studio/versions/(hv_[0-9A-HJKMNP-TV-Z]{26})$')
APR_PATH = re.compile(r'^/api/studio/approvals/(apr_[0-9A-HJKMNP-TV-Z]{26})$')
ADP_PATH = re.compile(r'^/api/studio/adapters/(adp_[0-9A-HJKMNP-TV-Z]{26})(/grant|/revoke|/probe)?$')
AID_PATH = re.compile(r'^/api/studio/assist/drafts/(aid_[0-9A-HJKMNP-TV-Z]{26})(/save)?$')
BODY_LIMIT = 256 * 1024


def _error(status, code, detail='', field=None):
    return status, {'error': code, 'detail': detail[:300], 'field': field}


def _int_param(query, name, default, lo, hi):
    values = query.get(name)
    if not values:
        return default
    if len(values) != 1 or not re.fullmatch(r'[0-9]{1,9}', values[0]):
        raise SchemaError('invalid_request', name, 'integer required')
    value = int(values[0])
    if not lo <= value <= hi:
        raise SchemaError('invalid_request', name, f'must be {lo}..{hi}')
    return value


class StudioAPI:
    def __init__(self, store, runner, mode, executors, assist, execq=None):
        self.execq = execq      # QueueAPI, only with AETHER_EXEC=cli
        self.assist = assist
        self.store = store
        self.runner = runner
        self.mode = mode
        self.executors = executors

    def routes(self):
        return (self.execq_routes() if self.execq else set()) | {('GET', '/api/studio/mode'), ('GET', '/api/studio/capabilities'), ('GET', '/api/studio/runs'),
                ('GET', '/api/studio/runs/<id>'), ('GET', '/api/studio/runs/<id>/events'),
                ('GET', '/api/studio/runs/<id>/evidence'), ('POST', '/api/studio/runs'),
                ('POST', '/api/studio/runs/<id>/cancel'), ('GET', '/api/studio/runs/<id>/audit'),
                ('POST', '/api/studio/runs/<id>/resume'), ('GET', '/api/studio/definitions'),
                ('POST', '/api/studio/definitions'), ('GET', '/api/studio/definitions/<id>'),
                ('PUT', '/api/studio/definitions/<id>'), ('POST', '/api/studio/definitions/<id>/archive'),
                ('POST', '/api/studio/definitions/<id>/unarchive'), ('GET', '/api/studio/versions/<id>'),
                ('GET', '/api/studio/approvals'), ('POST', '/api/studio/approvals/<id>'),
                ('POST', '/api/studio/validate'), ('POST', '/api/studio/export'), ('POST', '/api/studio/import'),
                ('GET', '/api/studio/adapters'), ('GET', '/api/studio/adapters/<id>'), ('POST', '/api/studio/adapters'),
                ('POST', '/api/studio/adapters/<id>/grant'), ('POST', '/api/studio/adapters/<id>/revoke'),
                ('POST', '/api/studio/adapters/<id>/probe'), ('GET', '/api/studio/assist/status'),
                ('POST', '/api/studio/assist/draft'), ('GET', '/api/studio/assist/drafts/<id>'),
                ('POST', '/api/studio/assist/drafts/<id>/save')}

    def handle(self, method, path, query, body=None, headers=None):
        try:
            return self._dispatch(method, path, query, body, headers or {})
        except SchemaError as error:
            status = 403 if error.code == 'capability_not_allowed' else 400
            return _error(status, error.code, error.detail, error.field)
        except NotFound:
            return _error(404, 'not_found')
        except TerminalState as error:
            return _error(409, 'terminal_state', f'run is {error}')
        except IdempotencyConflict:
            return _error(409, 'idempotency_conflict', 'key reused with a different body')
        except CasConflict as error:
            return _error(409, 'rev_conflict', str(error))
        except QueueFull:
            return _error(429, 'queue_full', 'more than 100 queued runs')
        except AssistRejected as error:
            return _error(error.status, error.code, error.detail)
        except StoreError as error:
            return _error(400, error.code, str(error))

    @staticmethod
    def execq_routes():
        from aether.studio import queue_api
        return queue_api.ROUTES

    def _dispatch(self, method, path, query, body, headers):
        if self.execq is not None and self.execq.owns(path):
            return self.execq.handle(method, path, body, query)
        if method == 'GET':
            if path == '/api/studio/mode':
                return 200, {'mode': self.mode.mode, 'provider': self.mode.provider, 'runtime': self.mode.runtime,
                             'schema_version': schema.SCHEMA_VERSION,
                             'demo_only': not any(a['granted'] for a in self.store.list_adapters()),
                             'executors': {k: e.health() for k, e in sorted(self.executors.items())},
                             'max_workers': self.runner.max_workers}
            if path == '/api/studio/capabilities':
                return 200, {'capabilities': capabilities.describe(self.mode.provider),
                             'adapters': self._adapter_executor().health()['adapters'],
                             'adapter_protocol': adapter_protocol.PROTOCOL}
            if path == '/api/studio/adapters':
                return 200, {'adapters': self._adapters()}
            if path == '/api/studio/assist/status':
                return 200, self.assist.status()
            match = AID_PATH.match(path)
            if match and match.group(2) is None:
                return 200, self.assist.get(match.group(1))
            if path == '/api/studio/runs':
                limit = _int_param(query, 'limit', 50, 1, 200)
                status = (query.get('status') or [None])[0]
                if status is not None and status not in schema.STATUSES:
                    raise SchemaError('invalid_request', 'status', 'unknown status')
                return 200, {'runs': self.store.list_runs(limit, status)}
            if path == '/api/studio/definitions':
                archived = (query.get('archived') or ['0'])[0]
                if archived not in ('0', '1'):
                    raise SchemaError('invalid_request', 'archived', '0 or 1')
                return 200, {'definitions': self.store.list_definitions(include_archived=archived == '1')}
            if path == '/api/studio/approvals':
                status = (query.get('status') or [None])[0]
                if status is not None and status not in ('pending', 'granted', 'denied', 'expired'):
                    raise SchemaError('invalid_request', 'status', 'unknown status')
                return 200, {'approvals': self.store.list_approvals(status)}
            match = DEF_PATH.match(path)
            if match and match.group(2) is None:
                return 200, self.store.get_definition(match.group(1))
            match = VER_PATH.match(path)
            if match:
                return 200, self.store.get_version(match.group(1))
            match = ADP_PATH.match(path)
            if match and match.group(2) is None:
                return 200, self._adapter_view(self.store.get_adapter(match.group(1)))
            match = RUN_PATH.match(path)
            if match and match.group(2) is None:
                return 200, self.store.get_run(match.group(1))
            if match and match.group(2) == '/events':
                cursor = _int_param(query, 'cursor', 0, 0, 10**9)
                limit = _int_param(query, 'limit', 100, 1, 200)
                events, has_more = self.store.events(match.group(1), cursor, limit)
                terminal = self.store.run_row(match.group(1))['status'] in schema.TERMINAL
                return 200, {'events': events, 'next_cursor': events[-1]['seq'] if events else cursor,
                             'has_more': has_more, 'poll_after_ms': 0 if (terminal and not has_more) else 500}
            if match and match.group(2) == '/audit':
                return 200, {'entries': self.store.audit(match.group(1)), 'verify': self.store.verify_audit(match.group(1))}
            if match and match.group(2) == '/evidence':
                run = self.store.get_run(match.group(1))
                evidence = dict(run['evidence'], sealed=run['status'] in schema.TERMINAL, label=run['label'])
                return 200, evidence
            return _error(404, 'not_found')
        if method == 'PUT':
            match = DEF_PATH.match(path)
            if match and match.group(2) is None:
                return 200, self._update_definition(match.group(1), body, headers)
            return _error(404, 'not_found')
        if method == 'POST':
            if path == '/api/studio/runs':
                return self._submit(body, headers)
            if path == '/api/studio/definitions':
                return 201, self._create_definition(body)
            if path == '/api/studio/validate':
                return 200, self._validate(body)
            if path == '/api/studio/assist/draft':
                return self.assist.draft(body)
            match = AID_PATH.match(path)
            if match and match.group(2):
                return 200, self.assist.save(match.group(1), body)
            if path == '/api/studio/export':
                return 200, self._export(body)
            if path == '/api/studio/import':
                return 201, exportimport.import_bundle(self.store, body, provider=self.mode.provider)
            if path == '/api/studio/adapters':
                return 201, self._enroll(body)
            match = ADP_PATH.match(path)
            if match and match.group(2):
                return 200, self._adapter_action(match.group(1), match.group(2)[1:], body)
            match = DEF_PATH.match(path)
            if match and match.group(2):
                if body:
                    raise SchemaError('invalid_request', '', 'body must be {}')
                rev = self._if_match(headers, required=False)
                return 200, self.store.set_archived(match.group(1), match.group(2) == '/archive', rev)
            match = APR_PATH.match(path)
            if match:
                return 200, self._decide(match.group(1), body)
            match = RUN_PATH.match(path)
            if match and match.group(2) in ('/cancel', '/resume'):
                if body not in ({}, {'confirm': match.group(1)}):
                    raise SchemaError('invalid_request', '', 'body must be {} or {"confirm": "<run id>"}')
                if match.group(2) == '/resume':
                    return 200, {'result': self.runner.resume(match.group(1)), 'run_id': match.group(1)}
                result = self.runner.cancel(match.group(1))
                return (202 if result == 'cancel_requested' else 200), {'result': result, 'run_id': match.group(1)}
            return _error(404, 'not_found')
        return _error(405, 'method_not_allowed')

    @staticmethod
    def _if_match(headers, *, required):
        raw = headers.get('If-Match')
        if raw is None:
            if required:
                raise SchemaError('invalid_request', 'If-Match', 'header required: current rev')
            return None
        raw = raw.strip().strip('"')
        if not re.fullmatch(r'[0-9]{1,9}', raw):
            raise SchemaError('invalid_request', 'If-Match', 'integer rev required')
        return int(raw)

    def _meta(self, body, *, name_required):
        name = body.get('name')
        if name is None and name_required:
            raise SchemaError('schema_invalid', '/name', 'required field missing')
        if name is not None:
            schema._str(name, '/name', 1, 80)
        description = body.get('description')
        if description is not None:
            schema._str(description, '/description', 0, 2000)
        return name, description

    def _create_definition(self, body):
        unknown = set(body) - {'name', 'description', 'spec'}
        if unknown:
            raise SchemaError('unknown_field', '/' + sorted(unknown)[0])
        if 'spec' not in body:
            raise SchemaError('schema_invalid', '/spec', 'required field missing')
        name, description = self._meta(body, name_required=True)
        spec = schema.validate_spec(body['spec'], provider=self.mode.provider)
        self.assist.guard_unsaved(spec)
        return self.store.create_definition(name, description or '', spec)

    def _update_definition(self, def_id, body, headers):
        rev = self._if_match(headers, required=True)
        unknown = set(body) - {'name', 'description', 'spec'}
        if unknown:
            raise SchemaError('unknown_field', '/' + sorted(unknown)[0])
        if not body:
            raise SchemaError('invalid_request', '', 'name, description or spec required')
        name, description = self._meta(body, name_required=False)
        spec = schema.validate_spec(body['spec'], provider=self.mode.provider) if 'spec' in body else None
        if spec is not None:
            self.assist.guard_unsaved(spec)
        return self.store.update_definition(def_id, rev, name=name, description=description, spec=spec)

    def _validate(self, body):
        """Dry-run validation: same rules as create/submit, nothing is stored."""
        unknown = set(body) - {'spec'}
        if unknown:
            raise SchemaError('unknown_field', '/' + sorted(unknown)[0])
        if 'spec' not in body:
            raise SchemaError('schema_invalid', '/spec', 'required field missing')
        spec = schema.validate_spec(body['spec'], provider=self.mode.provider)
        return {'valid': True, 'spec': spec, 'spec_sha256': schema.spec_sha256(spec),
                'stage_order': schema.topo_order(spec['stages'])}

    def _export(self, body):
        unknown = set(body) - {'definition_ids'}
        if unknown:
            raise SchemaError('unknown_field', '/' + sorted(unknown)[0])
        ids = body.get('definition_ids')
        if ids is not None:
            if not isinstance(ids, list) or not ids or len(ids) > 100 or not all(
                    isinstance(i, str) and schema.ID_RE.match(i) and i.startswith('hd_') for i in ids):
                raise SchemaError('invalid_request', '/definition_ids', 'list of definition ids required')
        return exportimport.export_bundle(self.store, ids)

    # ------------------------------------------------------------ adapters (AHS-04)
    def _adapter_executor(self):
        return self.executors['adapter']

    def _adapter_view(self, adapter, health=None):
        """Owner view of an enrollment. Script path/args are shown to the owner; never part of any export."""
        if health is None:
            health = {a['id']: a for a in self._adapter_executor().health()['adapters']}.get(adapter['id'])
        view = dict(adapter)
        view['health'] = None if health is None else {k: health[k] for k in ('status', 'observed_utc', 'detail', 'scopes')}
        return view

    def _adapters(self):
        health = {a['id']: a for a in self._adapter_executor().health()['adapters']}
        return [self._adapter_view(a, health.get(a['id'])) for a in self.store.list_adapters(include_revoked=True)]

    def _enroll(self, body):
        item = validate_enrollment(body, self.mode.data_dir)
        adapter = self.store.enroll_adapter(item['name'], item['script'], item['args'], item['sha256'])
        return self._adapter_view(adapter)

    def _adapter_action(self, adapter_id, action, body):
        executor = self._adapter_executor()
        if action == 'probe':
            if body:
                raise SchemaError('invalid_request', '', 'body must be {}')
            adapter = self.store.get_adapter(adapter_id)
            executor.probe(adapter)
            return self._adapter_view(adapter)
        if action == 'revoke':
            if body != {'confirm': adapter_id}:
                raise SchemaError('invalid_request', '', 'body must be {"confirm": "<adapter id>"}')
            adapter = self.store.revoke_adapter(adapter_id)
            stopped = executor.revoke(adapter_id)
            return dict(self._adapter_view(adapter), sessions_stopped=stopped)
        if set(body) != {'scopes', 'confirm'} or body['confirm'] != adapter_id:
            raise SchemaError('invalid_request', '', 'body must be {"scopes": [...], "confirm": "<adapter id>"}')
        scopes = body['scopes']
        if not isinstance(scopes, list) or len(set(map(str, scopes))) != len(scopes) or not all(
                isinstance(s, str) and s in adapter_protocol.KNOWN_CAPABILITIES for s in scopes):
            raise SchemaError('invalid_request', '/scopes', 'unique subset of ' + '|'.join(adapter_protocol.KNOWN_CAPABILITIES))
        adapter = self.store.grant_adapter(adapter_id, scopes)
        executor.forget_contact(adapter_id)
        return self._adapter_view(adapter)

    def _decide(self, approval_id, body):
        if set(body) != {'decision', 'confirm'} or body['decision'] not in ('grant', 'deny') \
                or not isinstance(body['confirm'], str):
            raise SchemaError('invalid_request', '', 'body must be {"decision": "grant"|"deny", "confirm": "<run id>"}')
        return self.store.decide_approval(approval_id, body['decision'], body['confirm'])

    def _submit(self, body, headers):
        key = headers.get('Idempotency-Key')
        if not isinstance(key, str) or not IDEMPOTENCY_RE.match(key):
            raise SchemaError('invalid_request', 'Idempotency-Key', 'header required: [A-Za-z0-9._:-]{1,128}')
        allowed = {'spec', 'name', 'version_id', 'definition_id', 'overrides', 'relaunch_of'}
        unknown = set(body) - allowed
        if unknown:
            raise SchemaError('unknown_field', '/' + sorted(unknown)[0])
        if sum(k in body for k in ('spec', 'version_id', 'definition_id')) != 1:
            raise SchemaError('invalid_request', '', 'exactly one of spec, version_id or definition_id')
        overrides = body.get('overrides', {})
        relaunch_of = body.get('relaunch_of')
        if relaunch_of is not None:
            if not isinstance(relaunch_of, str) or not schema.ID_RE.match(relaunch_of) or not relaunch_of.startswith('run_'):
                raise SchemaError('invalid_request', '/relaunch_of', 'run id required')
            self.store.run_row(relaunch_of)
        if 'spec' in body:
            name = body.get('name', 'Inline DEMO harness')
            schema._str(name, '/name', 1, 80)
            spec = schema.validate_spec(body['spec'], provider=self.mode.provider)
            self.assist.guard_unsaved(spec)
            version_id = self.store.ensure_inline_version(name, spec)
        else:
            if any(isinstance(body.get(k), str) and body[k].startswith(AID_PREFIX) for k in ('version_id', 'definition_id')):
                raise SchemaError('ai_draft_not_runnable', '/version_id', 'an AI draft is not a blueprint version; save it as a blueprint first')
            if 'name' in body:
                raise SchemaError('invalid_request', '/name', 'name only with inline spec')
            if 'definition_id' in body:
                def_id = body['definition_id']
                if not isinstance(def_id, str) or not schema.ID_RE.match(def_id) or not def_id.startswith('hd_'):
                    raise SchemaError('invalid_request', '/definition_id', 'definition id required')
                body_version = self.store.latest_version_id(def_id)
            else:
                body_version = body['version_id']
            version_id = body_version
            if not isinstance(version_id, str) or not schema.ID_RE.match(version_id) or not version_id.startswith('hv_'):
                raise SchemaError('invalid_request', '/version_id', 'version id required')
            spec = schema.validate_spec(self.store.get_version(version_id)['spec'], provider=self.mode.provider)
        snapshot = schema.apply_overrides(spec, overrides, provider=self.mode.provider)
        run_id, created = self.store.submit(version_id, snapshot, overrides, idempotency_key=key,
                                            request_sha256=sha256_hex(canonical_json(body)), relaunch_of=relaunch_of)
        self.runner.notify()
        run = self.store.get_run(run_id)
        run['idempotent_replay'] = not created
        return (201 if created else 200), run
