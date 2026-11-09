"""tools/ui_shots.py: import and helper functions only (the browser parts are exercised by running the tool)."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

TOOL = Path(__file__).resolve().parents[2] / 'tools' / 'ui_shots.py'
spec = importlib.util.spec_from_file_location('ui_shots', TOOL)
ui_shots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui_shots)   # must not import playwright or start anything


class HelperTests(unittest.TestCase):
    def test_imports_without_playwright_side_effects(self):
        self.assertEqual(ui_shots.WIDTHS, (1280, 390))
        self.assertEqual(ui_shots.THEMES, ('light', 'dark'))

    def test_free_port_is_unprivileged_loopback(self):
        port = ui_shots.free_port()
        self.assertTrue(1024 <= port <= 65535)

    def test_shot_name(self):
        self.assertEqual(ui_shots.shot_name('slots', 390, 'dark'), 'slots-390-dark.png')

    def test_parse_color(self):
        self.assertEqual(ui_shots.parse_color('rgb(1, 2, 3)'), (1.0, 2.0, 3.0, 1.0))
        self.assertEqual(ui_shots.parse_color('rgba(0, 0, 0, 0.5)'), (0.0, 0.0, 0.0, 0.5))
        self.assertEqual(ui_shots.parse_color('rgba(0, 0, 0, 0)')[3], 0.0)
        self.assertEqual(ui_shots.parse_color('transparent')[3], 0.0)
        self.assertIsNone(ui_shots.parse_color('nonsense'))

    def test_contrast_ratio_known_values(self):
        black, white = (0, 0, 0, 1.0), (255, 255, 255, 1.0)
        self.assertAlmostEqual(ui_shots.contrast_ratio(black, white), 21.0, places=2)
        self.assertAlmostEqual(ui_shots.contrast_ratio(white, white), 1.0, places=3)
        self.assertAlmostEqual(ui_shots.contrast_ratio((119, 119, 119, 1), white), 4.48, places=1)

    def test_blend_and_effective_background(self):
        half = ui_shots.blend((0, 0, 0, 0.5), (255, 255, 255, 1.0))
        self.assertAlmostEqual(half[0], 127.5)
        bg = ui_shots.effective_bg(['rgba(0, 0, 0, 0)', 'rgb(10, 20, 30)'], 'rgb(255, 255, 255)')
        self.assertEqual(bg[:3], (10.0, 20.0, 30.0))

    def test_results_markdown_reports_failures_honestly(self):
        results = ui_shots.Results()
        results.add('p', 'good', True, 'ok')
        results.add('p', 'bad', False, 'a | b')
        text = results.markdown('now', [])
        self.assertIn('| p | good | PASS | ok |', text)
        self.assertIn('| p | bad | FAIL | a / b |', text)
        self.assertEqual(len(results.failed), 1)

    def test_wrappers_run_the_fakes(self):
        with tempfile.TemporaryDirectory() as tmp:
            made = ui_shots.write_wrappers(Path(tmp) / 'bin')
            self.assertEqual(set(made), {'claude', 'codex'})
            for path in made.values():
                self.assertTrue(path.exists())
                text = path.read_text(encoding='utf-8')
                self.assertIn('fake_', text)
            if os.name != 'nt':
                self.assertTrue(os.access(made['claude'], os.X_OK))


if __name__ == '__main__':
    unittest.main()
