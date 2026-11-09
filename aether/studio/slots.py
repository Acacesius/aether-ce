"""Provider slots (CLI_EXECUTOR.md): which signed-in CLI accounts the CLI executor may use.

A slot names a CLI kind and the folder that CLI keeps its own login in (``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``).
Slots never hold credentials: unknown fields and secret-shaped values are refused. ``slots.json`` lives in the data
dir, is written atomically and restricted to the owner. Runtime ``state`` (limit blocks) is kept beside the slots.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading

from aether.studio import fsperm, scanner

FILE_NAME = 'slots.json'
VERSION = 1
KINDS = {'claude': 'CLAUDE_CONFIG_DIR', 'codex': 'CODEX_HOME'}
FIELDS = ('id', 'kind', 'label', 'config_dir', 'model', 'enabled', 'order')
REQUIRED = ('id', 'kind', 'label', 'config_dir')
ID_RE = re.compile(r'^[a-z][a-z0-9_-]{0,39}$')
MODEL_RE = re.compile(r'^[A-Za-z0-9._:\[\]-]{1,100}$')
MAX_SLOTS = 32
NOTE_MAX = 200


class SlotError(ValueError):
    def __init__(self, code: str, field: str = '', detail: str = ''):
        super().__init__(f'{code} {field} {detail}'.strip())
        self.code, self.field, self.detail = code, field, detail


def utc_iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def under(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or inside it (both already resolved)."""
    return path == root or root in path.parents


def _printable(value, field, low, high):
    if not isinstance(value, str) or not low <= len(value) <= high:
        raise SlotError('schema_invalid', field, f'string of {low}..{high} characters required')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise SlotError('schema_invalid', field, 'control characters not allowed')
    if scanner.scan(value):
        raise SlotError('secret_detected', field, 'secret-like value refused; slots never hold credentials')
    return value


def validate_slot(raw, data_dir) -> dict:
    """Validate one slot object; returns the normalised slot (``config_dir`` absolute, defaults filled)."""
    if not isinstance(raw, dict):
        raise SlotError('schema_invalid', '', 'object required')
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        raise SlotError('unknown_field', '/' + unknown[0], 'slots hold no credentials or extra settings')
    for name in REQUIRED:
        if name not in raw:
            raise SlotError('schema_invalid', '/' + name, 'required field missing')
    slot_id = raw['id']
    if not isinstance(slot_id, str) or not ID_RE.match(slot_id):
        raise SlotError('schema_invalid', '/id', 'lowercase key required')
    if raw['kind'] not in KINDS:
        raise SlotError('schema_invalid', '/kind', 'claude or codex')
    label = _printable(raw['label'], '/label', 1, 80)
    config = _printable(raw['config_dir'], '/config_dir', 1, 1000)
    if not os.path.isabs(config):
        raise SlotError('schema_invalid', '/config_dir', 'absolute path required')
    config_path = Path(config).resolve()
    data = Path(data_dir).resolve()
    if under(config_path, data) or under(data, config_path):
        raise SlotError('schema_invalid', '/config_dir', 'must not be, contain, or lie inside the data dir')
    model = raw.get('model')
    if model is not None and (not isinstance(model, str) or not MODEL_RE.match(model)):
        raise SlotError('schema_invalid', '/model', 'model name of letters, digits and ._:[]- only')
    enabled = raw.get('enabled', True)
    if not isinstance(enabled, bool):
        raise SlotError('schema_invalid', '/enabled', 'boolean required')
    order = raw.get('order', 0)
    if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order <= 999:
        raise SlotError('schema_invalid', '/order', 'integer 0..999 required')
    return {'id': slot_id, 'kind': raw['kind'], 'label': label, 'config_dir': str(config_path), 'model': model,
            'enabled': enabled, 'order': order}


