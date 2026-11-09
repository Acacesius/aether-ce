"""Studio UI: static asset serving through the guarded CE handler, CSP, validate route, and asset hygiene."""
import http.client
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
import io

from aether.studio import app as studio_app
from tests.studio import support

UI = Path(studio_app.__file__).parent / 'ui'
ROOT = Path(__file__).resolve().parents[2]
SVG_NS = 'http://www.w3.org/2000/svg'  # namespace identifier used by icons.js; never fetched
IMPORT = re.compile(r'^import\s+(?:\{([^}]*)\}\s+from\s+)?[\'"]([^\'"]+)[\'"];', re.M)
EXPORT = re.compile(r'^export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)', re.M)
# Emoji and pictographic symbols (incl. dingbats, misc symbols, variation selector 16, regional indicators).
EMOJI = re.compile('[\u2600-\u27bf\u2b00-\u2bff\ufe0f\U0001f000-\U0001faff]')
ICON_NAMES = ('queue', 'slots', 'run', 'tracker', 'builder', 'library', 'play', 'pause', 'cancel', 'up', 'down', 'grip',
              'check', 'alert', 'clock', 'plus', 'sun', 'moon', 'lock', 'download', 'chevron')


def _load_contrast():
    spec = importlib.util.spec_from_file_location('ui_contrast', ROOT / 'tools' / 'ui_contrast.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.app, self.c = support.serve(self)

    def raw(self, method, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.c.port, timeout=10)
        conn.request(method, path, headers={'Host': f'127.0.0.1:{self.c.port}', **(headers or {})})
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), body

    def test_root_redirects_and_index_served_without_token(self):
        status, headers, _ = self.raw('GET', '/')
        self.assertEqual((status, headers['Location']), (302, '/studio/'))
        status, headers, body = self.raw('GET', '/studio/')
        self.assertEqual(status, 200)
        self.assertIn(b'AETHER Studio', body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        csp = headers['Content-Security-Policy']
        for part in ("default-src 'none'", "script-src 'self'", "connect-src 'self'", "frame-ancestors 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn('unsafe', csp)
        self.assertNotIn('http', csp)

    def test_every_asset_served_with_content_type(self):
        for name, ctype in studio_app.UI_ASSETS.items():
            status, headers, body = self.raw('GET', '/studio/' + name)
            self.assertEqual((status, headers['Content-Type']), (200, ctype), name)
            self.assertEqual(body, (UI / name).read_bytes())

    def test_unknown_and_traversal_paths_404(self):
        for path in ('/studio/nope.js', '/studio/../app.py', '/studio/%2e%2e/app.py', '/studio/fonts/', '/studio/ui/lib.js'):
            self.assertEqual(self.raw('GET', path)[0], 404, path)

    def test_static_routes_keep_host_and_fetch_site_guards(self):
        self.assertEqual(self.c.request('GET', '/studio/', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.raw('GET', '/studio/', {'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.raw('GET', '/', {'Origin': 'http://evil.example'})[0], 403)

    def test_api_still_requires_token(self):
        self.assertEqual(self.c.request('GET', '/api/studio/runs', auth=False)[0], 401)
        self.assertEqual(self.raw('POST', '/studio/')[0], 403)  # writes need Origin; static paths are GET-only

    def test_validate_route(self):
        from aether.studio import schema
        spec = schema.demo_spec()
        status, body = self.c.request('POST', '/api/studio/validate', {'spec': spec})
        self.assertEqual(status, 200)
        self.assertEqual((body['valid'], body['stage_order']), (True, ['demo']))
        self.assertEqual(body['spec_sha256'], schema.spec_sha256(body['spec']))
        self.assertEqual(self.c.request('GET', '/api/studio/definitions')[1]['definitions'], [])  # nothing stored
        bad = dict(spec, mission='')
        status, body = self.c.request('POST', '/api/studio/validate', {'spec': bad})
        self.assertEqual((status, body['field'], body['error']), (400, '/mission', 'schema_invalid'))
        self.assertEqual(self.c.request('POST', '/api/studio/validate', {})[0], 400)
        self.assertEqual(self.c.request('POST', '/api/studio/validate', {'spec': spec, 'x': 1})[0], 400)


class AssetTests(unittest.TestCase):
    def test_ui_files_are_all_registered(self):
        on_disk = {str(p.relative_to(UI)) for p in UI.rglob('*') if p.is_file()}
        self.assertEqual(on_disk, set(studio_app.UI_ASSETS))

    def test_no_inline_script_external_url_or_innerhtml(self):
        html = (UI / 'index.html').read_text()
        self.assertNotRegex(html, r'(?i)\son[a-z]+\s*=')
        self.assertNotRegex(html, r'<script(?![^>]*\bsrc=)')
        for name in studio_app.UI_ASSETS:
            if name.endswith(('.js', '.css', '.html')):
                text = (UI / name).read_text(encoding='utf-8').replace(SVG_NS, '')
                self.assertNotRegex(text, r'https?://', name)
                self.assertNotIn('innerHTML', text, name)
                self.assertNotIn('eval(', text, name)

    def test_accessibility_contract(self):
        html = (UI / 'index.html').read_text()
        shell = (UI / 'studio.js').read_text()
        css = (UI / 'studio.css').read_text()
        self.assertIn('role="tablist"', html)
        self.assertIn('role="tabpanel"', html)
        for key in ('ArrowRight', 'ArrowLeft', 'Home', 'End', 'aria-selected', 'hashchange'):
            self.assertIn(key, shell)
        self.assertIn('prefers-reduced-motion', css)
        self.assertIn('prefers-color-scheme:light', css)
        self.assertIn(':focus-visible', css)
        self.assertIn('min-height:44px', css)
        self.assertRegex(css, r'\[data-theme=light\]')
        self.assertIn('lang="en"', html)
        self.assertNotIn('<link rel="stylesheet" href="http', html)

    @unittest.skipUnless(shutil.which('node'), 'node required')
    def test_javascript_parses(self):
        for path in UI.glob('*.js'):
            subprocess.run(['node', '--check', str(path)], check=True, capture_output=True, text=True)

    def test_builder_step_order_and_demo_labelling(self):
        builder = (UI / 'builder.js').read_text()
        self.assertIn("['Spec', 'Scope', 'Permissions', 'Review', 'Validate', 'Launch']", builder)
        self.assertIn('DEMO', (UI / 'lib.js').read_text())
        self.assertEqual(len(re.findall(r'/api/studio/validate', builder)), 1)

    def test_system_fonts_only(self):
        css = (UI / 'studio.css').read_text(encoding='utf-8')
        self.assertNotIn('@font-face', css)
        self.assertIn('system-ui', css)
        self.assertNotIn("font-src 'self'", studio_app.UI_CSP)
        self.assertFalse(any(name.startswith('fonts/') for name in studio_app.UI_ASSETS))

    def test_every_imported_module_exists_and_exports_the_names(self):
        modules = {p.name: p.read_text(encoding='utf-8') for p in UI.glob('*.js')}
        exports = {name: set(EXPORT.findall(text)) for name, text in modules.items()}
        seen = {'studio.js'}
        queue = ['studio.js']
        while queue:  # walk the import graph from the entry module
            current = queue.pop()
            for names, target in IMPORT.findall(modules[current]):
                with self.subTest(module=current, target=target):
                    self.assertTrue(target.startswith('./') and target.endswith('.js'), 'relative ES module path only')
                    target = target[2:]
                    self.assertIn(target, modules, 'imported module missing on disk')
                    self.assertIn(target, studio_app.UI_ASSETS, 'imported module not served')
                    for name in filter(None, (n.strip().split(' as ')[0] for n in names.split(','))):
                        self.assertIn(name, exports[target], f'{target} does not export {name}')
                    if target not in seen:
                        seen.add(target)
                        queue.append(target)
        self.assertEqual(seen, set(modules), 'every JS module is reachable from studio.js')
        index = (UI / 'index.html').read_text(encoding='utf-8')
        self.assertIn('src="/studio/studio.js"', index)

    def test_no_markup_injection_or_emoji(self):
        for name in studio_app.UI_ASSETS:
            text = (UI / name).read_text(encoding='utf-8')
            with self.subTest(name=name):
                self.assertIsNone(re.search(r'\.(?:inner|outer)HTML\s*\+?=', text))
                self.assertNotIn('insertAdjacentHTML', text)
                self.assertNotIn('document.write', text)
                self.assertIsNone(EMOJI.search(text), 'emoji or pictographic symbol')

    def test_shell_routes_and_placeholder_modules(self):
        shell = (UI / 'studio.js').read_text(encoding='utf-8')
        tabs = re.findall(r"\['(\w+)', '([^']+)', '\w+', mount\w+, (?:true|false)\]", shell)
        self.assertEqual([t[1] for t in tabs], ['Workspace', 'Slots', 'Tracker', 'Builder', 'Library', 'Runs'])
        for route in ("['quick', 'Quick Run', mountQuick", "['run', 'Run', mountRunPage"):
            self.assertIn(route, shell)
        self.assertIn('href="#quick"', (UI / 'index.html').read_text(encoding='utf-8'))
        self.assertIn("cliEnabled(mode) ? 'workspace' : 'quick'", shell)
        for module, fn in (('workspace', 'mountWorkspace'), ('slots', 'mountSlots'), ('tracker', 'mountTracker'), ('runpage', 'mountRunPage')):
            text = (UI / f'{module}.js').read_text(encoding='utf-8')
            self.assertRegex(text, rf'export function {fn}\(panel(, ctx)?\)')
            self.assertNotIn('coming in the next job', text)
        for theme in ("'auto', 'light', 'dark'", 'localStorage'):
            self.assertIn(theme, shell)

    def test_icons_built_with_createelementns(self):
        icons = (UI / 'icons.js').read_text(encoding='utf-8')
        self.assertIn('createElementNS', icons)
        self.assertIn(f"'{SVG_NS}'", icons)
        defined = set(re.findall(r'^  (\w+): \[', icons, re.M))
        self.assertTrue(set(ICON_NAMES) <= defined, set(ICON_NAMES) - defined)

    def test_design_doc_covers_tokens(self):
        doc = (ROOT / 'docs' / 'studio' / 'UI_DESIGN.md').read_text(encoding='utf-8')
        tokens = set(re.findall(r'(--[a-z0-9-]+)\s*:', (UI / 'studio.css').read_text(encoding='utf-8')))
        for token in tokens:
            self.assertIn(f'`{token}`', doc, token)
        for name in ICON_NAMES:
            self.assertIn(f'`{name}`', doc, name)


class ContrastTests(unittest.TestCase):
    def test_studio_css_passes(self):
        ui_contrast = _load_contrast()
        results = ui_contrast.check((UI / 'studio.css').read_text(encoding='utf-8'))
        statuses = {r['fg'] for r in results if r['fg'].startswith('--st-')}
        self.assertEqual(len(statuses), 6)
        self.assertEqual({r['theme'] for r in results}, {'dark', 'light'})
        self.assertEqual([r for r in results if not r['ok']], [])

    def test_cli_exit_codes(self):
        script = ROOT / 'tools' / 'ui_contrast.py'
        ok = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

    def test_detects_failures(self):
        ui_contrast = _load_contrast()
        css = (UI / 'studio.css').read_text(encoding='utf-8')
        bad = css.replace('--st-done-fg:#1d6b37;', '--st-done-fg:#c8e6d0;')  # light theme only: chip goes pale
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'studio.css'
            path.write_text(bad, encoding='utf-8')
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ui_contrast.main([str(path)]), 1)
            self.assertIn('FAIL light --st-done-fg', out.getvalue())
            path.write_text(css.replace('/* tokens:light */', ''), encoding='utf-8')
            with redirect_stderr(io.StringIO()):
                self.assertEqual(ui_contrast.main([str(path)]), 1)
        self.assertAlmostEqual(ui_contrast.ratio('#000000', '#ffffff'), 21.0)
        self.assertAlmostEqual(ui_contrast.ratio('#777777', '#777777'), 1.0)
