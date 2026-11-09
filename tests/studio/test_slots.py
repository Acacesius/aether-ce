"""CLI_EXECUTOR: provider slot validation, atomic persistence, limit state, owner-only permissions."""
from datetime import datetime, timedelta, timezone
import json
import os
import stat
import unittest
from unittest import mock

from aether.studio import fsperm, slots
from aether.studio.slots import SlotError, SlotStore

from tests.studio import support


class SlotTestBase(unittest.TestCase):
    def setUp(self):
        root = support.tempdir(self)
        self.data = root / 'data'
        self.data.mkdir()
        self.cfg = root / 'claude-config'
        self.cfg.mkdir()
        self.codex_cfg = root / 'codex-home'

    def slot(self, **kw):
        base = {'id': 'claude-a', 'kind': 'claude', 'label': 'Claude A', 'config_dir': str(self.cfg)}
        base.update(kw)
        return base


class ValidationTests(SlotTestBase):
    def bad(self, code, field, **kw):
        with self.assertRaises(SlotError) as ctx:
            slots.validate_slot(self.slot(**kw), self.data)
        self.assertEqual((ctx.exception.code, ctx.exception.field), (code, field))

    def test_valid_defaults(self):
        slot = slots.validate_slot(self.slot(), self.data)
        self.assertEqual(slot, {'id': 'claude-a', 'kind': 'claude', 'label': 'Claude A', 'config_dir': str(self.cfg.resolve()),
                                'model': None, 'enabled': True, 'order': 0})
        self.assertEqual(slots.slot_env(slot), {'CLAUDE_CONFIG_DIR': str(self.cfg.resolve())})
        codex = slots.validate_slot(self.slot(kind='codex', config_dir=str(self.codex_cfg), model='gpt-5-codex'), self.data)
        self.assertEqual(slots.slot_env(codex), {'CODEX_HOME': str(self.codex_cfg.resolve())})

    def test_rejections(self):
        self.bad('schema_invalid', '/id', id='Bad Id')
        self.bad('schema_invalid', '/kind', kind='gemini')
        self.bad('schema_invalid', '/label', label='')
        self.bad('schema_invalid', '/label', label='a\nb')
        self.bad('schema_invalid', '/config_dir', config_dir='relative/dir')
        self.bad('schema_invalid', '/config_dir', config_dir=str(self.data))
        self.bad('schema_invalid', '/config_dir', config_dir=str(self.data / 'sub'))
        self.bad('schema_invalid', '/config_dir', config_dir=str(self.data.parent))   # contains the data dir
        self.bad('schema_invalid', '/model', model='opus; rm -rf')
        self.bad('schema_invalid', '/enabled', enabled='yes')
        self.bad('schema_invalid', '/order', order=True)
        self.bad('schema_invalid', '/order', order=1000)

    def test_credentials_cannot_be_stored(self):
        self.bad('unknown_field', '/api_key', api_key='x')
        self.bad('unknown_field', '/token', token='x')
        secret = 'sk-ant-' + 'a' * 30
        self.bad('secret_detected', '/label', label=secret)

    def test_missing_required(self):
        raw = self.slot()
        del raw['config_dir']
        with self.assertRaises(SlotError) as ctx:
            slots.validate_slot(raw, self.data)
        self.assertEqual(ctx.exception.field, '/config_dir')

    def test_list_rules(self):
        with self.assertRaises(SlotError) as ctx:
            slots.validate_slots([self.slot(), self.slot(label='again')], self.data)
        self.assertEqual(ctx.exception.code, 'duplicate')
        with self.assertRaises(SlotError) as ctx:
            slots.validate_slots([self.slot(), self.slot(id='claude-b')], self.data)
        self.assertEqual(ctx.exception.field, '/slots/1/config_dir')
        ordered = slots.validate_slots([self.slot(order=5), self.slot(id='codex-a', kind='codex', config_dir=str(self.cfg), order=1)],
                                       self.data)
        self.assertEqual([s['id'] for s in ordered], ['codex-a', 'claude-a'])
        with self.assertRaises(SlotError):
            slots.validate_slots({'id': 'x'}, self.data)


