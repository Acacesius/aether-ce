"""Owner-only file permissions on every platform: ``chmod`` on POSIX, ``icacls`` on Windows (argv list, no shell)."""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import subprocess

IS_WINDOWS = os.name == 'nt'


def _icacls(path: Path, directory: bool) -> bool:
    try:
        user = getpass.getuser()
    except Exception:
        return False
    grant = f'{user}:(OI)(CI)F' if directory else f'{user}:F'
    try:
        done = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', grant], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def restrict(path, *, directory: bool | None = None, windows: bool = IS_WINDOWS) -> bool:
    """Make ``path`` accessible to the current user only. Returns True when the platform call succeeded."""
    path = Path(path)
    if directory is None:
        directory = path.is_dir()
    if windows:
        return _icacls(path, directory)
    try:
        os.chmod(path, 0o700 if directory else 0o600)
        return True
    except OSError:
        return False


# Principals that must never appear in a private path's ACL (icacls output, matched case-insensitively).
_BROAD_PRINCIPALS = ('everyone', 'builtin\\users', 'authenticated users', 'nt authority\\authenticated')


def verify_private(path, *, windows: bool = IS_WINDOWS) -> bool:
    """True only if ``path`` is verifiably owner-only: POSIX mode has no group/other bits (and is exactly 0600 for a
    file, 0700 for a directory); Windows ``icacls`` lists no broad principal (Everyone, Users, Authenticated Users)."""
    path = Path(path)
    if windows:
        try:
            done = subprocess.run(['icacls', str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=30, check=False, shell=False)
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode != 0:
            return False
        out = done.stdout
        text = (out.decode('utf-8', 'replace') if isinstance(out, bytes) else str(out or '')).lower()
        return bool(text.strip()) and not any(name in text for name in _BROAD_PRINCIPALS)
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return False
    return mode == (0o700 if path.is_dir() else 0o600)


def private_dir(path, *, windows: bool = IS_WINDOWS) -> Path:
    """Create (parents included) and restrict a directory."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    restrict(path, directory=True, windows=windows)
    return path
