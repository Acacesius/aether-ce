"""W2 entry point: host/port validation, data dir defaults, plain mode, fail-closed token permissions, subprocess smoke."""
import contextlib
import http.client
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from aether.studio import __main__ as entry
from aether.studio import fsperm, mode, proc
from aether.studio.mode import ModeError

LAN_IP = '.'.join(('192', '168', '1', '5'))
ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def run_main(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = entry.main(argv)
        except SystemExit as exit_:
            code = exit_.code if isinstance(exit_.code, int) else (1 if exit_.code else 0)
            if isinstance(exit_.code, str):
                err.write(exit_.code)
    return code, out.getvalue(), err.getvalue()


class Validation(unittest.TestCase):
    def test_host_refusal(self):
        for host in ('0.0.0.0', '::', LAN_IP, 'example.com'):
            code, out, err = run_main(['serve', '--host', host])
            self.assertNotEqual(code, 0, host)
            self.assertIn('127.0.0.1', err)

    def test_localhost_maps(self):
        self.assertEqual(entry.check_host('localhost'), '127.0.0.1')
        self.assertEqual(entry.check_host('127.0.0.1'), '127.0.0.1')

    def test_port_validation(self):
        for bad in ('0', '80', '1023', '65536', 'abc', '-1', ''):
            code, _, err = run_main(['serve', '--port', bad])
            self.assertNotEqual(code, 0, bad)
        self.assertEqual(entry.check_port('1024'), 1024)
        self.assertEqual(entry.check_port('65535'), 65535)
        self.assertEqual(entry.parser().parse_args(['serve']).port, 8080)

    def test_default_data_dir(self):
        home = Path('/h')
        self.assertEqual(entry.default_data_dir({'LOCALAPPDATA': '/L'}, home, 'nt'), Path('/L') / 'AetherCE')
        self.assertEqual(entry.default_data_dir({}, home, 'nt'), home / 'AppData' / 'Local' / 'AetherCE')
        self.assertEqual(entry.default_data_dir({}, home, 'posix'), home / '.local' / 'share' / 'aether-ce')
        self.assertEqual(entry.default_data_dir({'XDG_DATA_HOME': '/x'}, home, 'posix'), Path('/x') / 'aether-ce')

    def test_doctor_placeholder(self):
        import aether.studio as pkg
        with mock.patch.dict(sys.modules, {'aether.studio.doctor': None}), \
                mock.patch.object(pkg, 'doctor', None, create=True):
            delattr(pkg, 'doctor')
            code, _, err = run_main(['doctor'])
        self.assertEqual(code, 2)
        self.assertIn('doctor: not available', err)


class PlainMode(unittest.TestCase):
    def test_plain_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = mode.load_plain(Path(tmp), {})
            self.assertEqual((m.mode, m.provider, m.runtime), ('custom', 'none', 'plain'))
            self.assertTrue(m.custom)

    def test_refuses_private_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in mode.PRIVATE_ENV:
                with self.assertRaises(ModeError):
                    mode.load_plain(Path(tmp), {name: 'x'})

    def test_refuses_admin_token_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {'AETHER_ADMIN_TOKEN': 'secret-value'}):
                code, out, err = run_main(['serve', '--port', str(free_port()), '--data-dir', tmp])
            self.assertNotEqual(code, 0)
            self.assertNotIn('secret-value', out + err)

    def test_refuses_root_and_relative(self):
        with self.assertRaises(ModeError):
            mode.load_plain(Path(Path.cwd().anchor), {})
        with self.assertRaises(ModeError):
            mode.load_plain(Path('rel'), {})

    def test_server_loopback_only(self):
        from aether.studio.app import StudioServer
        app = mock.Mock()
        app.mode = mode.Mode('custom', 'none', 'plain', Path('/x'))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AETHER_ADMIN_TOKEN', None)
            for host in ('0.0.0.0', '::', LAN_IP):
                with self.assertRaises(ValueError):
                    StudioServer((host, 0), app, dockerenv=__file__)   # even with a "dockerenv" present


