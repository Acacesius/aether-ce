"""Fail-closed mode switch (INTEGRATION_DESIGN §2). Live is the default; CE runs only inside docker."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

PRIVATE_ENV = ('AETHER_EXECUTOR_SOCKET', 'AETHER_EXECUTOR_KEY_FILE', 'AETHER_HARNESS_ROOT', 'AETHER_ADMIN_TOKEN')
PRIVATE_ROOTS = ('.local', '.config')  # per-user storage under $HOME that the data dir must not live in
DOCKER_SOCKET = Path('/var/run') / 'docker.sock'


class ModeError(ValueError):
    """Startup configuration is refused; the message is a one-line reason with no values echoed."""


@dataclass(frozen=True)
class Mode:
    mode: str       # 'live' | 'custom'
    provider: str   # 'none' | 'local'
    runtime: str    # 'host' | 'docker' | 'plain' (plain python process on loopback, no docker)
    data_dir: Path

    @property
    def custom(self) -> bool:
        return self.mode == 'custom'


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load(env: Mapping[str, str] | None = None, *, home: Path | None = None,
         docker_socket: Path = DOCKER_SOCKET) -> Mode:
    env = os.environ if env is None else env
    raw = env.get('AETHER_MODE', '')
    if raw in ('', 'live'):
        if env.get('AETHER_STUDIO_DB'):
            raise ModeError('live mode must not open a Studio DB (AETHER_STUDIO_DB set)')
        return Mode('live', 'none', 'host', Path('.local'))
    if raw != 'custom':
        raise ModeError('unknown AETHER_MODE')
    if env.get('AETHER_RUNTIME') != 'docker':
        raise ModeError('AETHER_MODE=custom requires AETHER_RUNTIME=docker')
    provider = env.get('AETHER_PROVIDER', 'none') or 'none'
    if provider not in ('none', 'local'):
        raise ModeError('AETHER_PROVIDER must be none or local')
    for name in PRIVATE_ENV:
        if name in env:
            raise ModeError(f'private integration variable {name} is refused in custom mode')
    data_dir = Path(env.get('AETHER_DATA_DIR') or '/data')
    if not data_dir.is_absolute():
        raise ModeError('AETHER_DATA_DIR must be absolute')
    resolved = data_dir.resolve()
    home = Path.home() if home is None else home
    for root in PRIVATE_ROOTS:
        for base in {home / root, home.resolve() / root}:
            if _under(resolved, base) or _under(data_dir, base):
                raise ModeError('AETHER_DATA_DIR resolves under private storage')
    if os.path.lexists(docker_socket):
        raise ModeError('host_socket_present')
    return Mode('custom', provider, 'docker', resolved)


def load_plain(data_dir, env: Mapping[str, str] | None = None) -> Mode:
    """Plain-process mode (Windows/Linux/macOS, no docker): custom + provider none + runtime plain.

    Skips only the docker-only checks (AETHER_RUNTIME, docker socket, private-storage roots); keeps refusing the private
    integration variables and a data dir that is a filesystem root."""
    env = os.environ if env is None else env
    for name in PRIVATE_ENV:
        if name in env:
            raise ModeError(f'private integration variable {name} is refused in custom mode')
    data_dir = Path(data_dir)
    if not data_dir.is_absolute():
        raise ModeError('data dir must be absolute')
    resolved = data_dir.resolve()
    if resolved == Path(resolved.anchor) or resolved.parent == resolved:
        raise ModeError('data dir must not be a filesystem root')
    return Mode('custom', 'none', 'plain', resolved)


def load_or_exit(env: Mapping[str, str] | None = None) -> Mode:
    try:
        return load(env)
    except ModeError as error:
        raise SystemExit(f'AETHER mode refused: {error}') from None
