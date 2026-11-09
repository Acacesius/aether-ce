"""Queue driver with fake Claude CLIs: slot order, caps, limit stop + switch, persistence, pause, cancel, recovery."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import threading
import unittest

from aether.studio.driver import Driver
from aether.studio.exec_queue import QueueError, QueueStore
from aether.studio.executors.cli import CliExecutor
from aether.studio.slots import SlotStore
from aether.studio.store import Store

from tests.studio import support
from tests.studio.test_cli_executor import alive

FAKES = Path(__file__).resolve().parents[1] / 'fakes'


class DriverBase(unittest.TestCase):
    slot_ids = ('claude-a', 'claude-b')

    def setUp(self):
        self.root = support.tempdir(self)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.ws = self.root / 'ws'
        self.ws.mkdir()
        self.s = support.store(self, self.data / 'studio.sqlite3')
        self.build()

    def build(self, **driver_kw):
        self.queue = QueueStore(self.s, self.data)
        self.queue.ensure()
        self.slots = SlotStore(self.data)
        self.slots.save([{'id': sid, 'kind': 'claude', 'label': sid, 'config_dir': str(self.root / f'cfg-{sid}'), 'order': i}
                         for i, sid in enumerate(self.slot_ids)])
        base_env = {k: os.environ[k] for k in ('PATH', 'SystemRoot', 'TEMP', 'TMP') if k in os.environ}
        self.executor = CliExecutor(self.data, slots=self.slots, base_env=base_env,
                                    commands={'claude': [sys.executable, str(FAKES / 'fake_claude.py')]})
        options = dict(tick_seconds=0.02, idle_seconds=0.2, grace_seconds=0)
        options.update(driver_kw)
        self.driver = Driver(self.queue, self.slots, self.executor, **options)
        self.addCleanup(self.driver.stop)
        return self.driver

    def workspace(self, name='w'):
        path = self.root / name
        path.mkdir(exist_ok=True)
        return str(path)

    def add(self, prompt='success', slot='auto', ws=None):
        return self.queue.add(ws or self.workspace(), prompt, slot)['run_id']

    def run_until(self, run_id, *states, timeout=30):
        def check():
            self.driver.tick()
            item = self.queue.get(run_id)
            return item if item['state'] in states else None
        return support.wait_for(check, timeout=timeout, step=0.02)

    def slot_of(self, item):
        return [a['slot_id'] for a in item['attempts']]


class SelectionTests(DriverBase):
    def test_first_slot_in_order_runs_and_item_is_done(self):
        run_id = self.add()
        item = self.run_until(run_id, 'done')
        self.assertEqual(self.slot_of(item), ['claude-a'])
        self.assertEqual(item['attempts'][0]['state'], 'succeeded')
        self.assertEqual(self.queue.active_count('claude-a'), 0)

    def test_disabled_and_missing_cli_slots_are_skipped(self):
        self.slots.save([{'id': 'claude-a', 'kind': 'claude', 'label': 'a', 'config_dir': str(self.root / 'cfg-a'),
                          'order': 0, 'enabled': False},
                         {'id': 'claude-b', 'kind': 'claude', 'label': 'b', 'config_dir': str(self.root / 'cfg-b'), 'order': 1}])
        item = self.run_until(self.add(), 'done')
        self.assertEqual(self.slot_of(item), ['claude-b'])

    def test_pinned_slot_is_used_and_waits_when_blocked(self):
        item = self.run_until(self.add(slot='claude-b'), 'done')
        self.assertEqual(self.slot_of(item), ['claude-b'])
        self.slots.block('claude-b', datetime.now(timezone.utc) + timedelta(hours=1))
        run_id = self.add(slot='claude-b')
        for _ in range(5):
            self.driver.tick()
        self.assertEqual(self.queue.get(run_id)['state'], 'queued')       # pinned: never moved to claude-a

    def test_failure_is_recorded(self):
        item = self.run_until(self.add('crash'), 'failed')
        self.assertEqual(item['reason'], 'exit_nonzero')
        self.assertEqual(item['attempts'][0]['exit_code'], 3)


class ConcurrencyTests(DriverBase):
    def running(self):
        return self.queue.list('running')

    def test_default_is_one_at_a_time(self):
        first, second = self.add('hold 1'), self.add('hold 1')
        for _ in range(3):
            self.driver.tick()
        self.assertEqual([i['run_id'] for i in self.running()], [first])
        self.assertEqual(self.queue.get(second)['state'], 'queued')
        self.run_until(second, 'done')

    def test_global_cap_two_uses_two_slots(self):
        self.build(global_cap=2)
        ids = [self.add('hold 2') for _ in range(3)]
        self.driver.tick()
        running = self.running()
        self.assertEqual(len(running), 2)
        self.assertEqual(sorted(self.slot_of(self.queue.get(i['run_id']))[0] for i in running), ['claude-a', 'claude-b'])
        self.assertEqual(self.queue.get(ids[2])['state'], 'queued')

    def test_slot_cap_limits_one_slot(self):
        self.slot_ids = ('claude-a',)
        self.build(global_cap=3, slot_cap=1)
        for _ in range(2):
            self.add('hold 2')
        self.driver.tick()
        self.assertEqual(len(self.running()), 1)
        self.build(global_cap=3, slot_cap={'*': 2})
        self.driver.tick()
        self.assertEqual(len(self.running()), 2)

    def test_caps_are_clamped(self):
        driver = Driver(self.queue, self.slots, self.executor, global_cap=99, slot_cap='x')
        self.assertEqual((driver.global_cap, driver.slot_cap['*']), (4, 1))
        self.assertEqual(Driver(self.queue, self.slots, self.executor, global_cap=0).global_cap, 1)


class LimitTests(DriverBase):
    def test_limit_stop_requeues_at_front_and_switches_provider(self):
        ws = self.workspace('shared')            # limit_first keeps its marker in the workspace
        first = self.add('limit_first', ws=ws)
        other = self.add('success', ws=self.workspace('other'))
        self.driver.tick()
        support.wait_for(lambda: (self.driver.tick(), self.queue.get(first)['attempts'][:1] and
                                  self.queue.get(first)['attempts'][0]['state'] == 'limit_stop')[1])
        self.assertIsNotNone(self.slots.blocked_until('claude-a'))
        item = self.run_until(first, 'done')
        self.assertEqual([(a['slot_id'], a['state']) for a in item['attempts']],
                         [('claude-a', 'limit_stop'), ('claude-b', 'succeeded')])
        switches = [e for e in item['events'] if e['type'] == 'provider_switch']
        self.assertEqual([(e['from'], e['to'], e['reason']) for e in switches], [('claude-a', 'claude-b', 'limit_stop')])
        self.assertLess(item['position'], self.queue.get(other)['position'])      # re-queued at the FRONT
        slot_state = self.queue.slot_rows()['claude-a']
        self.assertTrue(slot_state['last_limit_at'])
        self.assertEqual(slot_state['blocked_until'], self.slots.state()['claude-a']['blocked_until'])
        actions = [e['action'] for e in self.queue.audit_entries()]
        for action in ('driver.start', 'driver.limit_stop', 'driver.requeue', 'driver.provider_switch', 'driver.succeeded'):
            self.assertIn(action, actions)

    def test_no_slot_available_stays_queued_and_wakes_at_earliest_block(self):
        until_a = datetime.now(timezone.utc) + timedelta(seconds=30)
        self.slots.block('claude-a', until_a)
        self.slots.block('claude-b', until_a + timedelta(hours=1))
        run_id = self.add()
        self.driver.idle_seconds = 3600
        self.driver.tick()
        self.assertEqual(self.queue.get(run_id)['state'], 'queued')
        wait = self.driver._wait_seconds()
        self.assertAlmostEqual(wait, 30, delta=2)                      # not the idle interval: the earliest block ends
        self.assertEqual(self.driver._earliest_block(), until_a.replace(microsecond=0))

    def test_block_expiry_releases_the_slot(self):
        now = [datetime.now(timezone.utc)]
        self.slots.clock = lambda: now[0]
        self.slots.block('claude-a', now[0] + timedelta(minutes=5))
        self.slots.block('claude-b', now[0] + timedelta(minutes=5))
        run_id = self.add()
        self.driver.tick()
        self.assertEqual(self.queue.get(run_id)['state'], 'queued')
        now[0] += timedelta(minutes=6)
        self.assertEqual(self.slot_of(self.run_until(run_id, 'done')), ['claude-a'])

    def test_blocked_until_persists_across_driver_instances(self):
        self.slots.block('claude-a', datetime.now(timezone.utc) + timedelta(hours=1), 'usage limit')
        old = self.driver
        old.stop()
        fresh = self.build()                       # new SlotStore, QueueStore, executor and Driver on the same files
        self.assertIsNot(fresh, old)
        item = self.run_until(self.add(), 'done')
        self.assertEqual(self.slot_of(item), ['claude-b'])
        self.assertIn('claude-a', SlotStore(self.data).state())


class PauseCancelReorderTests(DriverBase):
    def test_pause_blocks_new_starts_and_resume_continues(self):
        run_id = self.add()
        self.queue.set_paused(True)
        for _ in range(3):
            self.driver.tick()
        self.assertEqual(self.queue.get(run_id)['state'], 'queued')
        self.assertTrue(self.queue.paused())
        self.build()                                           # pause survives a new driver
        self.assertTrue(self.queue.paused())
        self.queue.set_paused(False)
        self.run_until(run_id, 'done')

    def test_pause_does_not_stop_running_item(self):
        run_id = self.add('hold 1')
        self.driver.tick()
        self.queue.set_paused(True)
        self.run_until(run_id, 'done')

    def test_reorder_changes_run_order(self):
        a, b, c = self.add(), self.add(), self.add()
        self.queue.reorder([c, a, b])
        self.assertEqual([i['run_id'] for i in self.queue.queued()], [c, a, b])
        self.driver.tick()
        self.assertEqual([i['run_id'] for i in self.queue.list('running')], [c])
        self.assertEqual(self.queue.get(a)['state'], 'queued')
        self.run_until(c, 'done')
        for bad in ([a, b], [a, a, b], [a, b, 'que_x']):
            with self.assertRaises(QueueError):
                self.queue.reorder(bad)

    def test_cancel_queued(self):
        run_id = self.add()
        self.assertEqual(self.driver.cancel(run_id), 'cancelled')
        self.assertEqual(self.queue.get(run_id)['state'], 'cancelled')
        with self.assertRaises(QueueError) as ctx:
            self.driver.cancel(run_id)
        self.assertEqual(ctx.exception.status, 409)

    @unittest.skipUnless(os.name != 'nt', 'pid liveness probe is POSIX')
    def test_cancel_running_kills_process_tree(self):
        run_id = self.add('slow')
        self.driver.tick()
        pidfile = self.root / 'cfg-claude-a' / 'child.pid'
        pid = int(support.wait_for(lambda: pidfile.exists() and pidfile.read_text() or None))
        self.assertTrue(alive(pid))
        self.driver.cancel(run_id)
        support.wait_for(lambda: not alive(pid), timeout=15)
        item = self.queue.get(run_id)
        self.assertEqual((item['state'], item['attempts'][0]['state']), ('cancelled', 'cancelled'))
        self.assertEqual(self.queue.active_count('claude-a'), 0)
        self.assertNotIn(run_id, self.driver.running)


class RecoveryAndThreadTests(DriverBase):
    def test_restart_recovery_blocks_running_and_resets_counts(self):
        running, waiting = self.add('slow'), self.add()
        self.driver.tick()
        self.assertEqual(self.queue.get(running)['state'], 'running')
        self.assertEqual(self.queue.active_count('claude-a'), 1)
        self.driver.stop()                       # kills the CLI; rows still say running (as after a crash)
        self.assertEqual(self.queue.get(running)['state'], 'running')
        fresh = self.build()
        result = fresh.recover()
        self.assertEqual(result['blocked'], [running])
        item = self.queue.get(running)
        self.assertEqual((item['state'], item['reason']), ('blocked', 'owner_action_required'))
        self.assertEqual(item['attempts'][0]['state'], 'interrupted')
        self.assertEqual(self.queue.get(waiting)['state'], 'queued')
        self.assertEqual(self.queue.active_count('claude-a'), 0)
        for _ in range(3):
            fresh.tick()
        self.assertEqual(self.queue.get(running)['state'], 'blocked')          # nothing re-executes by itself
        fresh.resume_item(running)
        self.assertEqual(self.queue.get(running)['state'], 'queued')
        with self.assertRaises(QueueError):
            fresh.resume_item(running)

    def test_thread_runs_queue_and_stops(self):
        before = {t.name for t in threading.enumerate()}
        self.assertNotIn('exec-queue-driver', before)
        self.driver.start()
        self.assertTrue(self.driver.alive)
        run_id = self.add()
        self.driver.wake()
        support.wait_for(lambda: self.queue.get(run_id)['state'] == 'done', timeout=30)
        self.driver.stop()
        self.assertFalse(self.driver.alive)
        self.assertNotIn('exec-queue-driver', {t.name for t in threading.enumerate()})

    def test_audit_chain_verifies_and_is_append_only(self):
        self.run_until(self.add(), 'done')
        verify = self.queue.verify_audit()
        self.assertTrue(verify['ok'])
        self.assertGreaterEqual(verify['entries'], 4)
        with self.assertRaises(Exception):
            with self.s.tx() as db:
                db.execute("UPDATE queue_audit SET actor='x'")
        with self.s.tx() as db:
            pass
        self.assertTrue(self.queue.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
