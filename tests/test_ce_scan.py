"""Scanner tests: synthetic positives (assembled at runtime so this file stays self-clean) and negatives."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

SPEC = importlib.util.spec_from_file_location('ce_scan', Path(__file__).resolve().parents[1] / 'tools' / 'ce_scan.py')
ce = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ce)

J = ''.join  # assemble literals from parts so no positive literal appears in this source file
POSITIVES = {
    'pem_private_key': '-----BEGIN ' + 'RSA PRIVATE' + ' KEY-----',
    'openssh_key': '-----BEGIN OPENSSH ' + 'PRIVATE KEY-----',
    'vendor_api_key': 'k = "' + 'sk-' + 'a1' * 12 + '"',
    'bearer_literal': 'Authorization: Bearer ' + 'abcdefghij' * 3,
    'token_shape_43': 'value = ' + 'A' * 43,
    'hex_secret': 'api_secret = "' + 'ab' * 20 + '"',
    'ip_rfc1918': 'host ' + '.'.join(['192', '168', '7', '9']),
    'ip_cgnat': 'host ' + '.'.join(['100', '70', '1', '2']),
    'ip_public': 'host ' + '.'.join(['8', '8', '4', '4']),
    'mac': 'addr ' + ':'.join(['aa', 'bb', 'cc', '00', '11', '22']),
    'overlay_dns': 'name.example.' + 'ts.' + 'net',
    'overlay_vpn': 'uses ' + 'tail' + 'scale here',
    'ssh_material': 'cp ~/.' + 'ssh/' + 'id_' + 'rsa x',
    'home_path': 'open("/ho' + 'me/someone/file")',
    'private_product': 'the ' + 'net' + 'dash' + ' service',
    'host_socket': 'mount /var/run/' + 'docker' + '.sock',
    'bind_any': 'listen ' + '0.0.0.0' + ':80',
    'dangerous_cli': 'codex --' + 'danger' + '-full-' + 'access',
}


def rules(line, deny=()):
    return {r for r, _ in ce.scan_line(line, deny)}


class LineRules(unittest.TestCase):
    def test_every_rule_has_a_positive(self):
        for rule, text in POSITIVES.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, rules(text))

    def test_all_declared_fail_rules_covered(self):
        declared = {r for r, _, level in ce.RULES if level == 'fail'} | {'ip_public'}
        self.assertEqual(declared - set(POSITIVES), set())

    def test_warn_rule_port(self):
        self.assertIn(('private_ports', 'warn'), ce.scan_line('port ' + '787' + '0'))

    def test_negatives(self):
        clean = [
            'url = "http://127.0.0.1:8080/healthz"',
            'doc range 192.0.2.10 198.51.100.7 203.0.113.9',
            'bind 0.0.0.0 inside container',
            'mac placeholder 00:00:00:00:00:00',
            'def test_a_very_long_method_name_that_is_exactly_43_chars_x(self):',
            'sha256 ' + 'ab' * 32,
            'version 3.13.1',
            'Bearer <token>',
            '"127.0.0.1:${AETHER_PUBLIC_PORT:-8080}:8080"' if False else 'plain text about nothing',
        ]
        for line in clean:
            with self.subTest(line=line):
                self.assertEqual(ce.scan_line(line), [])

    def test_def_with_43_char_name_not_flagged_but_call_is(self):
        name = 'x' * 40 + '_ab'
        self.assertEqual(len(name), 43)
        self.assertEqual(ce.scan_line(f'    def {name}(self):'), [])
        self.assertIn('token_shape_43', rules(f'    call({name})'))

    def test_ip_octet_range_and_version_like(self):
        self.assertNotIn('ip_public', rules('1.2.3.400'))
        self.assertIn('ip_public', rules('x ' + '.'.join(['1', '2', '3', '4']) + ' y'))

    def test_denylist_word_boundary_case_insensitive(self):
        import re
        deny = [re.compile(r'(?<![A-Za-z0-9])' + re.escape('Zork') + r'(?![A-Za-z0-9])', re.I)]
        self.assertIn('denylist_literal', rules('see zork here', deny))
        self.assertNotIn('denylist_literal', rules('zorkmid', deny))

    def test_own_source_is_clean(self):
        path = Path(ce.__file__)
        for no, text in ce._lines(path.read_bytes()):
            self.assertEqual(ce.scan_line(text), [], f'ce_scan.py line {no}')


class TreeAndCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'tree'
        self.root.mkdir()
        self.env = dict(os.environ)
        self.env.pop('AETHER_CE_DENYLIST', None)

    def run_cli(self, *args):
        out = io.StringIO()
        old = os.environ.get('AETHER_CE_DENYLIST')
        os.environ.pop('AETHER_CE_DENYLIST', None)
        os.environ.update({k: v for k, v in self.env.items() if k == 'AETHER_CE_DENYLIST'})
        try:
            with redirect_stdout(out):
                rc = ce.main(list(args))
        finally:
            if old is None:
                os.environ.pop('AETHER_CE_DENYLIST', None)
            else:
                os.environ['AETHER_CE_DENYLIST'] = old
        try:
            return rc, json.loads(out.getvalue() or 'null')
        except json.JSONDecodeError:
            return rc, None

    def test_clean_tree_exit_0(self):
        (self.root / 'a.txt').write_text('hello\n')
        rc, rep = self.run_cli(str(self.root), '--no-history')
        self.assertEqual((rc, rep['findings']), (0, []))
        self.assertEqual(rep['summary']['denylist'], 'pending_owner_file')

    def test_finding_exit_1_never_prints_text_and_hashes_line(self):
        secret_line = POSITIVES['ip_rfc1918']
        (self.root / 'a.txt').write_text('ok\n' + secret_line + '\n')
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ce.main([str(self.root), '--no-history'])
        self.assertEqual(rc, 1)
        raw = out.getvalue()
        self.assertNotIn('192.168', raw)
        f = json.loads(raw)['findings'][0]
        self.assertEqual(set(f), {'rule', 'path', 'line', 'sha256'})
        self.assertEqual((f['rule'], f['path'], f['line']), ('ip_rfc1918', 'a.txt', 2))
        self.assertEqual(f['sha256'], hashlib.sha256(secret_line.encode()).hexdigest())

    def test_binary_strings_scanned(self):
        (self.root / 'img.png').write_bytes(b'\x89PNG\r\n\x1a\n\0\0\0tEXtComment\0' + POSITIVES['home_path'].encode() + b'\0\1\2')
        rc, rep = self.run_cli(str(self.root), '--no-history')
        self.assertEqual(rc, 1)
        self.assertEqual(rep['findings'][0]['rule'], 'home_path')

    def test_path_rules(self):
        for name in ('.env', 'owner.token', 'x.key', 'data.sqlite3', 'log.jsonl'):
            (self.root / name).write_text('x\n')
        (self.root / '.env.example').write_text('A=1\n')
        rc, rep = self.run_cli(str(self.root), '--no-history')
        self.assertEqual(rc, 1)
        paths = sorted(f['path'] for f in rep['findings'] if f['rule'] == 'env_file')
        self.assertEqual(paths, ['.env', 'data.sqlite3', 'log.jsonl', 'owner.token', 'x.key'])

    def test_jsonl_allowed_under_examples(self):
        (self.root / 'examples').mkdir()
        (self.root / 'examples' / 'e.jsonl').write_text('{}\n')
        self.assertEqual(self.run_cli(str(self.root), '--no-history')[0], 0)

    def test_allowlist_by_rule_path_hash(self):
        line = POSITIVES['pem_private_key']
        (self.root / 'fx.py').write_text(line + '\n')
        allow = [{'rule': 'pem_private_key', 'path': 'fx.py', 'sha256': hashlib.sha256(line.encode()).hexdigest()}]
        (self.root / 'ce_scan_allow.json').write_text(json.dumps(allow))
        rc, rep = self.run_cli(str(self.root), '--no-history')
        self.assertEqual((rc, rep['summary']['allowlisted']), (0, 1))

    def test_denylist_applied_and_must_be_outside_tree(self):
        (self.root / 'a.txt').write_text('mentions Zorkville today\n')
        outside = Path(self.tmp.name) / 'deny.txt'
        outside.write_text('# comment\nzorkville\n')
        self.env['AETHER_CE_DENYLIST'] = str(outside)
        rc, rep = self.run_cli(str(self.root), '--no-history')
        self.assertEqual((rc, rep['findings'][0]['rule'], rep['summary']['denylist']), (1, 'denylist_literal', 'applied'))
        inside = self.root / 'deny.txt'
        inside.write_text('zorkville\n')
        self.env['AETHER_CE_DENYLIST'] = str(inside)
        self.assertEqual(self.run_cli(str(self.root), '--no-history')[0], 2)

    def test_errors_exit_2(self):
        self.assertEqual(self.run_cli(str(self.root / 'missing'))[0], 2)
        self.env['AETHER_CE_DENYLIST'] = str(Path(self.tmp.name) / 'nope.txt')
        self.assertEqual(self.run_cli(str(self.root))[0], 2)
        self.env.pop('AETHER_CE_DENYLIST')
        self.assertEqual(self.run_cli('--bogus')[0], 2)


def git(root, *args):
    cfg = ['-c', 'user.name=T', '-c', 'user.email=t@localhost.invalid', '-c', 'commit.gpgsign=false']
    return subprocess.run(['git', '-C', str(root), *cfg, *args], capture_output=True, check=True, text=True).stdout


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        git(self.root, 'init', '-q')

    def scan(self):
        sc = ce.Scanner(self.root)
        sc.run()
        return sc

    def test_single_clean_commit_passes(self):
        (self.root / 'a.txt').write_text('fine\n')
        git(self.root, 'add', '-A'); git(self.root, 'commit', '-qm', 'initial')
        sc = self.scan()
        self.assertEqual(sc.findings, [])
        self.assertEqual(sc.blobs, 1)

    def test_secret_in_old_commit_found_and_history_rule(self):
        (self.root / 'a.txt').write_text(POSITIVES['ip_cgnat'] + '\n')
        git(self.root, 'add', '-A'); git(self.root, 'commit', '-qm', 'one')
        (self.root / 'a.txt').write_text('clean\n')
        git(self.root, 'commit', '-qam', 'two')
        got = {f['rule'] for f in self.scan().findings}
        self.assertEqual(got, {'git_history', 'ip_cgnat'})

    def test_remote_rejected(self):
        (self.root / 'a.txt').write_text('fine\n')
        git(self.root, 'add', '-A'); git(self.root, 'commit', '-qm', 'initial')
        git(self.root, 'remote', 'add', 'origin', 'https://example.invalid/x.git')
        self.assertIn('git_remote', {f['rule'] for f in self.scan().findings})

    def test_commit_message_scanned(self):
        (self.root / 'a.txt').write_text('fine\n')
        git(self.root, 'add', '-A'); git(self.root, 'commit', '-qm', POSITIVES['home_path'])
        sc = self.scan()
        self.assertTrue(any(f['rule'] == 'home_path' and 'commit-message' in f['path'] for f in sc.findings))


if __name__ == '__main__':
    unittest.main()
