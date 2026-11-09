"""CE core gates (K4): slot selection edge cases, persistence across app instances, flag-off parity, restart order,
Windows process branch with a mocked platform, and source-level portability rules (grep/AST over aether/ and tools/)."""
import ast
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest import mock

from aether.studio import proc
from aether.studio.app import build
from aether.studio.driver import MAX_CAP, Driver, clamp_cap
from aether.studio.exec_queue import QueueStore
from aether.studio.queue_api import QueueAPI

from tests.studio import support
from tests.studio.test_driver import DriverBase

ROOT = Path(__file__).resolve().parents[2]


class SlotSelectionTests(DriverBase):
    def save(self, *specs):
        self.slots.save([{'id': sid, 'kind': kind, 'label': sid, 'config_dir': str(self.root / f'cfg-{sid}'),
                          'order': order, **extra} for sid, kind, order, extra in specs])

    def test_order_field_beats_listing_order_and_id(self):
        self.save(('claude-a', 'claude', 5, {}), ('claude-b', 'claude', 1, {}), ('claude-c', 'claude', 3, {}))
        self.assertEqual([s['id'] for s in self.slots.load()], ['claude-b', 'claude-c', 'claude-a'])
        self.assertEqual(self.slot_of(self.run_until(self.add(), 'done')), ['claude-b'])

    def test_disabled_first_blocked_second_picks_third(self):
        self.save(('claude-a', 'claude', 0, {'enabled': False}), ('claude-b', 'claude', 1, {}), ('claude-c', 'claude', 2, {}))
        self.slots.block('claude-b', datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(self.slot_of(self.run_until(self.add(), 'done')), ['claude-c'])

    def test_choose_slot_skips_slot_at_its_cap(self):
        self.build(global_cap=3, slot_cap=1)
        self.add('hold 2')
        self.driver.tick()
        self.assertEqual(self.queue.active_count('claude-a'), 1)
        self.assertEqual(self.driver.choose_slot('auto', self.slots.load())['id'], 'claude-b')
        self.assertIsNone(self.driver.choose_slot('claude-a', self.slots.load()))      # pinned and full: waits

    def test_auto_kind_filter_and_unknown_pinned_slot(self):
        slots = self.slots.load()
        self.assertEqual(self.driver.choose_slot('auto:claude', slots)['id'], 'claude-a')
        self.assertIsNone(self.driver.choose_slot('auto:codex', slots))
        self.assertIsNone(self.driver.choose_slot('no-such-slot', slots))
        run_id = self.add(slot='no-such-slot')
        self.driver.tick()
        self.assertEqual(self.queue.get(run_id)['state'], 'queued')

    def test_missing_cli_kind_slot_is_skipped_for_auto(self):
        self.save(('codex-a', 'codex', 0, {}), ('claude-a', 'claude', 1, {}))
        self.executor.commands.pop('codex', None)
        with mock.patch.object(self.executor, 'command', side_effect=lambda kind: ['x'] if kind == 'claude' else None):
            self.assertEqual(self.driver.choose_slot('auto', self.slots.load())['id'], 'claude-a')

    def test_cap_defaults_and_clamping(self):
        driver = Driver(self.queue, self.slots, self.executor)
        self.assertEqual((driver.global_cap, driver.slot_cap['*']), (1, 1))
        for value, expected in ((None, 1), (-5, 1), (0, 1), (1, 1), (4, 4), (5, 4), ('99', 4), ('2', 2), ('abc', 1), (3.9, 3)):
            self.assertEqual(clamp_cap(value), expected, value)
        self.assertEqual(MAX_CAP, 4)
        per = Driver(self.queue, self.slots, self.executor, slot_cap={'claude-a': 99, '*': 2})
        self.assertEqual((per._cap('claude-a'), per._cap('claude-b')), (4, 2))
        self.assertEqual(Driver(self.queue, self.slots, self.executor, slot_cap={'claude-a': 3})._cap('claude-b'), 1)

    def test_env_caps_reach_the_driver_clamped(self):
        for env, expected in (({}, (1, 1)), ({'AETHER_EXEC_MAX_RUNS': '99', 'AETHER_EXEC_SLOT_CAP': '99'}, (4, 4)),
                              ({'AETHER_EXEC_MAX_RUNS': '2', 'AETHER_EXEC_SLOT_CAP': 'junk'}, (2, 1))):
            app = build(support.ce_mode(support.tempdir(self)), exec_env={'AETHER_EXEC': 'cli', **env}, **support.FAST)
            self.addCleanup(app.store.close)
            self.assertEqual((app.driver.global_cap, app.driver.slot_cap['*']), expected, env)

    def test_limit_stop_attempt_is_not_failed(self):
        ws = self.workspace('limitws')
        first = self.add('limit_first', ws=ws)
        support.wait_for(lambda: (self.driver.tick(), [a['state'] for a in self.queue.get(first)['attempts']][:1] == ['limit_stop'])[1])
        item = self.queue.get(first)
        self.assertNotEqual(item['state'], 'failed')
        self.assertEqual(item['attempts'][0]['state'], 'limit_stop')


class PersistenceAcrossAppsTests(unittest.TestCase):
    ENV = {'AETHER_EXEC': 'cli'}

    def make(self, data):
        app = build(support.ce_mode(data), exec_env=self.ENV, **support.FAST)
        self.addCleanup(app.store.close)
        return app

    def test_block_survives_fresh_app_and_expiry_unblocks(self):
        data = support.tempdir(self)
        os.chmod(data, 0o700)
        cfg = support.tempdir(self)
        first = self.make(data)
        first.driver.slots.save([{'id': 'claude-a', 'kind': 'claude', 'label': 'a', 'config_dir': str(cfg / 'a')}])
        until = datetime.now(timezone.utc) + timedelta(hours=1)
        first.driver.slots.block('claude-a', until, 'usage limit')
        first.store.close()
        second = self.make(data)
        status, body = second.api.execq.handle('GET', '/api/studio/slots', None)
        self.assertEqual(status, 200)
        self.assertEqual(body['slots'][0]['blocked_until'], until.strftime('%Y-%m-%dT%H:%M:%SZ'))
        later = [until + timedelta(minutes=1)]
        second.driver.slots.clock = lambda: later[0]
        self.assertIsNone(second.driver.slots.blocked_until('claude-a'))
        self.assertEqual([s['id'] for s in second.driver.slots.available()], ['claude-a'])
        second.driver.slots.clock = lambda: until - timedelta(minutes=1)
        self.assertEqual(second.driver.slots.available(), [])
        status, body = second.api.execq.handle('POST', '/api/studio/slots/claude-a/clear-limit', {})
        self.assertEqual(status, 200)
        self.assertIsNone(body['slots'][0]['blocked_until'])
        third = self.make(data)
        self.assertEqual(third.driver.slots.state(), {})


class RestartOrderTests(DriverBase):
    def test_recovery_keeps_queue_order_of_waiting_items(self):
        running = self.add('slow')
        waiting = [self.add() for _ in range(3)]
        self.driver.tick()
        before = [i['run_id'] for i in self.queue.queued()]
        self.assertEqual(before, waiting)
        self.driver.stop()
        fresh = self.build()
        fresh.recover()
        self.assertEqual([i['run_id'] for i in self.queue.queued()], waiting)
        self.assertEqual(self.queue.get(running)['state'], 'blocked')
        self.assertEqual(self.queue.get(running)['reason'], 'owner_action_required')
        fresh.resume_item(running)
        self.assertEqual(self.queue.get(running)['state'], 'queued')        # resume re-queues; waiting items stay queued
        self.assertEqual(set(i['run_id'] for i in self.queue.queued()), {running, *waiting})


class FlagOffParityTests(unittest.TestCase):
    def test_unset_env_means_todays_behaviour(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AETHER_EXEC')}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertNotIn('AETHER_EXEC', os.environ)
            before = {t.name for t in __import__('threading').enumerate()}
            app = build(support.ce_mode(self._dir()), **support.FAST)
        self.addCleanup(app.store.close)
        self.assertIsNone(app.driver)
        self.assertIsNone(app.api.execq)
        self.assertEqual(sorted(app.api.executors), ['adapter', 'mock'])
        self.assertEqual(QueueStore.tables_exist(app.store), [])
        self.assertNotIn('exec-queue-driver', before)
        with app.store.tx() as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({n for n in names if n.startswith(('queue', 'slot'))}, names)
        self.assertFalse((app.mode.data_dir / 'slots.json').exists())

    def _dir(self):
        data = support.tempdir(self)
        os.chmod(data, 0o700)
        return data

    def test_off_routes_404_including_tracker_and_log(self):
        app, client = support.serve(self, exec_env={})
        for path in ('/queue', '/slots', '/tracker', '/runs/que_00000000000000000000000000/log',
                     '/runs/que_00000000000000000000000000/log/stream'):
            self.assertEqual(client.request('GET', '/api/studio' + path)[0], 404, path)
        self.assertEqual(client.request('POST', '/api/studio/slots/claude-a/clear-limit', {},
                                        headers={'Origin': client.origin})[0], 404)
        status, body = client.request('GET', '/api/studio/mode')
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body['executors']), ['adapter', 'mock'])

    def test_other_values_do_not_enable(self):
        for value in ('', '0', 'off', 'CLI', 'cli '):
            app = build(support.ce_mode(self._dir()), exec_env={'AETHER_EXEC': value}, **support.FAST)
            self.addCleanup(app.store.close)
            self.assertIsNone(app.driver, value)


