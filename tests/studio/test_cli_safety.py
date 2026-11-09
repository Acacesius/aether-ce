"""K5 safety review: executable checks for the claims in docs/SECURITY.md ("CLI executor mode")."""
import http.client
import os
from pathlib import Path
import stat
import unittest

from aether.studio import cli_events, run_log
from aether.studio.app import StudioServer, build
from aether.studio.executors import cli
from aether.studio.slots import SlotError, validate_slot

from tests.studio import support

ULID = 'que_00000000000000000000000000'
CLI_ENV = {'AETHER_EXEC': 'cli'}
SLOT = {'id': 'claude-a', 'kind': 'claude', 'label': 'a', 'config_dir': '/x/cfg'}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.root = support.tempdir(self).resolve()
        self.data = self.root / 'data'
        self.data.mkdir()

    def bad(self, raw):
        with self.assertRaises(cli.WorkspaceError):
            cli.validate_workspace(raw, self.data)

    def test_refused_shapes(self):
        (self.root / 'file').write_text('x', encoding='utf-8')
        for raw in ('', '   ', None, 5, 'relative/dir', str(self.root / 'missing'), str(self.root / 'file'),
                    Path(self.root.anchor), str(self.data), str(self.data / 'sub'), str(self.root)):
            with self.subTest(raw=raw):
                self.bad(raw)

    def test_symlink_into_data_dir_is_resolved_and_refused(self):
        link = self.root / 'link'
        try:
            link.symlink_to(self.data, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        self.bad(str(link))
        (self.data / 'sub').mkdir()
        self.bad(str(link / 'sub'))

    def test_valid_workspace_is_resolved(self):
        ws = self.root / 'ws'
        ws.mkdir()
        self.assertEqual(cli.validate_workspace(str(ws / '..' / 'ws'), self.data), ws)

    def test_slot_config_dir_cannot_be_data_dir(self):
        for path in (self.data, self.data / 'cfg', self.root):
            with self.assertRaises(SlotError):
                validate_slot(dict(SLOT, config_dir=str(path)), self.data)


class ExecutionTests(unittest.TestCase):
    def test_env_is_minimal(self):
        base = {'PATH': '/bin', 'HOME': '/h', 'ANTHROPIC_API_KEY': 'k', 'OPENAI_API_KEY': 'k', 'GITHUB_TOKEN': 't',
                'AETHER_ADMIN_TOKEN': 't', 'AWS_SECRET_ACCESS_KEY': 's', 'SSH_AUTH_SOCK': '/s', 'AETHER_EXEC': 'cli'}
        env = cli.build_env(SLOT, base)
        self.assertEqual(set(env), {'PATH', 'HOME', 'NO_COLOR', 'CLAUDE_CONFIG_DIR'})
        env = cli.build_env(dict(SLOT, kind='codex'), base)
        self.assertEqual(set(env), {'PATH', 'HOME', 'NO_COLOR', 'CODEX_HOME'})

    def test_claude_tool_lists_exact(self):
        self.assertEqual(cli.CLAUDE_ALLOWED, ('Read', 'Edit', 'MultiEdit', 'Write', 'Glob', 'Grep', 'LS', 'NotebookEdit',
                                              'TodoWrite'))
        self.assertEqual(cli.CLAUDE_DISALLOWED, ('Bash', 'BashOutput', 'KillShell', 'WebFetch', 'WebSearch', 'Task'))
        self.assertFalse(set(cli.CLAUDE_ALLOWED) & set(cli.CLAUDE_DISALLOWED))
        argv = cli.build_argv(SLOT, ['claude'], '/ws')
        self.assertTrue(all(isinstance(a, str) for a in argv))
        self.assertEqual(argv[argv.index('--allowedTools') + 1], ','.join(cli.CLAUDE_ALLOWED))
        self.assertNotIn('bypassPermissions', argv)
        self.assertFalse([a for a in argv if a.startswith('--dangerously')])

    def test_codex_argv_is_sandboxed_and_prompt_free(self):
        argv = cli.build_argv(dict(SLOT, kind='codex', model='m1'), ['codex'], '/ws')
        self.assertEqual(argv[argv.index('--sandbox') + 1], 'workspace-write')
        self.assertFalse([a for a in argv if a.startswith('--dangerously') or a.startswith('danger')])
        self.assertNotIn('--full-auto', argv)
        self.assertEqual(argv[-1], '-')


class LogTests(unittest.TestCase):
    def test_log_file_is_owner_only_and_served_text_redacted(self):
        root = support.tempdir(self)
        old = os.umask(0o022)       # a permissive umask must not leak the log
        self.addCleanup(os.umask, old)
        log = cli_events.RawLog(root / 'logs' / 'r.log')
        (root / 'logs').mkdir()
        secret = 'ghp_' + 'a1B2c3D4' * 5
        log.stdout('{"type":"assistant","message":{"content":[{"type":"text","text":"key %s"}]}}' % secret)
        log.stderr('token %s' % secret)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE((root / 'logs' / 'r.log').stat().st_mode), 0o600)
        chunk = run_log.read_chunk(root / 'logs' / 'r.log', 0, final=True)
        self.assertNotIn(secret, repr(chunk))


