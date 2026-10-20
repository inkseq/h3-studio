// Interface skins (owner request 2026-10-20). A skin only overrides design tokens and adds
// light decoration in skins.css; layout never changes. The choice is a per-browser convenience.
// Skins draw on traditional, public-domain motifs (ichimatsu, asanoha, seigaiha, Greek key,
// Art Deco sunbursts), never on
// characters, logos or names from someone else's work.

export const SKINS = [
  { id: 'default', swatch: ['#0b0c0c', '#cdf26e'] },
  {
    id: 'taisho',
    swatch: ['#0e1512', '#c8423b', '#2f7a63'],
    font: 'https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@600;700&display=swap',
  },
  { id: 'nordic', swatch: ['#f7f4ef', '#3f5a4c', '#c9a27a'], light: true },
  {
    id: 'deco',
    swatch: ['#0b0b0b', '#c9a54c', '#efe6cf'],
    font: 'https://fonts.googleapis.com/css2?family=Poiret+One&display=swap',
  },
  {
    id: 'aegean',
    swatch: ['#fbfbf8', '#1f5fa8', '#f2b134'],
    light: true,
    font: 'https://fonts.googleapis.com/css2?family=Marcellus&display=swap',
  },
];
const KEY = 'inkseq.skin';

export function readSkin() {
  try {
    const id = localStorage.getItem(KEY);
    return SKINS.some((skin) => skin.id === id) ? id : 'default';
  } catch {
    return 'default';
  }
}

export function applySkin(id) {
  const skin = SKINS.find((item) => item.id === id) || SKINS[0];
  const root = document.documentElement;
  if (skin.id === 'default') delete root.dataset.skin;
  else root.dataset.skin = skin.id;
  if (skin.font && !document.querySelector(`link[data-skin-font="${skin.id}"]`)) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = skin.font;
    link.dataset.skinFont = skin.id;
    document.head.appendChild(link);
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = skin.swatch[0];
  try {
    localStorage.setItem(KEY, skin.id);
  } catch {}
  window.dispatchEvent(new CustomEvent('inkseq:skin', { detail: skin.id }));
  return skin.id;
}
