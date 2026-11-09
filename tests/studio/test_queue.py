"""Queue store + HTTP API (AETHER_EXEC=cli) and flag-off parity."""
import json
import os
from pathlib import Path
import sys
import threading
import unittest

from aether.studio.exec_queue import QueueError, QueueStore, TABLES

from tests.studio import support

FAKES = Path(__file__).resolve().parents[1] / 'fakes'


class QueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.data = support.tempdir(self)
        self.s = support.store(self, self.data / 'studio.sqlite3')
        self.queue = QueueStore(self.s, self.data)

    def test_tables_only_after_ensure(self):
        self.assertEqual(QueueStore.tables_exist(self.s), [])
        self.queue.ensure()
        self.queue.ensure()                                   # idempotent
        self.assertEqual(sorted(QueueStore.tables_exist(self.s)), sorted(TABLES))

    def test_add_list_get_and_prompt_file(self):
        self.queue.ensure()
        ws = support.tempdir(self)
        item = self.queue.add(str(ws), 'do the thing', 'auto', 'hv_x')
        self.assertEqual((item['state'], item['position'], item['requested_slot'], item['blueprint_ref']),
                         ('queued', 1, 'auto', 'hv_x'))
        self.assertEqual(self.queue.prompt(item), 'do the thing')
        self.assertNotIn('do the thing', json.dumps(item))             # the prompt is not in the row
        if os.name != 'nt':
            self.assertEqual((self.data / 'queue-prompts' / item['prompt_ref']).stat().st_mode & 0o077, 0)
        second = self.queue.add(str(ws), 'two')
        self.assertEqual([i['run_id'] for i in self.queue.list()], [item['run_id'], second['run_id']])
        with self.assertRaises(QueueError) as ctx:
            self.queue.get('que_missing')
        self.assertEqual(ctx.exception.status, 404)

    def test_audit_entries_have_no_prompt_text(self):
        self.queue.ensure()
        self.queue.add(str(support.tempdir(self)), 'private words here')
        text = json.dumps(self.queue.audit_entries())
        self.assertNotIn('private words', text)
        self.assertTrue(self.queue.verify_audit()['ok'])


class ApiBase(unittest.TestCase):
    env = {'AETHER_EXEC': 'cli'}

    def setUp(self):
        self.app, self.client = support.serve(self, exec_env=self.env)
        self.root = support.tempdir(self)
        self.ws = self.root / 'ws'
        self.ws.mkdir()

    def call(self, method, path, body=None):
        headers = {'Origin': self.client.origin} if method == 'DELETE' else None
        return self.client.request(method, '/api/studio' + path, body, headers=headers)

    def use_fakes(self):
        self.app.driver.executor.commands['claude'] = [sys.executable, str(FAKES / 'fake_claude.py')]

    def put_slots(self, *ids):
        slots = [{'id': sid, 'kind': 'claude', 'label': sid, 'config_dir': str(self.root / f'cfg-{sid}'), 'order': i}
                 for i, sid in enumerate(ids)]
        return self.call('PUT', '/slots', {'slots': slots})


