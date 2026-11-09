"""The ONE process helper (CLI_EXECUTOR.md): process groups, tree kill and signal handlers live only here.

POSIX: children start in a new session (their own process group) and are ended with ``os.killpg`` (SIGTERM, then
SIGKILL). Windows: children start with ``CREATE_NEW_PROCESS_GROUP`` and are ended with ``taskkill /T /F /PID``.
Argv lists only; never a shell.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading

IS_WINDOWS = os.name == 'nt'
TASKKILL_TIMEOUT = 10


@dataclass
class ProcHandle:
    popen: subprocess.Popen
    argv: tuple
    windows: bool | None = None      # None: decided when the handle is made (os.name)
    killed: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        if self.windows is None:
            self.windows = IS_WINDOWS

    @property
    def pid(self) -> int:
        return self.popen.pid

    def poll(self):
        return self.popen.poll()

    def wait(self, timeout=None):
        return self.popen.wait(timeout)

    def close_pipes(self):
        for stream in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def resolve_cli(name: str, *, path: str | None = None, windows: bool = IS_WINDOWS) -> str | None:
    """Absolute path of a CLI on PATH (``shutil.which`` honours PATHEXT on Windows). For npm-style ``.cmd``/``.bat``
    shims a sibling ``.exe`` is preferred, so no batch interpreter sits between Studio and the CLI when avoidable."""
    found = shutil.which(name, path=path)
    if not found:
        return None
    found_path = Path(found)
    if windows and found_path.suffix.lower() in ('.cmd', '.bat'):
        exe = found_path.with_suffix('.exe')
        if exe.is_file():
            return str(exe.resolve())
    return str(found_path.resolve())


def group_kwargs(windows: bool | None = None) -> dict:
    """Popen keyword arguments that put the child in its own process group."""
    if IS_WINDOWS if windows is None else windows:
        return {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)}
    return {'start_new_session': True}


def popen_group(argv, **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` in a new process group (argv list, never a shell)."""
    assert isinstance(argv, (list, tuple)) and argv and all(isinstance(a, str) for a in argv), 'argv list required'
    kwargs.pop('shell', None)
    return subprocess.Popen(list(argv), shell=False, **group_kwargs(), **kwargs)


def start(argv, cwd, env, stdin: bytes | str | None = None) -> ProcHandle:
    """Start ``argv`` in ``cwd`` with exactly ``env``; stdout/stderr are pipes. ``stdin`` (if given) is written by a
    helper thread and then closed, so a large prompt cannot deadlock against a full stdout pipe."""
    if isinstance(stdin, str):
        stdin = stdin.encode('utf-8')
    popen = popen_group(argv, cwd=str(cwd), env=dict(env), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    handle = ProcHandle(popen, tuple(argv))
    if stdin is not None:
        def feed():
            try:
                popen.stdin.write(stdin)
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    popen.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
        threading.Thread(target=feed, name=f'proc-stdin-{popen.pid}', daemon=True).start()
    return handle


def signal_group(pid: int, *, force: bool = True) -> None:
    """POSIX only: send SIGKILL (``force``) or SIGTERM to the process group led by ``pid``; a gone group is fine."""
    try:
        os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _taskkill(pid: int) -> None:
    try:
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=TASKKILL_TIMEOUT, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        pass


def kill_tree(handle: ProcHandle, grace_seconds: float = 0) -> None:
    """End the whole process tree. ``grace_seconds > 0`` (POSIX): SIGTERM now, SIGKILL after the grace (timer thread,
    non-blocking). Windows has no polite console signal for another group: the tree is force-killed at once.
    Safe to call repeatedly and after the process has exited (POSIX still reaps stray group members)."""
    handle.killed.set()
    if handle.windows:
        if handle.popen.poll() is None:
            _taskkill(handle.pid)
            try:
                handle.popen.kill()   # the leader itself, in case taskkill was unavailable
            except OSError:
                pass
        return
    if grace_seconds and grace_seconds > 0:
        signal_group(handle.pid, force=False)
        timer = threading.Timer(grace_seconds, signal_group, args=(handle.pid,), kwargs={'force': True})
        timer.daemon = True
        timer.start()
    else:
        signal_group(handle.pid, force=True)


def on_terminate(callback) -> None:
    """Install ``callback(signum, frame)`` as the SIGTERM handler of this process (main thread only)."""
    signal.signal(signal.SIGTERM, callback)
