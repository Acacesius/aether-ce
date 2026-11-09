"""Relative links in the Markdown docs must resolve to files in this repository."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'\[[^\]]*\]\(([^)\s]+)\)')


class DocLinkTests(unittest.TestCase):
    def test_relative_links_resolve(self):
        docs = sorted(p for p in ROOT.rglob('*.md') if '.git' not in p.parts)
        self.assertGreaterEqual(len(docs), 8)
        checked = 0
        for doc in docs:
            for target in LINK.findall(doc.read_text()):
                if target.startswith(('http://', 'https://', 'mailto:', '#')):
                    continue
                with self.subTest(doc=str(doc.relative_to(ROOT)), target=target):
                    self.assertTrue((doc.parent / target.split('#')[0]).exists())
                checked += 1
        self.assertGreater(checked, 5)


if __name__ == '__main__':
    unittest.main()
