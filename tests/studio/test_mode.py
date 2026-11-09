from pathlib import Path
import tempfile
import unittest

from aether.studio import mode
from aether.studio.mode import ModeError, load

CE = {'AETHER_MODE': 'custom', 'AETHER_RUNTIME': 'docker', 'AETHER_PROVIDER': 'none', 'AETHER_DATA_DIR': '/data'}
NOSOCK = Path('/nonexistent/docker.sock')


def ce(**extra):
    env = dict(CE)
    env.update(extra)
    return env


class ModeTests(unittest.TestCase):
    def load(self, env, **kw):
        return load(env, docker_socket=kw.pop('docker_socket', NOSOCK), **kw)

    def test_mode_defaults_live(self):
        for env in ({}, {'AETHER_MODE': ''}, {'AETHER_MODE': 'live'}):
            self.assertEqual(self.load(env).mode, 'live')
            self.assertFalse(self.load(env).custom)

    def test_unknown_mode_refused(self):
        with self.assertRaisesRegex(ModeError, 'unknown AETHER_MODE'):
            self.load({'AETHER_MODE': 'cloud'})

    def test_custom_requires_docker(self):
        for runtime in (None, 'host', ''):
            env = ce()
            env.pop('AETHER_RUNTIME')
            if runtime is not None:
                env['AETHER_RUNTIME'] = runtime
            with self.assertRaises(ModeError):
                self.load(env)
        result = self.load(ce())
        self.assertEqual((result.mode, result.provider, result.runtime), ('custom', 'none', 'docker'))

    def test_custom_provider_allowlist(self):
        self.assertEqual(self.load(ce(AETHER_PROVIDER='local')).provider, 'local')
        for bad in ('openai', 'anthropic', 'cloud', 'NONE'):
            with self.assertRaises(ModeError):
                self.load(ce(AETHER_PROVIDER=bad))

    def test_custom_rejects_private_env(self):
        for name in mode.PRIVATE_ENV:
            with self.subTest(name=name), self.assertRaises(ModeError) as ctx:
                self.load(ce(**{name: 'x'}))
            self.assertNotIn('x\'', str(ctx.exception))

    def test_custom_rejects_private_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            for sub in ('.local', '.local/studio', '.config/x'):
                with self.subTest(sub=sub), self.assertRaises(ModeError):
                    self.load(ce(AETHER_DATA_DIR=str(home / sub)), home=home)
            self.assertEqual(self.load(ce(AETHER_DATA_DIR=str(home / 'elsewhere')), home=home).data_dir, home / 'elsewhere')

    def test_custom_requires_absolute_data_dir(self):
        with self.assertRaises(ModeError):
            self.load(ce(AETHER_DATA_DIR='relative'))

    def test_custom_rejects_docker_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = Path(tmp) / 'docker.sock'
            sock.touch()
            with self.assertRaisesRegex(ModeError, 'host_socket_present'):
                load(ce(), docker_socket=sock)

    def test_live_rejects_studio_db(self):
        with self.assertRaises(ModeError):
            self.load({'AETHER_STUDIO_DB': '/tmp/x.sqlite3'})

    def test_load_or_exit_is_one_line(self):
        with self.assertRaises(SystemExit) as ctx:
            mode.load_or_exit({'AETHER_MODE': 'bogus'})
        self.assertNotIn('\n', str(ctx.exception))
