"""CLI_EXECUTOR: CliExecutor with fake Claude / Codex CLIs (workspace rules, argv, env, events, limits, kill)."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest

from aether.studio.executors.base import ExecutorUnavailable, RunView, StageView
from aether.studio.executors.cli import (CLAUDE_ALLOWED, CliExecutor, WorkspaceError, build_argv, build_env,
                                         validate_workspace)
from aether.studio.slots import SlotStore

from tests.studio import support

FAKES = Path(__file__).resolve().parents[1] / 'fakes'
POSIX = os.name != 'nt'


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f'/proc/{pid}/stat').read_text().split()[2] != 'Z'
    except OSError:
        return True


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []

    def __call__(self, type_, data):
        with self.lock:
            self.events.append((type_, dict(data)))

    def logs(self, event=None):
        with self.lock:
            return [d for t, d in self.events if t == 'stage.log' and (event is None or d.get('event') == event)]

    def of(self, type_):
        with self.lock:
            return [d for t, d in self.events if t == type_]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.root = support.tempdir(self)
        self.data = self.root / 'data'
        self.data.mkdir()

    def test_valid(self):
        ws = self.root / 'ws'
        ws.mkdir()
        self.assertEqual(validate_workspace(str(ws), self.data), ws.resolve())

    def test_rejections(self):
        (self.root / 'file.txt').write_text('x', encoding='utf-8')
        (self.data / 'inner').mkdir()
        cases = {'': 'required', None: 'required', 'relative/ws': 'absolute', str(self.root / 'missing'): 'exist',
                 str(self.root / 'file.txt'): 'directory', str(self.data): 'data dir',
                 str(self.data / 'inner'): 'data dir', str(self.root): 'contain',
                 os.path.abspath(os.sep): 'root'}
        for raw, words in cases.items():
            with self.assertRaises(WorkspaceError, msg=raw) as ctx:
                validate_workspace(raw, self.data)
            self.assertIn(words, str(ctx.exception), raw)

    @unittest.skipUnless(POSIX, 'symlinks')
    def test_symlink_into_data_dir_refused(self):
        link = self.root / 'link'
        link.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            validate_workspace(str(link), self.data)


class CommandLineTests(unittest.TestCase):
    slot = {'id': 'c', 'kind': 'claude', 'config_dir': '/cfg', 'model': 'claude-opus-5-5'}

    def test_claude_argv(self):
        argv = build_argv(self.slot, ['claude'], Path('/ws'))
        self.assertEqual(argv[:8], ['claude', '-p', '--output-format', 'stream-json', '--verbose', '--permission-mode',
                                    'acceptEdits', '--allowedTools'])
        allowed = argv[8].split(',')
        self.assertEqual(tuple(allowed), CLAUDE_ALLOWED)
        for tool in ('Bash', 'WebFetch', 'WebSearch'):
            self.assertNotIn(tool, allowed)
            self.assertIn(tool, argv[argv.index('--disallowedTools') + 1].split(','))
        self.assertEqual(argv[-2:], ['--model', 'claude-opus-5-5'])

    def test_codex_argv_reads_stdin(self):
        argv = build_argv(dict(self.slot, kind='codex', model=None), ['codex'], Path('/ws'))
        self.assertEqual(argv[:3], ['codex', 'exec', '--json'])
        self.assertIn('workspace-write', argv)
        self.assertNotIn('-m', argv)
        self.assertEqual(argv[-1], '-')

    def test_minimal_env(self):
        base = {'PATH': '/bin', 'HOME': '/h', 'SystemRoot': 'C:\\Windows', 'TEMP': '/t',
                'ANTHROPIC_API_KEY': 'x', 'OPENAI_API_KEY': 'y', 'AETHER_EXEC': 'cli', 'RANDOM_VAR': 'z'}
        env = build_env(self.slot, base)
        self.assertEqual(env, {'PATH': '/bin', 'HOME': '/h', 'SystemRoot': 'C:\\Windows', 'TEMP': '/t',
                               'NO_COLOR': '1', 'CLAUDE_CONFIG_DIR': '/cfg'})
        self.assertEqual(build_env(dict(self.slot, kind='codex'), {})['CODEX_HOME'], '/cfg')


class ExecutorTestBase(unittest.TestCase):
    def setUp(self):
        root = support.tempdir(self)
        self.data = root / 'data'
        self.data.mkdir()
        self.ws = root / 'workspace'
        self.ws.mkdir()
        self.claude_cfg = root / 'claude-cfg'
        self.codex_cfg = root / 'codex-home'
        self.now = [datetime.now(timezone.utc)]
        self.slots = SlotStore(self.data, clock=lambda: self.now[0])
        self.slots.save([{'id': 'claude-a', 'kind': 'claude', 'label': 'A', 'config_dir': str(self.claude_cfg), 'order': 0},
                         {'id': 'codex-a', 'kind': 'codex', 'label': 'B', 'config_dir': str(self.codex_cfg), 'order': 1}])
        base_env = {'PATH': os.environ.get('PATH', ''), 'ANTHROPIC_API_KEY': 'must-not-leak'}
        for key in ('SystemRoot', 'TEMP', 'TMP'):
            if key in os.environ:
                base_env[key] = os.environ[key]
        self.executor = CliExecutor(self.data, slots=self.slots, clock=lambda: self.now[0], base_env=base_env,
                                    commands={'claude': [sys.executable, str(FAKES / 'fake_claude.py')],
                                              'codex': [sys.executable, str(FAKES / 'fake_codex.py')]})

    def run_cli(self, prompt, slot='claude-a', timeout=60, wait=30):
        rec = Recorder()
        handle = self.executor.launch(slot, str(self.ws), prompt, emit=rec, run_id='run_1', stage_key='build',
                                      timeout_seconds=timeout)
        result = support.wait_for(lambda: (r := self.executor.poll(handle))['state'] != 'running' and r, timeout=wait)
        return result, rec, handle

    def invocation(self, cfg):
        return json.loads((cfg / 'invocation.json').read_text(encoding='utf-8'))


class ClaudeRunTests(ExecutorTestBase):
    def test_success(self):
        result, rec, _ = self.run_cli('success please')
        self.assertEqual(result, {'state': 'exited', 'exit_code': 0})
        kinds = [d['event'] for d in rec.logs()]
        for kind in ('assistant_text', 'tool_use', 'tool_result', 'usage', 'final_result'):
            self.assertIn(kind, kinds)
        self.assertEqual(rec.of('stage.usage'), [{'tokens': 120, 'cost_usd_cents': 1}])
        self.assertTrue((self.ws / 'fake_output.txt').exists())             # ran in the workspace
        inv = self.invocation(self.claude_cfg)
        self.assertEqual(Path(inv['cwd']).resolve(), self.ws.resolve())
        self.assertEqual(inv['prompt'], 'success please')                    # prompt on stdin...
        self.assertNotIn('success please', inv['argv'])                      # ...never in argv
        self.assertIn('CLAUDE_CONFIG_DIR', inv['env'])
        self.assertNotIn('ANTHROPIC_API_KEY', inv['env'])
        raw = (self.data / 'cli-logs' / 'run_1.log').read_text(encoding='utf-8')
        self.assertIn('[aether] ', raw)
        self.assertIn('"type": "result"', raw)

    def test_limit_stop_blocks_slot_with_reset_time(self):
        result, rec, _ = self.run_cli('limit')
        self.assertEqual(result['state'], 'limit_stop')
        self.assertEqual(result['outcome'], 'limit_stop')
        until = self.slots.blocked_until('claude-a')
        self.assertIsNotNone(until)
        self.assertAlmostEqual((until - self.now[0]).total_seconds(), 7200, delta=60)   # fake reports epoch + 2h
        self.assertEqual(json.loads((self.data / 'slots.json').read_text(encoding='utf-8'))['state']['claude-a']['blocked_until'],
                         result['blocked_until'])
        self.assertTrue(rec.logs('limit_stop'))

    def test_stderr_limit_without_time_defaults_to_five_hours(self):
        result, _, _ = self.run_cli('stderr_limit Error: usage limit reached')
        self.assertEqual(result['state'], 'limit_stop')
        self.assertAlmostEqual((self.slots.blocked_until('claude-a') - self.now[0]).total_seconds(), 5 * 3600, delta=5)

    def test_blocked_slot_is_not_launched(self):
        self.slots.block('claude-a', self.now[0] + timedelta(hours=1))
        result, rec, _ = self.run_cli('success')
        self.assertEqual(result['state'], 'limit_stop')
        self.assertFalse((self.claude_cfg / 'invocation.json').exists())
        self.now[0] += timedelta(hours=2)                                   # block expired -> runs again
        self.assertEqual(self.run_cli('success')[0], {'state': 'exited', 'exit_code': 0})

    def test_auto_picks_next_unblocked_slot(self):
        self.slots.block('claude-a', self.now[0] + timedelta(hours=1))
        result, _, _ = self.run_cli('success', slot='auto')
        self.assertEqual(result, {'state': 'exited', 'exit_code': 0})
        self.assertTrue((self.codex_cfg / 'invocation.json').exists())

    def test_failures_are_not_limit_stops(self):
        self.assertEqual(self.run_cli('crash')[0], {'state': 'exited', 'exit_code': 3})
        self.assertEqual(self.run_cli('error_result')[0], {'state': 'exited', 'exit_code': 1})
        self.assertEqual(self.run_cli('talk_limit')[0], {'state': 'exited', 'exit_code': 2})   # model text ignored
        self.assertIsNone(self.slots.blocked_until('claude-a'))

    def test_crash_garbled_lines_become_raw(self):
        _, rec, _ = self.run_cli('crash')
        self.assertIn('raw', [d['event'] for d in rec.logs()])
        raw = (self.data / 'cli-logs' / 'run_1.log').read_text(encoding='utf-8')
        self.assertIn('[stderr] RuntimeError: fake crash', raw)

    def test_timeout_kills_tree(self):
        started = time.monotonic()
        result, _, _ = self.run_cli('slow', timeout=1, wait=20)
        self.assertEqual(result, {'state': 'exited', 'exit_code': None, 'reason': 'timeout'})
        self.assertLess(time.monotonic() - started, 15)
        if POSIX:
            child = int((self.claude_cfg / 'child.pid').read_text())
            support.wait_for(lambda: not alive(child), timeout=10)

    def test_cancel_kills_tree(self):
        rec = Recorder()
        handle = self.executor.launch('claude-a', str(self.ws), 'slow', emit=rec, run_id='run_c', timeout_seconds=60)
        pidfile = self.claude_cfg / 'child.pid'
        child = int(support.wait_for(lambda: pidfile.exists() and pidfile.read_text()))
        self.executor.cancel(handle, 0)
        result = support.wait_for(lambda: (r := self.executor.poll(handle))['state'] != 'running' and r, timeout=20)
        self.assertEqual(result['reason'], 'cancelled_by_owner')
        if POSIX:
            support.wait_for(lambda: not alive(child), timeout=10)
        self.executor.cancel(handle, 0)   # after the end: no-op

    def test_launch_refusals(self):
        rec = Recorder()
        with self.assertRaises(WorkspaceError):
            self.executor.launch('claude-a', str(self.data), 'success', emit=rec)
        with self.assertRaises(ExecutorUnavailable):
            self.executor.launch('ghost', str(self.ws), 'success', emit=rec)
        with self.assertRaises(ValueError):
            self.executor.launch('claude-a', str(self.ws), '   ', emit=rec)
        codes = [d['code'] for d in rec.of('executor.error')]
        self.assertEqual(codes, ['workspace_invalid', 'slot_unavailable', 'prompt_invalid'])
        missing = CliExecutor(self.data, slots=self.slots, commands={})
        missing.command = lambda kind: None
        with self.assertRaises(ExecutorUnavailable):
            missing.launch('claude-a', str(self.ws), 'success', emit=rec)

    def test_start_from_run_snapshot(self):
        snapshot = {'executor': {'kind': 'cli', 'ref': 'claude-a', 'workspace': str(self.ws)},
                    'stages': [{'key': 'build', 'prompt': 'success from snapshot'}]}
        rec = Recorder()
        handle = self.executor.start(RunView('run_s', False, snapshot),
                                     StageView('build', 1, 'cli:ignored', 30), rec)
        support.wait_for(lambda: self.executor.poll(handle)['state'] != 'running', timeout=30)
        self.assertEqual(self.invocation(self.claude_cfg)['prompt'], 'success from snapshot')

    def test_health(self):
        health = self.executor.health()
        self.assertEqual(health['status'], 'ready')
        self.assertEqual([s['id'] for s in health['slots']], ['claude-a', 'codex-a'])
        self.assertEqual(CliExecutor(support.tempdir(self)).health()['detail'], 'no slot configured')


class CodexRunTests(ExecutorTestBase):
    def test_success(self):
        result, rec, _ = self.run_cli('success', slot='codex-a')
        self.assertEqual(result, {'state': 'exited', 'exit_code': 0})
        kinds = [d['event'] for d in rec.logs()]
        for kind in ('assistant_text', 'tool_use', 'tool_result', 'usage'):
            self.assertIn(kind, kinds)
        self.assertEqual(rec.of('stage.usage'), [{'tokens': 230}])
        self.assertTrue((self.ws / 'codex_output.txt').exists())
        inv = self.invocation(self.codex_cfg)
        self.assertEqual(inv['argv'][:2], ['exec', '--json'])
        self.assertIn('CODEX_HOME', inv['env'])

    def test_limit_relative_reset(self):
        result, _, _ = self.run_cli('limit', slot='codex-a')
        self.assertEqual(result['state'], 'limit_stop')
        self.assertAlmostEqual((self.slots.blocked_until('codex-a') - self.now[0]).total_seconds(), 2 * 3600 + 300, delta=5)

    def test_crash(self):
        self.assertEqual(self.run_cli('crash', slot='codex-a')[0], {'state': 'exited', 'exit_code': 101})
        self.assertIsNone(self.slots.blocked_until('codex-a'))


class FlagTests(unittest.TestCase):
    def build(self, env):
        from aether.studio.app import build
        data = support.tempdir(self)
        os.chmod(data, 0o700)
        app = build(support.ce_mode(data), assist_env={}, exec_env=env)
        self.addCleanup(app.store.close)
        return app, data

    def test_off_by_default(self):
        for env in ({}, {'AETHER_EXEC': ''}, {'AETHER_EXEC': 'CLI'}, {'AETHER_EXEC': 'mock'}):
            app, data = self.build(env)
            self.assertEqual(sorted(app.api.executors), ['adapter', 'mock'], env)
            self.assertFalse((data / 'slots.json').exists())
            self.assertFalse((data / 'cli-logs').exists())

    def test_on(self):
        app, _ = self.build({'AETHER_EXEC': 'cli'})
        self.assertIn('cli', app.api.executors)
        self.assertIsInstance(app.api.executors['cli'], CliExecutor)


if __name__ == '__main__':
    unittest.main()
