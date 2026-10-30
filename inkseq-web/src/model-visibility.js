// Owner rule 2026-10-20: only admins see which model or deployment runs a job. Users see the
// quality tier (fast / standard / natural / refine), so the models underneath can change freely.
// The backend should also omit model ids for non-admins; this hides whatever still arrives.
import { readAccess } from './account-access.js';
import { translate } from './i18n/locale.js';

let admin = false;
readAccess().then((access) => {
  admin = access.admin === true;
});

export const modelsVisible = () => admin;

const TIERS = new Set(['fast', 'standard', 'natural', 'refine']);

// A profile's tier comes from the backend (`tier`); unknown profiles read as Standard.
export function profileTier(profile) {
  return TIERS.has(profile?.tier) ? profile.tier : 'standard';
}

export function profileLabel(profile, locale = 'en', showModels = modelsVisible()) {
  if (!profile) return translate(locale, 'deployment.default');
  return showModels
    ? profile.label || profile.model_id || profile.id
    : translate(locale, 'credits.tier.' + profileTier(profile));
}
