import time
import unittest

from aether.studio.executors.base import RunView, StageView
from aether.studio.executors.mock import MockExecutor

from tests.studio import support


def drive(step, *, cancel_after=None, timeout=5):
    ex = MockExecutor()
    events = []
    run = RunView('run_x', True, {})
    handle = ex.start(run, StageView('demo', 1, step, 5, ('out',)), lambda t, d: events.append((t, d)))
    end = time.monotonic() + timeout
    start = time.monotonic()
    while time.monotonic() < end:
        if cancel_after is not None and time.monotonic() - start > cancel_after:
            ex.cancel(handle, 0)
            cancel_after = None
        result = ex.poll(handle)
        if result['state'] != 'running':
            return result, events
        time.sleep(0.005)
    raise AssertionError('executor did not finish')


class MockExecutorTests(unittest.TestCase):
    def test_mock_success_emits_demo_events(self):
        result, events = drive('mock:success')
        self.assertEqual(result, {'state': 'exited', 'exit_code': 0})
        self.assertTrue(events)
        for _type, data in events:
            self.assertTrue(data['demo'])
            self.assertTrue(data['text'].startswith('[DEMO] '))

    def test_mock_fail_exit_1(self):
        self.assertEqual(drive('mock:fail:1')[0], {'state': 'exited', 'exit_code': 1})
        self.assertEqual(drive('mock:fail:7')[0]['exit_code'], 7)
        self.assertEqual(drive('mock:fail:0')[0]['exit_code'], 1)  # a "fail" can never exit 0

    def test_mock_hang_is_timed_out(self):
        ex = MockExecutor()
        handle = ex.start(RunView('run_x', True, {}), StageView('d', 1, 'mock:hang', 1, ()), lambda *a: None)
        time.sleep(0.1)
        self.assertEqual(ex.poll(handle)['state'], 'running')
        ex.cancel(handle, 0)
        time.sleep(0.1)
        self.assertEqual(ex.poll(handle), {'state': 'exited', 'exit_code': 143})

    def test_mock_cancel_interrupts_sleep(self):
        result, _ = drive('mock:sleep:30', cancel_after=0.05, timeout=3)
        self.assertEqual(result['exit_code'], 143)

    def test_mock_artifact_event_marked_demo(self):
        result, events = drive('mock:artifact:out')
        self.assertEqual(result['exit_code'], 0)
        art = [d for t, d in events if t == 'artifact.write'][0]
        self.assertTrue(art['demo'])
        self.assertIn('DEMO', art['content'])

    def test_mock_approval_step_blocks(self):
        result, _ = drive('mock:approval:workspace.write')
        self.assertEqual((result['state'], result['capability']), ('blocked', 'workspace.write'))

    def test_mock_verdict_step_emits_scripted_demo_verdict(self):
        for verdict in ('pass', 'fail'):
            result, events = drive(f'mock:verdict:{verdict}')
            self.assertEqual(result['exit_code'], 0)
            data = [d for t, d in events if t == 'stage.verdict']
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]['verdict'], verdict)
            self.assertIn('DEMO', data[0]['reason'])

    def test_unknown_step_rejected(self):
        for step in ('mock:rm -rf', 'sh:echo', 'mock:fail:1000', 'mock:', 'mock:verdict:maybe'):
            with self.assertRaises(ValueError):
                MockExecutor().start(RunView('r', True, {}), StageView('d', 1, step, 1, ()), lambda *a: None)

    def test_mock_declares_demo_and_no_host_capability(self):
        ex = MockExecutor()
        self.assertTrue(ex.demo)
        self.assertEqual(ex.capabilities(), {'demo.run', 'artifact.write'})
        self.assertEqual(ex.health()['status'], 'ready')
        self.assertIn('DEMO', ex.health()['detail'])

    def test_adapter_executor_without_enrollment_is_unavailable(self):
        s, ex, _, _ = support.adapter_env(self)
        self.assertEqual(ex.health()['status'], 'unavailable')
        self.assertEqual(ex.health('ext')['detail'], 'not enrolled')
        self.assertFalse(ex.demo)

    def test_adapter_run_without_enrollment_is_blocked_not_run(self):
        s, ex, _, _ = support.adapter_env(self)
        run_id = support.submit(s, spec=support.adapter_spec(ref='ext'))
        support.adapter_runner(self, s, ex)
        self.assertEqual(support.wait_status(s, run_id, 'blocked')['reason'], 'adapter_unavailable')
