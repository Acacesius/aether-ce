#!/usr/bin/env python3
"""CE export checker (stdlib only). Exit 0 = ready, 1 = findings, 2 = usage/tool error.

Checks the TRACKED files (git ls-files; falls back to walking the directory when there is no git repo):
  (a) no private literals: RFC1918/CGNAT addresses, overlay-VPN DNS names, absolute home paths (/home/<name>,
      C:\\Users\\<name>; the placeholders <user>/<you>/%USERNAME% are fine), e-mail addresses, token/key-shaped
      strings and the private-project denylist. Line rules are reused from tools/ce_scan.py.
  (b) LICENSE is the standard MIT text with the "Copyright (c) 2026 Brandon" line.
  (c) required files exist.
Findings print as `path:line: rule` - the matching text is NEVER printed.

Allowlist (for test fixtures that deliberately contain fake secrets): export_check_allow.json in the checked
directory (or --allow FILE): [{"rule": ..., "path": ..., "sha256": <sha256 of the offending line>, "why": <free text>}]. Each entry is
exact (one rule, one file, one line content), so it cannot hide anything else. `--list-hashes` is not offered on
purpose: compute the hash of the reviewed line yourself. Private-project names come from the file named by
$AETHER_CE_DENYLIST (outside the repo), exactly as for ce_scan.py.

Usage: export_check.py [DIR] [--allow FILE] [--plan]
  --plan   print the fresh-history export plan (steps only; nothing is executed, pushed or deleted) and exit 0.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REQUIRED = ['README.md', 'LICENSE', 'KNOWN_LIMITATIONS.md', 'start.bat', 'start.ps1', 'start.sh', 'docs/WINDOWS.md']
MIT_HEADER = 'MIT License'
MIT_COPYRIGHT = 'Copyright (c) 2026 Brandon'
MIT_MARKERS = ('Permission is hereby granted, free of charge', 'THE SOFTWARE IS PROVIDED "AS IS"',
               'copies or substantial portions of the Software.')
SKIP_WALK = {'.git', '__pycache__'}
BINARY_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.ico', '.zip', '.woff', '.woff2', '.pyc')

EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})')
EMAIL_OK_DOMAIN = re.compile(r'(?i)(?:^|\.)(?:example\.(?:com|org|net)|invalid|localhost|test)$|^users\.noreply\.github\.com$')
WIN_HOME = re.compile(r'(?i)\b[A-Z]:[\\/]+Users[\\/]+([^\\/\s"\'<>%*]+)')
WIN_HOME_OK = {'<user>', '<you>', 'you', 'user', 'x', 'name', 'username', 'public', 'default', 'all users'}
POSIX_HOME_PLACEHOLDER = re.compile(r'/home/(?:<user>|<you>)')


def _load_ce_scan():
    spec = importlib.util.spec_from_file_location('_ce_scan_for_export', HERE / 'ce_scan.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()


def tracked_files(root: Path):
    """Relative posix paths of tracked files; walks the directory if root is not a git work tree."""
    try:
        out = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, check=True).stdout
        if out or (root / '.git').exists():
            return sorted(p for p in out.decode('utf-8', 'replace').split('\0') if p)
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_WALK)
        for fn in sorted(filenames):
            found.append((Path(dirpath) / fn).relative_to(root).as_posix())
    return sorted(found)


def extra_line_rules(line: str):
    """Rules not in ce_scan: e-mail addresses and Windows home paths."""
    hits = set()
    for m in EMAIL.finditer(line):
        if not EMAIL_OK_DOMAIN.search(m.group(1)):
            hits.add('email')
    for m in WIN_HOME.finditer(line):
        if m.group(1).lower() not in WIN_HOME_OK:
            hits.add('home_path_windows')
    return hits


def load_allow(path: Path):
    if not path.exists():
        return set()
    return {(e['rule'], e['path'], e['sha256']) for e in json.loads(path.read_text(encoding='utf-8'))}


def check_license(root: Path):
    path = root / 'LICENSE'
    if not path.is_file():
        return []   # reported by the required-files check
    text = path.read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()
    out = []
    if not lines or lines[0].strip() != MIT_HEADER:
        out.append(('license_not_mit', 'LICENSE', 1))
    if not any(l.strip() == MIT_COPYRIGHT for l in lines[:5]):
        out.append(('license_copyright', 'LICENSE', 3))
    if not all(marker in text for marker in MIT_MARKERS):
        out.append(('license_body', 'LICENSE', 0))
    return out


def check(root: Path, allow=frozenset()):
    """Return (findings, files_checked). A finding is (rule, path, line)."""
    ce = _load_ce_scan()
    denylist, _status = ce.load_denylist(root=root)
    files = tracked_files(root)
    findings = []
    present = set(files)
    for need in REQUIRED:
        if need not in present or not (root / need).is_file():
            findings.append(('missing_required_file', need, 0))
    findings += check_license(root)
    checked = 0
    for rel in files:
        full = root / rel
        for rule in ce.scan_path_name(rel):
            findings.append((rule, rel, 0))
        if full.is_symlink() or not full.is_file() or rel.lower().endswith(BINARY_EXT):
            continue
        data = full.read_bytes()
        if ce._is_binary(data):
            continue
        checked += 1
        for no, text in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
            rules = {r for r, level in ce.scan_line(text, denylist) if level == 'fail'} | extra_line_rules(text)
            if POSIX_HOME_PLACEHOLDER.search(text) and 'home_path' in rules and not _real_posix_home(text):
                rules.discard('home_path')
            for rule in sorted(rules):
                if (rule, rel, sha(text)) not in allow:
                    findings.append((rule, rel, no))
    return findings, checked


def _real_posix_home(text: str) -> bool:
    return any(m.group(1) not in ('<user>', '<you>') for m in re.finditer(r'/home/([^/\s"\']+)', text)
               if not m.group(1).startswith('<'))


PLAN = """\
AETHER Community Edition - fresh-history export plan (steps only; this tool executes none of them).
Run from the canonical repository root, with the owner denylist outside the repo:
  export AETHER_CE_DENYLIST=<path-to-denylist-outside-repo>

  1. python3 tools/export_check.py .                      # must exit 0 on the source tree first
  2. EXPORT=$(mktemp -d)                                  # an EMPTY directory outside the repo
  3. git ls-files -z | xargs -0 -I{{}} cp --parents {{}} "$EXPORT"
       (or: git archive HEAD | tar -x -C "$EXPORT" - tracked files only, no .git, no __pycache__)
  4. cd "$EXPORT" && git init
  5. git add -A && git commit -m "{message}"               # the single commit; no remote
  6. python3 <repo>/tools/export_check.py "$EXPORT"       # must exit 0
  7. python3 <repo>/tools/ce_scan.py "$EXPORT"            # must exit 0 (history: 1 commit, no remote)
  8. git -C "$EXPORT" archive --format=zip -o <dest>/aether-ce-0.1.zip HEAD
       (zip with start.bat at the ROOT, no wrapping directory; check: unzip -l <zip> | grep ' start.bat$')
  9. Hand over the zip. Publishing/pushing is a separate owner decision; nothing here pushes or deletes.
"""


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    allow_path = None
    plan = '--plan' in args
    if plan:
        args.remove('--plan')
    if '--allow' in args:
        i = args.index('--allow')
        try:
            allow_path = Path(args[i + 1])
        except IndexError:
            print(__doc__, file=sys.stderr)
            return 2
        del args[i:i + 2]
    if len(args) > 1 or any(a.startswith('-') for a in args):
        print(__doc__, file=sys.stderr)
        return 2
    if plan:
        print(PLAN.format(message='AETHER Community Edition 0.1'), end='')
        return 0
    root = Path(args[0] if args else '.').resolve()
    try:
        if not root.is_dir():
            raise ValueError('not a directory')
        allow = load_allow(allow_path or root / 'export_check_allow.json')
        findings, checked = check(root, allow)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f'export_check error: {type(error).__name__}', file=sys.stderr)
        return 2
    for rule, path, line in findings:
        print(f'{path}:{line}: {rule}')
    print(f'export_check: {len(findings)} finding(s), {checked} text file(s) checked', file=sys.stderr)
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
