#!/usr/bin/env python3
"""CE release scanner. Walks EVERY file under a directory (including .git), plus every blob and commit message
reachable in git history. Exit 0 = clean, 1 = findings, 2 = scanner error.

Output is JSON on stdout: findings are {rule, path, line, sha256} where sha256 is the hash of the matching line.
The matched text is NEVER printed. The rule patterns below are written so that this file does not match its own rules.

Usage: ce_scan.py [DIR] [--allow FILE] [--no-history]
Optional private-literal denylist: file named by $AETHER_CE_DENYLIST (one literal per line, case-insensitive, word
boundary). The file must live outside DIR and is never read into any output. If unset, the denylist pass is reported
as "pending_owner_file" (not an error).
Allowlist: DIR/ce_scan_allow.json (or --allow): [{"rule":..., "path":..., "sha256":...}] - owner-reviewed.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

# (rule, compiled regex, level)  level: 'fail' or 'warn'.  Bracketed single characters keep this file self-clean.
RULES = [
    ('pem_private_key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), 'fail'),
    ('openssh_key', re.compile(r'-----BEGIN OPENSSH PRIVATE KE[Y]-----|ssh-(?:ed25519|rsa) AAAA'), 'fail'),
    ('vendor_api_key', re.compile(r'\b(?:sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|'
                                  r'github_pat_[A-Za-z0-9_]{20,}|xox[bap]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|tskey-[A-Za-z0-9-]{10,})'), 'fail'),
    ('bearer_literal', re.compile(r'Bearer [A-Za-z0-9._~+/-]{20,}'), 'fail'),
    ('token_shape_43', re.compile(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])'), 'fail'),
    ('hex_secret', re.compile(r'(?i)(?:secret|token|key|hmac)\s*[:=]\s*[\'"]?[0-9a-f]{32,}'), 'fail'),
    ('ip_rfc1918', re.compile(r'\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b'), 'fail'),
    ('ip_cgnat', re.compile(r'\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b'), 'fail'),
    ('mac', re.compile(r'\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b'), 'fail'),
    ('overlay_dns', re.compile(r'(?i)\.ts\.ne[t]\b|\btail[0-9a-f]{6}\b'), 'fail'),
    ('overlay_vpn', re.compile(r'(?i)tailscal[e]|tailne[t]|magicdn[s]'), 'fail'),
    ('ssh_material', re.compile(r'known_host[s]|\.ss[h]/|HostKeyAlia[s]|id_(?:ed25519|rsa)|BatchMod[e]='), 'fail'),
    ('home_path', re.compile(r'/home/[a-z_][a-z0-9_-]*|~/(?:arie[s]|project[s])|/Users/[A-Za-z]'), 'fail'),
    ('private_product', re.compile(r'(?i)\b(?:netdas[h]|blacksit[e]|krono[s]|arie[s]|edgebo[t]|genesi[s])\b'), 'fail'),
    ('private_ports', re.compile(r'\b(?:1886[6]|1876[5]|1886[8]|878[7]|786[0]|787[0]|1143[4]|860[0]|1886[5])\b'), 'warn'),
    ('host_socket', re.compile(r'/var/run/docker\.sock|/run/user/\d+|executor\.sock|privileged:\s*true|network_mode:\s*host|cap_ad[d]'), 'fail'),
    ('bind_any', re.compile(r'0\.0\.0\.0:|"0\.0\.0\.0"|^\s*-?\s*"?\d+:\d+"?\s*$'), 'fail'),
    ('dangerous_cli', re.compile(r'danger-full-acces[s]|--dangerously-skip-permission[s]|--ask-for-approva[l] never|systemd-ru[n]|tmux send-key[s]'), 'fail'),
]
IPV4 = re.compile(r'(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])')
DEF_LINE = re.compile(r'^\s*(?:async\s+)?def\s+\w+\(')
SAFE_NETS = [ipaddress.ip_network(n) for n in ('127.0.0.0/8', '0.0.0.0/32', '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24')]
CGNAT = ipaddress.ip_network('100.64' + '.0.0/10')
ENV_NAME = re.compile(r'(^|/)\.env(\..*)?$')
SECRET_NAME_SUFFIX = ('.key', '.pem', '.p12', '.sqlite3', '.sqlite', '.db')
MIN_STRING = 6
SKIP_DIRS_FOR_FILES = {'.git'}   # git internals are scanned through git itself (history pass), not as raw compressed objects


def sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()


def _ipv4_findings(line: str):
    for m in IPV4.finditer(line):
        parts = m.group(1).split('.')
        if any(int(p) > 255 for p in parts):
            continue
        try:
            addr = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if addr in CGNAT or addr.is_private and not any(addr in net for net in SAFE_NETS):
            continue   # reported by ip_rfc1918 / ip_cgnat instead
        if not any(addr in net for net in SAFE_NETS):
            yield 'ip_public'


def scan_line(line: str, denylist=()):
    """Yield (rule, level) for one line. Pure; used by the tests."""
    hit = set()
    is_def = bool(DEF_LINE.match(line))
    for rule, rx, level in RULES:
        if rule == 'token_shape_43' and is_def:
            continue
        if rule == 'mac' and rx.search(line):
            if any(m.group(0).replace('-', ':') != '00:00:00:00:00:00' for m in rx.finditer(line)):
                hit.add((rule, level))
            continue
        if rx.search(line):
            hit.add((rule, level))
    for rule in _ipv4_findings(line):
        hit.add((rule, 'fail'))
    if denylist:
        for rx in denylist:
            if rx.search(line):
                hit.add(('denylist_literal', 'fail'))
                break
    return sorted(hit)


def scan_path_name(rel: str):
    base = rel.rsplit('/', 1)[-1]
    parts = rel.split('/')
    if (ENV_NAME.search(rel) and base != '.env.example') or base == 'owner.token' or base.endswith(SECRET_NAME_SUFFIX):
        yield 'env_file'
    if '.local' in parts or 'audit' in parts or 'design-reference' in parts:
        yield 'env_file'
    if base.endswith('.jsonl') and 'examples' not in parts:
        yield 'env_file'


def _is_binary(data: bytes) -> bool:
    return b'\0' in data[:8192]


def _strings(data: bytes):
    for m in re.finditer(rb'[\x20-\x7e]{%d,}' % MIN_STRING, data):
        yield m.group(0).decode('ascii')


def _lines(data: bytes):
    """Return list of (line_no, text). Binary data yields extracted strings numbered in order."""
    if _is_binary(data):
        return list(enumerate(_strings(data), 1))
    return list(enumerate(data.decode('utf-8', 'replace').splitlines(), 1))


def load_denylist(env=os.environ, root: Path | None = None):
    """Return (compiled patterns, status). status in {'applied','pending_owner_file'}; raises OSError/ValueError on bad file."""
    name = env.get('AETHER_CE_DENYLIST', '')
    if not name:
        return [], 'pending_owner_file'
    path = Path(name).resolve()
    if root is not None:
        try:
            path.relative_to(root.resolve())
        except ValueError:
            pass
        else:
            raise ValueError('denylist must live outside the scanned directory')
    out = []
    for raw in path.read_text(encoding='utf-8').splitlines():
        item = raw.strip()
        if item and not item.startswith('#'):
            out.append(re.compile(r'(?<![A-Za-z0-9])' + re.escape(item) + r'(?![A-Za-z0-9])', re.I))
    return out, 'applied'


def load_allow(path: Path):
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding='utf-8'))
    return {(e['rule'], e['path'], e['sha256']) for e in data}


class Scanner:
    def __init__(self, root: Path, denylist=(), allow=frozenset(), history=True):
        self.root, self.denylist, self.allow, self.history = Path(root), denylist, allow, history
        self.findings, self.warnings, self.allowed = [], [], 0
        self.files = 0
        self.blobs = 0

    def _add(self, rule, level, path, line, text):
        digest = sha(text)
        if (rule, path, digest) in self.allow:
            self.allowed += 1
            return
        (self.findings if level == 'fail' else self.warnings).append({'rule': rule, 'path': path, 'line': line, 'sha256': digest})

    def scan_bytes(self, label: str, data: bytes):
        for no, text in _lines(data):
            for rule, level in scan_line(text, self.denylist):
                self._add(rule, level, label, no, text)

    def scan_tree(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not (d in SKIP_DIRS_FOR_FILES and Path(dirpath) == self.root))
            for fn in sorted(filenames):
                full = Path(dirpath) / fn
                rel = full.relative_to(self.root).as_posix()
                for rule in scan_path_name(rel):
                    self._add(rule, 'fail', rel, 0, rel)
                if full.is_symlink():
                    self._add('symlink', 'fail', rel, 0, rel)
                    continue
                self.files += 1
                self.scan_bytes(rel, full.read_bytes())
        git = self.root / '.git'
        if git.exists():
            for p in sorted(git.rglob('*')):
                rel = p.relative_to(self.root).as_posix()
                if p.is_file() and not rel.startswith('.git/objects/') and not rel.startswith('.git/refs/') and not p.is_symlink():
                    self.files += 1
                    self.scan_bytes(rel, p.read_bytes())

    def _git(self, *args, check=True):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, check=check).stdout

    def scan_history(self):
        if not (self.root / '.git').exists():
            return
        commits = self._git('rev-list', '--all').decode().split()
        head_count = int(self._git('rev-list', '--count', 'HEAD').decode().strip() or 0)
        if head_count != 1 or len(commits) != 1:
            self._add('git_history', 'fail', '.git', 0, f'commits={len(commits)} head={head_count}')
        if self._git('remote').strip():
            self._add('git_remote', 'fail', '.git/config', 0, 'remote configured')
        for line in self._git('for-each-ref', '--format=%(refname)').decode().splitlines():
            if line.startswith('refs/remotes/') or line.startswith('refs/stash'):
                self._add('git_history', 'fail', line, 0, line)
        # commit metadata + messages
        for c in commits:
            meta = self._git('show', '-s', '--format=%an <%ae>%n%cn <%ce>%n%B', c)
            self.scan_bytes(f'.git@{c[:10]}:commit-message', meta)
        # every blob reachable from any ref
        seen = set()
        listing = self._git('rev-list', '--all', '--objects').decode().splitlines()
        for entry in listing:
            oid, _, name = entry.partition(' ')
            if not name or oid in seen:
                continue
            kind = self._git('cat-file', '-t', oid).decode().strip()
            if kind != 'blob':
                continue
            seen.add(oid)
            self.blobs += 1
            for rule in scan_path_name(name):
                self._add(rule, 'fail', f'.git@blob:{name}', 0, name)
            self.scan_bytes(f'.git@blob:{name}', self._git('cat-file', 'blob', oid))
        # unreachable objects must not exist in a fresh export
        fsck = self._git('fsck', '--unreachable', '--no-reflogs', check=False).decode().splitlines()
        for entry in fsck:
            if entry.startswith('unreachable'):
                self._add('git_history', 'fail', '.git', 0, entry)

    def run(self):
        self.scan_tree()
        if self.history:
            self.scan_history()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    allow_path = None
    history = True
    if '--allow' in args:
        i = args.index('--allow'); allow_path = Path(args[i + 1]); del args[i:i + 2]
    if '--no-history' in args:
        args.remove('--no-history'); history = False
    if len(args) > 1 or any(a.startswith('-') for a in args):
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(args[0] if args else '.').resolve()
    try:
        if not root.is_dir():
            raise ValueError('not a directory')
        denylist, status = load_denylist(root=root)
        allow = load_allow(allow_path or root / 'ce_scan_allow.json')
        scanner = Scanner(root, denylist, allow, history)
        scanner.run()
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f'scanner error: {type(error).__name__}', file=sys.stderr)
        return 2
    counts = {}
    for f in scanner.findings:
        counts[f['rule']] = counts.get(f['rule'], 0) + 1
    report = {'findings': scanner.findings, 'warnings': scanner.warnings,
              'summary': {'files_scanned': scanner.files, 'history_blobs_scanned': scanner.blobs, 'findings': len(scanner.findings),
                          'warnings': len(scanner.warnings), 'allowlisted': scanner.allowed, 'by_rule': counts,
                          'denylist': status, 'history': history}}
    print(json.dumps(report, indent=1))
    return 1 if scanner.findings else 0


if __name__ == '__main__':
    sys.exit(main())
