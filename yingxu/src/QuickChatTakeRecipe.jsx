import React from 'react';
import { Disclosure } from './ui/Disclosure.jsx';
import { modelsVisible, profileLabel } from './model-visibility.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { recipeFor, effectiveControlSchema } from './quick-chat-model.js';
import GenerationControl from './GenerationControl.jsx';
export default function TakeRecipe({ take, state }) {
  const { t, locale } = useI18n();
  if (!take?.recipe)
    return (
      <Disclosure summary={t('quickView.inputDetails')}>
        <p role="status">{t('takes.recipeMissing')}</p>
      </Disclosure>
    );
  const value = take.recipe,
    recipe = recipeFor(state.capabilities, value),
    schema = effectiveControlSchema(recipe),
    profile = state.capabilities?.deployment_profiles?.find(
      (p) => p.id === value.deployment_profile_id,
    );
  const roles = {
    first_frame: 'workspace.detail31',
    last_frame: 'workspace.detail32',
    guides: 'workspace.detail33',
    images: 'workspace.detail28',
    videos: 'workspace.detail29',
    audios: 'workspace.detail30',
  };
  return (
    <Disclosure summary={t('takes.recipe')}>
      <p>
        {recipe?.mode === 'fl' ? t('workspace.status13') : t('workspace.status14')} ·{' '}
        {profile
          ? profileLabel(profile, locale)
          : (modelsVisible() && (value.model_id || value.deployment_profile_id)) ||
            t('deployment.default')}
      </p>
      <p className="qc-card-prompt">{value.prompt}</p>
      <fieldset disabled className="qc-form-grid">
        {Object.entries(value.controls || {}).map(([field, item]) =>
          schema[field] ? (
            <GenerationControl
              key={field}
              field={field}
              schema={schema[field]}
              value={item}
              onChange={() => {}}
            />
          ) : (
            <label key={field}>
              {field}
              <output>{typeof item === 'object' ? JSON.stringify(item) : String(item)}</output>
            </label>
          ),
        )}
      </fieldset>
      <p>{t('takes.lockedSeed', { seed: take.seed })}</p>
      <ul>
        {Object.entries(value.inputs || {}).flatMap(([role, items]) =>
          (Array.isArray(items) ? items : items ? [items] : []).map((item, index) => {
            const asset = state.materials.find((b) => b.asset_id === item.asset_id)?.asset;
            return (
              <li key={role + index}>
                {t(roles[role] || 'takes.input')} ·{' '}
                {asset?.file_name || t('takes.inputNumber', { number: index + 1 })}
                {item.source_range && ` · ${item.source_range.start}–${item.source_range.end}s`}
                {item.time_seconds !== undefined && ` · ${item.time_seconds}s`}
                {item.include_audio !== undefined &&
                  ' · ' + t(item.include_audio ? 'takes.soundOn' : 'takes.soundOff')}
                {item.use_audio !== undefined &&
                  ' · ' + t(item.use_audio ? 'takes.soundOn' : 'takes.soundOff')}
              </li>
            );
          }),
        )}
      </ul>
    </Disclosure>
  );
}
