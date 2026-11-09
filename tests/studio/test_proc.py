"""CLI_EXECUTOR: the one process helper (start, kill_tree, resolve_cli) on POSIX, Windows branch mocked."""
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from aether.studio import proc

from tests.studio import support

POSIX = os.name != 'nt'


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:   # a zombie still answers kill(0); check its state where /proc exists
        return Path(f'/proc/{pid}/stat').read_text().split()[2] != 'Z'
    except OSError:
        return True


class StartTests(unittest.TestCase):
    def test_stdin_env_cwd(self):
        cwd = support.tempdir(self)
        code = 'import os, sys; print(sys.stdin.read().upper()); print(os.getcwd()); print(sorted(os.environ))'
        env = {'ONLY': '1'}
        if not POSIX:
            env['SystemRoot'] = os.environ.get('SystemRoot', '')
        handle = proc.start([sys.executable, '-I', '-c', code], cwd, env, stdin='héllo ' * 20000)
        out, _ = handle.popen.communicate(timeout=30)
        lines = out.decode('utf-8').splitlines()
        self.assertEqual(lines[0], 'HÉLLO ' * 20000)
        self.assertEqual(Path(lines[1]).resolve(), cwd.resolve())
        self.assertIn("'ONLY'", lines[2])
        if POSIX:
            self.assertNotIn("'PATH'", lines[2])

    def test_argv_list_required(self):
        with self.assertRaises(AssertionError):
            proc.popen_group('echo hi')

    def test_group_kwargs(self):
        self.assertEqual(proc.group_kwargs(windows=False), {'start_new_session': True})
        self.assertEqual(proc.group_kwargs(windows=True), {'creationflags': 0x00000200})


@unittest.skipUnless(POSIX, 'POSIX kill path')
class PosixKillTests(unittest.TestCase):
    def spawn_tree(self):
        root = support.tempdir(self)
        pidfile = root / 'child.pid'
        code = ('import subprocess, sys, time; c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]);'
                f'open({str(pidfile)!r}, "w").write(str(c.pid)); time.sleep(60)')
        handle = proc.start([sys.executable, '-I', '-c', code], root, {'PATH': os.environ.get('PATH', '')})
        self.addCleanup(handle.close_pipes)
        child = int(support.wait_for(lambda: pidfile.exists() and pidfile.read_text()))
        return handle, child

    def test_kill_tree_force(self):
        handle, child = self.spawn_tree()
        proc.kill_tree(handle, 0)
        self.assertEqual(handle.wait(10), -9)
        support.wait_for(lambda: not alive(child), timeout=10)
        proc.kill_tree(handle, 0)   # repeated kill after exit is harmless

    def test_kill_tree_grace_sigterm_then_sigkill(self):
        code = 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready", flush=True); time.sleep(60)'
        handle = proc.start([sys.executable, '-I', '-c', code], support.tempdir(self), {})
        self.addCleanup(handle.close_pipes)
        handle.popen.stdout.readline()
        started = time.monotonic()
        proc.kill_tree(handle, 0.3)
        self.assertEqual(handle.wait(10), -9)        # SIGTERM ignored -> SIGKILL after the grace
        self.assertGreaterEqual(time.monotonic() - started, 0.25)

    def test_kill_tree_grace_polite_exit(self):
        handle = proc.start([sys.executable, '-I', '-c', 'import time; print("r", flush=True); time.sleep(60)'],
                            support.tempdir(self), {})
        self.addCleanup(handle.close_pipes)
        handle.popen.stdout.readline()
        proc.kill_tree(handle, 5)
        self.assertEqual(handle.wait(10), -15)


class WindowsBranchTests(unittest.TestCase):
    def handle(self, running=True):
        popen = mock.Mock(spec=subprocess.Popen)
        popen.pid = 4242
        popen.poll.return_value = None if running else 0
        return proc.ProcHandle(popen, ('x',), windows=True)

    def test_taskkill_tree(self):
        handle = self.handle()
        with mock.patch('aether.studio.proc.subprocess.run') as run, mock.patch('aether.studio.proc.os.killpg') as killpg:
            proc.kill_tree(handle, 5)
        self.assertEqual(run.call_args.args[0], ['taskkill', '/T', '/F', '/PID', '4242'])
        self.assertFalse(run.call_args.kwargs['shell'])
        handle.popen.kill.assert_called_once()
        killpg.assert_not_called()

    def test_exited_process_not_signalled(self):
        handle = self.handle(running=False)
        with mock.patch('aether.studio.proc.subprocess.run') as run:
            proc.kill_tree(handle)
        run.assert_not_called()

    def test_taskkill_missing_is_tolerated(self):
        with mock.patch('aether.studio.proc.subprocess.run', side_effect=FileNotFoundError):
            proc.kill_tree(self.handle())


class ResolveTests(unittest.TestCase):
    def test_which_and_cmd_shim_prefers_exe(self):
        root = support.tempdir(self)
        for name in ('claude.cmd', 'claude.exe', 'codex.cmd'):
            (root / name).write_text('', encoding='utf-8')
            os.chmod(root / name, 0o755)
        with mock.patch('aether.studio.proc.shutil.which', side_effect=lambda n, path=None: str(root / f'{n}.cmd')):
            self.assertEqual(proc.resolve_cli('claude', windows=True), str((root / 'claude.exe').resolve()))
            self.assertEqual(proc.resolve_cli('codex', windows=True), str((root / 'codex.cmd').resolve()))
            self.assertEqual(proc.resolve_cli('claude', windows=False), str((root / 'claude.cmd').resolve()))

    def test_real_which(self):
        root = support.tempdir(self)
        if POSIX:
            (root / 'fakecli').write_text('', encoding='utf-8')
            os.chmod(root / 'fakecli', 0o755)
            self.assertEqual(proc.resolve_cli('fakecli', path=str(root)), str((root / 'fakecli').resolve()))
        self.assertIsNone(proc.resolve_cli('definitely-not-a-cli-x9', path=str(root)))


if __name__ == '__main__':
    unittest.main()
