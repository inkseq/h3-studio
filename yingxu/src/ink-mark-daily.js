// One ink-drop face or animal per local calendar day, the same for everyone that day.
// Stable brand spots (favicon, app icon, sign-in) keep the default mark.
export const DAILY_INK_MARKS = [
  'wink',
  'cat',
  'play',
  'rabbit',
  'fall',
  'fox',
  'ripple',
  'panda',
  'brush',
  'frog',
  'screen',
  'chick',
  'tentacles',
  'penguin',
  'gloss',
  'squid',
  'smile',
  'fish',
  'cutout',
  'bear',
  'pair',
  'owl',
  'spiral',
  'mouse',
];

export function dailyInkMark(date = new Date()) {
  const day = Math.floor(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000);
  return DAILY_INK_MARKS[
    ((day % DAILY_INK_MARKS.length) + DAILY_INK_MARKS.length) % DAILY_INK_MARKS.length
  ];
}
