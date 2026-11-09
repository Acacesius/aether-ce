#!/usr/bin/env python3
"""Studio UI verification: screenshots plus behaviour checks, against a scratch server with FAKE data only.

Run with a Python that has Playwright and a Chromium build installed, for example

    /srv/docker/ersatztv/tools/venv/bin/python tools/ui_shots.py

What it does
  * creates a scratch data folder under the system temp folder (removed at the end);
  * starts the Studio server as a plain loopback Python process with AETHER_EXEC=cli and a PATH that only holds
    wrapper scripts for tests/fakes/fake_claude.py and tests/fakes/fake_codex.py (no real CLI, no network);
  * seeds fake slots ("Claude A", "Codex B", "Claude C") and queues fake runs through the real HTTP API
    (done, failed, running, blocked on a usage limit, queued);
  * drives Chromium over Workspace, Slots, Run (live and finished), Tracker (+ drawer), Builder and Library at widths
    1280 and 390 in light and dark, writing docs/ui-shots/<view>-<width>-<theme>.png;
  * checks every page (console/page errors, no sideways scroll at 390, text contrast >= 4.5:1, 44px touch targets at
    390, Tab reaches the main controls, no private paths on screen) and writes docs/ui-shots/RESULTS.md.

Playwright is imported lazily. If it or its browser is missing the tool says so and exits 2; it installs nothing.
Exit 0 = every check passed, 1 = at least one check failed, 2 = could not run.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / 'tests' / 'fakes'
OUT = ROOT / 'docs' / 'ui-shots'
WIDTHS = (1280, 390)
THEMES = ('light', 'dark')
MIN_TEXT_CONTRAST = 4.5
MIN_TOUCH = 44
TOKEN_KEY = 'aether.studio.owner'
THEME_KEY = 'aether.studio.theme'

SERVER_CODE = r'''
import sys
from pathlib import Path
from aether.studio import app as studio_app
from aether.studio.mode import Mode
port, data = int(sys.argv[1]), Path(sys.argv[2])
application = studio_app.build(Mode('custom', 'none', 'docker', data), max_workers=1, public_port=port)
httpd = studio_app.StudioServer(('127.0.0.1', port), application)
application.runner.start()
application.driver.start()
print('ready', flush=True)
try:
    httpd.serve_forever(poll_interval=0.2)
finally:
    application.driver.stop()
    application.runner.stop()
'''


# ---------------------------------------------------------------- helpers (importable without a browser)
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def write_wrappers(bin_dir: Path, fakes: Path = FAKES, python: str | None = None) -> dict:
    """Create executable ``claude`` and ``codex`` wrappers in ``bin_dir`` that run the fake CLIs. Returns their paths."""
    python = python or sys.executable
    bin_dir.mkdir(parents=True, exist_ok=True)
    made = {}
    for name, script in (('claude', 'fake_claude.py'), ('codex', 'fake_codex.py')):
        target = Path(fakes) / script
        if os.name == 'nt':
            path = bin_dir / f'{name}.cmd'
            path.write_text(f'@echo off\r\n"{python}" "{target}" %*\r\n', encoding='utf-8')
        else:
            path = bin_dir / name
            path.write_text(f'#!/bin/sh\nexec "{python}" "{target}" "$@"\n', encoding='utf-8')
            path.chmod(0o755)
        made[name] = path
    return made


def parse_color(text: str):
    """'rgb(1, 2, 3)' / 'rgba(1, 2, 3, 0.5)' / 'color(srgb 0.1 0.2 0.3)' -> (r, g, b, a) with 0..255 floats, or None."""
    text = (text or '').strip()
    if text == 'transparent':
        return (0.0, 0.0, 0.0, 0.0)
    try:
        if text.startswith('rgb'):
            inner = text[text.index('(') + 1:text.rindex(')')].replace('/', ' ').replace(',', ' ').split()
            nums = [float(x.rstrip('%')) for x in inner]
            return (nums[0], nums[1], nums[2], nums[3] if len(nums) > 3 else 1.0)
        if text.startswith('color(srgb'):
            nums = [float(x) for x in text[text.index('srgb') + 4:text.rindex(')')].replace('/', ' ').split()]
            return (nums[0] * 255, nums[1] * 255, nums[2] * 255, nums[3] if len(nums) > 3 else 1.0)
    except (ValueError, IndexError):
        return None
    return None


def blend(top, bottom):
    """Composite colour ``top`` (with alpha) over an opaque ``bottom``."""
    alpha = top[3]
    return tuple(top[i] * alpha + bottom[i] * (1 - alpha) for i in range(3)) + (1.0,)


def luminance(rgb) -> float:
    def channel(value):
        value = value / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def shot_name(view: str, width: int, theme: str) -> str:
    return f'{view}-{width}-{theme}.png'


class Results:
    def __init__(self):
        self.rows = []   # (page, check, ok, detail)

    def add(self, page: str, check: str, ok: bool, detail: str = ''):
        self.rows.append((page, check, bool(ok), detail))

    @property
    def failed(self):
        return [row for row in self.rows if not row[2]]

    def markdown(self, stamp: str, shots: list) -> str:
        lines = ['# Studio UI verification results', '',
                 f'Generated by `tools/ui_shots.py` ({stamp}). Fake data only: fake CLIs, scratch data folder, loopback server.',
                 '', f'Checks: {len(self.rows)}, passed: {len(self.rows) - len(self.failed)}, failed: {len(self.failed)}.', '',
                 '| page | check | result | detail |', '|---|---|---|---|']
        for page, check, ok, detail in self.rows:
            lines.append(f'| {page} | {check} | {"PASS" if ok else "FAIL"} | {detail.replace("|", "/").replace(chr(10), " ")} |')
        lines += ['', f'Screenshots: {len(shots)} files in this folder, named `<view>-<width>-<theme>.png`.', '']
        return '\n'.join(lines)


def request(base: str, token: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'Origin': base})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b'null')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'null')


# ---------------------------------------------------------------- scratch server + seed data
class Scratch:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='aether-ui-shots-'))
        self.data = self.root / 'data'
        self.created = [self.root]
        tmp = Path(tempfile.gettempdir())
        self.workspace = self._generic(tmp / 'demo-project', self.root / 'demo-project')
        self.slot_root = self._generic(tmp / 'demo-slots', self.root / 'demo-slots')
        (self.workspace / 'README.md').write_text('# Demo project\n', encoding='utf-8')
        self.bin = self.root / 'bin'
        write_wrappers(self.bin)
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.proc = None
        self.token = ''
        self.ids = {}

    def _generic(self, wanted: Path, fallback: Path) -> Path:
        """Prefer a generic, believable folder name; fall back inside the scratch root if it already exists."""
        path = fallback if wanted.exists() else wanted
        path.mkdir(parents=True, exist_ok=True)
        if path == wanted:
            self.created.append(path)
        return path

    def start(self):
        system_path = os.environ.get('PATH', '') if os.name == 'nt' else '/usr/bin:/bin'
        env = {'PATH': str(self.bin) + os.pathsep + system_path,
               'AETHER_EXEC': 'cli', 'AETHER_EXEC_MAX_RUNS': '4', 'AETHER_EXEC_SLOT_CAP': '1',
               'PYTHONPATH': str(ROOT), 'PYTHONIOENCODING': 'utf-8', 'HOME': str(self.root), 'USERPROFILE': str(self.root)}
        for key in ('SystemRoot', 'TEMP', 'TMP'):
            if key in os.environ:
                env[key] = os.environ[key]
        self.proc = subprocess.Popen([sys.executable, '-c', SERVER_CODE, str(self.port), str(self.data)], env=env,
                                     cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError('server exited: ' + (self.proc.stdout.read() or '')[-500:])
            try:
                urllib.request.urlopen(self.base + '/', timeout=1).read()
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:
            raise RuntimeError('server did not start')
        self.token = (self.data / 'owner.token').read_text(encoding='utf-8').strip()

    def call(self, method, path, body=None):
        return request(self.base, self.token, method, path, body)

    def seed(self):
        cfg = lambda name: str(self.slot_root / name)  # noqa: E731
        status, body = self.call('PUT', '/api/studio/slots', {'slots': [
            {'id': 'claude-a', 'kind': 'claude', 'label': 'Claude A', 'config_dir': cfg('claude-a'), 'order': 0},
            {'id': 'codex-b', 'kind': 'codex', 'label': 'Codex B', 'config_dir': cfg('codex-b'), 'order': 1},
            {'id': 'claude-c', 'kind': 'claude', 'label': 'Claude C', 'config_dir': cfg('claude-c'), 'order': 2}]})
        assert status == 200, body
        plan = [('done', 'codex-b', 'success Add a short usage section to the README.'),
                ('failed', 'codex-b', 'crash Refactor the config loader.'),
                ('blocked', 'claude-a', 'limit'),
                ('running', 'claude-c', 'stream 600'),
                ('queued', 'claude-c', 'success Update the changelog.')]
        for name, slot, prompt in plan:
            status, body = self.call('POST', '/api/studio/queue', {'prompt': prompt, 'workspace': str(self.workspace), 'slot': slot})
            assert status in (200, 201), body
            self.ids[name] = body.get('run_id') or body.get('id')
            if name in ('done', 'failed', 'blocked'):   # settle one by one so the order on screen is stable
                self.wait_for(name, {'done': 'done', 'failed': 'failed', 'blocked': 'queued'}[name])
            if name == 'running':
                self.wait_for('running', 'running')
        # the blocked run: wait until its slot shows a usage-limit block
        self.until(lambda: any(s.get('blocked_until') for s in self.call('GET', '/api/studio/slots')[1]['slots']), 'slot block')
        self.until(lambda: self.call('GET', f"/api/studio/runs/{self.ids['running']}/log")[1].get('events'), 'live log')

    def until(self, check, what, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(0.3)
        raise RuntimeError('timed out waiting for ' + what)

    def wait_for(self, name, state):
        run_id = self.ids[name]
        self.until(lambda: self.call('GET', f'/api/studio/queue/{run_id}')[1].get('state') == state, f'{name} -> {state}')

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                for run_id in list(self.ids.values()):   # kills the sleeping fake CLI before the server goes away
                    self.call('DELETE', f'/api/studio/queue/{run_id}')
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            for path in reversed(self.created):
                shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------- in-page checks
JS_CONTRAST = r'''() => {
  const bgOf = (el) => { const layers = []; for (let n = el; n; n = n.parentElement) { layers.push(getComputedStyle(n).backgroundColor); } return layers; };
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const own = (el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
  const rows = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!own(el) || !visible(el) || el.closest('.sr-only, script, style, svg, [hidden]')) continue;
    const s = getComputedStyle(el);
    rows.push({ fg: s.color, layers: bgOf(el), opacity: s.opacity, chip: el.matches('.chip, .badge') || !!el.closest('.chip, .badge'),
                tag: el.tagName.toLowerCase(), cls: el.className && el.className.baseVal === undefined ? String(el.className).slice(0, 40) : '',
                text: el.textContent.trim().slice(0, 30) });
  }
  const body = getComputedStyle(document.body);
  return { rows, body: { fg: body.color, layers: bgOf(document.body) }, rootBg: getComputedStyle(document.documentElement).backgroundColor };
}'''

JS_TOUCH = r'''() => {
  const bad = [];
  const sel = 'button, a[href], input:not([type=hidden]), select, textarea, summary, [role=tab], [role=button], [tabindex]:not([tabindex="-1"])';
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest('[hidden], .sr-only, dialog:not([open])') || el.classList.contains('skip')) continue;
    let target = el;
    if (el.matches('input[type=checkbox], input[type=radio]')) target = el.closest('label') || el;
    if (el.matches('[tabindex]') && !el.matches('button, a, input, select, textarea, summary, [role]')) continue;  // scroll regions, panels
    const r = target.getBoundingClientRect(); const s = getComputedStyle(target);
    if (r.width === 0 || r.height === 0 || s.visibility === 'hidden' || s.display === 'none') continue;
    // clipped away inside a scroller that is itself off screen is still a target; only skip zero-size
    if (r.height < 43.5 || (el.matches('button.icon-btn') && r.width < 43.5)) {
      bad.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}[${(el.getAttribute('aria-label') || el.textContent || el.type || '').trim().slice(0, 24)}] ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
  }
  return bad;
}'''

JS_PRIVATE = r'''(needles) => {
  const text = (document.body.innerText || '') + ' ' + [...document.querySelectorAll('[title],[aria-label],[value]')]
    .map((n) => (n.getAttribute('title') || '') + ' ' + (n.getAttribute('aria-label') || '') + ' ' + (n.value || '')).join(' ');
  return needles.filter((n) => n && text.includes(n));
}'''

JS_FOCUS = r'''() => { const a = document.activeElement; if (!a || a === document.body) return null;
  return { tag: a.tagName.toLowerCase(), inTabs: !!a.closest('#tabs'), inPanel: !!a.closest('#panel'), inDrawer: !!a.closest('.drawer'),
           label: (a.getAttribute('aria-label') || a.textContent || a.id || '').trim().slice(0, 30) }; }'''


def effective_bg(layers, root_bg):
    """Walk computed backgrounds from the element up; composite semi-transparent layers over what is beneath."""
    base = parse_color(root_bg)
    stack = [parse_color(c) for c in layers]
    colour = (255.0, 255.0, 255.0, 1.0) if base is None or base[3] < 1 else base
    for layer in reversed([c for c in stack if c is not None and c[3] > 0]):
        colour = blend(layer, colour)
    return colour


def contrast_checks(data, results, page_name):
    low, chips_low, worst_chip = [], [], None
    for row in data['rows']:
        fg = parse_color(row['fg'])
        if fg is None:
            continue
        bg = effective_bg(row['layers'], data['rootBg'])
        fg = blend(fg, bg) if fg[3] < 1 else fg
        ratio = contrast_ratio(fg, bg) * 1.0
        if row['chip']:
            worst_chip = ratio if worst_chip is None else min(worst_chip, ratio)
            if ratio < MIN_TEXT_CONTRAST:
                chips_low.append(f"{row['text']!r} {ratio:.2f}")
        elif ratio < MIN_TEXT_CONTRAST:
            low.append(f"{row['tag']}.{row['cls']} {row['text']!r} {ratio:.2f}")
    body_bg = effective_bg(data['body']['layers'], data['rootBg'])
    body_ratio = contrast_ratio(parse_color(data['body']['fg']), body_bg)
    results.add(page_name, 'body text contrast >= 4.5', body_ratio >= MIN_TEXT_CONTRAST, f'{body_ratio:.2f}:1')
    results.add(page_name, 'all visible text contrast >= 4.5', not low, '; '.join(low[:5]) or f'{len(data["rows"])} text elements')
    results.add(page_name, 'status chip contrast >= 4.5', not chips_low,
                '; '.join(chips_low[:5]) or ('no chips on this page' if worst_chip is None else f'lowest {worst_chip:.2f}:1'))


def tab_check(page, results, page_name, panel_has_controls=True, limit=45):
    """Tab from the page top. With the modal drawer open focus is trapped inside it, which is the correct behaviour,
    so there the tab bar is not expected and the drawer controls must be reachable instead."""
    page.evaluate('() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }')
    seen, tabs, panel, drawer = [], False, False, False
    for _ in range(limit):
        page.keyboard.press('Tab')
        info = page.evaluate(JS_FOCUS)
        if info is None:
            continue
        seen.append(info['label'])
        tabs |= info['inTabs']
        panel |= info['inPanel']
        drawer |= info['inDrawer']
    ok = drawer if drawer else (tabs and (panel or not panel_has_controls))
    results.add(page_name, 'Tab key reaches tabs and main controls', ok,
                f"tabs={tabs} panel={panel or drawer} first focus: {', '.join(seen[:6])}")


# ---------------------------------------------------------------- the run
VIEWS = ('workspace', 'slots', 'run-live', 'run-done', 'tracker', 'tracker-drawer', 'builder', 'library')


def open_view(page, scratch, view):
    hash_ = {'workspace': '#workspace', 'slots': '#slots', 'tracker': '#tracker', 'tracker-drawer': '#tracker',
             'builder': '#builder', 'library': '#library',
             'run-live': '#run/' + scratch.ids['running'], 'run-done': '#run/' + scratch.ids['done']}[view]
    page.goto(scratch.base + '/#x')
    page.evaluate('(h) => { location.hash = h; }', hash_)
    page.wait_for_selector('#app:not([hidden])', timeout=15000)
    page.wait_for_selector('#panel > *', timeout=15000)
    if view == 'workspace':
        page.wait_for_timeout(500)
    if view in ('run-live', 'run-done'):
        page.wait_for_selector('#panel .chip', timeout=15000)
        page.wait_for_timeout(1500)
    if view.startswith('tracker'):
        page.wait_for_selector('tr[data-run]', timeout=15000)
    if view == 'tracker-drawer':
        row = page.locator('tr[data-run]').first
        target = row.locator('button, a').first
        (target if target.count() else row).click()
        page.wait_for_selector('.drawer', timeout=8000)
        page.wait_for_timeout(300)
    page.wait_for_timeout(400)


def run_matrix(scratch, results, outdir, only_views=None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('Playwright is not installed for this Python; run with the venv that has it. Nothing was installed.', file=sys.stderr)
        return None
    shots = []
    needles = [str(scratch.root), str(scratch.data), str(Path.home()), '/home/']
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as error:   # noqa: BLE001  (missing browser build is the usual cause)
            print('Chromium could not be launched (browser build missing?). Nothing was installed. ' + str(error).splitlines()[0],
                  file=sys.stderr)
            return None
        for width in WIDTHS:
            for theme in THEMES:
                context = browser.new_context(viewport={'width': width, 'height': 900 if width > 600 else 844},
                                              color_scheme=theme, reduced_motion='reduce', device_scale_factor=1)
                context.add_init_script(f'try {{ sessionStorage.setItem({json.dumps(TOKEN_KEY)}, {json.dumps(scratch.token)});'
                                        f' localStorage.setItem({json.dumps(THEME_KEY)}, {json.dumps(theme)}); }} catch (e) {{}}')
                for view in only_views or VIEWS:
                    page = context.new_page()
                    problems = []
                    page.on('console', lambda m, p=problems: p.append('console: ' + m.text[:120]) if m.type == 'error' else None)
                    page.on('pageerror', lambda e, p=problems: p.append('pageerror: ' + str(e)[:120]))
                    name = f'{view} {width} {theme}'
                    try:
                        open_view(page, scratch, view)
                        attr = page.evaluate('() => document.documentElement.getAttribute("data-theme")')
                        results.add(name, 'theme applied', attr == theme, f'data-theme={attr}')
                        file = outdir / shot_name(view, width, theme)
                        page.screenshot(path=str(file), full_page=True)
                        shots.append(file)
                        contrast_checks(page.evaluate(JS_CONTRAST), results, name)
                        results.add(name, 'no private paths on screen', not (leak := page.evaluate(JS_PRIVATE, needles)), ', '.join(leak) if leak else 'clean')
                        if width <= 400:
                            sw, iw = page.evaluate('() => [document.documentElement.scrollWidth, window.innerWidth]')
                            results.add(name, 'no horizontal scroll (scrollWidth <= innerWidth)', sw <= iw, f'{sw} <= {iw}')
                            small = page.evaluate(JS_TOUCH)
                            results.add(name, 'interactive elements >= 44px', not small, '; '.join(small[:6]) or 'all targets >= 44px')
                        tab_check(page, results, name)
                        page.wait_for_timeout(300)
                        results.add(name, 'no console or page errors', not problems, '; '.join(problems[:3]) or 'none')
                    except Exception as error:   # noqa: BLE001
                        results.add(name, 'page loaded and checked', False, f'{type(error).__name__}: {str(error).splitlines()[0][:150]}')
                    finally:
                        page.close()
                context.close()
        browser.close()
    return shots


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--out', default=str(OUT), help='output folder (default docs/ui-shots)')
    parser.add_argument('--view', action='append', choices=VIEWS, help='only these views (repeatable)')
    args = parser.parse_args(argv)
    try:
        import playwright  # noqa: F401
    except ImportError:
        print('Playwright is not installed for this Python; use /srv/docker/ersatztv/tools/venv/bin/python. Nothing was installed.',
              file=sys.stderr)
        return 2
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    scratch = Scratch()
    results = Results()
    try:
        scratch.start()
        scratch.seed()
        shots = run_matrix(scratch, results, outdir, args.view)
    except Exception as error:   # noqa: BLE001
        print(f'could not run: {type(error).__name__}: {error}', file=sys.stderr)
        return 2
    finally:
        scratch.stop()
    if shots is None:
        return 2
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    (outdir / 'RESULTS.md').write_text(results.markdown(stamp, shots), encoding='utf-8')
    for page_name, check, ok, detail in results.failed:
        print(f'FAIL {page_name}: {check}: {detail}')
    print(f'{len(results.rows) - len(results.failed)}/{len(results.rows)} checks passed; {len(shots)} screenshots in {outdir}')
    return 1 if results.failed else 0


if __name__ == '__main__':
    sys.exit(main())
