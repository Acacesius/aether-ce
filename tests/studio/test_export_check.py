"""export_check tests. Planted bad values are assembled at runtime so this file stays self-clean."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('export_check', ROOT / 'tools' / 'export_check.py')
ec = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ec)

HOME = '/ho' + 'me/' + 'x'
IP = '.'.join(['10', '0', '0', '5'])
TOKEN = 'sk-' + 'a1' * 14
LICENSE = (ROOT / 'LICENSE').read_text(encoding='utf-8')


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ec.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class ExportCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for rel in ec.REQUIRED:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('hello\n', encoding='utf-8')
        (self.root / 'LICENSE').write_text(LICENSE, encoding='utf-8')

    def put(self, rel, text):
        (self.root / rel).write_text(text, encoding='utf-8')

    def test_clean_fixture_passes(self):
        code, out, _ = run(self.root)
        self.assertEqual((code, out), (0, ''))

    def assert_fails(self, needle, secret=None):
        code, out, err = run(self.root)
        self.assertEqual(code, 1)
        self.assertIn(needle, out)
        if secret:
            self.assertNotIn(secret, out + err)
        return out

    def test_home_path(self):
        self.put('app.py', 'ok\nroot = "' + HOME + '/data"\n')
        self.assert_fails('app.py:2: home_path', HOME)

    def test_placeholder_paths_pass(self):
        self.put('d.md', '/ho' + 'me/<user>/x and C:\\Users\\<you>\\x\n')
        self.assertEqual(run(self.root)[0], 0)

    def test_windows_home(self):
        self.put('w.md', 'C:\\Users\\' + 'carol\\x\n')
        self.assert_fails('w.md:1: home_path_windows', 'carol')

    def test_private_ip(self):
        self.put('net.py', 'HOST = "' + IP + '"\n')
        self.assert_fails('net.py:1: ip_rfc1918', IP)

    def test_token(self):
        self.put('cfg.py', '\n\nKEY = "' + TOKEN + '"\n')
        self.assert_fails('cfg.py:3: vendor_api_key', TOKEN)

    def test_email(self):
        addr = 'bob' + '@' + 'corp.io'
        self.put('m.md', addr + '\n')
        self.assert_fails('m.md:1: email', addr)

    def test_wrong_license(self):
        self.put('LICENSE', 'All rights reserved.\n')
        self.assert_fails('LICENSE:1: license_not_mit')

    def test_wrong_copyright(self):
        self.put('LICENSE', LICENSE.replace(ec.MIT_COPYRIGHT, 'Copyright (c) 2026 Someone'))
        self.assert_fails('license_copyright')

    def test_missing_start_bat(self):
        (self.root / 'start.bat').unlink()
        self.assert_fails('start.bat:0: missing_required_file')

    def test_allowlist_exact_line(self):
        line = 'HOST = "' + IP + '"'
        self.put('fx.py', line + '\n')
        entry = {'rule': 'ip_rfc1918', 'path': 'fx.py', 'sha256': hashlib.sha256(line.encode()).hexdigest(), 'why': 'fixture'}
        (self.root / 'export_check_allow.json').write_text(json.dumps([entry]), encoding='utf-8')
        self.assertEqual(run(self.root)[0], 0)
        self.put('fx.py', line + ' # changed\n')
        self.assertEqual(run(self.root)[0], 1)

    def test_only_tracked_files_in_git_repo(self):
        if not shutil.which('git'):
            self.skipTest('git missing')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True)
        self.put('untracked.py', 'HOST = "' + IP + '"\n')
        self.assertEqual(run(self.root)[0], 0)

    def test_plan_has_no_side_effects(self):
        before = sorted(p.name for p in ROOT.iterdir())
        code, out, _ = run('--plan')
        self.assertEqual(code, 0)
        for word in ('git init', 'AETHER Community Edition 0.1', 'ce_scan.py', 'start.bat', 'export_check.py'):
            self.assertIn(word, out)
        self.assertEqual(sorted(p.name for p in ROOT.iterdir()), before)

    def test_real_repo_is_clean(self):
        self.assertEqual(run(ROOT)[0], 0)


if __name__ == '__main__':
    unittest.main()
