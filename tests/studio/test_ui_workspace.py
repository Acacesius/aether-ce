"""Workspace and Slots views: static analysis (stdlib) plus node-based logic tests of the exported pure helpers."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from aether.studio import app as studio_app

UI = Path(studio_app.__file__).parent / 'ui'
WORKSPACE = (UI / 'workspace.js').read_text(encoding='utf-8')
SLOTS = (UI / 'slots.js').read_text(encoding='utf-8')
CSS = (UI / 'studio.css').read_text(encoding='utf-8')


class StaticTests(unittest.TestCase):
    def test_exports_and_mount_signatures(self):
        self.assertRegex(WORKSPACE, r'export function mountWorkspace\(panel\)')
        self.assertRegex(SLOTS, r'export function mountSlots\(panel\)')
        for name in ('validatePath', 'reorderIds', 'moveId', 'queuedIds', 'ageText', 'buildPrompt', 'cliNotice'):
            self.assertRegex(WORKSPACE, rf'export (?:function|const) {name}\b')
        for name in ('formatCountdown', 'clockLabel', 'slotState', 'slotPayload', 'slotsBody'):
            self.assertRegex(SLOTS, rf'export function {name}\b')

    def test_workspace_uses_documented_routes_only_through_api(self):
        for route in ("'/api/studio/queue'", "'/api/studio/queue/reorder'", "/resume`", "'/api/studio/queue/' + (paused ? 'resume' : 'pause')"):
            self.assertIn(route.strip("'"), WORKSPACE)
        for text in (WORKSPACE, SLOTS):
            self.assertNotIn('fetch(', text)
        self.assertIn("'PUT', '/api/studio/slots'", SLOTS)
        self.assertIn('/clear-limit', SLOTS)

    def test_workspace_behaviours_present(self):
        for needle in ('dragstart', 'dragover', 'drop', 'ArrowUp', 'ArrowDown', 'document.hidden', 'visibilitychange', 'clearInterval',
                       'paused-banner', 'showModal', "location.hash = '#run/'", 'AETHER_EXEC=cli', 'aria-label'):
            self.assertIn(needle, WORKSPACE, needle)
        for state in ('queued', 'running', 'blocked', 'done', 'failed', 'cancelled'):
            self.assertIn(state, WORKSPACE)
        for slot in ("'auto:claude'", "'auto:codex'", "'auto'"):
            self.assertIn(slot, WORKSPACE)

    def test_slots_behaviours_present(self):
        for needle in ('setInterval', 'clearInterval', 'role: \'switch\'', 'showModal', 'AETHER_EXEC=cli', 'clear-limit', 'confirmButton',
                       'never asks for', 'keeps its own sign-in'):
            self.assertIn(needle, SLOTS + WORKSPACE, needle)
        self.assertEqual(SLOTS.count('setInterval'), 2)
        self.assertEqual(SLOTS.count('clearInterval'), 2)

    def test_no_token_or_secret_fields_in_slot_editor(self):
        self.assertNotRegex(SLOTS, r"(?i)type:\s*'password'")
        self.assertNotRegex(SLOTS, r"(?i)\b(api[_ ]?key|secret|password)\b\s*[:=]")
        self.assertNotRegex(SLOTS, r"localStorage|sessionStorage")

    def test_css_classes_used_exist(self):
        for cls in ('qrow', 'drop-target', 'dragging', 'paused-banner', 'slot-grid', 'qtools', 'grip'):
            self.assertIn('.' + cls, CSS, cls)
        for page in (WORKSPACE, SLOTS):
            self.assertNotIn('innerHTML', page)

    def test_icons_used_are_defined(self):
        icons = (UI / 'icons.js').read_text(encoding='utf-8')
        defined = set(re.findall(r'^  (\w+): \[', icons, re.M))
        used = set(re.findall(r"icon\('(\w+)'", WORKSPACE + SLOTS)) | set(re.findall(r"emptyCard\('(\w+)'", WORKSPACE + SLOTS))
        self.assertTrue(used <= defined, used - defined)


NODE_SCRIPT = r"""
import { validatePath, reorderIds, moveId, queuedIds, ageText, buildPrompt } from './workspace.js';
import { formatCountdown, slotState, slotPayload, slotsBody } from './slots.js';
const out = {};
out.paths = ['', '   ', 'rel/dir', './x', '/work/p', 'C:\\Users\\x\\proj', 'c:/work', 'C:foo', '\\\\srv\\share\\dir', '\\\\srv', '/a\u0000b', '~/p']
  .map((p) => validatePath(p).ok);
