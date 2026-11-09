"""``python -m aether.studio doctor [--port N] [--data-dir PATH]``: a plain-text environment report to paste back.

Every check is a function ``check_*(ctx) -> list[(level, text)]`` (level ``OK``/``WARN``/``FAIL``). The report never
prints environment values, token contents or the contents of any slot config folder, and every printed path has the
current user name replaced with ``<user>``. Exit code 1 when any check FAILs.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

from aether.studio import adapter_sandbox, fsperm, proc, slots as slots_module

OK, WARN, FAIL = 'OK', 'WARN', 'FAIL'
TAGS = {OK: '[ OK ]', WARN: '[WARN]', FAIL: '[FAIL]'}
VERSION_TIMEOUT = 10
SPAWN_BUDGET = 5.0
FAKE_CLI = ("import sys, time\n"
            "print('doctor-ready', flush=True)\n"
            "time.sleep(60)\n")


# ---------------------------------------------------------------- redaction
def user_names(home=None) -> list:
    names = set()
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    try:
        base = re.split(r'[\\/]', str(home if home is not None else Path.home()).rstrip('\\/'))[-1]
        names.add(base)
    except Exception:
        pass
    return sorted((n for n in names if n), key=len, reverse=True)


def redact(text, names=None) -> str:
    """Replace the user name (any case) with ``<user>``. Names of 3+ characters are replaced anywhere; shorter ones
    only as a whole path component, so a one-letter account does not mangle the report."""
    text = str(text)
    for name in (user_names() if names is None else names):
        if len(name) >= 3:
            text = re.sub(re.escape(name), '<user>', text, flags=re.IGNORECASE)
        else:
            text = re.sub(r'(?<![A-Za-z0-9_.-])' + re.escape(name) + r'(?![A-Za-z0-9_.-])', '<user>', text,
                          flags=re.IGNORECASE)
    return text


class Context:
    def __init__(self, data_dir, port=8080, resolve=None, names=None):
        self.data_dir = Path(data_dir)
        self.port = port
        self.resolve = resolve or proc.resolve_cli
        self.names = names
        self.slot_results = []      # filled by check_slots: (slot, cli_path or None, runs_ok)


# ---------------------------------------------------------------- checks
def check_platform(ctx):
    out = [(OK, f'OS: {platform.system()} {platform.release()} ({sys.platform}, os.name={os.name}), '
                f'machine {platform.machine()}')]
    v = tuple(sys.version_info)[:3]
    out.append((OK if v >= (3, 11) else FAIL,
                f'Python {v[0]}.{v[1]}.{v[2]}' + ('' if v >= (3, 11) else ' (3.11 or newer required)')))
    version = None
    try:
        from importlib import metadata
        version = metadata.version('aether')
    except Exception:
        version = getattr(sys.modules.get('aether'), '__version__', None)
    out.append((OK, f'CE version: {version}' if version else 'CE version: not available (running from source)'))
    return out


def write_probe(directory: Path) -> None:
    """Create and delete a probe file; raises OSError on failure."""
    fd, name = tempfile.mkstemp(prefix='.doctor-', suffix='.tmp', dir=str(directory))
    try:
        os.write(fd, b'probe')
    finally:
        os.close(fd)
        os.unlink(name)


def check_data_dir(ctx):
    d = ctx.data_dir
    existed = d.is_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        write_probe(d)
    except OSError as error:
        return [(FAIL, f'Data dir {d}: not usable ({type(error).__name__}); choose another with --data-dir')]
    return [(OK, f'Data dir {d}: ' + ('exists' if existed else 'created') + ' and writable')]


def check_token(ctx):
    d = ctx.data_dir
    out = []
    try:
        fd, name = tempfile.mkstemp(prefix='.doctor-perm-', suffix='.tmp', dir=str(d))
        os.close(fd)
    except OSError as error:
        return [(FAIL, f'Token permission probe: cannot create a file in the data dir ({type(error).__name__})')]
    try:
        probe = Path(name)
        restricted = fsperm.restrict(probe, directory=False)
        verified = fsperm.verify_private(probe)
        if restricted and verified:
            out.append((OK, 'Token permission: a probe file can be restricted to the current user and verifies private'))
        else:
            out.append((FAIL, 'Token permission: probe file could not be made owner-only '
                              f'(restrict={restricted}, verify={verified}); Studio refuses to start in this folder'))
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    real = d / 'owner.token'
    if real.exists():
        if fsperm.verify_private(real):
            out.append((OK, 'Owner token file exists and its protection verifies (contents not read)'))
        else:
            out.append((WARN, 'Owner token file exists but is not verifiably owner-only (contents not read); '
                              'Studio will try to restrict it at startup'))
    else:
        out.append((OK, 'Owner token file does not exist yet (created on first start)'))
    return out


def check_port(ctx):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', ctx.port))
    except OSError as error:
        return [(WARN, f'Port {ctx.port} on 127.0.0.1 is busy or not allowed ({type(error).__name__}); '
                       'use --port with another number')]
    finally:
        sock.close()
    return [(OK, f'Port {ctx.port} on 127.0.0.1 is free')]


def cli_version(path: str) -> str:
    """First line of ``<cli> --version`` (argv list, 10 s timeout), truncated to 120 characters."""
    try:
        done = subprocess.run([path, '--version'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=VERSION_TIMEOUT, check=False, shell=False)
    except subprocess.TimeoutExpired:
        return f'(--version timed out after {VERSION_TIMEOUT}s)'
    except (OSError, subprocess.SubprocessError) as error:
        return f'(could not run: {type(error).__name__})'
    text = (done.stdout or b'').decode('utf-8', 'replace').strip() or (done.stderr or b'').decode('utf-8', 'replace').strip()
    line = text.splitlines()[0].strip() if text else '(no output)'
    return line[:120]


def check_slots(ctx):
    try:
        items = slots_module.SlotStore(ctx.data_dir).load()
    except (slots_module.SlotError, OSError) as error:
        return [(FAIL, f'slots.json could not be loaded: {getattr(error, "code", type(error).__name__)}; fix or remove it')]
    if not items:
        return [(WARN, 'No slots configured: add one in the Slots view')]
    lines, results = [], []
    for slot in items:
        head = f'Slot {slot["id"]} [{slot["kind"]}] "{slot["label"]}"' + ('' if slot['enabled'] else ' (disabled)')
        cli = ctx.resolve(slot['kind'])
        entry = {'slot': slot, 'cli': cli, 'works': False}
        results.append(entry)
        if not cli:
            lines.append((slot['enabled'] and 'MISSING' or OK, f'{head}: CLI "{slot["kind"]}" not found on PATH'))
            entry['line'] = len(lines) - 1
        else:
            version = cli_version(cli)
            entry['works'] = slot['enabled'] and not version.startswith('(')
            lines.append((OK if not version.startswith('(') else WARN, f'{head}: CLI {cli}; --version: {version}'))
        config = Path(slot['config_dir'])
        lines.append((OK if config.is_dir() else WARN,
                      f'  config dir {config}: ' + ('exists' if config.is_dir() else 'does not exist yet (log in with the CLI)')))
    any_works = any(r['works'] for r in results)
    final = []
    for level, text in lines:
        if level == 'MISSING':
            level = WARN if any_works else FAIL
        final.append((level, text))
    ctx.slot_results = results
    return final


def check_dry_spawn(ctx):
    start = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='aether-doctor-') as tmp:
            script = Path(tmp) / 'fake_cli.py'
            script.write_text(FAKE_CLI, encoding='utf-8')
            env = {k: os.environ[k] for k in ('SYSTEMROOT', 'PATH') if k in os.environ}
            handle = proc.start([sys.executable, str(script)], tmp, env)
            try:
                box = []
                reader = threading.Thread(target=lambda: box.append(handle.popen.stdout.readline()), daemon=True)
                reader.start()
                reader.join(SPAWN_BUDGET)
                if not box or b'doctor-ready' not in box[0]:
                    return [(FAIL, 'Dry spawn: the test process started but its output line did not arrive in time')]
            finally:
                proc.kill_tree(handle)
                try:
                    handle.wait(max(0.5, SPAWN_BUDGET - (time.monotonic() - start)))
                except subprocess.TimeoutExpired:
                    return [(FAIL, 'Dry spawn: the test process is still running after kill_tree')]
                finally:
                    handle.close_pipes()
    except Exception as error:
        return [(FAIL, f'Dry spawn: could not start a process ({type(error).__name__}: {error})')]
    elapsed = time.monotonic() - start
    if elapsed > SPAWN_BUDGET:
        return [(FAIL, f'Dry spawn: too slow ({elapsed:.1f}s, limit {SPAWN_BUDGET:.0f}s)')]
    return [(OK, f'Dry spawn: started, read a line, and killed the process tree in {elapsed:.1f}s')]


def check_landlock(ctx):
    if not sys.platform.startswith('linux'):
        return [(OK, 'Landlock: not applicable: adapter sandbox disabled, CLI executor unaffected')]
    ok, message = adapter_sandbox.availability()
    if ok:
        return [(OK, 'Landlock: adapter sandbox available')]
    return [(WARN, f'Landlock: {message}')]


CHECKS = (('Platform', check_platform), ('Data dir', check_data_dir), ('Token permission', check_token),
          ('Port', check_port), ('Slots', check_slots), ('Dry spawn', check_dry_spawn), ('Landlock', check_landlock))


def run_checks(ctx):
    """[(section, [(level, text)])]; a check that raises is reported as FAIL, never crashes the report."""
    report = []
    for title, check in CHECKS:
        try:
            lines = check(ctx)
        except Exception as error:
            lines = [(FAIL, f'{title} check crashed ({type(error).__name__})')]
        report.append((title, lines))
    return report


def render(report, names=None) -> str:
    out = ['AETHER CE doctor report', '']
    for title, lines in report:
        out.append(f'== {title}')
        out.extend(f'{TAGS[level]} {redact(text, names)}' for level, text in lines)
        out.append('')
    fails = sum(level == FAIL for _, lines in report for level, _ in lines)
    warns = sum(level == WARN for _, lines in report for level, _ in lines)
    out.append(f'Summary: {fails} FAIL, {warns} WARN')
    return '\n'.join(out)


def main(argv=None, env=None, out=None) -> int:
    ap = argparse.ArgumentParser(prog='python -m aether.studio doctor')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--data-dir', default=None)
    args = ap.parse_args(argv)
    if args.data_dir:
        data_dir = Path(os.path.abspath(args.data_dir))
    else:
        from aether.studio.__main__ import default_data_dir
        data_dir = Path(os.path.abspath(default_data_dir(os.environ if env is None else env, Path.home(), os.name)))
    report = run_checks(Context(data_dir, args.port))
    print(render(report), file=out or sys.stdout)
    return 1 if any(level == FAIL for _, lines in report for level, _ in lines) else 0