class HttpGuardTests(unittest.TestCase):
    PATHS = (('GET', '/api/studio/queue'), ('GET', '/api/studio/slots'), ('GET', '/api/studio/tracker'),
             ('GET', '/api/studio/queue/audit'), ('GET', f'/api/studio/queue/{ULID}'),
             ('GET', f'/api/studio/runs/{ULID}/log'), ('GET', f'/api/studio/runs/{ULID}/log/stream'),
             ('DELETE', f'/api/studio/queue/{ULID}'), ('POST', '/api/studio/queue'), ('PUT', '/api/studio/slots'),
             ('POST', '/api/studio/queue/pause'), ('POST', '/api/studio/slots/claude-a/clear-limit'))

    def raw(self, client, method, path, headers):
        conn = http.client.HTTPConnection('127.0.0.1', client.port, timeout=10)
        body = '{}' if method in ('POST', 'PUT') else None
        if body:
            headers = {'Content-Type': 'application/json', **headers}
        conn.request(method, path, body=body, headers=headers)
        status = conn.getresponse().status
        conn.close()
        return status

    def test_every_new_route_enforces_host_origin_site_and_token(self):
        app, client = support.serve(self, exec_env=CLI_ENV)
        good = {'Host': f'127.0.0.1:{client.port}', 'Authorization': 'Bearer ' + client.token}
        for method, path in self.PATHS:
            write = method != 'GET'
            ok = dict(good, **({'Origin': client.origin} if write else {}))
            with self.subTest(method=method, path=path):
                self.assertNotIn(self.raw(client, method, path, ok), (401, 403))
                self.assertEqual(self.raw(client, method, path, dict(ok, Host='evil.example')), 403)
                self.assertEqual(self.raw(client, method, path, dict(ok, Origin='http://evil.example')), 403)
                self.assertEqual(self.raw(client, method, path, dict(ok, **{'Sec-Fetch-Site': 'cross-site'})), 403)
                self.assertEqual(self.raw(client, method, path, {k: v for k, v in ok.items() if k != 'Authorization'}), 401)
                self.assertEqual(self.raw(client, method, path, dict(ok, Authorization='Bearer wrong')), 401)
                if write:
                    self.assertEqual(self.raw(client, method, path, {k: v for k, v in ok.items() if k != 'Origin'}), 403)

    def test_bind_must_be_loopback_without_docker(self):
        app, _ = support.serve(self, exec_env=CLI_ENV)
        for address in ('0.0.0.0', '::', '192.0.2.1', 'localhost', ''):
            with self.subTest(address=address), self.assertRaises(ValueError):
                StudioServer((address, 0), app, dockerenv=Path('/nonexistent-dockerenv'))

    def test_slots_view_has_no_credentials(self):
        app, client = support.serve(self, exec_env=CLI_ENV)
        cfg = str(support.tempdir(self) / 'cfg')
        status, _ = client.request('PUT', '/api/studio/slots', {'slots': [dict(SLOT, config_dir=cfg)]})
        self.assertEqual(status, 200)
        status, body = client.request('GET', '/api/studio/slots')
        self.assertEqual(sorted(body['slots'][0]), sorted(
            ['id', 'kind', 'label', 'config_dir', 'model', 'enabled', 'order', 'cli_found', 'blocked_until',
             'last_limit_at', 'active_count', 'cap']))
        status, _ = client.request('PUT', '/api/studio/slots', {'slots': [dict(SLOT, config_dir=cfg, token='x')]})
        self.assertEqual(status, 400)
        status, _ = client.request('PUT', '/api/studio/slots', {'slots': [dict(SLOT, config_dir=cfg, label='ghp_' + 'a1B2c3D4' * 5)]})
        self.assertEqual(status, 400)
        text = (Path(app.mode.data_dir) / 'slots.json').read_text(encoding='utf-8')
        self.assertNotIn('ghp_', text)


if __name__ == '__main__':
    unittest.main()
