"""Plain-process entry point: ``python -m aether.studio serve [--host 127.0.0.1] [--port N] [--data-dir PATH] [--open]``.

Loopback only, no docker. Enables the CLI executor. The owner token is stored in a private file and never printed."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading
from webbrowser import open as open_browser

from aether.studio import fsperm, mode as mode_module, proc

LOOPBACK = '127.0.0.1'


def default_data_dir(env, home, os_name) -> Path:
    """%LOCALAPPDATA%\\AetherCE on Windows (fallback <home>/AppData/Local), else $XDG_DATA_HOME or ~/.local/share."""
    home = Path(home)
    if os_name == 'nt':
        base = env.get('LOCALAPPDATA')
        return (Path(base) if base else home / 'AppData' / 'Local') / 'AetherCE'
    xdg = env.get('XDG_DATA_HOME')
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / '.local' / 'share'
    return base / 'aether-ce'


def check_host(value: str) -> str:
    if value in (LOOPBACK, 'localhost'):
        return LOOPBACK
    raise argparse.ArgumentTypeError('Studio listens on 127.0.0.1 only (got a non-loopback host); LAN/public binds are refused')


def check_port(value: str) -> int:
    if not value.isdigit() or not 1024 <= int(value) <= 65535:
        raise argparse.ArgumentTypeError('port must be an integer between 1024 and 65535')
    return int(value)


def protect_token(path: Path) -> None:
    """Fail closed: the owner token file must be verifiably owner-only or startup stops (nothing is printed)."""
    if not fsperm.restrict(path, directory=False) or not fsperm.verify_private(path):
        raise SystemExit(f'Refusing to start: could not restrict the owner token file to the current user ({path}). '
                         'Fix the folder permissions or choose another --data-dir.')


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog='python -m aether.studio')
    sub = top.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve', help='run Studio on loopback')
    serve.add_argument('--host', type=check_host, default=LOOPBACK)
    serve.add_argument('--port', type=check_port, default=8080)
    serve.add_argument('--data-dir', default=None)
    serve.add_argument('--open', action='store_true', help='open the browser once the server is listening')
    sub.add_parser('doctor', help='environment checks')
    return top


def serve(args, env=None) -> int:
    from aether.studio.app import StudioServer, build
    env = os.environ if env is None else env
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(env, Path.home(), os.name)
    data_dir = Path(os.path.abspath(data_dir))
    try:
        mode = mode_module.load_plain(data_dir, env)
    except mode_module.ModeError as error:
        raise SystemExit(f'AETHER mode refused: {error}') from None
    try:
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        app = build(mode, public_port=args.port, exec_env={'AETHER_EXEC': 'cli'})
    except OSError as error:
        raise SystemExit(f'Cannot prepare data dir: {type(error).__name__}') from None
    except Exception as error:
        raise SystemExit(f'Cannot start Studio: {type(error).__name__}: {error}') from None
    try:
        protect_token(app.auth.token_path)
        httpd = StudioServer((args.host, args.port), app)
    except BaseException:
        app.store.close()
        raise
    app.runner.start()
    if app.driver is not None:
        app.driver.start()
    proc.on_terminate(lambda signum, frame: threading.Thread(target=httpd.shutdown, daemon=True).start())
    url = f'http://{args.host}:{args.port}/'
    print(f'AETHER Studio CE listening: {url}', flush=True)
    print(f'Owner token stored in: {app.auth.token_path}', flush=True)
    print(f'Data dir: {data_dir}', flush=True)
    if args.open:
        threading.Timer(0.3, lambda: open_browser(url)).start()   # socket is already bound and listening
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


def main(argv=None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw[:1] == ['doctor']:
        try:
            from aether.studio import doctor
        except ImportError:
            print('doctor: not available', file=sys.stderr)
            return 2
        return doctor.main(raw[1:])
    args = parser().parse_args(argv)
    return serve(args)


if __name__ == '__main__':
    sys.exit(main())
