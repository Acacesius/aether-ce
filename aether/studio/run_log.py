"""Read side of the per-run raw CLI log (CLI_EXECUTOR.md, "Tracking and live log").

Offsets are byte offsets into ``<data dir>/cli-logs/<run id>.log``. A chunk always starts and ends on a line boundary:
the log is append-only and every line ends with ``\\n``, so cutting at the last newline never splits a line or a UTF-8
character. A client that sends an offset inside a character is moved forward to the next character start; a negative or
non-numeric offset is 0 and an offset past the end is the end. Text is redacted before it leaves the process.
"""
from __future__ import annotations

from pathlib import Path

from aether.studio import cli_events, scanner

DEFAULT_LIMIT = 65536
MAX_LIMIT = 1_048_576
HARD_LINE_CAP = 4 * 1024 * 1024   # a single line longer than this is cut only at EOF-of-window (never expected)


def clamp_offset(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def clamp_limit(value) -> int:
    try:
        return max(1, min(MAX_LIMIT, int(value)))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_LIMIT


def read_chunk(path, after=0, limit=DEFAULT_LIMIT, *, final=False, kind='claude') -> dict:
    """Return ``{offset, next_offset, size, raw, events}`` for complete lines starting at ``after``.

    ``final`` (the run is over): an unterminated last line is delivered too, since nothing will complete it."""
    after, limit = clamp_offset(after), clamp_limit(limit)
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return {'offset': 0, 'next_offset': 0, 'size': 0, 'raw': '', 'events': []}
    after = min(after, size)
    with open(path, 'rb') as stream:
        if after:                                     # step off a UTF-8 continuation byte (bad client offset)
            stream.seek(after)
            while after < size:
                head = stream.read(1)
                if head and (head[0] & 0xC0) == 0x80:
                    after += 1
                else:
                    break
        stream.seek(after)
        data = stream.read(limit)
        while data and b'\n' not in data and len(data) < HARD_LINE_CAP:
            more = stream.read(limit)                 # one very long line: keep reading until it ends
            if not more:
                break
            data += more
    cut = data.rfind(b'\n') + 1
    if final and data and after + len(data) >= size:
        cut = len(data)                               # unterminated tail of a finished run
    data = data[:cut]
    return {'offset': after, 'next_offset': after + len(data), 'size': size, **_parse(data, after, kind)}


def _parse(data: bytes, base: int, kind: str) -> dict:
    events, raw_lines, position = [], [], base
    current = kind
    for raw_line in data.split(b'\n'):
        if not raw_line and position - base >= len(data):
            break
        line = raw_line.decode('utf-8', 'replace').rstrip('\r')
        clean, _ = scanner.redact(line)
        raw_lines.append(clean)
        if line.startswith(cli_events.HEADER):
            for part in line[len(cli_events.HEADER):].split():
                if part.startswith('kind='):
                    current = part[5:]
            events.append({'kind': 'header', 'text': clean[len(cli_events.HEADER):], 'offset': position})
        elif line.startswith(cli_events.STDERR):
            events.append({'kind': 'stderr', 'text': clean[len(cli_events.STDERR):], 'offset': position})
        else:
            for event in cli_events.parse_line(line, current):
                event = dict(event, offset=position)
                for key, value in event.items():
                    if isinstance(value, str):
                        event[key] = scanner.redact(value)[0]
                events.append(event)
        position += len(raw_line) + 1
    return {'raw': '\n'.join(raw_lines) + ('\n' if raw_lines else ''), 'events': events}
