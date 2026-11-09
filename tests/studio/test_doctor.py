"""W3: doctor command (redaction, each check, exit codes, subprocess smoke)."""
import contextlib
import io
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from aether.studio import doctor, slots

ROOT = Path(__file__).resolve().parents[2]
H = '/ho' + 'me/'
LEVELS = {doctor.OK, doctor.WARN, doctor.FAIL}


def levels(lines):
    return [level for level, _ in lines]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.data = self.root / 'data'
        self.bin = self.root / 'bin'
        self.bin.mkdir()

    def fake_cli(self, name='claude', body="print('claude-fake 1.2.3')"):
        script = self.bin / f'{name}_impl.py'
        script.write_text(body + '\n', encoding='utf-8')
        wrapper = self.bin / name
        wrapper.write_text(f'#!{sys.executable}\nimport runpy\nrunpy.run_path({str(script)!r}, run_name="__main__")\n',
                           encoding='utf-8')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        return str(wrapper)

    def add_slot(self, kind='claude', slot_id='a', enabled=True):
        config = self.root / f'cfg_{slot_id}'
        config.mkdir(exist_ok=True)
        store = slots.SlotStore(self.data)
        store.save(store.load() + [{'id': slot_id, 'kind': kind, 'label': 'Main', 'config_dir': str(config),
                                    'enabled': enabled}])
        return config

    def ctx(self, resolve=None, port=None):
        self.data.mkdir(parents=True, exist_ok=True)
        return doctor.Context(self.data, port or free_port(), resolve=resolve, names=[])


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Redaction(unittest.TestCase):
    def test_posix_and_windows_paths_and_case(self):
        names = ['Brandon']
        self.assertEqual(doctor.redact(H + 'brandon/x', names), H + '<user>/x')
        self.assertEqual(doctor.redact('C:\\Users\\BRANDON\\AppData\\Local\\AetherCE', names),
                         'C:\\Users\\<user>\\AppData\\Local\\AetherCE')
        self.assertEqual(doctor.redact('C:/Us' + 'ers/BrAnDoN/x ' + H + 'Brandon', names), 'C:/Us' + 'ers/<user>/x ' + H + '<user>')

    def test_short_name_only_whole_component(self):
        self.assertEqual(doctor.redact(H + 'jo/data and joint', ['jo']), H + '<user>/data and joint')

    def test_user_names_include_getuser_and_home_basename(self):
        with mock.patch('getpass.getuser', return_value='Alice'):
            self.assertIn('Alice', doctor.user_names('C:\\Users\\alice_h'))
            self.assertIn('alice_h', doctor.user_names('C:\\Users\\alice_h'))


