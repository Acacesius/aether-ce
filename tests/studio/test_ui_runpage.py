"""Run page: static analysis (stdlib) plus node-based tests of the exported pure helpers (skipped without node)."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from aether.studio import app as studio_app

UI = Path(studio_app.__file__).parent / 'ui'
RUNPAGE = (UI / 'runpage.js').read_text(encoding='utf-8')
CSS = (UI / 'studio.css').read_text(encoding='utf-8')


class StaticTests(unittest.TestCase):
    def test_exports(self):
        self.assertRegex(RUNPAGE, r'export function mountRunPage\(panel, ctx\)')
        for name in ('mergeEvents', 'backoff', 'elapsedText', 'truncateText', 'timelineSegments', 'actionsFor'):
            self.assertRegex(RUNPAGE, rf'export function {name}\b')

    def test_no_unsafe_dom_or_direct_fetch(self):
        self.assertNotIn('innerHTML', RUNPAGE)
        self.assertNotIn('insertAdjacentHTML', RUNPAGE)
        self.assertNotIn('fetch(', RUNPAGE)
        self.assertNotRegex(RUNPAGE, r'localStorage|sessionStorage')

    def test_routes_and_polling(self):
        for needle in ('/api/studio/queue/', '/api/studio/runs/${id}/log?after=${state.nextOffset}&wait=', 'next_offset',
                       'view.done', 'role: \'log\'', 'aria-pressed', 'confirmButton', 'Reconnecting', 'provider_switches'):
            self.assertIn(needle, RUNPAGE, needle)

    def test_timers_cleaned_up_on_unmount(self):
        self.assertIn('return stop;', RUNPAGE)
        self.assertIn('clearTimeout(pollTimer)', RUNPAGE)
        self.assertEqual(RUNPAGE.count('setInterval('), 2)
        self.assertIn('clearInterval(itemTimer)', RUNPAGE)
        self.assertIn('clearInterval(clockTimer)', RUNPAGE)
        self.assertIn('if (stopped)', RUNPAGE)

    def test_offset_only_advances_after_success(self):
        # the offset is read for the request and written only by append(), which runs after the response
        self.assertEqual(len(re.findall(r'state = \{', RUNPAGE)), 2)  # initial + append()
        self.assertIn('MAX_NODES = 2000', RUNPAGE)
        self.assertIn('older entries were removed from view', RUNPAGE)

    def test_css_classes_and_icons(self):
        for cls in ('rp-flow', 'rp-log', 'lg-error', 'lg-prose', 'rp-timeline', 'rp-switch'):
            self.assertIn('.' + cls, CSS, cls)
        icons = (UI / 'icons.js').read_text(encoding='utf-8')
        defined = set(re.findall(r'^  (\w+): \[', icons, re.M))
        used = set(re.findall(r"icon\('(\w+)'", RUNPAGE)) | set(re.findall(r"emptyCard\('(\w+)'", RUNPAGE))
        used |= set(re.findall(r"icon\(paused \? '(\w+)' : '(\w+)'", RUNPAGE)[0]) if "icon(paused ?" in RUNPAGE else set()
        self.assertTrue(used <= defined, used - defined)


NODE_SCRIPT = r"""
import { mergeEvents, backoff, elapsedText, truncateText, timelineSegments, actionsFor, shortHash } from './runpage.js';
const out = {};
out.backoff = [0, 1, 2, 3, 4, 5, 6, 50, -1, 'x'].map(backoff);
// reconnect: a chunk is applied, the next poll fails, the retry replays from the last offset (server may resend)
const ev = (offset, text) => ({ kind: 'assistant_text', text, offset });
let s = { events: [], nextOffset: 0 };
const first = { events: [ev(0, 'a'), ev(10, 'b')], next_offset: 20 };
let m = mergeEvents(s, first); s = { events: m.events, nextOffset: m.nextOffset };
out.first = [m.events.length, m.nextOffset];
m = mergeEvents(s, first); s = { events: m.events, nextOffset: m.nextOffset };   // exact replay
out.replay = [m.events.length, m.nextOffset, m.added.length];
m = mergeEvents(s, { events: [ev(10, 'b'), ev(20, 'c'), ev(20, 'c2')], next_offset: 40 }); s = { events: m.events, nextOffset: m.nextOffset };
out.overlap = [m.events.map((e) => e.text).join(','), m.nextOffset];
m = mergeEvents(s, { events: [], next_offset: 5 });                                // stale offset never goes backwards
out.stale = [m.nextOffset, m.events.length];
const frozen = Object.freeze({ events: Object.freeze([ev(0, 'x')]), nextOffset: 10 });
out.pure = mergeEvents(frozen, { events: [ev(10, 'y')], next_offset: 12 }).events.length;
out.cap = (() => { const r = mergeEvents({ events: [ev(0, '1'), ev(1, '2'), ev(2, '3')], nextOffset: 3 }, { events: [ev(3, '4')], next_offset: 4 }, 2); return [r.events.map((e) => e.text).join(''), r.dropped]; })();
out.garbage = [mergeEvents({ events: [], nextOffset: NaN }, null).nextOffset, mergeEvents({ events: [], nextOffset: 0 }, { events: [{ kind: 'raw', text: 'z' }], next_offset: 'q' }).events.length];
const t0 = Date.parse('2026-01-01T00:00:00Z');
out.elapsed = [elapsedText('2026-01-01T00:00:00Z', null, t0 + 5000), elapsedText('2026-01-01T00:00:00Z', null, t0 + 125000),
  elapsedText('2026-01-01T00:00:00Z', '2026-01-01T01:02:03Z', t0), elapsedText(null, null, t0), elapsedText('x', null, t0), elapsedText('2026-01-01T00:00:10Z', null, t0)];