def validate_slots(items, data_dir) -> list:
    if not isinstance(items, list) or len(items) > MAX_SLOTS:
        raise SlotError('schema_invalid', '/slots', f'list of at most {MAX_SLOTS} slots required')
    slots, ids, dirs = [], set(), set()
    for i, raw in enumerate(items):
        try:
            slot = validate_slot(raw, data_dir)
        except SlotError as error:
            raise SlotError(error.code, f'/slots/{i}{error.field}', error.detail) from None
        if slot['id'] in ids:
            raise SlotError('duplicate', f'/slots/{i}/id', 'slot ids must be unique')
        key = (slot['kind'], os.path.normcase(slot['config_dir']))
        if key in dirs:
            raise SlotError('duplicate', f'/slots/{i}/config_dir', 'two slots of one kind share a config folder')
        ids.add(slot['id'])
        dirs.add(key)
        slots.append(slot)
    return sorted(slots, key=lambda s: (s['order'], s['id']))


def slot_env(slot) -> dict:
    """The only variable a slot contributes to the CLI environment."""
    return {KINDS[slot['kind']]: slot['config_dir']}


class SlotStore:
    """``slots.json`` in the data dir. Thread-safe within one process; writes are atomic replacements."""

    def __init__(self, data_dir, *, clock=lambda: datetime.now(timezone.utc)):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / FILE_NAME
        self.clock = clock
        self._lock = threading.RLock()

    # ------------------------------------------------------------ file
    def _read(self) -> dict:
        try:
            text = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return {'version': VERSION, 'slots': [], 'state': {}}
        try:
            doc = json.loads(text)
        except ValueError:
            raise SlotError('corrupt', '', f'{FILE_NAME} is not valid JSON; fix or remove it') from None
        if not isinstance(doc, dict) or doc.get('version') != VERSION:
            raise SlotError('corrupt', '/version', f'{FILE_NAME} version {VERSION} expected')
        slots = validate_slots(doc.get('slots', []), self.data_dir)
        state = doc.get('state', {})
        if not isinstance(state, dict):
            raise SlotError('corrupt', '/state', 'object required')
        ids = {s['id'] for s in slots}
        clean = {}
        for slot_id, entry in state.items():
            if slot_id in ids and isinstance(entry, dict) and parse_iso(entry.get('blocked_until', '')):
                clean[slot_id] = {'blocked_until': utc_iso(parse_iso(entry['blocked_until'])),
                                  'note': str(entry.get('note', ''))[:NOTE_MAX]}
        return {'version': VERSION, 'slots': slots, 'state': clean}

    def _write(self, doc: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'.{FILE_NAME}.{os.getpid()}.{os.urandom(4).hex()}.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                json.dump(doc, stream, indent=2, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            fsperm.restrict(tmp, directory=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------ slots
    def load(self) -> list:
        with self._lock:
            return self._read()['slots']

    def save(self, items) -> list:
        """Replace the slot list (validated). Block state of slots that remain is kept."""
        with self._lock:
            slots = validate_slots(items, self.data_dir)
            doc = self._read()
            ids = {s['id'] for s in slots}
            self._write({'version': VERSION, 'slots': slots,
                         'state': {k: v for k, v in doc['state'].items() if k in ids}})
            return slots

    def get(self, slot_id) -> dict | None:
        return next((s for s in self.load() if s['id'] == slot_id), None)

    # ------------------------------------------------------------ limit state
    def state(self) -> dict:
        with self._lock:
            return self._read()['state']

    def block(self, slot_id, until: datetime, note: str = '') -> str:
        with self._lock:
            doc = self._read()
            if not any(s['id'] == slot_id for s in doc['slots']):
                raise SlotError('not_found', '/id', 'unknown slot')
            doc['state'][slot_id] = {'blocked_until': utc_iso(until), 'note': str(note)[:NOTE_MAX]}
            self._write(doc)
            return doc['state'][slot_id]['blocked_until']

    def unblock(self, slot_id) -> None:
        with self._lock:
            doc = self._read()
            if doc['state'].pop(slot_id, None) is not None:
                self._write(doc)

    def blocked_until(self, slot_id) -> datetime | None:
        """The block end if the slot is still blocked now, else None."""
        entry = self.state().get(slot_id)
        if not entry:
            return None
        until = parse_iso(entry['blocked_until'])
        return until if until and until > self.clock() else None

    def available(self, kind: str | None = None) -> list:
        """Enabled, unblocked slots in selection order."""
        return [s for s in self.load() if s['enabled'] and (kind is None or s['kind'] == kind)
                and self.blocked_until(s['id']) is None]
