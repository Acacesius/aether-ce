"""StudioApplication: CE-only HTTP server. Same guard/response rules as the live AETHER handler, Studio routes only."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from aether.security import OwnerAuth, SecurityError, private_directory
from aether.studio import mode as mode_module
from aether.studio import proc
from aether.studio.api import BODY_LIMIT, StudioAPI
from aether.studio.assist import provider as provider_module
from aether.studio.assist.service import Assist
from aether.studio.exec_queue import QueueError
from aether.studio.executors.adapter import AdapterExecutor
from aether.studio.executors.mock import MockExecutor
from aether.studio.runner import Runner
from aether.studio.store import Store

LOG_STREAM = re.compile(r'^/api/studio/runs/(que_[0-9A-HJKMNP-TV-Z]{26})/log/stream$')
DOCKERENV = Path('/.dockerenv')
UI_DIR = Path(__file__).resolve().parent / 'ui'
UI_ASSETS = {'index.html': 'text/html; charset=utf-8', 'studio.css': 'text/css; charset=utf-8',
             **{f'{name}.js': 'text/javascript; charset=utf-8'
                for name in ('studio', 'lib', 'icons', 'quickrun', 'builder', 'library', 'runs', 'assist',
                             'workspace', 'slots', 'tracker', 'runpage')}}
# Static assets get no inline-script/style allowance; connect-src 'self' is the API on the same origin.
UI_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
          "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def load_ui(directory=UI_DIR):
    """Read every UI asset once at startup; a missing asset fails loudly instead of 404-ing at runtime."""
    assets = {}
    for name, content_type in UI_ASSETS.items():
        path = Path(directory) / name
        assert path.is_file(), f'missing Studio UI asset {name}'
        assets[name] = (path.read_bytes(), content_type)
    return assets


class StudioApplication:
    def __init__(self, mode, *, store, runner, auth, executors, assist, public_port=None, driver=None, execq=None):
        if not mode.custom:
            raise mode_module.ModeError('StudioApplication requires AETHER_MODE=custom')
        self.mode = mode
        self.store = store
        self.runner = runner
        self.auth = auth
        self.public_port = public_port
        self.ui = load_ui()
        self.driver = driver     # queue driver thread, only with AETHER_EXEC=cli
        self.api = StudioAPI(store, runner, mode, executors, assist, execq)


class StudioHandler(BaseHTTPRequestHandler):
    server_version = 'AETHER-Studio-CE/0.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    @property
    def app(self):
        return self.server.app

    def _hosts(self):
        ports = {self.server.server_address[1]}
        if self.app.public_port:
            ports.add(self.app.public_port)
        return {f'{h}:{p}' for p in ports for h in ('127.0.0.1', 'localhost')}

    def _guard(self, *, write=False, api=False):
        hosts = self.headers.get_all('Host', [])
        if len(hosts) != 1 or hosts[0] not in self._hosts():
            self.json_response({'error': 'invalid_host'}, 403)
            return False
        origins = self.headers.get_all('Origin', [])
        if len(origins) > 1 or (origins and origins[0] != 'http://' + hosts[0]) or (write and not origins):
            self.json_response({'error': 'invalid_origin'}, 403)
            return False
        fetch = self.headers.get_all('Sec-Fetch-Site', [])
        if len(fetch) > 1 or (fetch and fetch[0] not in ('same-origin', 'none')):
            self.json_response({'error': 'cross_site_request'}, 403)
            return False
        if api:
            values = self.headers.get_all('Authorization', [])
            if len(values) != 1 or not self.app.auth.verify_bearer(values[0]):
                self.json_response({'error': 'owner_auth_required'}, 401)
                return False
        return True

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            return self.json_response({'error': 'invalid_target'}, 400)
        if parsed.path == '/healthz':
            if not self._guard():
                return
            return self.json_response({'status': 'ok', 'mode': 'custom', 'demo_only': True})
        if not self._guard(api=parsed.path.startswith('/api/')):
            return
        if parsed.path == '/':
            return self._respond(b'', 'text/plain; charset=utf-8', 302, {'Location': '/studio/'})
        if parsed.path.startswith('/studio/') or parsed.path == '/studio':
            return self._static(parsed.path)
        if not parsed.path.startswith('/api/studio/'):
            return self.json_response({'error': 'not_found'}, 404)
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
        except ValueError:
            return self.json_response({'error': 'invalid_request'}, 400)
        execq = self.app.api.execq
        if execq is not None:
            match = LOG_STREAM.match(parsed.path)
            if match:
                return self._sse(execq, match.group(1), query)
        self._api('GET', parsed.path, query, None)

    def _sse(self, execq, run_id, query):
        """Server-sent events for the raw run log (one thread per connection; ThreadingHTTPServer)."""
        after = (query.get('after') or ['0'])[0]
        try:
            events = execq.stream(run_id, after)
        except QueueError as error:
            return self.json_response({'error': error.code}, error.status)
        self.send_response(200)
        for name, value in (('Content-Type', 'text/event-stream; charset=utf-8'), ('Cache-Control', 'no-store'),
                            ('X-Content-Type-Options', 'nosniff'), ('Connection', 'close')):
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        try:
            for item in events:
                if item is None:
                    self.wfile.write(b': keepalive\n\n')
                else:
                    name, view = item
                    self.wfile.write(f'event: {name}\ndata: {json.dumps(view, sort_keys=True)}\n\n'.encode('utf-8'))
                self.wfile.flush()
        except OSError:
            pass

    def do_POST(self):
        self._write('POST')

    def do_PUT(self):
        self._write('PUT')

    def _write(self, method):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            return self.json_response({'error': 'invalid_target'}, 400)
        if not self._guard(write=True, api=True):
            return
        if parsed.query or not parsed.path.startswith('/api/studio/'):
            return self.json_response({'error': 'not_found'}, 404)
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get_all('Transfer-Encoding') or len(lengths) != 1:
            return self.json_response({'error': 'content_length_required'}, 400)
        try:
            length = int(lengths[0])
        except ValueError:
            return self.json_response({'error': 'invalid_length'}, 400)
        if not 1 <= length <= BODY_LIMIT:
            return self.json_response({'error': 'body_too_large'}, 413)
        if self.headers.get('Content-Type') != 'application/json':
            return self.json_response({'error': 'json_required'}, 415)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete body')

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise KeyError('duplicate_field')
                    result[key] = value
                return result
            body = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(body, dict):
                raise ValueError('Invalid fields')
        except KeyError:
            return self.json_response({'error': 'duplicate_field'}, 400)
        except (ValueError, UnicodeError):
            return self.json_response({'error': 'invalid_request'}, 400)
        self._api(method, parsed.path, {}, body)

    def _api(self, method, path, query, body):
        headers = {}
        for name in ('Idempotency-Key', 'If-Match'):
            values = self.headers.get_all(name, [])
            if len(values) == 1:
                headers[name] = values[0]
        try:
            status, payload = self.app.api.handle(method, path, query, body, headers)
        except (OSError, SecurityError):
            status, payload = 503, {'error': 'private_storage_unavailable'}
        except Exception:
            status, payload = 500, {'error': 'internal_error'}
        self.json_response(payload, status)

    def do_DELETE(self):
        parsed = urlsplit(self.path)
        if self.app.driver is None or parsed.scheme or parsed.netloc or not parsed.path.startswith('/api/studio/queue/'):
            return self.do_OPTIONS()      # flag off (or not a queue item): 405 exactly as before
        if not self._guard(write=True, api=True):
            return
        if parsed.query:
            return self.json_response({'error': 'not_found'}, 404)
        self._api('DELETE', parsed.path, {}, None)

    def do_OPTIONS(self):
        if not self._guard(api=urlsplit(self.path).path.startswith('/api/')):
            return
        self.json_response({'error': 'method_not_allowed'}, 405)

    do_PATCH = do_OPTIONS
    do_HEAD = do_OPTIONS
    do_TRACE = do_OPTIONS
    do_CONNECT = do_OPTIONS

    def _static(self, path):
        if path == '/studio':
            return self._respond(b'', 'text/plain; charset=utf-8', 302, {'Location': '/studio/'})
        name = path[len('/studio/'):] or 'index.html'
        asset = self.app.ui.get(name)
        if asset is None:
            return self.json_response({'error': 'not_found'}, 404)
        self._respond(asset[0], asset[1], 200, {'Content-Security-Policy': UI_CSP})

    def _respond(self, payload, content_type, status, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        headers = {'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'", **(extra or {})}
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(payload)

    def json_response(self, obj, status=200):
        return self._respond(json.dumps(obj, sort_keys=True).encode(), 'application/json; charset=utf-8', status)

    def log_message(self, fmt, *args):
        return  # never log paths, queries or headers


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app, *, dockerenv=DOCKERENV):
        if 'AETHER_ADMIN_TOKEN' in os.environ:
            raise SecurityError('Studio refuses AETHER_ADMIN_TOKEN in its environment')
        if address[0] != '127.0.0.1':
            if not (address[0] == '0.0.0.0' and app.mode.runtime == 'docker' and Path(dockerenv).exists()):
                raise ValueError('Studio binds 0.0.0.0 only inside docker; otherwise IPv4 loopback')
        self.app = app
        super().__init__(address, StudioHandler)


def cli_enabled(env) -> bool:
    """The CLI executor exists only with ``AETHER_EXEC=cli``; anything else keeps today's executor set."""
    return env.get('AETHER_EXEC') == 'cli'