class TokenProtection(unittest.TestCase):
    def test_fail_closed_when_restrict_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'd'
            with mock.patch.object(fsperm, 'restrict', return_value=False):
                code, out, err = run_main(['serve', '--port', str(free_port()), '--data-dir', str(data)])
            self.assertNotEqual(code, 0)
            token = (data / 'owner.token').read_text(encoding='utf-8').strip()
            self.assertGreater(len(token), 20)
            self.assertNotIn(token, out + err)
            self.assertNotIn('listening', out)
            self.assertNotIn('Owner token stored in', out)

    def test_fail_closed_when_verify_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fsperm, 'verify_private', return_value=False):
                code, out, err = run_main(['serve', '--port', str(free_port()), '--data-dir', str(Path(tmp) / 'd')])
            self.assertNotEqual(code, 0)

    @unittest.skipIf(os.name == 'nt', 'POSIX mode bits')
    def test_posix_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'f'
            f.write_text('x', encoding='utf-8')
            os.chmod(f, 0o600)
            self.assertTrue(fsperm.verify_private(f, windows=False))
            os.chmod(f, 0o644)
            self.assertFalse(fsperm.verify_private(f, windows=False))
            os.chmod(f, 0o660)
            self.assertFalse(fsperm.verify_private(f, windows=False))
            self.assertFalse(fsperm.verify_private(Path(tmp) / 'missing', windows=False))

    def test_windows_icacls_mocked(self):
        def fake(stdout, code=0):
            return mock.Mock(returncode=code, stdout=stdout)
        ok = b'C:\\x\\owner.token ME\\user:(F)\n\nSuccessfully processed 1 files'
        with mock.patch.object(fsperm.subprocess, 'run', return_value=fake(ok)) as run:
            self.assertTrue(fsperm.verify_private('owner.token', windows=True))
            self.assertEqual(run.call_args.args[0][0], 'icacls')
            self.assertIs(run.call_args.kwargs['shell'], False)
        broad = b'owner.token ME\\user:(F)\n BUILTIN\\Users:(R)\n'
        with mock.patch.object(fsperm.subprocess, 'run', return_value=fake(broad)):
            self.assertFalse(fsperm.verify_private('owner.token', windows=True))
        with mock.patch.object(fsperm.subprocess, 'run', return_value=fake(ok, 5)):
            self.assertFalse(fsperm.verify_private('owner.token', windows=True))
        with mock.patch.object(fsperm.subprocess, 'run', side_effect=OSError):
            self.assertFalse(fsperm.verify_private('owner.token', windows=True))
        with mock.patch.object(fsperm.subprocess, 'run', return_value=fake(b'')):
            self.assertFalse(fsperm.verify_private('owner.token', windows=True))

    def test_restrict_windows_failure_is_false(self):
        with mock.patch.object(fsperm.subprocess, 'run', return_value=mock.Mock(returncode=1)):
            self.assertFalse(fsperm.restrict('owner.token', directory=False, windows=True))


class Scripts(unittest.TestCase):
    def test_start_bat(self):
        raw = (ROOT / 'start.bat').read_bytes()
        self.assertIn(b'\r\n', raw)
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
        text = raw.decode('utf-8').lower()
        self.assertIn('-m aether.studio serve --open', text)
        self.assertIn('python.org', text)
        self.assertNotIn('powershell', text)
        for line in text.splitlines():
            if not line.startswith('rem'):
                for word in ('token', 'venv', 'pip ', 'echo %token'):
                    self.assertNotIn(word, line)

    def test_other_scripts(self):
        sh = (ROOT / 'start.sh').read_text(encoding='utf-8')
        ps = (ROOT / 'start.ps1').read_text(encoding='utf-8')
        self.assertTrue(sh.startswith('#!/bin/sh'))
        self.assertNotIn('\r', sh)
        for text in (sh, ps):
            self.assertIn('aether.studio serve --open', text)
            self.assertNotIn('owner.token', text)
        self.assertIn('ExecutionPolicy', ps)
        self.assertTrue(os.access(ROOT / 'start.sh', os.X_OK) or os.name == 'nt')


class Smoke(unittest.TestCase):
    def test_real_subprocess(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data'
            env = {k: v for k, v in os.environ.items() if not k.startswith('AETHER_')}
            env.update({'PYTHONPATH': str(ROOT), 'HOME': tmp, 'ANTHROPIC_API_KEY': 'fake-test-value'})
            handle = proc.start([sys.executable, '-m', 'aether.studio', 'serve', '--port', str(port),
                                 '--data-dir', str(data)], ROOT, env)
            try:
                status = None
                deadline = time.time() + 10
                while time.time() < deadline and handle.popen.poll() is None:
                    try:
                        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                        conn.request('GET', '/healthz', headers={'Host': f'127.0.0.1:{port}'})
                        status = conn.getresponse().status
                        conn.close()
                        break
                    except OSError:
                        time.sleep(0.2)
                self.assertEqual(status, 200)
                self.assertTrue((data / 'owner.token').is_file())
            finally:
                proc.kill_tree(handle, grace_seconds=2)
                try:
                    out, err = handle.popen.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    handle.popen.kill()
                    out, err = handle.popen.communicate()
            token = (data / 'owner.token').read_text(encoding='utf-8').strip()
            text = (out + err).decode('utf-8', 'replace')
            self.assertNotIn(token, text)
            self.assertIn(f'http://127.0.0.1:{port}/', text)
            self.assertIn('Owner token stored in:', text)


if __name__ == '__main__':
    unittest.main()
