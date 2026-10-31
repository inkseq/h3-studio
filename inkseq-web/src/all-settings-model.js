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

// h3-studio#746 backend inventory: one allowlisted list of every stored number. The page
// prefers it; the per-endpoint reads above remain the fallback for an older server.
const EDIT_PAGES = {
  '/v1/operator/admin/spending-settings': '/admin?section=limits',
  '/v1/operator/dstack/admin/start-rules': '/admin?section=limits',
  '/v1/admin/credits/prices': '/admin?section=pricing',
  '/v1/admin/story-chat/settings': '/admin?section=pricing',
};
export const editPage = (path) => EDIT_PAGES[path] || null;

export function inventoryGroups(inventory) {
  const groups = new Map();
  for (const row of inventory?.items || []) {
    if (!groups.has(row.group)) groups.set(row.group, []);
    groups.get(row.group).push(row);
  }
  return [...groups.entries()];
}

// Server ids look like "capacity.max_machines" or "start.<profile>.job_seconds"; the last
// part names the setting.
export const settingKey = (row) => String(row.id).split('.').at(-1);

export function displayValue(row) {
  const value = row.value;
  if (value === null || value === undefined) return '—';
  if (row.unit === 'micro_usd' && Number.isFinite(value)) return '$' + +(value / 1e6).toFixed(2);
  if (row.unit === 'usd_cents' && Number.isFinite(value)) return '$' + (value / 100).toFixed(2);
  if (row.unit === 'seconds' && Number.isFinite(value))
    return value >= 120 && value % 60 === 0 ? value / 60 + ' min' : value + ' s';
  if (row.unit === 'boolean') return value ? 'on' : 'off';
  if (typeof value === 'object') return JSON.stringify(value).slice(0, 120);
  return String(value);
}

// Decided items the server still reports as pending ("pending:<field>").
export function pendingFields(inventory) {
  return new Set(
    (inventory?.unavailable || [])
      .filter((entry) => String(entry).startsWith('pending:'))
      .map((entry) => entry.slice('pending:'.length)),
  );
}

export const INVENTORY_ORDER = [
  'capacity',
  'start_rules',
  'credits',
  'packs',
  'model_limits',
  'runtime',
  'modal_defaults',
  'story_chat',
  'content',
];

// Where a NOT_STORED row now lives in the inventory; once the server lists it, the row leaves
// the "not adjustable yet" list.
const COVERED_BY = {
  startupTimeout: 'runtime.startup_timeout_seconds',
  modalContainers: 'modal_defaults.max_containers',
  modalIdle: 'modal_defaults.idle_timeout',
  packExpiry: 'credits.expiry_policy',
  welcomeExpiry: 'credits.expiry_policy',
};

export function stillNotStored(inventory) {
  if (!inventory) return NOT_STORED;
  const ids = new Set((inventory.items || []).map((row) => row.id));
  return NOT_STORED.filter((row) => !ids.has(COVERED_BY[row.id]));
}

// Translation keys to try for a row's label, most specific first.
export function labelKeys(row) {
  const key = settingKey(row);
  if (row.group === 'capacity') return ['capacity.field.' + key];
  if (row.group === 'start_rules') return ['rules.field.' + key];
  return ['allSettings.key.' + row.group + '.' + key, 'allSettings.key.' + key];
}

export const humanize = (key) => key.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

// Which profile, mode or pack a row belongs to.
export const rowScope = (row) =>
  [row.profile_id, row.mode, row.pack_id].filter(Boolean).join(' · ');