def build(mode, *, max_workers=1, public_port=None, assist_env=None, assist_resolver=provider_module.system_resolver,
          exec_env=None, **runner_options):
    private_directory(mode.data_dir)
    auth = OwnerAuth(mode.data_dir / 'owner.token')
    store = Store(mode.data_dir / 'studio.sqlite3')
    executors = {'mock': MockExecutor(),
                 'adapter': AdapterExecutor(store, data_dir=mode.data_dir, provider=mode.provider)}
    env = os.environ if exec_env is None else exec_env
    driver = execq = None
    if cli_enabled(env):   # experimental, off by default (CLI_EXECUTOR.md)
        from aether.studio.driver import Driver
        from aether.studio.exec_queue import QueueStore
        from aether.studio.executors.cli import CliExecutor
        from aether.studio.queue_api import QueueAPI
        executors['cli'] = cli = CliExecutor(mode.data_dir)
        queue = QueueStore(store, mode.data_dir)
        queue.ensure()
        driver = Driver(queue, cli.slots, cli, global_cap=env.get('AETHER_EXEC_MAX_RUNS', 1),
                        slot_cap=env.get('AETHER_EXEC_SLOT_CAP', 1))
        execq = QueueAPI(queue, driver, cli.slots, cli, mode.data_dir)
    runner = Runner(store, executors, max_workers=max_workers, artifacts_dir=mode.data_dir / 'artifacts',
                    provider=mode.provider, **runner_options)
    chat, reason = provider_module.from_env(os.environ if assist_env is None else assist_env, resolver=assist_resolver)
    return StudioApplication(mode, store=store, runner=runner, auth=auth, executors=executors,
                             assist=Assist(store, mode, chat, reason), public_port=public_port,
                             driver=driver, execq=execq)


