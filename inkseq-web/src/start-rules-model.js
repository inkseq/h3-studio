// Prepared machine start rules (h3-studio#695): one rule set per exact pool, configuration and
// mode. The server validates again; this only keeps the form honest before asking for a preview.
export const START_RULE_FIELDS = [
  ['job_seconds', 1, 86400],
  ['boot_seconds', 1, 86400],
  ['minimum_jobs', 1, 10000],
];

// Profiles that share pool, configuration and mode share one rule set; keep the first of each.
export function ruleTargets(profiles = []) {
  const seen = new Map();
  for (const profile of profiles) {
    if (!profile?.id || !profile.pool || !profile.configuration_id || !profile.mode) continue;
    const key = [profile.pool, profile.configuration_id, profile.mode].join('\u0000');
    if (!seen.has(key)) seen.set(key, profile);
  }
  return [...seen.values()].sort(
    (a, b) =>
      String(a.model_id).localeCompare(String(b.model_id)) ||
      String(a.mode).localeCompare(String(b.mode)) ||
      String(a.pool).localeCompare(String(b.pool)),
  );
}

export function draftFromRules(value = {}) {
  return Object.fromEntries(START_RULE_FIELDS.map(([id]) => [id, String(value[id] ?? '')]));
}

// Whole numbers within the server's bounds; returns the value to send or the first bad field.
export function parseRules(draft) {
  const value = {};
  for (const [id, min, max] of START_RULE_FIELDS) {
    const text = String(draft[id] ?? '').trim();
    const number = /^\d+$/.test(text) ? Number(text) : NaN;
    if (!Number.isSafeInteger(number) || number < min || number > max)
      return { error: { id, min, max } };
    value[id] = number;
  }
  return { value };
}

export function sameRules(a, b) {
  return START_RULE_FIELDS.every(([id]) => a?.[id] === b?.[id]);
}

// Operator routes refuse with {code, message}; FastAPI defaults use {detail}.
export function refusalCode(failure) {
  const detail = failure?.detail?.detail ?? failure?.detail;
  return typeof detail === 'string' ? detail : detail?.code || '';
}

// Option names show only what tells the targets apart; the parts they all share come back
// separately. The configuration is added only when model, mode, GPU and pool still collide.
export function targetLabels(targets = []) {
  const parts = (item) => [
    item.model_id,
    item.mode?.toUpperCase(),
    (item.gpu_names || []).join('/'),
    item.pool,
  ];
  const rows = targets.map(parts);
  const same = (column) => rows.length > 1 && rows.every((row) => row[column] === rows[0][column]);
  const shared = rows.length ? rows[0].filter((part, column) => part && same(column)) : [];
  const names = rows.map((row) => row.filter((part, column) => part && !same(column)).join(' · '));
  const counts = new Map();
  for (const name of names) counts.set(name, (counts.get(name) || 0) + 1);
  return {
    shared: shared.join(' · '),
    labels: Object.fromEntries(
      targets.map((item, index) => [
        item.id,
        counts.get(names[index]) > 1
          ? [names[index], item.configuration_id].filter(Boolean).join(' · ')
          : names[index],
      ]),
    ),
  };
}