class StoreTests(SlotTestBase):
    def test_missing_file_is_empty(self):
        self.assertEqual(SlotStore(self.data).load(), [])

    def test_save_load_roundtrip_and_file_mode(self):
        store = SlotStore(self.data)
        store.save([self.slot(model='claude-opus-5-5')])
        self.assertEqual(SlotStore(self.data).get('claude-a')['model'], 'claude-opus-5-5')
        doc = json.loads((self.data / 'slots.json').read_text(encoding='utf-8'))
        self.assertEqual(doc['version'], 1)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE((self.data / 'slots.json').stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.data.iterdir()], ['slots.json'])   # no temp file left behind

    def test_invalid_save_leaves_file_untouched(self):
        store = SlotStore(self.data)
        store.save([self.slot()])
        before = (self.data / 'slots.json').read_bytes()
        with self.assertRaises(SlotError):
            store.save([self.slot(kind='nope')])
        self.assertEqual((self.data / 'slots.json').read_bytes(), before)

    def test_failed_write_cleans_temp_and_keeps_old(self):
        store = SlotStore(self.data)
        store.save([self.slot()])
        before = (self.data / 'slots.json').read_bytes()
        with mock.patch('aether.studio.slots.os.replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                store.save([self.slot(label='New')])
        self.assertEqual((self.data / 'slots.json').read_bytes(), before)
        self.assertEqual([p.name for p in self.data.iterdir()], ['slots.json'])

    def test_corrupt_file_reported_not_replaced(self):
        (self.data / 'slots.json').write_text('{nope', encoding='utf-8')
        with self.assertRaises(SlotError) as ctx:
            SlotStore(self.data).load()
        self.assertEqual(ctx.exception.code, 'corrupt')
        (self.data / 'slots.json').write_text(json.dumps({'version': 1, 'slots': [self.slot(token='x')]}), encoding='utf-8')
        with self.assertRaises(SlotError):
            SlotStore(self.data).load()

    def test_block_state(self):
        now = [datetime(2026, 1, 1, 12, tzinfo=timezone.utc)]
        store = SlotStore(self.data, clock=lambda: now[0])
        store.save([self.slot(), self.slot(id='codex-a', kind='codex', config_dir=str(self.codex_cfg))])
        self.assertEqual(store.block('claude-a', now[0] + timedelta(hours=2), 'usage limit'), '2026-01-01T14:00:00Z')
        self.assertEqual(store.blocked_until('claude-a'), now[0] + timedelta(hours=2))
        self.assertEqual([s['id'] for s in store.available()], ['codex-a'])
        self.assertEqual(SlotStore(self.data, clock=lambda: now[0]).state()['claude-a']['note'], 'usage limit')
        store.save([self.slot(label='renamed')])            # state survives a slot edit...
        self.assertIsNotNone(store.blocked_until('claude-a'))
        now[0] += timedelta(hours=3)                         # ...and expires by time
        self.assertIsNone(store.blocked_until('claude-a'))
        with self.assertRaises(SlotError):
            store.block('ghost', now[0])
        store.unblock('claude-a')
        self.assertEqual(store.state(), {})


class PermissionHelperTests(unittest.TestCase):
    def test_posix_chmod(self):
        if os.name == 'nt':
            self.skipTest('POSIX only')
        root = support.tempdir(self)
        path = root / 'f'
        path.write_text('x', encoding='utf-8')
        os.chmod(path, 0o644)
        self.assertTrue(fsperm.restrict(path))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        sub = fsperm.private_dir(root / 'a' / 'b')
        self.assertEqual(stat.S_IMODE(sub.stat().st_mode), 0o700)

    def test_windows_icacls_branch(self):
        with mock.patch('aether.studio.fsperm.subprocess.run') as run, \
                mock.patch('aether.studio.fsperm.getpass.getuser', return_value='owner'):
            run.return_value.returncode = 0
            self.assertTrue(fsperm.restrict('C:\\data\\slots.json', directory=False, windows=True))
            argv = run.call_args.args[0]
            self.assertEqual(argv, ['icacls', 'C:\\data\\slots.json', '/inheritance:r', '/grant:r', 'owner:F'])
            self.assertFalse(run.call_args.kwargs['shell'])
            fsperm.restrict('C:\\data', directory=True, windows=True)
            self.assertEqual(run.call_args.args[0][-1], 'owner:(OI)(CI)F')
            run.return_value.returncode = 5
            self.assertFalse(fsperm.restrict('C:\\data', directory=True, windows=True))


if __name__ == '__main__':
    unittest.main()
