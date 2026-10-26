// Interface skins (owner request 2026-10-20). A skin only overrides design tokens and adds
// light decoration in skins.css; layout never changes. The choice is a per-browser convenience.
// Skins draw on traditional, public-domain motifs (ichimatsu, asanoha, seigaiha, Greek key,
// Art Deco sunbursts), never on
// characters, logos or names from someone else's work.

export const SKINS = [
  // Brand look (owner, 2026-10-26): white or near-black with one coral accent; follows the system.
  { id: 'coral', swatch: ['#ffffff', '#ff5a36', '#0f1115'], light: true },
  { id: 'coral-dark', swatch: ['#0b0c0e', '#ff6b47', '#f4f4f5'] },
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
// Light and dark base looks (owner, 2026-10-26): until the viewer picks one, the app follows
// the system setting, live.
export const LIGHT = 'coral',
  DARK = 'coral-dark';
const systemDark = () => {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  } catch {
    return true;
  }
};

// The viewer's explicit choice, or '' when the app should follow the system.
export function chosenSkin() {
  try {
    const id = localStorage.getItem(KEY);
    return SKINS.some((skin) => skin.id === id) ? id : '';
  } catch {
    return '';
  }
}

export function readSkin() {
  return chosenSkin() || (systemDark() ? DARK : LIGHT);
}

export const isLightSkin = (id) => !!SKINS.find((skin) => skin.id === id)?.light;

// Forget the viewer's choice and follow the system again.
export function followSystem() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // Nothing stored to forget.
  }
  return applySkin(readSkin(), { remember: false });
}

// Apply the current look and keep following the system while nothing is chosen.
export function startSkin() {
  applySkin(readSkin(), { remember: false });
  try {
    window
      .matchMedia('(prefers-color-scheme: dark)')
      .addEventListener('change', () => chosenSkin() || applySkin(readSkin(), { remember: false }));
  } catch {
    // Old browsers: the look is fixed until the next load.
  }
}

export function applySkin(id, { remember = true } = {}) {
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
  if (remember)
    try {
      localStorage.setItem(KEY, skin.id);
    } catch {
      // The choice then lasts for this page only.
    }
  window.dispatchEvent(new CustomEvent('inkseq:skin', { detail: skin.id }));
  return skin.id;
}
