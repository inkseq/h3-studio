// Admin capacity and spending settings (#456): every number is an editable, versioned setting.
// Draft values are display text (dollars, minutes, percent); this module converts and validates
// them against the server's own bounds and builds the partial `changes` the preview expects.
import { dollarsToMicros, microsToDollars } from './money.js';

// id is the field key, or key.sub for the parts of an object-valued field.
export const CAPACITY_GROUPS = [
  {
    id: 'money',
    fields: [
      ['tenant_daily_limit_microusd', 'usd'],
      ['tenant_daily_warning_fraction', 'percent'],
      ['fleet_hourly_limit_microusd', 'usdHour'],
      ['max_hourly_price_microusd', 'usdHour'],
      ['mode_hourly_ceiling_microusd.ref', 'usdHour'],
      ['mode_hourly_ceiling_microusd.fl', 'usdHour'],
      ['mode_hourly_ceiling_microusd.other', 'usdHour'],
    ],
  },
  {
    id: 'machines',
    fields: [
      ['max_machines', 'count'],
      ['idle_shutdown_seconds', 'minutes'],
      ['max_runtime_seconds', 'minutes'],
      ['test_task_limit_microusd', 'usd'],
    ],
  },
  {
    id: 'queue',
    fields: [
      ['idle_active_seconds', 'minutes'],
      ['idle_quiet_seconds', 'minutes'],
      ['interactive_activity_window_seconds', 'minutes'],
      ['interactive_wait_target_seconds', 'minutes'],
      ['batch_start_target_seconds', 'minutes'],
      ['batch_guaranteed_share_slots.numerator', 'count'],
      ['batch_guaranteed_share_slots.denominator', 'count'],
      ['per_user_minimum_machine_share', 'count'],
    ],
  },
];

export const FIELD_UNITS = Object.fromEntries(CAPACITY_GROUPS.flatMap((group) => group.fields));
const keyOf = (id) => id.split('.')[0];
const subOf = (id) => id.split('.')[1];

export function leaf(value, id) {
  const raw = value?.[keyOf(id)];
  return subOf(id) ? raw?.[subOf(id)] : raw;
}

// What is actually in force now. Before adoption the new fields are only recommendations, so
// the baseline is the server defaults overlaid with the legacy values that do apply.
export function baseline(settings) {
  return { ...(settings.capacity_policy?.defaults || {}), ...settings.value };
}

const NUMBER = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

export function toText(id, raw) {
  if (raw === undefined || raw === null) return '';
  const unit = FIELD_UNITS[id];
  if (unit === 'usd' || unit === 'usdHour') return microsToDollars(raw);
  if (unit === 'minutes') return String(Math.round((raw / 60) * 100) / 100);
  if (unit === 'percent') return String(Math.round(raw * 100));
  return String(raw);
}

export function fromText(id, text) {
  const value = String(text).trim();
  const unit = FIELD_UNITS[id];
  if (unit === 'usd' || unit === 'usdHour') return dollarsToMicros(value);
  if (!NUMBER.test(value)) throw Error('invalid_number');
  const number = Number(value);
  if (unit === 'minutes') return Math.round(number * 60);
  if (unit === 'percent') return Math.round(number) / 100;
  if (!Number.isSafeInteger(number)) throw Error('invalid_number');
  return number;
}

export function draftFrom(value) {
  return Object.fromEntries(
    Object.keys(FIELD_UNITS).map((id) => [id, toText(id, leaf(value, id))]),
  );
}

function bounded(id, raw, bounds, next) {
  const key = keyOf(id),
    rule = bounds?.[key];
  if (!rule) return null;
  if (key === 'batch_guaranteed_share_slots') {
    const { numerator, denominator } = next[key];
    if (
      subOf(id) === 'numerator' &&
      (numerator < rule.numerator_minimum || numerator >= denominator)
    )
      return { code: 'range', minimum: rule.numerator_minimum, maximum: denominator - 1 };
    if (
      subOf(id) === 'denominator' &&
      (denominator < rule.denominator_minimum || denominator > rule.denominator_maximum)
    )
      return {
        code: 'range',
        minimum: rule.denominator_minimum,
        maximum: rule.denominator_maximum,
      };
    return null;
  }
  const range = subOf(id) ? rule.fields?.[subOf(id)] : rule;
  if (!range) return null;
  if (raw < range.minimum || raw > range.maximum)
    return {
      code: 'range',
      minimum: toText(id, range.minimum),
      maximum: toText(id, range.maximum),
    };
  return null;
}

// Only changed fields go to the server; an object-valued field is sent whole if any part changed.
// Unchanged fields are never range-checked, so a legacy value outside the new bounds can stay.
export function buildChanges(settings, draft) {
  const base = baseline(settings),
    bounds = settings.capacity_policy?.field_bounds,
    next = structuredClone(base),
    errors = {},
    changed = new Set();
  for (const id of Object.keys(FIELD_UNITS)) {
    if (draft[id] === undefined || draft[id] === toText(id, leaf(base, id))) continue;
    let raw;
    try {
      raw = fromText(id, draft[id]);
    } catch {
      errors[id] = { code: 'number' };
      continue;
    }
    if (raw === leaf(base, id)) continue;
    if (subOf(id)) next[keyOf(id)] = { ...next[keyOf(id)], [subOf(id)]: raw };
    else next[id] = raw;
    changed.add(id);
  }
  for (const id of changed) {
    const problem = bounded(id, leaf(next, id), bounds, next);
    if (problem) errors[id] = problem;
  }
  const changes = {};
  for (const id of changed) changes[keyOf(id)] = next[keyOf(id)];
  return { changes, errors, changed: [...changed] };
}

// Fields whose value in force differs from the recommended default, e.g. the legacy fleet cap.
export function offDefault(settings) {
  const defaults = settings.capacity_policy?.defaults || {},
    base = baseline(settings);
  return Object.keys(FIELD_UNITS).filter(
    (id) => leaf(defaults, id) !== undefined && leaf(defaults, id) !== leaf(base, id),
  );
}

export const pendingControl = (settings, id) =>
  !!settings.capacity_policy?.controls?.[keyOf(id)]?.pending_implementation;

// The server's write needs exactly what its preview returned, plus the human's reason.
export function confirmBody(preview, changes, reason) {
  return {
    expected_version: preview.expected_version,
    changes,
    reason: reason.trim(),
    confirmation_digest: preview.confirmation_digest,
    profile_snapshots: preview.profile_snapshots,
  };
}

// Fleet arithmetic the preview reports: how many machines the hourly cap really allows.
export function fleetShortfall(consequence) {
  if (consequence?.code !== 'fleet_limits_combination') return null;
  const per = consequence.required_hourly_microusd / Math.max(1, consequence.machines);
  if (consequence.required_hourly_microusd <= consequence.fleet_hourly_limit_microusd) return null;
  return {
    machines: consequence.machines,
    allowed: Math.max(0, Math.floor(consequence.fleet_hourly_limit_microusd / per)),
  };
}
