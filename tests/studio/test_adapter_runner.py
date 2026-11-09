"""AHS-04: third-party adapters through the same Runner: register, negotiate, submit, verifiable events, disconnects,
cancel, revoke, and every hostile behaviour rejected and recorded (never silently dropped)."""
import os
import time
import unittest

from aether.studio import adapter_protocol as proto
from aether.studio import schema

from tests.studio import support

TERMINAL = ('succeeded', 'failed', 'cancelled', 'blocked')


def submit(s, spec):
    return support.submit(s, spec=spec)


def errors(events):
    return [e['data']['code'] for e in events if e['type'] == 'executor.error']


class AdapterRunTests(unittest.TestCase):
    def setUp(self):
        self.s, self.ex, self.data, self.dir = support.adapter_env(self)
        self.runner = support.adapter_runner(self, self.s, self.ex)

    def run_spec(self, spec, *statuses, timeout=20):
        run_id = submit(self.s, spec)
        self.runner.notify()
        run = support.wait_status(self.s, run_id, *(statuses or TERMINAL), timeout=timeout)
        return run, support.all_events(self.s, run_id)

    def hostile(self, mode, *args, name='hostile', scopes=('observe', 'submit', 'cancel')):
        return support.enroll(self.s, self.data, self.dir / 'hostile_adapter.py', name=name, args=(mode, *args),
                              scopes=scopes)

    def live_sessions(self):
        with self.ex._lock:
            return sum(len(v) for v in self.ex._live.values())

    def run_session(self, run_id):
        """Wait for the run's first adapter event, then return its (non-probe) session."""
        support.wait_for(lambda: any(e['type'] == 'adapter.event' for e in support.all_events(self.s, run_id)))
        with self.ex._lock:
            sessions = [s for group in self.ex._live.values() for s in group if s.emit is not None]
        self.assertEqual(len(sessions), 1)
        return sessions[0]

    # ------------------------------------------------------------ happy path
    def test_echo_registers_negotiates_submits_and_emits_verifiable_events(self):
        adapter = support.enroll(self.s, self.data, self.dir / 'echo_adapter.py')
        run, events = self.run_spec(support.adapter_spec('submit:hello world', outputs=('out',)))
        self.assertEqual((run['status'], run['reason'], run['exit_code'], run['demo'], run['label']),
                         ('succeeded', None, 0, False, None))
        session = [e for e in events if e['type'] == 'adapter.session']
        self.assertEqual(len(session), 1)
        self.assertEqual(session[0]['data']['adapter_id'], adapter['id'])
        self.assertEqual(session[0]['data']['capabilities'], ['cancel', 'observe', 'submit'])
        adapter_events = [e for e in events if e['type'] == 'adapter.event']
        self.assertEqual([e['data']['kind'] for e in adapter_events], ['status', 'log', 'artifact', 'progress', 'result'])
        for event in adapter_events:
            self.assertFalse(event['demo'])                          # non-DEMO ...
            self.assertEqual(event['data']['source'], 'adapter')     # ... but marked as adapter-origin
            self.assertEqual(event['data']['protocol_version'], proto.PROTOCOL)
        self.assertEqual(proto.verify_stored(events), {'sessions': 1, 'events': 5, 'recomputed': 5, 'redacted': 0})
        self.assertEqual([a['name'] for a in run['evidence']['artifacts']], ['out'])
        self.assertEqual(run['evidence']['events_sha256'], schema.sha256_hex(schema.canonical_json(events)))
        support.wait_for(lambda: self.live_sessions() == 0)
        self.assertEqual(list(self.ex.scratch_root.iterdir()), [])   # scratch removed after exit

    def test_observe_step_needs_only_observe(self):
        support.enroll(self.s, self.data, self.dir / 'echo_adapter.py', scopes=('observe',))
        run, events = self.run_spec(support.adapter_spec('observe:ext-job-42'))
        self.assertEqual(run['status'], 'succeeded')
        refs = {e['data']['ref'] for e in events if e['type'] == 'adapter.event'}
        self.assertEqual(refs, {'ext-job-42'})

    def test_adapter_failure_exit_code(self):
        support.enroll(self.s, self.data, self.dir / 'echo_adapter.py')
        run, _ = self.run_spec(support.adapter_spec('submit:fail:7'))
        self.assertEqual((run['status'], run['reason'], run['exit_code']), ('failed', 'exit_nonzero', 7))

    # ------------------------------------------------------------ least privilege / negotiation
    def test_default_enrollment_grants_nothing_and_blocks(self):
        support.enroll(self.s, self.data, self.dir / 'echo_adapter.py', scopes=())
        run, _ = self.run_spec(support.adapter_spec(), 'blocked')
        self.assertEqual(run['reason'], 'adapter_unavailable')
        self.assertEqual(self.ex.health('echo')['detail'], 'observe not granted')

    def test_submit_requires_explicit_grant(self):
        support.enroll(self.s, self.data, self.dir / 'echo_adapter.py', scopes=('observe',))
        run, _ = self.run_spec(support.adapter_spec('submit:x'), 'blocked')
        self.assertEqual(run['reason'], 'capability_unavailable')

    def test_not_enrolled_blocks(self):
        run, _ = self.run_spec(support.adapter_spec(ref='nobody'), 'blocked')
        self.assertEqual(run['reason'], 'adapter_unavailable')

    def test_version_mismatch_refused_clearly(self):
        self.hostile('bad_version', name='echo')
        run, _ = self.run_spec(support.adapter_spec(), 'blocked')
        self.assertEqual(run['reason'], 'adapter_unavailable')
        detail = self.ex.health('echo')['detail']
        self.assertIn('adapter_version_mismatch', detail)
        self.assertIn(proto.PROTOCOL, detail)

    def test_unknown_capability_refused(self):
        self.hostile('unknown_cap', name='echo')
        run, _ = self.run_spec(support.adapter_spec(), 'blocked')
        self.assertEqual(run['reason'], 'adapter_unavailable')
        self.assertIn('adapter_unknown_capability', self.ex.health('echo')['detail'])

    def test_script_changed_after_enrollment_is_unavailable(self):
        support.enroll(self.s, self.data, self.dir / 'echo_adapter.py')
        with open(self.dir / 'echo_adapter.py', 'a') as stream:
            stream.write('\n# modified\n')
        run, _ = self.run_spec(support.adapter_spec(), 'blocked')
        self.assertEqual(run['reason'], 'adapter_unavailable')
        self.assertIn('script changed', self.ex.health('echo')['detail'])

    # ------------------------------------------------------------ hostile protocol behaviour
    def test_hostile_protocol_violations_fail_the_run_with_recorded_error(self):
        cases = {'oversized': 'adapter_oversized', 'bad_seq': 'adapter_bad_sequence', 'bad_chain': 'adapter_bad_chain',
                 'exec_request': 'adapter_unknown_message', 'garbage': 'adapter_invalid_json',
                 'dup_keys': 'adapter_duplicate_field', 'event_before_accept': 'adapter_unexpected_message',
                 'undeclared_artifact': 'adapter_artifact_undeclared'}
        for i, (mode, code) in enumerate(cases.items()):
            with self.subTest(mode=mode):
                name = f'h{i}'
                adapter = self.hostile(mode, name=name)
                run, events = self.run_spec(support.adapter_spec(ref=name))
                self.assertEqual((run['status'], run['reason']), ('failed', 'executor_error'), mode)
                self.assertIn(code, errors(events))
                accepted = [e for e in events if e['type'] == 'adapter.event']
                proto.verify_stored(events)               # whatever was accepted is still a valid chain
                if mode in ('bad_seq', 'bad_chain'):
                    self.assertEqual(len(accepted), 1)    # the good first event only; the bad one is not stored
                self.s.revoke_adapter(adapter['id'])

    def test_crash_is_disconnect_failure(self):
        self.hostile('crash', name='echo')
        run, events = self.run_spec(support.adapter_spec())
        self.assertEqual((run['status'], run['reason']), ('failed', 'executor_error'))
        error = [e['data'] for e in events if e['type'] == 'executor.error'][0]
        self.assertEqual(error['code'], 'adapter_disconnected')
        self.assertIn('exited 3', error['message'])

    def test_unresponsive_adapter_fails_unavailable_and_is_killed(self):
        self.hostile('hang', name='echo')
        run, events = self.run_spec(support.adapter_spec())
        self.assertEqual((run['status'], run['reason']), ('failed', 'adapter_unavailable'))
        self.assertIn('adapter_unresponsive', errors(events))
        support.wait_for(lambda: self.live_sessions() == 0)

    def test_secret_shaped_log_is_stored_redacted_and_chain_still_verifies(self):
        self.hostile('leaky_log', name='echo')
        run, events = self.run_spec(support.adapter_spec())
        self.assertEqual(run['status'], 'succeeded')
        log = [e['data'] for e in events if e['type'] == 'adapter.event' and e['data']['kind'] == 'log'][0]
        self.assertTrue(log['redacted'])
        self.assertNotIn('A' * 30, log['data']['text'])
        self.assertEqual(proto.verify_stored(events)['redacted'], 1)

    # ------------------------------------------------------------ cancel / revoke
    def test_cancel_with_scope_is_honoured_by_adapter(self):
        support.enroll(self.s, self.data, self.dir / 'echo_adapter.py')
        run_id = submit(self.s, support.adapter_spec('submit:sleep:30'))
        self.runner.notify()
        support.wait_for(lambda: any(e['type'] == 'adapter.event' for e in support.all_events(self.s, run_id)))
        self.runner.cancel(run_id)
        run = support.wait_status(self.s, run_id, 'cancelled', timeout=10)
        self.assertEqual(run['reason'], 'cancelled_by_owner')
        results = [e['data']['data'] for e in support.all_events(self.s, run_id)
                   if e['type'] == 'adapter.event' and e['data']['kind'] == 'result']
        self.assertEqual(results, [{'exit_code': 143}])
        self.assertEqual(errors(support.all_events(self.s, run_id)), [])

    def test_cancel_ignored_is_killed_after_grace(self):
        self.hostile('ignore_cancel', name='echo')
        run_id = submit(self.s, support.adapter_spec(grace=1))
        self.runner.notify()
        session = self.run_session(run_id)
        started = time.monotonic()
        self.runner.cancel(run_id)
        support.wait_status(self.s, run_id, 'cancelled', timeout=10)
        support.wait_for(lambda: session.exited, timeout=5)
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        with self.assertRaises(ProcessLookupError):
            os.killpg(session.proc.pid, 0)            # the whole process group is gone

    def test_revoke_stops_events_and_fails_run(self):
        adapter = support.enroll(self.s, self.data, self.dir / 'echo_adapter.py')
        run_id = submit(self.s, support.adapter_spec('submit:sleep:30'))
        self.runner.notify()
        self.run_session(run_id)
        self.s.revoke_adapter(adapter['id'])
        self.assertEqual(self.ex.revoke(adapter['id']), 1)
        run = support.wait_status(self.s, run_id, 'failed')
        self.assertEqual(run['reason'], 'adapter_unavailable')
        events = support.all_events(self.s, run_id)
        revoked_at = [e['seq'] for e in events if e['type'] == 'executor.error' and e['data']['code'] == 'adapter_revoked']
        self.assertEqual(len(revoked_at), 1)
        self.assertFalse([e for e in events if e['type'] == 'adapter.event' and e['seq'] > revoked_at[0]])
        support.wait_for(lambda: self.live_sessions() == 0)
        run2, _ = self.run_spec(support.adapter_spec(), 'blocked')    # and it cannot be used again
        self.assertEqual(run2['reason'], 'adapter_unavailable')


