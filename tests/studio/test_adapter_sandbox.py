"""AHS-04: an adapter gains no shell, process, network, signal or out-of-scratch file access through the protocol or
from its process (sandbox: Landlock + seccomp + minimal env). Verified with the hostile ``escape`` fixture."""
import os
import socket
import unittest
from unittest import mock

from aether.studio import adapter_protocol as proto

from tests.studio import support


class SandboxEscapeTests(unittest.TestCase):
    def setUp(self):
        self.s, self.ex, self.data, self.dir = support.adapter_env(self)
        self.runner = support.adapter_runner(self, self.s, self.ex)
        self.outside = support.tempdir(self)
        self.planted = self.outside / 'planted.txt'
        self.planted.write_text('PLANTED-OWNER-ONLY-CONTENT')
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(0.2)
        self.addCleanup(self.listener.close)

    def escape_run(self):
        port = self.listener.getsockname()[1]
        support.enroll(self.s, self.data, self.dir / 'hostile_adapter.py', name='escape',
                       args=('escape', str(self.planted), str(port)))
        with mock.patch.dict(os.environ, {'AETHER_FAKE_CREDENTIAL': 'must-not-leak'}):
            run_id = support.submit(self.s, spec=support.adapter_spec(ref='escape'))
            self.runner.notify()
            run = support.wait_status(self.s, run_id, 'succeeded', 'failed', 'blocked', timeout=20)
        events = support.all_events(self.s, run_id)
        reports = dict(e['data']['data']['text'].split('=', 1) for e in events
                       if e['type'] == 'adapter.event' and e['data']['kind'] == 'log')
        return run, events, reports

    def test_escape_attempts_are_all_denied(self):
        run, events, reports = self.escape_run()
        self.assertEqual(run['status'], 'succeeded', events)
        self.assertEqual(proto.verify_stored(events)['events'], len(reports) + 1)
        # environment: fixed minimal set, nothing inherited from Studio
        self.assertEqual(reports.pop('env').split(','),
                         ['AETHER_ADAPTER_PROTOCOL', 'HOME', 'LANG', 'LC_ALL', 'PATH', 'TMPDIR'])
        self.assertEqual(reports.pop('scratch_write'), 'ALLOWED:2')   # its own scratch dir only
        self.assertTrue(reports.pop('read_owner_token').startswith('DENIED:'))
        expected = {'read_secret': 'DENIED:PermissionError', 'list_home': 'DENIED:PermissionError',
                    'shell': 'DENIED:PermissionError', 'system': 'DENIED:PermissionError',
                    'fork': 'DENIED:PermissionError', 'tcp': 'DENIED:PermissionError',
                    'unix_socket': 'DENIED:PermissionError', 'signal_parent': 'DENIED:PermissionError',
                    'execv': 'DENIED:PermissionError'}
        self.assertEqual(reports, expected)
        with self.assertRaises(socket.timeout):
            self.listener.accept()                                 # no connection ever arrived
        text = str(events)
        for leaked in ('PLANTED-OWNER-ONLY-CONTENT', 'must-not-leak', 'pwned'):
            self.assertNotIn(leaked, text)

    def test_owner_token_and_database_unreadable_from_adapter(self):
        (self.data / 'owner.token').write_text('OWNER-TOKEN-PLACEHOLDER')
        self.planted = self.data / 'planted.txt'
        self.planted.write_text('DATA-DIR-CONTENT')
        run, events, reports = self.escape_run()
        self.assertEqual(run['status'], 'succeeded')
        self.assertEqual(reports['read_owner_token'], 'DENIED:PermissionError')
        self.assertEqual(reports['read_secret'], 'DENIED:PermissionError')
        self.assertNotIn('DATA-DIR-CONTENT', str(events))
        self.assertEqual(reports['list_home'], 'DENIED:PermissionError')   # the data dir itself
        self.assertNotIn('OWNER-TOKEN-PLACEHOLDER', str(events))

    def test_sandbox_unavailable_fails_closed(self):
        # An interpreter the sandbox cannot confine must not run the adapter unconfined.
        support.enroll(self.s, self.data, self.dir / 'echo_adapter.py')
        broken = self.dir / 'sandbox_broken.py'
        broken.write_text('import sys\nsys.stderr.write("sandbox_unavailable: test\\n")\nsys.exit(70)\n')
        with mock.patch('aether.studio.executors.adapter.SANDBOX', broken):
            health = self.ex.health('echo')
        self.assertEqual(health['status'], 'unavailable')
        self.assertIn('sandbox_unavailable', health['detail'])


if __name__ == '__main__':
    unittest.main()
