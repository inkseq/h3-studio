import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { FloatingMessage } from './floating-notice.js';
import { ArrowUpRight, SlidersHorizontal } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { store } from './store.js';
import {
  CONTROL_LABELS,
  defaultsFor,
  effectiveControlSchema,
  recipeFor,
} from './quick-chat-model.js';
import {
  checkedGenerationDefaults,
  generationDefaultsEditor,
} from './settings-generation-defaults.js';

export default function SettingsGenerationDefaults({ account, api, active }) {
  const { t } = useI18n(),
    snapshot = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const [data, setData] = useState(null),
    [error, setError] = useState(''),
    request = useRef(0);
  useEffect(() => {
    const sequence = ++request.current;
    setData(null);
    setError('');
    if (!account || !active) return;
    Promise.all([api.request('/v1/storyboard/preferences'), api.capabilities()])
      .then(([preferences, capabilities]) => {
        if (sequence === request.current)
          setData({ account, preferences: checkedGenerationDefaults(preferences), capabilities });
      })
      .catch(() => {
        if (sequence === request.current) setError('settings.defaultsFailed');
      });
    return () => {
      request.current++;
    };
  }, [account, api, active]);
  const current = data?.account === account ? data : null,
    authored = current?.preferences.defaults || {},
    recipe = recipeFor(current?.capabilities, authored);
  const recommended = defaultsFor(
      current?.capabilities,
      recipe?.id,
      authored.deployment_profile_id,
    ),
    controls = { ...recommended.controls, ...authored.controls };
  const schema = effectiveControlSchema(recipe),
    keys = Object.keys(schema).filter(
      (key) =>
        !['duration', 'seed'].includes(key) &&
        !['array', 'object'].includes(schema[key].type) &&
        controls[key] !== undefined,
    );
  const changed =
    Object.keys(authored.controls || {}).length +
    ['recipe_id', 'deployment_profile_id'].filter((key) => authored[key]).length;
  const href = generationDefaultsEditor(snapshot, account);
  const label = (key) =>
    Object.hasOwn(CONTROL_LABELS, key) ? t('adjunct.control.' + key) : schema[key].title || key;
  const value = (key) =>
    typeof controls[key] === 'boolean'
      ? t(controls[key] ? 'settings.enabled' : 'settings.disabled')
      : controls[key] === null
        ? t('settings.recommended')
        : String(controls[key]);
  return (
    <section className="card-box settings-generation-defaults">
      <div className="row tight">
        <SlidersHorizontal size={18} />
        <div>
          <b>{t('settings.h3Defaults')}</b>
          <br />
          <small className="muted">
            {current
              ? t(
                  changed
                    ? changed === 1
                      ? 'settings.defaultsChangedOne'
                      : 'settings.defaultsChanged'
                    : 'settings.defaultsRecommended',
                  { count: changed },
                )
              : t(
                  error
                    ? 'settings.defaultsFailed'
                    : account
                      ? 'settings.loading'
                      : 'settings.login',
                )}
          </small>
        </div>
      </div>
      <p className="muted">{t('settings.storyDefaultsScope')}</p>
      {error && <p role="alert">{t(error)}</p>}
      {current && (
        <>
          <dl>
            <dt>{t('workbench.mode')}</dt>
            <dd>
              {recipe
                ? t(recipe.mode === 'ref' ? 'workspace.status14' : 'workspace.status13')
                : t('settings.recommended')}
            </dd>
            <dt>{t('workbench.profile')}</dt>
            <dd>
              {current.capabilities?.deployment_profiles?.find(
                (profile) => profile.id === authored.deployment_profile_id,
              )?.label ||
                authored.deployment_profile_id ||
                t('settings.recommended')}
            </dd>
            {['resolution', 'aspect_ratio', 'steps', 'generate_audio']
              .filter((key) => keys.includes(key))
              .map((key) => (
                <React.Fragment key={key}>
                  <dt>{label(key)}</dt>
                  <dd>{value(key)}</dd>
                </React.Fragment>
              ))}
          </dl>
          <details>
            <summary>{t('settings.allDefaultParameters')}</summary>
            <dl>
              {keys.map((key) => (
                <React.Fragment key={key}>
                  <dt>{label(key)}</dt>
                  <dd>{value(key)}</dd>
                </React.Fragment>
              ))}
            </dl>
          </details>
        </>
      )}
      <a className="settings-story-defaults" href={href || '/'}>
        <span>{t(href ? 'settings.editStoryDefaults' : 'settings.chooseStoryDefaults')}</span>
        <ArrowUpRight size={15} />
      </a>
    </section>
  );
}