class HealthTests(unittest.TestCase):
    def test_health_has_observed_timestamp_and_goes_stale_honestly(self):
        s, ex, data, directory = support.adapter_env(self, probe_ttl=0.3)
        self.assertEqual(ex.health()['status'], 'unavailable')
        self.assertEqual(ex.health()['detail'], 'no adapter enrolled')
        support.enroll(s, data, directory / 'echo_adapter.py')
        self.assertEqual(ex.health()['adapters'][0]['detail'], 'not yet contacted')   # aggregate never launches
        health = ex.health('echo')
        self.assertEqual(health['status'], 'ready')
        self.assertRegex(health['observed_utc'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(ex.health()['status'], 'ready')
        time.sleep(0.4)
        stale = ex.health()
        self.assertEqual(stale['status'], 'degraded')
        self.assertIn('last contact', stale['adapters'][0]['detail'])
        self.assertEqual(ex.health('echo')['status'], 'ready')    # a ref lookup re-probes

    def test_leftover_scratch_from_a_crash_is_removed_at_startup(self):
        from aether.studio.executors.adapter import AdapterExecutor
        s, ex, data, _ = support.adapter_env(self)
        (ex.scratch_root / 'run_x-work-1-abc').mkdir()
        (ex.scratch_root / 'run_x-work-1-abc' / 'note.txt').write_text('left over')
        AdapterExecutor(s, data_dir=data, **support.ADAPTER_FAST)
        self.assertEqual(list(ex.scratch_root.iterdir()), [])

    def test_executor_capabilities_are_adapter_scopes_only(self):
        _, ex, _, _ = support.adapter_env(self)
        self.assertEqual(ex.capabilities(), {'adapter.observe', 'adapter.submit', 'adapter.cancel'})
        self.assertFalse(ex.demo)


if __name__ == '__main__':
    unittest.main()
