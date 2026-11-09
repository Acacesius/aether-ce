# Studio UI design

The single reference for how AETHER Studio looks and behaves. Every view in `aether/studio/ui/` follows it. Tokens
live in [`studio.css`](../../aether/studio/ui/studio.css); icons in [`icons.js`](../../aether/studio/ui/icons.js);
contrast is enforced by [`tools/ui_contrast.py`](../../tools/ui_contrast.py).

## Direction
Calm, polished, readable. Studio is a tool you leave open for hours while agents work, so it should be quiet:

* Neutral surfaces, **one accent colour** (blue) used for the primary action, the selected tab, links and focus.
  Colour carries meaning only in status chips and error/success text, and is always paired with a word.
* Generous whitespace from a fixed spacing scale; cards with a soft shadow instead of heavy borders.
* System font stack only. No web fonts, no CDN, no images. Icons are inline SVG line drawings.
* No emoji anywhere in the UI. No animation beyond short colour/position transitions, and none with reduced motion.
* Plain HTML/CSS/ES modules, no build step. All DOM is built with `h()` / `textContent` / `createElementNS`;
  never `innerHTML` with data.

## Themes
* Three-way cycle on the masthead button: **auto -> light -> dark**. Saved in `localStorage`
  (`aether.studio.theme`). `auto` follows `prefers-color-scheme`.
* Dark tokens are the `:root` default; light tokens apply through `:root[data-theme=light]` and, in `auto`, through
  `@media (prefers-color-scheme:light){:root:not([data-theme])}`. The two light copies must be identical
  (`ui_contrast.py` fails otherwise).
* Each token block sits between `/* tokens:dark */` or `/* tokens:light */` and `/* end tokens */`, one declaration
  per line, so tools can parse it.

## Colour tokens

| token | dark | light | use |
|---|---|---|---|
| `--bg` | #0f1419 | #f6f8fa | page background |
| `--surface` | #161c23 | #ffffff | cards, masthead, inputs |
| `--surface-2` | #1d252e | #eef1f4 | hover, table header, code blocks |
| `--border` | #2c3743 | #d6dde4 | dividers, card outlines (decorative) |
| `--border-strong` | #64778a | #78869a | input and button outlines (3:1 non-text) |
| `--text` | #e6edf3 | #16202a | body text |
| `--muted` | #9fb0c0 | #4a5866 | hints, metadata, inactive tabs |
| `--accent` | #7cb7ff | #1f5fbf | the one accent: primary button, selected tab, links |
| `--accent-soft` | #16304d | #e3eefc | selected step / tinted background behind accent text |
| `--on-accent` | #0b1622 | #ffffff | text on `--accent` |
| `--focus` | #7cb7ff | #1f5fbf | focus ring (same hue as accent) |
| `--danger` | #ff9b9b | #b42329 | errors, destructive buttons |
| `--success` | #8fdca8 | #1d6b37 | success text |
| `--warning` | #f2c66d | #7a4b00 | warning text |
| `--shadow` | dark shadow | light shadow | card/drawer elevation |
| `--st-queued-fg` / `--st-queued-bg` | #c9d1d9 / #262d36 | #3b4552 / #eceff3 | status chip: queued (neutral) |
| `--st-running-fg` / `--st-running-bg` | #9ccbff / #12304f | #1b4f9c / #e3eefc | status chip: running (blue) |
| `--st-blocked-fg` / `--st-blocked-bg` | #f2c66d / #3a2b0e | #7a4b00 / #fbefd5 | status chip: blocked (amber) |
| `--st-done-fg` / `--st-done-bg` | #8fdca8 / #12321f | #1d6b37 / #e2f3e7 | status chip: done / succeeded (green) |
| `--st-failed-fg` / `--st-failed-bg` | #ffaaa8 / #401619 | #a1262b / #fce5e5 | status chip: failed (red) |
| `--st-cancelled-fg` / `--st-cancelled-bg` | #c9bfd8 / #2b2633 | #564a66 / #eeebf3 | status chip: cancelled (grey-violet) |

Legacy chip classes map onto these: `st-succeeded` = done, `st-interrupted`/`bad`/`risk-high`/`ap-denied` = failed,
`demo`/`risk-medium`/`ap-pending` = blocked (DEMO also gets an outline), `reason`/`st-skipped`/`risk-low`/`ap-expired`
= queued, `ap-granted` = running.

## Contrast (computed)
WCAG relative-luminance ratios, produced by `python3 tools/ui_contrast.py` (which exits non-zero below the minimum).
Text pairs need 4.5:1; non-text pairs (focus ring, input outlines) need 3:1.

| pair | dark | light |
|---|---|---|
| queued chip | 9.00 | 8.43 |
| running chip | 7.94 | 6.77 |
| blocked chip | 8.53 | 6.50 |
| done chip | 8.64 | 5.67 |
| failed chip | 8.59 | 6.18 |
| cancelled chip | 8.35 | 6.94 |
| text on bg / surface / surface-2 | 15.67 / 14.51 / 13.10 | 15.48 / 16.48 / 14.54 |
| muted on bg / surface / surface-2 | 8.33 / 7.71 / 6.96 | 6.85 / 7.29 / 6.43 |
| accent on bg / surface / accent-soft | 8.88 / 8.23 / 6.44 | 5.72 / 6.09 / 5.19 |
| on-accent on accent (primary button) | 8.74 | 6.09 |
| danger on bg / surface; surface on danger (armed button) | 9.18 / 8.50 / 8.50 | 6.14 / 6.54 / 6.54 |
| success on bg / surface | 11.43 / 10.59 | 6.14 / 6.54 |
| warning on bg / surface | 11.52 / 10.68 | 6.96 / 7.41 |
| focus on bg / surface (3:1) | 8.88 / 8.23 | 5.72 / 6.09 |
| border-strong on bg / surface (3:1) | 4.01 / 3.71 | 3.48 / 3.70 |

