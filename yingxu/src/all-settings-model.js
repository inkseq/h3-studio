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

// Numbers that still live in code or host files, or that are decided but not built yet
// (owner 2026-10-28, h3-studio#746). Grouped like the admin's mental model; each moves into
// audited settings and out of this list.
export const NOT_STORED = [
  // Speed and queue
  { id: 'firstVideoTarget', group: 'speed', value: '5', unit: 'minutes', where: 'planned' },
  { id: 'nextVideoInterval', group: 'speed', value: '2', unit: 'minutes', where: 'planned' },
  { id: 'modalTrigger', group: 'speed', value: '3', unit: 'minutes', where: 'planned' },
  { id: 'perUserQueue', group: 'speed', value: '5', unit: 'count', where: 'planned' },
  { id: 'fairShare', group: 'speed', value: 'on', unit: 'switch', where: 'planned' },
  { id: 'memberPriority', group: 'speed', value: 'on', unit: 'switch', where: 'planned' },
  { id: 'startupTimeout', group: 'speed', value: '20', unit: 'minutes', where: 'code' },
  // Our spending
  { id: 'globalHourly', group: 'cost', value: '20', unit: 'usdHour', where: 'planned' },
  { id: 'modalHourly', group: 'cost', value: '4', unit: 'usdHour', where: 'code' },
  { id: 'modalContainers', group: 'cost', value: '2', unit: 'count', where: 'code' },
  { id: 'modalIdle', group: 'cost', value: '60', unit: 'seconds', where: 'code' },
  // Customer prices
  { id: 'sharpRate', group: 'price', value: '8', unit: 'creditsPerSecond', where: 'planned' },
  {
    id: 'refVideoSurcharge',
    group: 'price',
    value: '4',
    unit: 'creditsPerSecond',
    where: 'planned',
  },
  { id: 'packExpiry', group: 'price', value: '12', unit: 'months', where: 'planned' },
  { id: 'welcomeExpiry', group: 'price', value: '30', unit: 'days', where: 'planned' },
  { id: 'memberPlans', group: 'price', value: '12 / 29', unit: 'usdMonth', where: 'planned' },
  { id: 'memberCredits', group: 'price', value: '960 / 2,400', unit: 'credits', where: 'planned' },
  { id: 'nonMemberMarkup', group: 'price', value: '30', unit: 'percent', where: 'planned' },
  { id: 'foundingOffer', group: 'price', value: '50% · 3 · 4', unit: 'offer', where: 'planned' },
  // Shot limits (tied to the model)
  { id: 'referenceImages', group: 'limits', value: '9 (17)', unit: 'count', where: 'hostFile' },
  { id: 'ramFloor', group: 'limits', value: '41', unit: 'gib', where: 'hostFile' },
  // AI assistant
  { id: 'chatPerDay', group: 'assistant', value: '—', unit: 'count', where: 'planned' },
  { id: 'chatDailySpend', group: 'assistant', value: '—', unit: 'usd', where: 'planned' },
  // Users and emergency switches
  { id: 'signupMode', group: 'switches', value: 'open', unit: 'mode', where: 'planned' },
  { id: 'busyMode', group: 'switches', value: 'off', unit: 'switch', where: 'planned' },
  { id: 'modalOn', group: 'switches', value: 'off', unit: 'switch', where: 'code' },
];

export const NOT_STORED_GROUPS = ['speed', 'cost', 'price', 'limits', 'assistant', 'switches'];
