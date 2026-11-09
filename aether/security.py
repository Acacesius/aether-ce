"""Private owner credential storage. Never log credentials or authorization headers."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat

from aether.studio import fsperm


class SecurityError(ValueError):
    """Private storage is missing, unsafe, or malformed."""


def _posix() -> bool:
    """True when POSIX ownership/mode semantics apply (evaluated at call time)."""
    return os.name == 'posix' and hasattr(os, 'getuid')


def owned_by_me(info) -> bool:
    """POSIX: the stat result belongs to the current user. Elsewhere there is no uid to compare, so this is True and
    protection comes from ``fsperm`` ACLs instead."""
    return (not _posix()) or info.st_uid == os.getuid()


def _open_flags() -> int:
    """O_NOFOLLOW / O_NONBLOCK where they exist (0 on Windows); O_BINARY so token bytes are never newline-translated."""
    return (getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))


def private_directory(path: Path) -> None:
    """Create a private directory or reject unsafe existing storage (no chmod repair)."""
    path = Path(path)
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        created = False
    if created and not _posix():
        fsperm.restrict(path, directory=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or not owned_by_me(info):
        raise SecurityError("Private directory must be an owner-owned directory")
    if _posix() and stat.S_IMODE(info.st_mode) != 0o700:
        raise SecurityError("Private directory must be owner-owned with mode 0700")


def private_open(path: Path, flags: int) -> int:
    """Open a regular owner-only file without following its final symlink."""
    path = Path(path)
    private_directory(path.parent)
    fd = os.open(path, flags | _open_flags(), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not owned_by_me(info):
            raise SecurityError("Private file must be a regular owner-owned file")
        if _posix() and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise SecurityError("Private file must be singly linked, owner-owned, mode 0600")
        if not _posix() and flags & os.O_CREAT:
            fsperm.restrict(path, directory=False)
        return fd
    except BaseException:
        os.close(fd)
        raise


class OwnerAuth:
    """Instantiate at startup; pass only verify_bearer's boolean to trusted actions."""

    def __init__(self, token_path: Path = Path('.local/owner.token')):
        self.token_path = Path(token_path)
        try:
            fd = private_open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            self._read()
        else:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(secrets.token_urlsafe(32) + '\n')
                stream.flush()
                os.fsync(stream.fileno())

    def _read(self) -> str:
        fd = private_open(self.token_path, os.O_RDONLY)
        with os.fdopen(fd, encoding='utf-8') as stream:
            value = stream.read(256)
        if len(value) != 44 or not value.endswith('\n'):
            raise SecurityError("Owner token has invalid format")
        value = value[:-1]
        if any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_' for c in value):
            raise SecurityError("Owner token has invalid format")
        return value

    def verify_bearer(self, header: str | None) -> bool:
        try:
            expected = self._read()
            supplied = header[7:] if isinstance(header, str) and header.startswith('Bearer ') else ''
            # Compare fixed-size byte strings even for absent or malformed headers.
            candidate = supplied.encode('ascii') if len(supplied) == 43 else b'\x00' * 43
            return hmac.compare_digest(expected.encode('ascii'), candidate)
        except (OSError, SecurityError, UnicodeError):
            return False

    def fingerprint(self, key: bytes) -> bytes | None:
        """Keyed digest of the current 'Bearer <token>' header value; never the token itself.

        Lets the RDP proxy bind handles/tickets/sessions to the owner-token generation and
        detect rotation without retaining the bearer.
        """
        try:
            return hmac.new(key, ('Bearer ' + self._read()).encode('ascii'), hashlib.sha256).digest()
        except (OSError, SecurityError, UnicodeError):
            return None