def _port(name, default):
    raw = os.environ.get(name, str(default))
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise SystemExit(f'{name} must be an unprivileged TCP port')
    return int(raw)


def main(mode=None):
    mode = mode or mode_module.load_or_exit()
    if not mode.custom:
        raise SystemExit('aether.studio.app runs only with AETHER_MODE=custom')
    port = _port('AETHER_PORT', 8080)
    public_port = _port('AETHER_PUBLIC_PORT', port)
    workers = os.environ.get('AETHER_STUDIO_MAX_WORKERS', '1')
    if workers not in ('1', '2', '3', '4'):
        raise SystemExit('AETHER_STUDIO_MAX_WORKERS must be 1..4')
    bind = os.environ.get('AETHER_BIND', '127.0.0.1')
    app = build(mode, max_workers=int(workers), public_port=public_port)
    httpd = StudioServer((bind, port), app)
    app.runner.start()
    if app.driver is not None:
        app.driver.start()

    def terminate(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    proc.on_terminate(terminate)
    print(f'AETHER Studio CE (DEMO executor; enrolled adapters only by explicit grant) listening {bind}:{port}; published 127.0.0.1:{public_port}', flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if app.driver is not None:
            app.driver.stop()
        app.runner.stop()
        httpd.server_close()
        app.store.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