## Scales (theme-independent)

| token | value | token | value |
|---|---|---|---|
| `--sp-1` | 4px | `--fs-xs` | 0.75rem (chips, mono metadata) |
| `--sp-2` | 8px | `--fs-sm` | 0.875rem (hints, tables) |
| `--sp-3` | 12px | `--fs-md` | 1rem (body) |
| `--sp-4` | 16px | `--fs-lg` | 1.125rem (h3, brand) |
| `--sp-5` | 24px | `--fs-xl` | 1.375rem (h2) |
| `--sp-6` | 32px | `--fs-2xl` | 1.75rem (h1) |
| `--sp-7` | 48px | `--lh-body` / `--lh-tight` | 1.55 / 1.25 |
| `--r-sm` | 6px (controls) | `--r-md` | 10px (tables, small cards) |
| `--r-lg` | 14px (cards, dialogs) | `--r-pill` | 999px (chips, switch) |
| `--touch` | 44px minimum target | `--content` | 1080px max content width |
| `--sans` | `system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif` | `--mono` | `ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace` |
| `--ease` | cubic-bezier(.2,0,0,1) | `--dur` | 140ms |

Headings use weight 650 in the same sans stack; there is no display face.

## Interaction rules
* **Touch targets**: every button, tab, link-button, summary, checkbox row and field is at least 44px tall
  (`min-height:44px`) and 44px wide for icon-only buttons.
* **Focus ring**: `:focus-visible` draws a 2px `--focus` outline, offset 2px. Never removed for keyboard focus.
* **Reduced motion**: `@media (prefers-reduced-motion:reduce)` disables all animation and transitions and smooth scroll.
* **Keyboard**: the tablist follows the ARIA pattern (Left/Right/Home/End, roving tabindex). Dialogs trap focus via
  the native `<dialog>`; drawers return focus to their opener; Escape closes both.
* **Labels**: icon-only controls carry `aria-label`; decorative icons are `aria-hidden`. Live status uses
  `role=status` / `aria-live=polite`; errors use `role=alert`.
* **Destructive actions** use the two-step confirm (`confirmButton`): first press arms (danger fill), second runs.
* **Windows paths** (`C:\Users\x\proj`) are shown verbatim in monospace and wrap with `overflow-wrap:anywhere`.

## Layout
* **Desktop (> 640px)**: sticky masthead (brand left; theme and lock right) then a horizontal top tab bar with icon +
  label, with the Quick Run link right-aligned. Content column up to 1080px, 16px gutters.
* **Phone (390px)**: same order. The tab bar becomes a horizontally scrollable strip inside its own container
  (scroll-snap, never the page), so the page never scrolls sideways. Masthead buttons collapse to icons with
  `aria-label`s; fact lists stack to one column; cards use 12px/16px padding; toasts span the width.
* **Routes**: `#workspace`, `#slots`, `#tracker`, `#builder[/id]`, `#library`, `#runs[/id]` are tabs. `#quick`
  (Quick Run, DEMO) and `#run/<id>` (single run page; selects the Runs tab) are extra routes. The default route is
  `#workspace` when `/api/studio/mode` lists a `cli` executor (Studio started with `AETHER_EXEC=cli`), otherwise
  `#quick`.

## Icons
`icon(name, {label, size, className})` in `icons.js` returns an `<svg>` built with `createElementNS`: 24x24 grid,
1.75 stroke, round caps and joins, `currentColor`, decorative unless `label` is given. Set:

| name | meaning | name | meaning |
|---|---|---|---|
| `workspace` | folder; Workspace tab | `queue` | ordered list of work |
| `slots` | provider slots | `run` | a run (terminal); Runs tab |
| `tracker` | activity line; Tracker tab | `builder` | blocks; Builder tab |
| `library` | books; Library tab | `play` | start / Quick Run |
| `pause` | pause the queue | `cancel` | cancel (circled x) |
| `up` | move up | `down` | move down |
| `grip` | drag handle | `check` | done / confirmed |
| `alert` | warning / failure | `clock` | waiting, time |
| `plus` | add | `sun` | light theme |
| `moon` | dark theme | `auto` | auto theme (half disc) |
| `lock` | lock Studio | `download` | export / download |
| `chevron` | disclosure / next | | |

## Components
All live in `studio.css`; existing class names used by builder, library, runs and quick run keep working.

| component | markup | notes |
|---|---|---|
| button | `<button>` / `.button`; variants `.primary`, `.danger`, `.ghost`, `.icon-btn` | one `.primary` per view; armed state `[data-armed]` |
| chip | `.chip` (legacy `.badge`) + `.st-queued` … `.st-cancelled` | always text, optional 14px icon |
| card | `.card`; empty state `.card.empty-card` (`emptyCard()` in `lib.js`) | radius `--r-lg`, `--shadow` |
| table | `.table-wrap > table.table` | wrapper scrolls horizontally, never the page |
| drawer | `.drawer` (+ `.scrim`) | right side, `min(440px,100vw)` wide |
| dialog | native `<dialog>` + `.dialog-actions` | backdrop dims; Escape closes |
| switch | `label.switch > input[type=checkbox][role=switch] + span` | 42x24 track inside a 44px row |
| field | `.field > label + control (+ .hint, .field-error)` | `labelled()` helper; `aria-invalid` shows a 2px danger border |
| toast | `.toast-region > .toast(.ok/.error)` | region is `aria-live=polite`; auto-dismiss is optional, never for errors |
| tabs / nav | `.nav > #tabs[role=tablist] + .nav-link` | selected tab: accent text + 2px underline |