class WindowsProcessTests(unittest.TestCase):
    def test_start_uses_new_process_group_and_no_shell(self):
        popen = mock.Mock(spec=subprocess.Popen)
        popen.pid = 7
        with mock.patch.object(proc, 'IS_WINDOWS', True), \
                mock.patch.object(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, create=True), \
                mock.patch('aether.studio.proc.subprocess.Popen', return_value=popen) as fake:
            handle = proc.start(['tool.exe', '--x'], 'C:/ws', {'A': 'b'})
        self.assertTrue(handle.windows)
        args, kwargs = fake.call_args
        self.assertEqual(args[0], ['tool.exe', '--x'])
        self.assertEqual(kwargs['creationflags'], 0x200)
        self.assertNotIn('start_new_session', kwargs)
        self.assertIs(kwargs['shell'], False)
        self.assertIsInstance(args[0], list)

    def test_posix_start_uses_new_session_not_creationflags(self):
        popen = mock.Mock(spec=subprocess.Popen)
        popen.pid = 7
        with mock.patch.object(proc, 'IS_WINDOWS', False), \
                mock.patch('aether.studio.proc.subprocess.Popen', return_value=popen) as fake:
            handle = proc.start(['tool'], '/ws', {})
        self.assertFalse(handle.windows)
        self.assertTrue(fake.call_args.kwargs['start_new_session'])
        self.assertNotIn('creationflags', fake.call_args.kwargs)
        self.assertIs(fake.call_args.kwargs['shell'], False)

    def test_kill_tree_on_windows_platform_uses_taskkill_only(self):
        popen = mock.Mock(spec=subprocess.Popen)
        popen.pid = 99
        popen.poll.return_value = None
        with mock.patch.object(proc, 'IS_WINDOWS', True):
            handle = proc.ProcHandle(popen, ('x',))
        with mock.patch('aether.studio.proc.subprocess.run') as run, mock.patch('aether.studio.proc.os.killpg', create=True) as killpg, \
                mock.patch('aether.studio.proc.threading.Timer') as timer:
            proc.kill_tree(handle, grace_seconds=5)
        self.assertEqual(run.call_args.args[0], ['taskkill', '/T', '/F', '/PID', '99'])
        self.assertIs(run.call_args.kwargs['shell'], False)
        killpg.assert_not_called()
        timer.assert_not_called()