out.trunc = [truncateText('abc', 5), truncateText('abcdef', 3), truncateText(null, 3), truncateText('a\u{1F600}b', 2)];
out.timeline = timelineSegments([{ attempt: 1, slot: 'a', outcome: 'limit_stop' }, { attempt: 2, slot: 'b', outcome: 'ok' }],
  [{ from: 'a', to: 'b', reason: 'usage limit' }]).map((x) => [x.slot, x.switched && x.switched.reason]);
out.timelineEmpty = timelineSegments(undefined, undefined).length;
out.actions = [actionsFor('running', null), actionsFor('blocked', 'owner_action_required'), actionsFor('blocked', 'x'), actionsFor('done', null)];
out.hash = shortHash('0123456789abcdef0123');
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

    def test_backoff_exponential_capped(self):
        self.assertEqual(self.out['backoff'], [500, 1000, 2000, 4000, 8000, 10000, 10000, 10000, 500, 500])

    def test_no_duplicate_lines_across_reconnect(self):
        self.assertEqual(self.out['first'], [2, 20])
        self.assertEqual(self.out['replay'], [2, 20, 0])
        self.assertEqual(self.out['overlap'], ['a,b,c,c2', 40])
        self.assertEqual(self.out['stale'], [40, 4])

    def test_offset_never_regresses_and_input_not_mutated(self):
        self.assertEqual(self.out['pure'], 2)
        self.assertEqual(self.out['garbage'], [0, 1])

    def test_cap_drops_oldest(self):
        self.assertEqual(self.out['cap'], ['34', 2])

    def test_elapsed_and_truncate(self):
        self.assertEqual(self.out['elapsed'], ['5s', '2m 05s', '1h 02m 03s', '–', '–', '0s'])
        self.assertEqual(self.out['trunc'][0], {'text': 'abc', 'truncated': False, 'hidden': 0})
        self.assertEqual(self.out['trunc'][1], {'text': 'abc', 'truncated': True, 'hidden': 3})
        self.assertEqual(self.out['trunc'][2]['text'], '')
        self.assertEqual(self.out['trunc'][3], {'text': 'a\U0001F600', 'truncated': True, 'hidden': 1})

    def test_timeline_and_actions(self):
        self.assertEqual(self.out['timeline'], [['a', None], ['b', 'usage limit']])
        self.assertEqual(self.out['timelineEmpty'], 0)
        run, approve, resume, done = self.out['actions']
        self.assertEqual((run['cancel'], run['resume'], run['approve']), (True, False, False))
        self.assertEqual((approve['approve'], approve['resume']), (True, False))
        self.assertEqual((resume['resume'], resume['approve']), (True, False))
        self.assertEqual(done, {'cancel': False, 'resume': False, 'approve': False})
        self.assertEqual(self.out['hash'], '0123456789ab')


if __name__ == '__main__':
    unittest.main()
