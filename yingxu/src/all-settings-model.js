// Admin "All settings" (h3-studio#746): one read-only list of every tunable number, built
// from the live stored values the other admin pages edit. Nothing here is a copy; numbers
// that still live in code are listed as such until they move into audited settings.
import { CAPACITY_GROUPS, baseline, leaf, toText } from './capacity-settings-model.js';

const keyOf = (id) => id.split('.')[0];

// Capacity and spending: the values in force now, with the latest audited change per field.
export function capacityRows(settings) {
  if (!settings) return [];
  const value = baseline(settings),
    latest = settings.capacity_policy?.latest_changes || {};
  return CAPACITY_GROUPS.flatMap((group) =>
    group.fields.map(([id, unit]) => ({
      id,
      group: group.id,
      value: toText(id, leaf(value, id)),
      unit,
      change: latest[keyOf(id)] || null,
    })),
  );
}

// Credit rates per quality tier, welcome credits and the published credit packs.
export function priceRows(prices, packs) {
  const rows = [];
  for (const [tier, rate] of Object.entries(prices?.rates || {}))
    rows.push({ id: 'rate.' + tier, tier, value: String(rate), unit: 'creditsPerSecond' });
  if (Number.isSafeInteger(prices?.welcome_credits))
    rows.push({ id: 'welcome', value: String(prices.welcome_credits), unit: 'credits' });
  for (const pack of packs?.packs || [])
    rows.push({
      id: 'pack.' + pack.id,
      pack: pack.id,
      value: pack.credits + ' / ' + (pack.amount_cents / 100).toFixed(2),
      unit: 'creditsForUsd',
    });
  return rows;
}

// Numbers that still live in code or host files (h3-studio#746 moves them into settings).
export const NOT_STORED = [
  { id: 'modalHourly', value: '4.00', unit: 'usdHour', where: 'code' },
  { id: 'modalContainers', value: '2', unit: 'count', where: 'code' },
  { id: 'modalIdle', value: '60', unit: 'seconds', where: 'code' },
  { id: 'startupTimeout', value: '20', unit: 'minutes', where: 'code' },
  { id: 'referenceImages', value: '9 (17)', unit: 'count', where: 'hostFile' },
  { id: 'ramFloor', value: '41', unit: 'gib', where: 'hostFile' },
  { id: 'firstVideoTarget', value: '5', unit: 'minutes', where: 'planned' },
  { id: 'perUserQueue', value: '5', unit: 'count', where: 'planned' },
];
