"""Windows readiness gate (static + mocked; Windows itself is not available to the test suite)."""
import ast
import importlib
import os
from pathlib import Path
import pkgutil
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

import aether
from aether import security
from aether.studio import adapter_sandbox, fsperm
from aether.studio.executors import adapter as adapter_exec

ROOT = Path(aether.__file__).resolve().parent
# Documented ACCEPTED-LINUX-ONLY files (see docs/studio/WINDOWS_AUDIT.md): the adapter sandbox is Linux-only and is
# disabled with a message elsewhere. They may mention these names; nothing else may.
LINUX_ONLY_ALLOW = {'studio/executors/adapter.py', 'studio/adapter_sandbox.py'}
PROC_ONLY = {'studio/proc.py'}
CLI_PATH = ['studio/exec_queue.py', 'studio/driver.py', 'studio/slots.py', 'studio/limits.py', 'studio/proc.py',
            'studio/queue_api.py', 'studio/fsperm.py', 'security.py', 'studio/runner.py', 'studio/cli_events.py',
            'studio/run_log.py']


def sources():
    for path in sorted(ROOT.rglob('*.py')):
        yield path.relative_to(ROOT).as_posix(), path.read_text(encoding='utf-8')


class StaticGate(unittest.TestCase):
    def test_no_fcntl_fork_shell(self):
        for rel, text in sources():
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertNotIn('fcntl', [a.name.split('.')[0] for a in node.names], rel)
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual((node.module or '').split('.')[0], 'fcntl', rel)
                if isinstance(node, ast.Attribute) and node.attr in ('fork', 'forkpty') \
                        and isinstance(node.value, ast.Name) and node.value.id == 'os':
                    self.fail(f'{rel}:{node.lineno} os.{node.attr}')
                if isinstance(node, ast.keyword) and node.arg == 'shell':
                    self.assertFalse(isinstance(node.value, ast.Constant) and node.value.value is True,
                                     f'{rel}:{node.value.lineno} shell=True')
            self.assertIsNone(re.search(r'\bfcntl\b', text), rel)

    def test_kill_signals_confined(self):
        for rel, text in sources():
            if rel in PROC_ONLY:
                continue
            for word in ('SIGKILL', 'killpg'):
                if word in text:
                    self.assertIn(rel, LINUX_ONLY_ALLOW, f'{rel} mentions {word}')

    def test_allowlist_files_are_documented(self):
        doc = (ROOT.parent / 'docs/studio/WINDOWS_AUDIT.md').read_text(encoding='utf-8')
        for rel in LINUX_ONLY_ALLOW:
            self.assertIn('ACCEPTED-LINUX-ONLY', doc)
            self.assertIn(Path(rel).name, doc)

    def test_no_bare_text_open_in_cli_path(self):
        for rel in CLI_PATH:
            tree = ast.parse((ROOT / rel).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else None
                attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
                kws = {k.arg for k in node.keywords}
                if name == 'open':
                    mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else \
                        next((k.value.value for k in node.keywords if k.arg == 'mode'), 'r')
                    if 'b' not in str(mode):
                        self.assertIn('encoding', kws, f'{rel}:{node.lineno} open() without encoding')
                if attr in ('read_text', 'write_text'):
                    self.assertIn('encoding', kws, f'{rel}:{node.lineno} {attr}() without encoding')

    def test_no_os_rename_or_unguarded_posix_calls(self):
        for rel, text in sources():
            self.assertNotIn('os.rename(', text, rel)
            if rel in LINUX_ONLY_ALLOW:
                continue
            lines = text.splitlines()
            for m in re.finditer(r'os\.(O_NOFOLLOW|O_NONBLOCK|getuid)\b', text):
                n = text[:m.start()].count('\n')
                window = '\n'.join(lines[max(0, n - 12):n + 1])
                self.assertTrue('hasattr(os' in window or 'getattr(os' in lines[n],
                                f'{rel}:{n + 1} unguarded {m.group(0)}')


class MockedWindows(unittest.TestCase):
    def test_sandbox_reports_unavailable(self):
        with mock.patch.object(os, 'name', 'nt'), mock.patch.object(sys, 'platform', 'win32'):
            ok, message = adapter_sandbox.availability()
            self.assertFalse(ok)
            self.assertIn('Linux', message)
            self.assertEqual(adapter_exec.sandbox_status(), (False, message))
            with self.assertRaises(adapter_exec.Refused) as ctx:
                adapter_exec.AdapterExecutor._open(mock.Mock(), {'id': 1}, 'x')
            self.assertEqual(ctx.exception.code, 'sandbox_unavailable')

    def test_sandbox_darwin_unavailable(self):
        with mock.patch.object(sys, 'platform', 'darwin'):
            self.assertFalse(adapter_sandbox.availability()[0])

    def test_static_reason_blocks_adapter(self):
        ex = mock.Mock()
        adapter = {'revoked': False, 'granted': ['observe'], 'script': 'x', 'script_sha256': 'y'}
        with mock.patch.object(sys, 'platform', 'win32'):
            self.assertIn('sandbox unavailable', adapter_exec.AdapterExecutor._static(ex, adapter))

    def test_sandbox_never_runs_unconfined(self):
        with mock.patch.object(sys, 'platform', 'win32'), mock.patch.object(os, 'name', 'nt'):
            with self.assertRaises(adapter_sandbox.Unavailable):
                adapter_sandbox._libc()

    def test_posix_predicate_follows_os_name(self):
        with mock.patch.object(os, 'name', 'nt'):
            self.assertFalse(security._posix())
            self.assertEqual(security._open_flags() & (getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)),
                             getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))   # flags are not os.name-gated

    def test_security_helpers_skip_getuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'priv'
            token = directory / 'owner.token'
            # pathlib cannot be used with os.name patched, so the predicate itself is patched for the I/O part.
            with mock.patch.object(security, '_posix', return_value=False), \
                    mock.patch.object(os, 'getuid', side_effect=AssertionError('getuid called'), create=True), \
                    mock.patch.object(fsperm, 'restrict', return_value=True) as restrict:
                security.private_directory(directory)
                fd = security.private_open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                os.close(fd)
                self.assertTrue(security.owned_by_me(token.stat()))
                self.assertTrue(restrict.called)
            self.assertTrue(stat.S_ISREG(token.stat().st_mode))

    def test_security_flags_without_posix_constants(self):
        with mock.patch.object(os, 'O_NOFOLLOW', create=True), mock.patch.object(os, 'O_NONBLOCK', create=True):
            del os.O_NOFOLLOW, os.O_NONBLOCK
            self.assertNotIn('O_NOFOLLOW', dir(os))
            self.assertEqual(security._open_flags(), getattr(os, 'O_BINARY', 0))

    def test_posix_checks_still_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            loose = Path(tmp) / 'loose'
            loose.mkdir()
            os.chmod(loose, 0o755)
            with self.assertRaises(security.SecurityError):
                security.private_directory(loose)


class ImportsWithoutPosixModules(unittest.TestCase):
    def test_every_module_imports_without_fcntl(self):
        names = [m.name for m in pkgutil.walk_packages([str(ROOT)], 'aether.') if not m.name.endswith('__main__')]
        self.assertGreater(len(names), 20)
        saved = {k: sys.modules.get(k) for k in ('fcntl', 'resource', 'termios', 'pwd', 'grp')}
        try:
            for k in saved:
                sys.modules[k] = None          # `import k` now raises ImportError
            for name in names:
                sys.modules.pop(name, None)
            for name in names:
                try:
                    importlib.import_module(name)
                except ImportError as error:
                    self.fail(f'{name} failed to import without POSIX modules: {error}')
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
            for name in names:
                sys.modules.pop(name, None)
            for name in names:
                importlib.import_module(name)

    def test_sandbox_module_imports_without_resource(self):
        name = 'aether.studio.adapter_sandbox'
        saved = sys.modules.pop(name, None)
        try:
            with mock.patch.dict(sys.modules, {'resource': None}):
                module = importlib.import_module(name)
                self.assertIsNone(module.resource)
                with self.assertRaises(module.Unavailable):
                    module.limit_resources()
        finally:
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved


if __name__ == '__main__':
    unittest.main()
