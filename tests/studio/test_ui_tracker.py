"""Tracker view: static analysis (stdlib) plus node-based tests of the exported pure helpers (skipped without node)."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from aether.studio import app as studio_app

UI = Path(studio_app.__file__).parent / 'ui'
TRACKER = (UI / 'tracker.js').read_text(encoding='utf-8')
CSS = (UI / 'studio.css').read_text(encoding='utf-8')


class StaticTests(unittest.TestCase):
    def test_exports(self):
        self.assertRegex(TRACKER, r'export function mountTracker\(panel\)')
        for name in ('toCsv', 'csvCell', 'sortRows', 'successRate', 'providerTotals', 'fmtDuration', 'csvFilename', 'trackerQuery'):
            self.assertRegex(TRACKER, rf'export function {name}\b')

    def test_safe_dom_and_api_only(self):
        for bad in ('innerHTML', 'insertAdjacentHTML', 'fetch(', 'localStorage', 'sessionStorage', 'import('):
            self.assertNotIn(bad, TRACKER, bad)
        self.assertEqual(re.findall(r'https?://[^\'"`\s]+', TRACKER), ['http://www.w3.org/2000/svg'])  # only the SVG namespace id
        self.assertIn("api('GET', '/api/studio/tracker?'", TRACKER)
        self.assertIn("'\\ufeff'", TRACKER)

    def test_accessibility_hooks(self):
        for needle in ('aria-sort', "role: 'dialog'", "'aria-modal'", "e.key === 'Escape'", 'opener.focus()', "class: 'sr-only'",
                       'aria-labelledby', 'not reported', "'#run/'", 'setTimeout(() => { filters.q', 'debounce'):
            self.assertIn(needle, TRACKER, needle)
        self.assertIn('text/csv', TRACKER)
        self.assertIn('new Blob', TRACKER)

    def test_unreported_never_zero(self):
        self.assertRegex(TRACKER, r"typeof n === 'number' \? .* : UNREPORTED")
        self.assertRegex(TRACKER, r"typeof cents === 'number' \? .* : UNREPORTED")

    def test_chart_colours_from_tokens(self):
        for cls in ('.tk-bar.runs', '.tk-bar.rate', '.tk-bar.time', '.tk-table td::before', '.tk-drawer-head'):
            self.assertIn(cls, CSS, cls)
        block = CSS[CSS.index('/* ---- tracker */'):CSS.index('@media (prefers-reduced-motion')]
        self.assertNotRegex(block, r'#[0-9a-fA-F]{3,8}\b')
        self.assertNotIn('TRACKER_PLACEHOLDER', TRACKER)

    def test_mobile_cards_without_horizontal_scroll(self):
        block = CSS[CSS.index('/* ---- tracker */'):]
        self.assertIn('.tk-table thead{display:none}', block)
        self.assertIn('.tk-wrap{overflow:visible', block)

    def test_icons_exist(self):
        icons = (UI / 'icons.js').read_text(encoding='utf-8')
        defined = set(re.findall(r'^  (\w+): \[', icons, re.M))
        used = set(re.findall(r"icon\('(\w+)'", TRACKER)) | set(re.findall(r"emptyCard\('(\w+)'", TRACKER))
        self.assertTrue(used <= defined, used - defined)


NODE_SCRIPT = r"""
import { toCsv, csvCell, sortRows, successRate, providerTotals, fmtDuration, fmtTokens, fmtCost, csvFilename, csvRows, CSV_HEADER, trackerQuery, itemTitle } from './tracker.js';
const out = {};
out.cells = ['plain', 'a,b', 'say "hi"', 'two\nlines', 'cr\rx', '', null, undefined, 0, 12.5, 'é'].map(csvCell);
out.inject = ['=1+1', '+SUM(A1)', '-2+3', '@cmd', '\tx', '\rx', 'safe=1', ' =x', '=a,"b"'].map(csvCell);
out.numbers = [csvCell(-5), csvCell(0)];
out.csv = toCsv(['a', 'b'], [['1', 'x,y'], ['=z', 'q"r']]);
const items = [
  { run_id: 'r1', status: 'done', workspace: 'C:\\Users\\x\\proj', provider: { slot: 'b', kind: 'codex' }, started_at: '2026-01-02T00:00:00Z', wall_seconds: 30, provider_switches: [], attempts: [{}], tokens: null, cost_usd_cents: null, evidence: { log_sha256: 'aa', result_sha256: null } },
  { run_id: 'r2', status: 'failed', workspace: '/w/two', provider: { slot: 'a', kind: 'claude' }, started_at: '2026-01-01T00:00:00Z', wall_seconds: 90, provider_switches: [{}, {}], attempts: [{}, {}], tokens: 10, cost_usd_cents: 250 },
  { run_id: 'r3', status: 'queued', workspace: '/w/three', provider: null, started_at: null, created_at: '2026-01-03T00:00:00Z', wall_seconds: null, provider_switches: [], attempts: [] },
  { run_id: 'r4', status: 'done', workspace: '/w/four', provider: { slot: 'a', kind: 'claude' }, started_at: '2026-01-04T00:00:00Z', wall_seconds: 90, provider_switches: [{}], attempts: [{}] },
];
const ids = (rows) => rows.map((r) => r.run_id).join(',');
const copy = JSON.stringify(items);
out.sortStarted = [ids(sortRows(items, 'started', 'asc')), ids(sortRows(items, 'started', 'desc'))];
out.sortDuration = [ids(sortRows(items, 'duration', 'asc')), ids(sortRows(items, 'duration', 'desc'))];
out.sortSwitches = ids(sortRows(items, 'switches', 'desc'));
out.sortProvider = ids(sortRows(items, 'provider', 'asc'));
out.sortStatus = ids(sortRows(items, 'status', 'asc'));
out.sortUnknown = ids(sortRows(items, 'nope', 'asc'));
out.pure = JSON.stringify(items) === copy;
out.rate = [successRate({ done: 3, failed: 1 }), successRate({ done: 0, failed: 0, cancelled: 4 }), successRate({}), successRate(undefined), successRate({ done: 2 })];
out.totals = providerTotals(items).map((t) => [t.provider, t.runs, t.seconds, t.rate]);
out.dur = [fmtDuration(5), fmtDuration(125), fmtDuration(3725), fmtDuration(null), fmtDuration(-1), fmtDuration(0)];
out.unreported = [fmtTokens(null), fmtTokens(undefined), fmtTokens(0), fmtTokens(1234), fmtCost(null), fmtCost(0), fmtCost(250)];
out.filename = [csvFilename(new Date(2026, 0, 5)), csvFilename(new Date(2026, 10, 25))];
const rows = csvRows(items);
out.header = CSV_HEADER.length === rows[0].length;
const tokCol = CSV_HEADER.indexOf('tokens');
out.tokCells = rows.map((r) => csvCell(r[tokCol]));
out.title = [itemTitle(items[0]), itemTitle({ run_id: 'abcdefgh12345678', title: '  hello \n world ' }), itemTitle({ run_id: 'abcdefgh12345678' })];
out.query = [trackerQuery({ status: 'done', provider: 'slot:a', q: ' x y ', since: '2026-01-01', until: '' }, 25, 50),
  trackerQuery({ provider: 'kind:codex' }, 200, 0), trackerQuery({}, 25, 0)];
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

    def test_csv_quoting_rfc4180(self):
        self.assertEqual(self.out['cells'], ['plain', '"a,b"', '"say ""hi"""', '"two\nlines"', '"cr\rx"', '', '', '', '0', '12.5', 'é'])
        self.assertEqual(self.out['csv'], 'a,b\r\n1,"x,y"\r\n\'=z,"q""r"\r\n')

    def test_csv_formula_injection_neutralised(self):
        self.assertEqual(self.out['inject'], ["'=1+1", "'+SUM(A1)", "'-2+3", "'@cmd", "'\tx", "\"'\rx\"", 'safe=1', ' =x', '"\'=a,""b"""'])
        self.assertEqual(self.out['numbers'], ['-5', '0'])   # real numbers are not text, so left alone

    def test_unreported_tokens_are_empty_cells_not_zero(self):
        self.assertEqual(self.out['tokCells'], ['', '10', '', ''])
        self.assertEqual(self.out['unreported'], ['not reported', 'not reported', '0', '1,234', 'not reported', '$0.00', '$2.50'])
        self.assertTrue(self.out['header'])

    def test_sort_rows_stable_nulls_last_and_pure(self):
        self.assertTrue(self.out['pure'])
        self.assertEqual(self.out['sortStarted'], ['r2,r1,r3,r4', 'r4,r3,r1,r2'])
        self.assertEqual(self.out['sortDuration'], ['r1,r2,r4,r3', 'r2,r4,r1,r3'])   # r2 and r4 tie: input order kept
        self.assertEqual(self.out['sortSwitches'], 'r2,r4,r1,r3')
        self.assertEqual(self.out['sortProvider'], 'r2,r4,r1,r3')
        self.assertEqual(self.out['sortStatus'], 'r1,r4,r2,r3')
        self.assertEqual(self.out['sortUnknown'], 'r1,r2,r3,r4')

    def test_success_rate(self):
        self.assertEqual(self.out['rate'], [0.75, None, None, None, 1])

    def test_provider_totals(self):
        self.assertEqual(self.out['totals'], [['a', 2, 180, 0.5], ['b', 1, 30, 1], ['unassigned', 1, 0, None]])

    def test_formatting(self):
        self.assertEqual(self.out['dur'], ['5s', '2m 05s', '1h 02m', '–', '–', '0s'])
        self.assertEqual(self.out['filename'], ['tracker-20260105.csv', 'tracker-20261125.csv'])
        self.assertEqual(self.out['title'], ['proj (r1…r1)', 'hello world', 'abcdefgh…5678'])

    def test_query_building(self):
        self.assertEqual(self.out['query'], ['status=done&slot=a&q=x+y&since=2026-01-01&limit=25&offset=50', 'kind=codex&limit=200&offset=0', 'limit=25&offset=0'])


if __name__ == '__main__':
    unittest.main()