class QueueApiTests(ApiBase):
    def test_slots_round_trip_and_no_secrets(self):
        status, body = self.call('GET', '/slots')
        self.assertEqual((status, body), (200, {'slots': []}))
        status, body = self.put_slots('claude-a', 'claude-b')
        self.assertEqual(status, 200)
        self.assertEqual([s['id'] for s in body['slots']], ['claude-a', 'claude-b'])
        self.assertEqual(body['slots'][0]['active_count'], 0)
        self.assertIsNone(body['slots'][0]['blocked_until'])
        self.assertEqual(self.call('GET', '/slots')[1]['slots'][1]['cap'], 1)
        text = json.dumps(body).lower()
        for word in ('token', 'secret', 'password', 'api_key'):
            self.assertNotIn(word, text)

    def test_slots_validation(self):
        status, body = self.call('PUT', '/slots', {'slots': [{'id': 'a', 'kind': 'claude', 'label': 'x',
                                                             'config_dir': str(self.root / 'c'), 'token': 'abc'}]})
        self.assertEqual((status, body['error']), (400, 'unknown_field'))
        self.assertEqual(self.call('PUT', '/slots', {'slots': 'no'})[0], 400)
        self.assertEqual(self.call('PUT', '/slots', {'other': 1})[0], 400)
        self.assertEqual(self.call('GET', '/slots')[1], {'slots': []})

    def test_clear_limit(self):
        self.put_slots('claude-a')
        from datetime import datetime, timedelta, timezone
        self.app.driver.slots.block('claude-a', datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertIsNotNone(self.call('GET', '/slots')[1]['slots'][0]['blocked_until'])
        status, body = self.call('POST', '/slots/claude-a/clear-limit', {})
        self.assertEqual(status, 200)
        self.assertIsNone(body['slots'][0]['blocked_until'])
        self.assertEqual(self.call('POST', '/slots/nope/clear-limit', {})[0], 404)
        self.assertIn('slots.clear_limit', [e['action'] for e in self.app.driver.queue.audit_entries()])

    def test_queue_round_trip_pause_reorder_cancel(self):
        self.put_slots('claude-a')
        self.assertEqual(self.call('POST', '/queue/pause', {})[1]['paused'], True)
        ids = []
        for text in ('one', 'two', 'three'):
            status, item = self.call('POST', '/queue', {'prompt': text, 'workspace': str(self.ws)})
            self.assertEqual((status, item['state'], item['requested_slot']), (201, 'queued', 'auto'))
            ids.append(item['run_id'])
        status, body = self.call('GET', '/queue')
        self.assertEqual([i['run_id'] for i in body['items']], ids)
        status, body = self.call('POST', '/queue/reorder', {'ids': ids[::-1]})
        self.assertEqual([i['run_id'] for i in body['items']], ids[::-1])
        self.assertEqual(self.call('POST', '/queue/reorder', {'ids': ids[:2]})[0], 400)
        self.assertEqual(self.call('GET', f'/queue/{ids[0]}')[1]['state'], 'queued')
        status, body = self.call('DELETE', f'/queue/{ids[0]}')
        self.assertEqual((status, body['result']), (200, 'cancelled'))
        self.assertEqual(self.call('DELETE', f'/queue/{ids[0]}')[0], 409)
        self.assertEqual(self.call('DELETE', '/queue/que_00000000000000000000000000')[0], 404)
        entries = self.call('GET', '/queue/audit')[1]
        self.assertTrue(entries['verify']['ok'])
        actions = [e['action'] for e in entries['entries']]
        for action in ('queue.pause', 'queue.add', 'queue.reorder', 'queue.cancel', 'slots.update'):
            self.assertIn(action, actions)

    def test_post_validation(self):
        self.put_slots('claude-a')
        good = {'prompt': 'x', 'workspace': str(self.ws)}
        for bad in ({'workspace': str(self.ws)}, {'prompt': 'x'}, dict(good, extra=1), dict(good, prompt=''),
                    dict(good, workspace=str(self.root / 'missing')), dict(good, workspace='relative'),
                    dict(good, workspace=str(self.app.mode.data_dir)), dict(good, slot='nope'),
                    dict(good, blueprint_ref='bad ref!'), dict(good, prompt='key sk-ant-api03-' + 'a' * 40)):
            self.assertEqual(self.call('POST', '/queue', bad)[0], 400, bad)
        self.assertEqual(self.call('POST', '/queue', dict(good, slot='claude-a'))[0], 201)
        self.assertEqual(self.call('POST', '/queue', dict(good, slot='auto:claude'))[0], 201)

    def test_auth_origin_and_csrf_conventions(self):
        self.assertEqual(self.client.request('GET', '/api/studio/queue', auth=False)[0], 401)
        self.assertEqual(self.client.request('POST', '/api/studio/queue/pause', {}, headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.client.request('DELETE', '/api/studio/queue/que_00000000000000000000000000')[0], 403)   # no Origin
        self.assertEqual(self.client.request('PUT', '/api/studio/queue', {}, headers={'Origin': self.client.origin})[0], 405)

    def test_driver_runs_item_end_to_end(self):
        self.use_fakes()
        self.put_slots('claude-a', 'claude-b')
        self.app.driver.start()
        self.addCleanup(self.app.driver.stop)
        status, item = self.call('POST', '/queue', {'prompt': 'success', 'workspace': str(self.ws)})
        support.wait_for(lambda: self.call('GET', f'/queue/{item["run_id"]}')[1]['state'] == 'done', timeout=30)
        got = self.call('GET', f'/queue/{item["run_id"]}')[1]
        self.assertEqual(got['attempts'][0]['slot_id'], 'claude-a')
        self.assertTrue((self.ws / 'fake_output.txt').exists())
        self.assertEqual(self.call('POST', '/queue/resume', {})[1]['paused'], False)


class FlagOffTests(unittest.TestCase):
    def test_off_means_no_tables_thread_or_routes(self):
        app, client = support.serve(self, exec_env={})
        self.assertIsNone(app.driver)
        self.assertEqual(QueueStore.tables_exist(app.store), [])
        self.assertNotIn('exec-queue-driver', {t.name for t in threading.enumerate()})
        self.assertNotIn('cli', app.api.executors)
        self.assertFalse((app.mode.data_dir / 'queue-prompts').exists())
        for method, path in (('GET', '/queue'), ('GET', '/slots'), ('GET', '/queue/audit')):
            self.assertEqual(client.request(method, '/api/studio' + path)[0], 404, path)
        origin = {'Origin': client.origin}
        self.assertEqual(client.request('POST', '/api/studio/queue', {'a': 1}, headers=origin)[0], 404)
        self.assertEqual(client.request('PUT', '/api/studio/slots', {'slots': []}, headers=origin)[0], 404)
        self.assertEqual(client.request('DELETE', '/api/studio/queue/que_00000000000000000000000000', headers=origin)[0], 405)
        self.assertFalse(any(r[1].startswith('/api/studio/queue') for r in app.api.routes()))

    def test_on_registers_everything(self):
        app, _ = support.serve(self, exec_env={'AETHER_EXEC': 'cli', 'AETHER_EXEC_MAX_RUNS': '3'})
        self.assertIsNotNone(app.driver)
        self.assertEqual(app.driver.global_cap, 3)
        self.assertFalse(app.driver.alive)               # support.serve does not start it; app.main does
        self.assertEqual(sorted(QueueStore.tables_exist(app.store)), sorted(TABLES))


if __name__ == '__main__':
    unittest.main()


class OrchestratorLimitStopTests(unittest.TestCase):
    """K1 gap: a ``limit_stop`` poll result blocks the stage and run (owner_action_required) instead of crashing."""

    def test_limit_stop_blocks_run_and_resume_reruns(self):
        from aether.studio.executors.mock import MockExecutor

        class Limited(MockExecutor):
            first = True

            def poll(self, handle):
                if Limited.first:
                    Limited.first = False
                    return {'state': 'limit_stop', 'exit_code': None, 'outcome': 'limit_stop', 'slot': 'claude-a',
                            'blocked_until': '2030-01-01T00:00:00Z'}
                return super().poll(handle)

        s = support.store(self)
        runner = support.runner(self, s, executor=Limited())
        run_id = support.submit(s)
        run = support.wait_status(s, run_id, 'blocked', 'failed', 'succeeded')
        self.assertEqual((run['status'], run['reason']), ('blocked', 'owner_action_required'))
        stage = [r for r in s.stages(run_id)][-1]
        self.assertEqual(stage['status'], 'blocked')
        audit = [e for e in s.audit(run_id) if e['action'] == 'stage.blocked']
        self.assertEqual(audit[-1]['detail']['outcome'], 'limit_stop')
        runner.resume(run_id)
        self.assertEqual(support.wait_status(s, run_id, 'succeeded', 'failed')['status'], 'succeeded')
