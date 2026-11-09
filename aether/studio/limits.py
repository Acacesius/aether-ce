"""Usage-limit detection for Claude / Codex CLIs (CLI_EXECUTOR.md).

Only text the CLI itself produced counts (stderr, non-JSON lines, error events, error results); assistant text and
tool output are ignored so a model *discussing* limits cannot block a slot. ``detect`` returns the reset time when
one can be parsed, otherwise now + 5 hours.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from aether.studio import cli_events

DEFAULT_BLOCK = timedelta(hours=5)
MAX_BLOCK = timedelta(days=31)
SNIPPET_MAX = 200

LIMIT_RE = re.compile(r'(?i)(?:usage[ _]limit[ _](?:reached|exceeded|hit)|usage_limit_reached|'
                      r'hit your (?:usage |session |weekly |daily )?limit|'
                      r'\b\d+[- ]hour limit (?:reached|hit)|\b(?:weekly|daily|session|opus) limit (?:reached|hit))')
EPOCH_RE = re.compile(r'\|\s*(\d{10})(?!\d)')
ISO_RE = re.compile(r'\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)')
UNIT = r'(?:days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)'
REL_RE = re.compile(rf'(?i)\b(?:in|after)\s+((?:\d+\s*{UNIT}\b(?:\s*,?\s*(?:and\s+)?)?)+)')
REL_PART = re.compile(rf'(?i)(\d+)\s*({UNIT})\b')
CLOCK = r'(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?\s*m?\.?(?![a-z])'
ZONE = r'(?:\s*\(?\s*([A-Za-z]+(?:/[A-Za-z_+-]+)+|UTC|GMT)\s*\)?)?'
MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
DATE_CLOCK_RE = re.compile(rf'(?i)\b(?:resets?|reset at|try again (?:on|at))\s+(?:on\s+)?({"|".join(MONTHS)})[a-z]*\.?\s+'
                           rf'(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?:at\s+)?{CLOCK}{ZONE}')
CLOCK_RE = re.compile(rf'(?i)\b(?:resets?|reset at|try again at|available (?:again )?at|again at)\s+(?:at\s+)?{CLOCK}{ZONE}')


def _zone(name, now):
    if name:
        if name.upper() in ('UTC', 'GMT'):
            return timezone.utc
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:   # unknown zone or no tz database (common on Windows without tzdata)
            pass
    return now.astimezone().tzinfo


def _clock(hour, minute, ampm):
    hour = int(hour)
    if ampm is None and minute is None:
        return None   # a bare number ("resets 3") is too ambiguous; fall back to the default block
    minute = int(minute) if minute else 0
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.lower() == 'p' else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_reset(text: str, now: datetime) -> datetime | None:
    """Reset time named in ``text`` (aware UTC), or None. ``now`` must be timezone-aware."""
    match = EPOCH_RE.search(text)
    if match:
        return datetime.fromtimestamp(int(match.group(1)), timezone.utc)
    match = ISO_RE.search(text)
    if match:
        try:
            value = datetime.fromisoformat(match.group(1).replace('Z', '+00:00').replace(' ', 'T'))
            return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except ValueError:
            pass
    match = REL_RE.search(text)
    if match:
        seconds = 0
        for amount, unit in REL_PART.findall(match.group(1)):
            unit = unit.lower()
            scale = 86400 if unit.startswith('d') else 3600 if unit.startswith('h') else 1 if unit.startswith('s') else 60
            seconds += int(amount) * scale
        if seconds:
            return now + timedelta(seconds=seconds)
    match = DATE_CLOCK_RE.search(text)
    if match:
        month, day, hour, minute, ampm, zone = match.groups()
        clock = _clock(hour, minute, ampm)
        if clock:
            tz = _zone(zone, now)
            local = now.astimezone(tz)
            try:
                value = local.replace(month=MONTHS.index(month[:3].lower()) + 1, day=int(day), hour=clock[0],
                                      minute=clock[1], second=0, microsecond=0)
            except ValueError:
                value = None
            if value is not None:
                if value < local - timedelta(days=1):
                    value = value.replace(year=value.year + 1)
                return value.astimezone(timezone.utc)
    match = CLOCK_RE.search(text)
    if match:
        hour, minute, ampm, zone = match.groups()
        clock = _clock(hour, minute, ampm)
        if clock:
            tz = _zone(zone, now)
            local = now.astimezone(tz)
            value = local.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            if value <= local:
                value += timedelta(days=1)
            return value.astimezone(timezone.utc)
    return None


def detect(text: str, now: datetime | None = None) -> dict | None:
    """``{'message', 'reset_utc' (datetime), 'parsed' (bool)}`` when ``text`` reports a usage limit, else None."""
    now = now or datetime.now(timezone.utc)
    match = LIMIT_RE.search(text or '')
    if not match:
        return None
    # The reset hint is usually on the same line as the limit message; look there first, then in the whole text.
    start = text.rfind('\n', 0, match.start()) + 1
    end = text.find('\n', match.end())
    line = text[start:end if end >= 0 else len(text)]
    reset = parse_reset(line, now) or parse_reset(text, now)
    parsed = reset is not None and now < reset <= now + MAX_BLOCK
    return {'message': line.strip()[:SNIPPET_MAX], 'reset_utc': reset if parsed else now + DEFAULT_BLOCK,
            'parsed': parsed}


def diagnostic_text(lines, kind: str) -> str:
    """The parts of a log section that came from the CLI itself, not from the model or its tools."""
    out = []
    for line in lines:
        if line.startswith(cli_events.STDERR):
            out.append(line[len(cli_events.STDERR):])
            continue
        if line.startswith(cli_events.HEADER):
            continue
        for event in cli_events.parse_line(line, kind):
            if event['kind'] in ('raw', 'error'):
                out.append(event['text'])
            elif event['kind'] == 'final_result' and event.get('is_error'):
                out.append(event['text'])
    return '\n'.join(out)


def detect_in_log(log: cli_events.RawLog, kind: str, now: datetime | None = None) -> dict | None:
    return detect(diagnostic_text(log.tail(), kind), now)