# ------------------------------------------------------------------ source rules
def sources():
    files = []
    for top in ('aether', 'tools'):
        files += [p for p in sorted((ROOT / top).rglob('*.py')) if '__pycache__' not in p.parts]
    return files


def rel(path):
    return path.relative_to(ROOT).as_posix()


PROC = 'aether/studio/proc.py'


class SourceRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = {rel(p): p.read_text(encoding='utf-8') for p in sources()}
        cls.trees = {name: ast.parse(text) for name, text in cls.texts.items()}

    def offenders(self, pattern, allow=()):
        regex = re.compile(pattern)
        return [f'{name}:{n}: {line.strip()}' for name, text in self.texts.items() if name not in allow
                for n, line in enumerate(text.splitlines(), 1) if regex.search(line)]

    def used_names(self, name):
        """Identifiers actually used as code: names, attributes, keyword args, imports (not docstrings or comments)."""
        found = set()
        for node in ast.walk(self.trees[name]):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                found.add(node.arg)
            elif isinstance(node, ast.Import):
                found.update(part for a in node.names for part in a.name.split('.'))
            elif isinstance(node, ast.ImportFrom):
                found.update((node.module or '').split('.'))
                found.update(a.name for a in node.names)
        return found

    def strings(self, name):
        return {n.value for n in ast.walk(self.trees[name]) if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    def using(self, names, allow=()):
        return sorted(f'{n}: {sorted(self.used_names(n) & set(names))}' for n in self.trees
                      if n not in allow and self.used_names(n) & set(names))

    def test_files_were_found(self):
        self.assertIn(PROC, self.texts)
        self.assertGreater(len(self.texts), 20)

    def test_no_shell_true(self):
        self.assertEqual(self.offenders(r'shell\s*=\s*True'), [])
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == 'shell':
                    self.assertFalse(isinstance(node.value, ast.Constant) and node.value.value is True, name)
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    self.assertNotIn((node.func.value.id, node.func.attr), (('os', 'system'), ('os', 'popen')), name)

    def test_no_posix_only_primitives(self):
        self.assertEqual(self.using({'fcntl', 'flock', 'lockf', 'fork', 'forkpty', 'tmux'}), [])
        for name in self.trees:
            self.assertFalse({s for s in self.strings(name) if s.split(' ')[0] in ('tmux', 'screen')}
                             - {'tmux.*'}, name)      # capabilities.py only lists tmux among FORBIDDEN names

    def test_process_group_and_kill_primitives_only_in_proc(self):
        names = {'killpg', 'setsid', 'start_new_session', 'SIGKILL', 'SIGTERM', 'CREATE_NEW_PROCESS_GROUP', 'creationflags'}
        self.assertEqual(self.using(names, allow=(PROC,)), [])
        self.assertEqual(sorted(n for n in self.trees if n != PROC and 'taskkill' in self.strings(n)), [])
        used = self.used_names(PROC)
        for needed in ('killpg', 'SIGKILL'):
            self.assertIn(needed, used)
        for needed in ('start_new_session', 'creationflags'):
            self.assertIn(needed, self.strings(PROC))
        self.assertTrue(any(s == 'taskkill' for s in self.strings(PROC)))

    def test_no_signal_handlers_or_os_kill_outside_proc(self):
        bad = []
        for name, tree in self.trees.items():
            if name == PROC:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    if (node.func.value.id, node.func.attr) in (('os', 'kill'), ('signal', 'signal'), ('signal', 'raise_signal')):
                        bad.append(f'{name}:{node.lineno}')
        self.assertEqual(bad, [])

    def test_text_open_has_encoding(self):
        bad = []
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                label = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ''
                if label not in ('open', 'fdopen', 'read_text', 'write_text'):
                    continue
                if label == 'open' and isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in ('os', 'webbrowser', 'tarfile', 'zipfile', 'io'):
                    if fn.value.id == 'os':
                        continue      # os.open is a file descriptor call
                if any(k.arg == 'encoding' for k in node.keywords):
                    continue
                mode = None
                mode_index = 0 if isinstance(fn, ast.Attribute) and label == 'open' and not (
                    isinstance(fn.value, ast.Name) and fn.value.id in ('io', 'codecs')) else 1
                if label in ('read_text', 'write_text'):
                    bad.append(f'{name}:{node.lineno} {label}')
                    continue
                if len(node.args) > mode_index and isinstance(node.args[mode_index], ast.Constant):
                    mode = node.args[mode_index].value
                for k in node.keywords:
                    if k.arg == 'mode' and isinstance(k.value, ast.Constant):
                        mode = k.value.value
                if isinstance(mode, str) and 'b' in mode:
                    continue
                bad.append(f'{name}:{node.lineno} {label}({mode!r})')
        self.assertEqual(bad, [])

    def test_no_shell_in_subprocess_argv(self):
        shells = {'bash', 'sh', 'zsh', 'dash', 'cmd', 'cmd.exe', 'powershell', 'pwsh', '/bin/sh', '/bin/bash'}
        bad = []
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                dotted = f'{fn.value.id}.{fn.attr}' if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else ''
                if dotted.startswith('subprocess.') or (isinstance(fn, ast.Attribute) and fn.attr in ('popen_group',)):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.lower() in shells:
                            bad.append(f'{name}:{sub.lineno}')
        self.assertEqual(bad, [])
        for name in self.trees:
            self.assertFalse({v for v in self.strings(name) if v in ('bash', '/bin/bash', 'bash.exe')}, name)


if __name__ == '__main__':
    unittest.main()