out.reorder = [reorderIds(['a', 'b', 'c'], 'c', 'a'), reorderIds(['a', 'b', 'c'], 'a', 'c'), reorderIds(['a', 'b'], 'x', 'a'), reorderIds(['a', 'b'], 'a', 'a')];
out.move = [moveId(['a', 'b', 'c'], 'b', -1), moveId(['a', 'b', 'c'], 'a', -1), moveId(['a', 'b', 'c'], 'c', 1), moveId(['a'], 'q', 1)];
const frozen = Object.freeze(['a', 'b']);
out.pure = reorderIds(frozen, 'b', 'a');
out.queued = queuedIds([{ run_id: 'q1', state: 'queued' }, { run_id: 'r', state: 'running' }, { run_id: 'q2', state: 'queued' }]);
const t0 = Date.parse('2026-01-01T00:00:00Z');
out.count = [formatCountdown('2026-01-01T00:00:05Z', t0), formatCountdown('2026-01-01T00:10:09Z', t0), formatCountdown('2026-01-01T02:03:04Z', t0),
  formatCountdown('2026-01-01T00:00:00Z', t0), formatCountdown('2025-12-31T00:00:00Z', t0), formatCountdown('nonsense', t0), formatCountdown(null, t0)];
out.age = [ageText('2026-01-01T00:00:00Z', t0 + 5000), ageText('2026-01-01T00:00:00Z', t0 + 150000), ageText('2026-01-01T00:00:00Z', t0 + 7200000),
  ageText('2026-01-01T00:00:00Z', t0 + 3 * 86400000), ageText('x', t0)];
out.prompt = [buildPrompt('Mission', ''), buildPrompt('Mission', 'More'), buildPrompt('', 'Only'), buildPrompt(null, null)];
const slot = { id: 'a', kind: 'claude', label: 'A', config_dir: '/x', model: '', enabled: true, order: 0, cli_found: true, blocked_until: null, active_count: 0, cap: 1, last_limit_at: null };
out.state = [slotState(slot, t0).key, slotState({ ...slot, enabled: false }, t0).key, slotState({ ...slot, active_count: 1 }, t0).key,
  slotState({ ...slot, blocked_until: '2026-01-01T01:00:00Z' }, t0).key, slotState({ ...slot, blocked_until: '2025-01-01T01:00:00Z' }, t0).key];
out.payload = slotPayload(slot);
out.body = [slotsBody([slot], 'a', null).slots.length, slotsBody([slot], 'a', { ...slot, enabled: false }).slots[0].enabled, slotsBody([slot], 'b', { ...slot, id: 'b' }).slots.length];
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which('node'), 'node is not installed; the pure-helper logic tests are skipped')
class NodeLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        for path in UI.glob('*.js'):
            (root / path.name).write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
        (root / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
        (root / 'check.js').write_text(NODE_SCRIPT, encoding='utf-8')
        done = subprocess.run(['node', str(root / 'check.js')], capture_output=True, text=True, timeout=60, cwd=str(root))
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.out = json.loads(done.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_validate_path(self):
        self.assertEqual(self.out['paths'], [False, False, False, False, True, True, True, False, True, False, False, False])

    def test_reorder_and_move(self):
        self.assertEqual(self.out['reorder'], [['c', 'a', 'b'], ['b', 'c', 'a'], ['a', 'b'], ['a', 'b']])
        self.assertEqual(self.out['move'], [['b', 'a', 'c'], ['a', 'b', 'c'], ['a', 'b', 'c'], ['a']])
        self.assertEqual(self.out['pure'], ['b', 'a'])
        self.assertEqual(self.out['queued'], ['q1', 'q2'])

    def test_countdown_and_age(self):
        self.assertEqual(self.out['count'], ['0:05', '10:09', '2:03:04', '', '', '', ''])
        self.assertEqual(self.out['age'], ['just now', '2 min', '2 h', '3 d', '–'])

    def test_prompt_and_slot_helpers(self):
        self.assertEqual(self.out['prompt'], ['Mission', 'Mission\n\nMore', 'Only', ''])
        self.assertEqual(self.out['state'], ['available', 'disabled', 'running', 'blocked', 'available'])
        self.assertEqual(set(self.out['payload']), {'id', 'kind', 'label', 'config_dir', 'model', 'enabled', 'order'})
        self.assertIsNone(self.out['payload']['model'])
        self.assertEqual(self.out['body'], [0, False, 2])


if __name__ == '__main__':
    unittest.main()