class Checks(Base):
    def test_platform_ok_and_old_python_fails(self):
        self.assertNotIn(doctor.FAIL, levels(doctor.check_platform(self.ctx())))
        with mock.patch.object(sys, 'version_info', (3, 10, 0, 'final', 0)):
            self.assertIn(doctor.FAIL, levels(doctor.check_platform(self.ctx())))

    def test_data_dir_ok_and_token(self):
        ctx = self.ctx()
        self.assertEqual(levels(doctor.check_data_dir(ctx)), [doctor.OK])
        self.assertEqual(levels(doctor.check_token(ctx))[0], doctor.OK)
        self.assertEqual(os.listdir(self.data), [])

    def test_real_token_never_read(self):
        ctx = self.ctx()
        token = self.data / 'owner.token'
        token.write_text('SECRETTOKENVALUE', encoding='utf-8')
        token.chmod(0o600)
        report = doctor.render(doctor.run_checks(ctx), [])
        self.assertNotIn('SECRETTOKENVALUE', report)
        self.assertIn('Owner token file exists and its protection verifies', report)

    def test_unwritable_data_dir_fails_exit_1(self):
        with mock.patch.object(doctor, 'write_probe', side_effect=PermissionError('no')):
            self.assertEqual(levels(doctor.check_data_dir(self.ctx())), [doctor.FAIL])
            out = io.StringIO()
            self.assertEqual(doctor.main(['--data-dir', str(self.data), '--port', str(free_port())], out=out), 1)
        self.assertIn('[FAIL]', out.getvalue())

    def test_token_probe_failure(self):
        with mock.patch('aether.studio.fsperm.verify_private', return_value=False):
            self.assertEqual(levels(doctor.check_token(self.ctx()))[0], doctor.FAIL)

    def test_busy_port_warns(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            s.listen(1)
            self.assertEqual(levels(doctor.check_port(self.ctx(port=s.getsockname()[1]))), [doctor.WARN])
        self.assertEqual(levels(doctor.check_port(self.ctx())), [doctor.OK])

    def test_no_slots_warns(self):
        lines = doctor.check_slots(self.ctx())
        self.assertEqual(levels(lines), [doctor.WARN])
        self.assertIn('Slots view', lines[0][1])

    def test_slot_with_fake_cli(self):
        self.add_slot()
        cli = self.fake_cli()
        lines = doctor.check_slots(self.ctx(resolve=lambda kind: cli))
        self.assertEqual(levels(lines), [doctor.OK, doctor.OK])
        self.assertIn('claude-fake 1.2.3', lines[0][1])
        self.assertIn(cli, lines[0][1])

    def test_missing_cli_warn_if_another_works_else_fail(self):
        self.add_slot('claude', 'a')
        self.add_slot('codex', 'b')
        cli = self.fake_cli()
        both = doctor.check_slots(self.ctx(resolve=lambda kind: cli if kind == 'claude' else None))
        self.assertIn(doctor.WARN, levels(both))
        self.assertNotIn(doctor.FAIL, levels(both))
        none = doctor.check_slots(self.ctx(resolve=lambda kind: None))
        self.assertIn(doctor.FAIL, levels(none))

    def test_version_truncated_first_line(self):
        self.add_slot()
        cli = self.fake_cli(body="print('v' * 300)\nprint('second line')")
        text = doctor.check_slots(self.ctx(resolve=lambda kind: cli))[0][1]
        self.assertIn('v' * 120, text)
        self.assertNotIn('v' * 121, text)
        self.assertNotIn('second line', text)

    def test_version_timeout(self):
        with mock.patch('subprocess.run', side_effect=subprocess.TimeoutExpired(['x'], 10)):
            self.assertIn('timed out', doctor.cli_version('x'))

    def test_config_dir_contents_never_read(self):
        config = self.add_slot()
        (config / 'credentials_SECRETNAME.json').write_text('SECRETCONTENT', encoding='utf-8')
        cli = self.fake_cli()
        report = doctor.render(doctor.run_checks(self.ctx(resolve=lambda kind: cli)), [])
        self.assertNotIn('SECRETNAME', report)
        self.assertNotIn('SECRETCONTENT', report)
        self.assertIn(str(config), report)

    def test_dry_spawn(self):
        self.assertEqual(levels(doctor.check_dry_spawn(self.ctx())), [doctor.OK])

    def test_dry_spawn_failure(self):
        with mock.patch('aether.studio.proc.start', side_effect=OSError('x')):
            self.assertEqual(levels(doctor.check_dry_spawn(self.ctx())), [doctor.FAIL])

    def test_landlock_not_applicable_off_linux(self):
        with mock.patch.object(sys, 'platform', 'win32'):
            lines = doctor.check_landlock(self.ctx())
        self.assertEqual(levels(lines), [doctor.OK])
        self.assertIn('not applicable: adapter sandbox disabled, CLI executor unaffected', lines[0][1])


class Whole(Base):
    def run_main(self, *extra):
        out = io.StringIO()
        code = doctor.main(['--data-dir', str(self.data), '--port', str(free_port()), *extra], out=out)
        return code, out.getvalue()

    def test_all_ok_exit_0(self):
        self.add_slot()
        cli = self.fake_cli()
        with mock.patch.object(proc_module(), 'resolve_cli', lambda name, **kw: cli):
            code, text = self.run_main()
        self.assertEqual(code, 0, text)
        self.assertNotIn('[FAIL]', text)
        for section in ('Platform', 'Data dir', 'Token permission', 'Port', 'Slots', 'Dry spawn', 'Landlock'):
            self.assertIn('== ' + section, text)

    def test_windows_mock_still_runs(self):
        ctx = self.ctx()   # build Paths first: under a mocked os.name, Path() would make literal '\\tmp' dirs
        with mock.patch.object(os, 'name', 'nt'), mock.patch.object(sys, 'platform', 'win32'):
            report = doctor.run_checks(ctx)
        self.assertEqual({lvl for _, ls in report for lvl, _ in ls} - LEVELS, set())
        self.assertEqual(len(report), 7)

    def test_username_redacted_in_report(self):
        report = [('Data dir', [(doctor.OK, H + 'Zed/data and C:\\Users\\zed\\x')])]
        text = doctor.render(report, ['zed'])
        self.assertNotIn('Zed', text)
        self.assertNotIn('zed', text)

    def test_crashing_check_becomes_fail(self):
        bad = mock.Mock(side_effect=RuntimeError('x'))
        with mock.patch.object(doctor, 'CHECKS', (('Port', bad),)):
            report = doctor.run_checks(self.ctx())
        self.assertIn(doctor.FAIL, levels([l for _, ls in report for l in ls]))


def proc_module():
    from aether.studio import proc
    return proc


class Subprocess(Base):
    def test_module_entry(self):
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'SYSTEMROOT', 'HOME', 'USERPROFILE')}
        env['PYTHONPATH'] = str(ROOT)
        done = subprocess.run([sys.executable, '-m', 'aether.studio', 'doctor', '--data-dir', str(self.data),
                               '--port', str(free_port())], cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        text = done.stdout.decode('utf-8')
        self.assertIn('AETHER CE doctor report', text)
        self.assertIn('Summary:', text)
        self.assertIn(done.returncode, (0, 1))
        self.assertEqual(done.returncode, 1 if '[FAIL]' in text else 0)
        self.assertIn('[ OK ] Dry spawn', text)


if __name__ == '__main__':
    unittest.main()
