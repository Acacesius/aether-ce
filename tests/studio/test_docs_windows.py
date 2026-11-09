"""Docs checks for the Windows guide, README quick start and cross references."""
import argparse
import re
import unittest
from pathlib import Path

from aether.studio import __main__ as entry

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / 'README.md', ROOT / 'KNOWN_LIMITATIONS.md', ROOT / 'docs' / 'WINDOWS.md']
PRIVATE = [re.compile(r'/home/[a-z]'), re.compile(r'/Users/[A-Za-z]+/'), re.compile(r'\b(?:10\.\d+|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\b')]


def read(path):
    return path.read_text(encoding='utf-8')


class WindowsDocs(unittest.TestCase):
    def test_windows_md_content(self):
        text = read(ROOT / 'docs' / 'WINDOWS.md')
        low = text.lower()
        for needle in ('start.bat', 'doctor', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'owner token', 'uninstall', 'update', 'untested'):
            self.assertIn(needle.lower(), low, needle)

    def test_commands_and_flags_exist(self):
        top = entry.parser()
        sub = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction))
        known = {'doctor': {'--port', '--data-dir', '-h', '--help'}}
        known['serve'] = {o for a in sub.choices['serve']._actions for o in a.option_strings}
        for doc in DOCS + [ROOT / 'docs' / 'QUICKSTART.md']:
            for line in read(doc).splitlines():
                for m in re.finditer(r'(?:python3?|py -3) -m aether\.studio\b([^`\n]*)', line):
                    words = m.group(1).split()
                    cmd = words[0] if words and not words[0].startswith('-') else 'serve'
                    self.assertIn(cmd, sub.choices, f'{doc.name}: {line}')
                    for flag in re.findall(r'--[a-z][a-z-]*', m.group(1)):
                        self.assertIn(flag, known[cmd], f'{doc.name}: {flag}')
        # flag tokens in prose of WINDOWS.md that belong to start.bat / serve
        for flag in set(re.findall(r'`(--[a-z][a-z-]*)', read(ROOT / 'docs' / 'WINDOWS.md'))) - {'--version'}:   # --version is the CLIs' own
            self.assertTrue(flag in known['serve'] or flag in known['doctor'], flag)

    def test_readme_windows_before_docker(self):
        text = read(ROOT / 'README.md')
        self.assertIn('start.bat', text)
        self.assertLess(text.index('start.bat'), text.index('Docker, secondary'))
        self.assertNotRegex(text, r'runs? only in docker|in one Docker container, published')

    def test_limitations_content(self):
        low = read(ROOT / 'KNOWN_LIMITATIONS.md').lower()
        for needle in ('untested by the author', 'icacls', 'loopback', 'docs/feedback.md', 'linux only'):
            self.assertIn(needle, low)
        self.assertTrue((ROOT / 'feedback' / 'TESTER_FEEDBACK.md').is_file())

    def test_no_private_paths(self):
        for doc in DOCS:
            for rx in PRIVATE:
                self.assertIsNone(rx.search(read(doc)), f'{doc.name}: {rx.pattern}')

    def test_links_resolve(self):
        for doc in DOCS:
            for target in re.findall(r'\]\(([^)#\s]+)(?:#[^)]*)?\)', read(doc)):
                if '://' in target:
                    continue
                self.assertTrue((doc.parent / target).exists(), f'{doc.name}: {target}')


if __name__ == '__main__':
    unittest.main()
