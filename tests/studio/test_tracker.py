"""K3: tracking records, live log offsets / long-poll / SSE, tracker filters and totals (fake CLIs)."""
import hashlib
import http.client
import json
import threading
import time
import unittest

from aether.studio import run_log
from aether.studio.queue_api import QueueAPI
from tests.studio import support
from tests.studio.test_driver import DriverBase
from tests.studio.test_queue import ApiBase


class TrackBase(DriverBase):
    def setUp(self):
        super().setUp()
        self.api = QueueAPI(self.queue, self.driver, self.slots, self.executor, self.data)

    def get(self, path, **query):
        return self.api.handle('GET', path, None, {k: [str(v)] for k, v in query.items()})


class RecordTests(TrackBase):
    def test_success_record_has_reported_figures_and_evidence(self):
        run_id = self.add('success')
        self.run_until(run_id, 'done')
        support.wait_for(lambda: self.queue.track(run_id)['evidence']['log_sha256'])
        t = self.queue.track(run_id)
        self.assertEqual(t['status'], 'done')
        self.assertEqual(t['provider'], {'slot': 'claude-a', 'kind': 'claude'})
        self.assertEqual([(a['slot'], a['outcome']) for a in t['attempts']], [('claude-a', 'ok')])
        self.assertTrue(t['started_at'] and t['ended_at'])
        self.assertGreaterEqual(t['wall_seconds'], 0)
        self.assertEqual(t['cost_usd_cents'], 1)
        self.assertIsInstance(t['tokens'], int)
        raw = self.executor.log_path(run_id).read_bytes()
        self.assertEqual(t['evidence']['log_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(t['evidence']['result_sha256'], hashlib.sha256(b'Done: wrote fake_output.txt').hexdigest())

    def test_usage_is_null_when_cli_reports_none(self):
        run_id = self.add('error_result')
        self.run_until(run_id, 'failed')
        t = self.queue.track(run_id)
        self.assertIsNone(t['tokens'])
        self.assertIsNone(t['cost_usd_cents'])
        self.assertEqual(t['attempts'][0]['outcome'], 'failed')
        self.assertEqual(self.queue.tracker()['summary']['tokens'], {'sum': None, 'reported_count': 0})

    def test_switch_recorded_after_limit_stop_and_audit_chain(self):
        run_id = self.add('limit_first', ws=self.workspace('shared'))
        self.run_until(run_id, 'done')
        t = self.queue.track(run_id)
        self.assertEqual([(a['slot'], a['outcome']) for a in t['attempts']], [('claude-a', 'limit_stop'), ('claude-b', 'ok')])
        self.assertEqual([(s['from'], s['to'], s['reason']) for s in t['provider_switches']],
                         [('claude-a', 'claude-b', 'limit_stop')])
        self.assertEqual(t['provider']['slot'], 'claude-b')
        self.assertTrue(self.queue.verify_audit()['ok'])
        actions = [e['action'] for e in self.queue.audit_entries()]
        self.assertIn('track.evidence', actions)

    def test_cancelled_outcome(self):
        run_id = self.add('hold 30')
        self.driver.tick()
        support.wait_for(lambda: self.queue.last_attempt(run_id) is not None)
        self.driver.cancel(run_id)
        support.wait_for(lambda: (self.driver.tick(), self.queue.track(run_id)['attempts'][0]['outcome'])[1] == 'cancelled')
        self.assertEqual(self.queue.track(run_id)['status'], 'cancelled')

    def test_migration_adds_columns_to_old_attempts_table(self):
        db = self.s._db
        with self.s.tx() as tx:
            tx.execute('DROP TABLE queue_track')
            tx.execute('ALTER TABLE queue_attempts DROP COLUMN tokens')
        self.queue.ensure()
        names = {r['name'] for r in db.execute('PRAGMA table_info(queue_attempts)')}
        self.assertIn('tokens', names)


class LogChunkTests(unittest.TestCase):
    def setUp(self):
        self.path = support.tempdir(self) / 'x.log'

    def test_never_splits_a_line_and_resumes(self):
        self.path.write_bytes(b'one\ntwo\nthr')
        a = run_log.read_chunk(self.path, 0, 1000)
        self.assertEqual(a['raw'], 'one\ntwo\n')
        self.assertEqual(a['next_offset'], 8)
        with open(self.path, 'ab') as f:
            f.write(b'ee\n')
        b = run_log.read_chunk(self.path, a['next_offset'], 1000)
        self.assertEqual((b['raw'], b['next_offset']), ('three\n', 14))

    def test_final_delivers_unterminated_tail(self):
        self.path.write_bytes(b'one\ntail')
        self.assertEqual(run_log.read_chunk(self.path, 0, 100, final=True)['raw'], 'one\ntail\n')

    def test_small_limit_still_returns_whole_lines(self):
        self.path.write_bytes(b'aaaaaaaaaa\nbb\n')
        c = run_log.read_chunk(self.path, 0, 3)
        self.assertEqual(c['raw'], 'aaaaaaaaaa\n')
        self.assertEqual(c['next_offset'], 11)

    def test_multibyte_char_never_split(self):
        text = 'héllo € \U0001f600\n'
        self.path.write_text(text, encoding='utf-8')
        data = text.encode('utf-8')
        c = run_log.read_chunk(self.path, 0, 4)       # limit lands inside a character
        self.assertEqual(c['raw'], text)
        self.assertEqual(c['next_offset'], len(data))
        mid = run_log.read_chunk(self.path, 2, 100)   # offset inside the 2-byte e-acute
        self.assertEqual(mid['offset'], 3)
        self.assertNotIn('�', mid['raw'])

    def test_bad_offsets_are_clamped(self):
        self.path.write_bytes(b'one\n')
        for bad in (-5, 'x', None, '', 10**30):
            c = run_log.read_chunk(self.path, bad, 100)
            self.assertIn(c['next_offset'], (0, 4))
        self.assertEqual(run_log.read_chunk(self.path, -5)['raw'], 'one\n')
        self.assertEqual(run_log.read_chunk(self.path, 99)['next_offset'], 4)

    def test_missing_file(self):
        self.assertEqual(run_log.read_chunk(self.path.with_name('none.log'), 0)['raw'], '')


class LogApiTests(TrackBase):
    def test_log_events_offsets_and_done(self):
        run_id = self.add('success')
        self.run_until(run_id, 'done')
        status, view = self.get(f'/api/studio/runs/{run_id}/log')
        self.assertEqual(status, 200)
        self.assertTrue(view['done'])
        self.assertTrue(view['events'])
        self.assertEqual(view['next_offset'], self.executor.log_path(run_id).stat().st_size)
        again = self.get(f'/api/studio/runs/{run_id}/log', after=view['next_offset'])[1]
        self.assertEqual((again['raw'], again['events'], again['done']), ('', [], True))
        part = self.get(f'/api/studio/runs/{run_id}/log', after=-3, limit=1)[1]
        self.assertEqual(part['offset'], 0)

    def test_unknown_run_is_404(self):
        self.assertEqual(self.get('/api/studio/runs/que_' + '0' * 26 + '/log')[0], 404)

    def test_long_poll_returns_on_new_data(self):
        run_id = self.add('success')
        self.run_until(run_id, 'done')
        path = self.executor.log_path(run_id)
        size = path.stat().st_size
        # reopen the item so the log is not final: mark running directly
        with self.s.tx() as db:
            db.execute("UPDATE queue SET state='running' WHERE run_id=?", (run_id,))

        def append():
            time.sleep(0.4)
            with open(path, 'ab') as f:
                f.write(b'late line\n')
        threading.Thread(target=append, daemon=True).start()
        start = time.monotonic()
        view = self.get(f'/api/studio/runs/{run_id}/log', after=size, wait=10)[1]
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(view['raw'], 'late line\n')
        self.assertFalse(view['done'])
        start = time.monotonic()
        self.get(f'/api/studio/runs/{run_id}/log', after=view['next_offset'], wait=0.3)
        self.assertLess(time.monotonic() - start, 3)

    def test_wait_is_capped(self):
        self.assertEqual(QueueAPI._wait({'wait': ['999']}), 25.0)
        self.assertEqual(QueueAPI._wait({'wait': ['-1']}), 0.0)
        self.assertEqual(QueueAPI._wait({'wait': ['x']}), 0.0)


class TrackerTests(TrackBase):
    def setUp(self):
        super().setUp()
        self.ok = self.add('success')
        self.run_until(self.ok, 'done')
        self.bad = self.add('error_result', ws=self.workspace('w2'))
        self.run_until(self.bad, 'failed')

    def ids(self, **query):
        status, body = self.get('/api/studio/tracker', **query)
        self.assertEqual(status, 200, body)
        return {i['run_id'] for i in body['items']}, body

    def test_all_and_summary(self):
        ids, body = self.ids()
        self.assertEqual(ids, {self.ok, self.bad})
        self.assertEqual(body['total'], 2)
        s = body['summary']
        self.assertEqual((s['counts_by_status']['done'], s['counts_by_status']['failed']), (1, 1))
        self.assertEqual(s['cost_usd_cents'], {'sum': 1, 'reported_count': 1})
        self.assertEqual(s['tokens']['reported_count'], 1)
        self.assertGreaterEqual(s['total_wall_seconds'], 0)

    def test_filters(self):
        self.assertEqual(self.ids(status='failed')[0], {self.bad})
        self.assertEqual(self.ids(slot='claude-a')[0], {self.ok, self.bad})
        self.assertEqual(self.ids(slot='claude-b')[0], set())
        self.assertEqual(self.ids(provider='claude-a', kind='claude')[0], {self.ok, self.bad})
        self.assertEqual(self.ids(kind='codex')[0], set())
        self.assertEqual(self.ids(q=self.ok[-8:])[0], {self.ok})
        self.assertEqual(self.ids(since='2000-01-01', until='2999-12-31')[0], {self.ok, self.bad})
        self.assertEqual(self.ids(until='2000-01-01')[0], set())
        self.assertEqual(self.ids(since='2999-01-01')[0], set())
        self.assertEqual(self.ids(q='100%')[0], set())

    def test_paging_and_totals_follow_filter(self):
        ids, body = self.ids(limit=1)
        self.assertEqual((len(ids), body['total']), (1, 2))
        self.assertEqual(len(self.ids(limit=1, offset=1)[0]), 1)
        body = self.ids(status='done')[1]
        self.assertEqual(body['total'], 1)
        self.assertEqual(body['summary']['counts_by_status']['failed'], 0)

    def test_bad_filters_400(self):
        for q in ({'status': 'nope'}, {'since': 'yesterday'}, {'kind': 'x'}, {'slot': 'A B'}, {'limit': 'x'}):
            self.assertEqual(self.get('/api/studio/tracker', **q)[0], 400, q)


class HttpTests(ApiBase):
    def test_routes_and_sse_over_http(self):
        self.use_fakes()
        self.put_slots('claude-a')
        self.app.driver.start()
        self.addCleanup(self.app.driver.stop)
        run_id = self.call('POST', '/queue', {'prompt': 'success', 'workspace': str(self.ws)})[1]['run_id']
        support.wait_for(lambda: self.call('GET', f'/queue/{run_id}')[1]['state'] == 'done', timeout=30)
        self.assertEqual(self.call('GET', '/tracker')[0], 200)
        status, view = self.call('GET', f'/runs/{run_id}/log?wait=1')
        self.assertEqual(status, 200)
        self.assertTrue(view['done'])
        conn = http.client.HTTPConnection('127.0.0.1', self.client.port, timeout=10)
        conn.request('GET', f'/api/studio/runs/{run_id}/log/stream',
                     headers={'Host': f'127.0.0.1:{self.client.port}', 'Authorization': 'Bearer ' + self.client.token})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn('text/event-stream', response.getheader('Content-Type'))
        body = response.read().decode('utf-8')
        conn.close()
        self.assertIn('event: done', body)
        data = [l for l in body.splitlines() if l.startswith('data: ')]
        self.assertTrue(json.loads(data[-1][6:])['done'])
        self.assertEqual(self.client.request('GET', f'/api/studio/runs/{run_id}/log', auth=False)[0], 401)
        self.assertEqual(self.call('GET', '/runs/que_' + '0' * 26 + '/log/stream')[0], 404)


class FlagOffTests(unittest.TestCase):
    def test_routes_404_when_off(self):
        app, client = support.serve(self)
        self.assertIsNone(app.api.execq)
        for path in ('/api/studio/tracker', '/api/studio/runs/que_' + '0' * 26 + '/log',
                     '/api/studio/runs/que_' + '0' * 26 + '/log/stream'):
            self.assertEqual(client.request('GET', path)[0], 404, path)


if __name__ == '__main__':
    unittest.main()
