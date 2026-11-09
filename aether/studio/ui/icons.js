// Inline SVG line icons (24x24 grid, 1.75 stroke, currentColor). Built with createElementNS only: no markup strings.
const NS = 'http://www.w3.org/2000/svg'; // the SVG namespace identifier; nothing is fetched from it

// Each icon is a list of [tag, attributes]. Paths use only the drawing commands needed for a calm, uniform set.
const P = (d) => ['path', { d }];
const C = (cx, cy, r) => ['circle', { cx, cy, r }];
const R = (x, y, width, height, rx) => ['rect', { x, y, width, height, rx }];

export const ICONS = {
  workspace: [P('M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z')],
  queue: [P('M9 6h11M9 12h11M9 18h11'), C(4.5, 6, 1), C(4.5, 12, 1), C(4.5, 18, 1)],
  slots: [R(3, 4, 18, 6.5, 2), R(3, 13.5, 18, 6.5, 2), C(7, 7.25, 1), C(7, 16.75, 1)],
  run: [R(3, 4, 18, 16, 2.5), P('M7.5 9.5l3 2.5-3 2.5M13 15h4')],
  tracker: [P('M3 12h4l3-7 4 14 3-7h4')],
  builder: [R(3.5, 3.5, 7, 7, 1.5), R(13.5, 3.5, 7, 7, 1.5), R(3.5, 13.5, 7, 7, 1.5), P('M17 13.5v7M13.5 17h7')],
  library: [R(4, 4, 4, 16, 1), R(10, 4, 4, 16, 1), P('M16.2 5.3l3.4-.9 3 14.6-3.4.9z')],
  play: [P('M7.5 4.8v14.4L19 12z')],
  pause: [P('M8.5 5v14M15.5 5v14')],
  cancel: [C(12, 12, 9), P('M9 9l6 6M15 9l-6 6')],
  up: [P('M12 19V5M6 11l6-6 6 6')],
  down: [P('M12 5v14M6 13l6 6 6-6')],
  grip: [C(9, 6, 1), C(15, 6, 1), C(9, 12, 1), C(15, 12, 1), C(9, 18, 1), C(15, 18, 1)],
  check: [P('M5 12.5l4.5 4.5L19 7.5')],
  alert: [P('M12 3.8l9 15.7H3z'), P('M12 10v4M12 17v.01')],
  clock: [C(12, 12, 9), P('M12 7v5l3.2 2')],
  plus: [P('M12 5v14M5 12h14')],
  sun: [C(12, 12, 4), P('M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4')],
  moon: [P('M20 14.5A8.5 8.5 0 1 1 9.5 4a6.8 6.8 0 0 0 10.5 10.5z')],
  auto: [C(12, 12, 9), P('M12 3v18')],
  lock: [R(5, 11, 14, 10, 2), P('M8 11V8a4 4 0 0 1 8 0v3')],
  download: [P('M12 4v11M7 10l5 5 5-5M5 20h14')],
  chevron: [P('M9 6l6 6-6 6')],
};

// icon(name, {label, size, className}) -> SVGElement. Decorative (aria-hidden) unless a label is given.
export function icon(name, { label = '', size = 20, className = '' } = {}) {
  const shapes = ICONS[name];
  if (!shapes) throw new Error('Studio UI assertion: unknown icon ' + name);
  const svg = document.createElementNS(NS, 'svg');
  const attrs = { viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor', 'stroke-width': '1.75',
                  'stroke-linecap': 'round', 'stroke-linejoin': 'round', class: ('icon ' + className).trim(), focusable: 'false' };
  for (const [key, value] of Object.entries(attrs)) svg.setAttribute(key, String(value));
  if (label) {
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', label);
  } else {
    svg.setAttribute('aria-hidden', 'true');
  }
  for (const [tag, shapeAttrs] of shapes) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(shapeAttrs)) node.setAttribute(key, String(value));
    if (tag === 'circle' && name === 'grip') node.setAttribute('fill', 'currentColor');
    svg.append(node);
  }
  return svg;
}
