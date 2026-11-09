#!/usr/bin/env python3
"""Check Studio UI colour contrast (WCAG 2.x relative luminance) from the token blocks in studio.css.

Parses the blocks between "/* tokens:dark */" / "/* tokens:light */" and "/* end tokens */", plus the
prefers-color-scheme:light copy (it must equal tokens:light). Checks every text/background pair in PAIRS at >= 4.5:1
and every non-text pair in UI_PAIRS at >= 3:1, in both themes.

Usage: ui_contrast.py [path/to/studio.css] [--json]
Exit 0 = all pairs pass, 1 = a pair fails or a token is missing/unparseable.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

DEFAULT_CSS = Path(__file__).resolve().parent.parent / 'aether' / 'studio' / 'ui' / 'studio.css'
STATUSES = ('queued', 'running', 'blocked', 'done', 'failed', 'cancelled')
TEXT_MIN = 4.5
UI_MIN = 3.0

# (foreground token, background token): text that must reach 4.5:1.
PAIRS = [(f'--st-{s}-fg', f'--st-{s}-bg') for s in STATUSES] + [
    ('--text', '--bg'), ('--text', '--surface'), ('--text', '--surface-2'),
    ('--muted', '--bg'), ('--muted', '--surface'), ('--muted', '--surface-2'),
    ('--accent', '--bg'), ('--accent', '--surface'), ('--accent', '--accent-soft'), ('--on-accent', '--accent'),
    ('--danger', '--bg'), ('--danger', '--surface'), ('--surface', '--danger'),
    ('--success', '--bg'), ('--success', '--surface'),
    ('--warning', '--bg'), ('--warning', '--surface'),
]
# Non-text UI (focus ring, input borders): WCAG 1.4.11 asks 3:1 against the adjacent colour.
UI_PAIRS = [('--focus', '--bg'), ('--focus', '--surface'), ('--border-strong', '--bg'), ('--border-strong', '--surface')]

BLOCK = re.compile(r'/\*\s*tokens:(dark|light)\s*\*/\s*:root[^{]*\{(.*?)\}\s*/\*\s*end tokens\s*\*/', re.S)
MEDIA = re.compile(r'@media\s*\(prefers-color-scheme:light\)\s*\{\s*:root:not\(\[data-theme\]\)\s*\{(.*?)\}\s*\}', re.S)
DECL = re.compile(r'(--[a-z0-9-]+)\s*:\s*([^;]+);')
HEX = re.compile(r'^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$')


def parse_tokens(body: str) -> dict:
    return {name: value.strip() for name, value in DECL.findall(body)}


def parse(css: str) -> dict:
    themes = {name: parse_tokens(body) for name, body in BLOCK.findall(css)}
    if set(themes) != {'dark', 'light'}:
        raise ValueError('studio.css must contain exactly the tokens:dark and tokens:light blocks')
    media = MEDIA.search(css)
    if not media:
        raise ValueError('studio.css is missing the prefers-color-scheme:light token copy')
    if parse_tokens(media.group(1)) != themes['light']:
        raise ValueError('prefers-color-scheme:light tokens differ from tokens:light')
    return themes


def hex_rgb(value: str) -> tuple:
    match = HEX.match(value)
    if not match:
        raise ValueError(f'not a hex colour: {value!r}')
    digits = match.group(1)
    if len(digits) == 3:
        digits = ''.join(c * 2 for c in digits)
    return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))


def luminance(value: str) -> float:
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in hex_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg: str, bg: str) -> float:
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def check(css: str) -> list:
    """Return one result dict per (theme, pair). Raises ValueError for structural problems."""
    results = []
    for theme, tokens in sorted(parse(css).items()):
        for pairs, minimum in ((PAIRS, TEXT_MIN), (UI_PAIRS, UI_MIN)):
            for fg, bg in pairs:
                if fg not in tokens or bg not in tokens:
                    raise ValueError(f'{theme}: missing token {fg if fg not in tokens else bg}')
                value = ratio(tokens[fg], tokens[bg])
                results.append({'theme': theme, 'fg': fg, 'bg': bg, 'fg_hex': tokens[fg], 'bg_hex': tokens[bg],
                                'ratio': round(value, 2), 'min': minimum, 'ok': value >= minimum})
    return results


def main(argv: list) -> int:
    args = [a for a in argv if not a.startswith('--')]
    path = Path(args[0]) if args else DEFAULT_CSS
    try:
        results = check(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        print(f'ui_contrast: {error}', file=sys.stderr)
        return 1
    failed = [r for r in results if not r['ok']]
    if '--json' in argv:
        print(json.dumps({'ok': not failed, 'results': results}, indent=2))
    else:
        for r in results:
            print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['theme']:<5} {r['fg']:<18} on {r['bg']:<16} "
                  f"{r['fg_hex']} / {r['bg_hex']}  {r['ratio']:>5.2f}:1 (min {r['min']})")
        print(f'{len(results) - len(failed)}/{len(results)} pairs pass')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
