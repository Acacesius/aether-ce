// Studio shell: owner-token gate, theme, ARIA tablist and hash routing.
// Tabs: #workspace #slots #tracker #builder[/id] #library #runs[/id]. Extra routes: #quick (Quick Run), #run/<id>.
import { api, h, clear, session, badge, errorText, ApiError, assert } from './lib.js';
import { icon } from './icons.js';
import { mountQuick } from './quickrun.js';
import { mountBuilder } from './builder.js';
import { mountLibrary } from './library.js';
import { mountRuns } from './runs.js';
import { mountWorkspace } from './workspace.js';
import { mountSlots } from './slots.js';
import { mountTracker } from './tracker.js';
import { mountRunPage } from './runpage.js';

// [id, label, icon, mount, ctx]. ctx=true: mount(panel, {args, mode}); otherwise the legacy mount(panel, args).
const TABS = [
  ['workspace', 'Workspace', 'workspace', mountWorkspace, true],
  ['slots', 'Slots', 'slots', mountSlots, true],
  ['tracker', 'Tracker', 'tracker', mountTracker, true],
  ['builder', 'Builder', 'builder', mountBuilder, false],
  ['library', 'Library', 'library', mountLibrary, false],
  ['runs', 'Runs', 'run', mountRuns, false],
];
// Routes without their own tab. `tab` is the tab shown as selected (null: none, the nav link is current instead).
const ROUTES = [
  ['quick', 'Quick Run', mountQuick, false, null],
  ['run', 'Run', mountRunPage, true, 'runs'],
];
const $ = (id) => { const node = document.getElementById(id); assert(node, 'missing #' + id); return node; };
let unmount = () => {};
let mode = null;

// ---- theme: auto (prefers-color-scheme) -> light -> dark, remembered locally
const THEMES = ['auto', 'light', 'dark'];
const THEME_ICON = { auto: 'auto', light: 'sun', dark: 'moon' };
function applyTheme(theme) {
  assert(THEMES.includes(theme), 'unknown theme');
  if (theme === 'auto') document.documentElement.removeAttribute('data-theme'); else document.documentElement.dataset.theme = theme;
  const next = THEMES[(THEMES.indexOf(theme) + 1) % 3];
  $('theme').replaceChildren(icon(THEME_ICON[theme]), h('span', { class: 'label' }, 'Theme: ' + theme));
  $('theme').setAttribute('aria-label', `Theme: ${theme}. Switch to ${next}`);
  try { localStorage.setItem('aether.studio.theme', theme); } catch { /* preference is optional */ }
}
function initTheme() {
  let saved = 'auto';
  try { saved = localStorage.getItem('aether.studio.theme') || 'auto'; } catch { /* default */ }
  applyTheme(THEMES.includes(saved) ? saved : 'auto');
  $('theme').addEventListener('click', () => applyTheme(THEMES[(THEMES.indexOf(document.documentElement.dataset.theme || 'auto') + 1) % 3]));
}

// ---- tabs
function buildTabs() {
  const list = $('tabs');
  TABS.forEach(([id, label, iconName], i) => {
    list.append(h('button', { type: 'button', role: 'tab', id: 'tab-' + id, 'aria-controls': 'panel', 'aria-selected': 'false', tabindex: i ? '-1' : '0',
      onclick: () => { location.hash = '#' + id; } }, icon(iconName), h('span', null, label)));
  });
  list.addEventListener('keydown', (e) => {
    const tabs = [...list.querySelectorAll('[role=tab]')];
    const at = tabs.indexOf(document.activeElement);
    if (at < 0) return;
    const to = { ArrowRight: (at + 1) % tabs.length, ArrowLeft: (at + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    tabs[to].focus();
    tabs[to].click();
  });
  $('lock').replaceChildren(icon('lock'), h('span', { class: 'label' }, 'Lock'));
  $('lock').setAttribute('aria-label', 'Lock Studio');
  $('quick-link').replaceChildren(icon('play'), h('span', { class: 'label' }, 'Quick Run'));
  $('quick-link').setAttribute('aria-label', 'Quick Run (DEMO)');
}

// The CLI executor is "on" when the server registered it (AETHER_EXEC=cli); then Workspace is home, else Quick Run.
const cliEnabled = (info) => Boolean(info && info.executors && Object.prototype.hasOwnProperty.call(info.executors, 'cli'));
const defaultRoute = () => (cliEnabled(mode) ? 'workspace' : 'quick');

function route() {
  const [id, ...rest] = location.hash.replace(/^#/, '').split('/');
  const tab = TABS.find((t) => t[0] === id);
  const extra = tab ? null : ROUTES.find((r) => r[0] === id);
  if (!tab && !extra) { location.hash = '#' + defaultRoute(); return; }
  const [label, mount, wantsCtx, selected] = tab ? [tab[1], tab[3], tab[4], tab[0]] : [extra[1], extra[2], extra[3], extra[4]];
  unmount();
  document.querySelectorAll('#tabs [role=tab]').forEach((node) => {
    const on = node.id === 'tab-' + selected;
    node.setAttribute('aria-selected', String(on));
    node.tabIndex = on ? 0 : -1;
  });
  if (!selected) $('tabs').querySelector('[role=tab]').tabIndex = 0;  // keep the tablist reachable by keyboard
  const quick = $('quick-link');
  if (id === 'quick') quick.setAttribute('aria-current', 'page'); else quick.removeAttribute('aria-current');
  const panel = $('panel');
  panel.setAttribute('aria-labelledby', selected ? 'tab-' + selected : 'quick-link');
  clear(panel);
  unmount = (wantsCtx ? mount(panel, { args: rest, mode }) : mount(panel, rest)) || (() => {});
  document.title = `${label} · AETHER Studio`;
}

// ---- login + mode banner
function lock(message) {
  unmount(); unmount = () => {};
  session.clear();
  mode = null;
  $('app').hidden = true; $('login').hidden = false;
  $('login-msg').textContent = message || '';
}

async function enter() {
  try {
    mode = await api('GET', '/api/studio/mode');
    $('login').hidden = true; $('app').hidden = false;
    clear($('banner')).append(...[badge(mode.demo_only ? 'DEMO ONLY' : 'LIVE', mode.demo_only ? 'demo' : ''),
      cliEnabled(mode) ? badge('CLI executor on', 'st-running') : null,
      h('span', null, `mode ${mode.mode} · provider ${mode.provider} · runtime ${mode.runtime} · schema ${mode.schema_version}`)].filter(Boolean));
    route();
  } catch (e) {
    lock(e instanceof ApiError && e.status === 401 ? 'That token was not accepted.' : errorText(e));
  }
}

function init() {
  initTheme(); buildTabs();
  $('login-form').addEventListener('submit', (e) => {
    e.preventDefault();
    session.set($('token').value.trim());
    $('token').value = '';
    enter();
  });
  $('lock').addEventListener('click', () => lock('Locked.'));
  window.addEventListener('studio:unauthorized', () => lock('The session token was rejected. Enter it again.'));
  window.addEventListener('hashchange', () => { if (!$('app').hidden) route(); });
  if (session.token) enter(); else lock('');
}
init();
