import { usePageActive } from './AppRouteCache.jsx';
import { useI18n, LanguageSwitcher } from './i18n/I18nProvider.jsx';
import './i18n/workspace-language.css';
import React, { useEffect, useRef, useState } from 'react';
import { X, Settings2 } from 'lucide-react';
import { DeploymentProfileSelect } from './DeploymentProfile.jsx';
import GenerationAvailability from './GenerationAvailability.jsx';
import { modeAvailability, requireModeAvailable } from './generation-availability.js';
import {
  CONTROL_GROUPS,
  clone,
  defaultsFor,
  effectiveControlSchema,
  recipeFor,
  settingsProblems,
  bindingParticipates,
  automaticInputMode,
} from './quick-chat-model.js';
import Control from './GenerationControl.jsx';
export function ChatDialog({
  title,
  onClose,
  children,
  wide = false,
  className = '',
  eyebrow = '',
}) {
  const { t, locale } = useI18n();
  const pageActive = usePageActive();
  const ref = useRef(),
    opener = useRef(globalThis.document?.activeElement);
  useEffect(() => {
    const dialog = ref.current;
    if (!pageActive) {
      dialog.close();
      return;
    }
    dialog.showModal();
    dialog.querySelector('button,input,textarea,select')?.focus();
    return () => {
      dialog.close();
      if (opener.current?.isConnected && !opener.current.closest('[inert]')) opener.current.focus();
    };
  }, [pageActive]);
  useEffect(() => {
    const dialog = ref.current,
      close = () => onClose();
    dialog.addEventListener('inkseq-close-after-trash', close);
    return () => dialog.removeEventListener('inkseq-close-after-trash', close);
  }, [onClose]);
  return (
    <dialog
      className={'qc-dialog ' + (wide ? 'qc-dialog-wide ' : '') + className}
      ref={ref}
      aria-label={title}
      onCancel={(e) => {
        e.preventDefault();
        if (!ref.current?.querySelector('.item-menu [aria-expanded="true"]')) onClose();
      }}
    >
      <div className="qc-dialog-head">
        <div>
          {eyebrow && <span className="qc-eyebrow">{eyebrow}</span>}
          <h2>{title}</h2>
        </div>
        <button type="button" aria-label={t('workspace.copy88')} onClick={onClose}>
          <X size={18} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
function ProfileInputBoundary({ support }) {
  const { t, locale } = useI18n();
  const roles = {
    first_frame: t('chatControls.copy1'),
    last_frame: t('chatControls.copy2'),
    image: t('chatControls.copy3'),
    video: t('chatControls.copy4'),
    audio: t('chatControls.copy5'),
  };
  if (!support?.input_notes && !support?.joint_cases?.length) return null;
  return (
    <section className="qc-profile-boundary">
      <strong>{t('workspace.copy72')}</strong>
      {support.input_notes && <p className="qc-muted">{support.input_notes}</p>}
      {support.joint_cases?.length > 0 && (
        <ul className="qc-muted">
          {support.joint_cases.map((item, index) => (
            <li key={index}>
              {t('chatControls.copy29', {
                width: item.width,
                height: item.height,
                steps: item.steps,
                frames: item.frames,
              })}
              {item.input_roles?.length
                ? item.input_roles.map((role) => roles[role] || role).join(' ＋ ')
                : t('chatControls.copy6')}
            </li>
          ))}
        </ul>
      )}
      <p className="qc-muted">{t('workspace.copy135')}</p>
    </section>
  );
}
export default function QuickChatControls({
  capabilities,
  settings,
  copiesMax = 4,
  bindings = [],
  cardPrompt,
  scope = 'next',
  modeIntent,
  busy = false,
  onSave,
  onClose,
  onEditMaterials,
  availability,
  operator = false,
  onRefreshAvailability,
  availabilityChecking = false,
}) {
  const { t, locale } = useI18n();
  const [draft, setDraft] = useState(() => clone(settings)),
    [prompt, setPrompt] = useState(cardPrompt || ''),
    [error, setError] = useState(''),
    [pendingMode, setPendingMode] = useState(null);
  const recipe = recipeFor(capabilities, draft),
    schema = effectiveControlSchema(recipe);
  const [mode, setMode] = useState(modeIntent || recipe?.mode || 'auto');
  const set = (field, value) =>
    setDraft((current) => {
      const controls = { ...current.controls };
      if (value === undefined) delete controls[field];
      else controls[field] = value;
      return { ...current, controls };
    });
  const fields = (names) =>
    names
      .filter((name) => schema[name])
      .map((name) => (
        <Control
          key={name}
          field={name}
          schema={schema[name]}
          value={draft.controls?.[name]}
          onChange={(value) => set(name, value)}
        />
      ));
  function effectiveMode(intent) {
    return intent === 'auto'
      ? automaticInputMode(
          bindings,
          recipe,
          recipeFor(capabilities, {
            ...draft,
            recipe_id: capabilities?.recipes?.find((item) => item.mode === 'ref')?.id,
          }),
        )
      : intent;
  }
  function target(intent) {
    const base = capabilities?.recipes?.find((item) => item.mode === effectiveMode(intent));
    return base ? recipeFor(capabilities, { ...draft, recipe_id: base.id }) : null;
  }
  function applyMode(intent) {
    const next = target(intent);
    if (!next) {
      setError(t('chatControls.copy7'));
      return;
    }
    if (next.mode !== recipe?.mode) {
      try {
        requireModeAvailable(availability, capabilities, draft, next.mode);
      } catch (error) {
        setError(error.message);
        setPendingMode(null);
        return;
      }
    }
    const defaults = defaultsFor(capabilities, next.id, draft.deployment_profile_id),
      nextSchema = effectiveControlSchema(next),
      controls = { ...defaults.controls };
    for (const [field, value] of Object.entries(draft.controls || {}))
      if (nextSchema[field] && (!nextSchema[field].enum || nextSchema[field].enum.includes(value)))
        controls[field] = value;
    setDraft({ ...draft, recipe_id: next.id, controls });
    setMode(intent);
    setPendingMode(null);
    setError('');
  }
  function changeMode(intent) {
    const next = target(intent);
    if (!next) {
      setError(t('chatControls.copy7'));
      return;
    }
    if (next.mode !== recipe?.mode) {
      try {
        requireModeAvailable(availability, capabilities, draft, next.mode);
      } catch (error) {
        setError(error.message);
        return;
      }
    }
    const excluded = bindings.filter(
      (b) => bindingParticipates(b, recipe) && !bindingParticipates(b, next),
    );
    if (excluded.length) {
      setPendingMode({ intent, count: excluded.length });
      return;
    }
    applyMode(intent);
  }
  async function save(event) {
    event.preventDefault();
    if (pendingMode) {
      setError(t('chatControls.copy8'));
      return;
    }
    if (recipe?.mode !== recipeFor(capabilities, settings)?.mode) {
      try {
        requireModeAvailable(availability, capabilities, draft);
      } catch (error) {
        setError(error.message);
        return;
      }
    }
    const problems = settingsProblems(draft, recipe, copiesMax);
    if (scope === 'card' && !prompt.trim()) problems.unshift(t('chatControls.copy9'));
    if (problems.length) {
      setError(
        problems.map((problem) => (problem.startsWith('takes.') ? t(problem) : problem)).join(' '),
      );
      return;
    }
    setError('');
    try {
      await onSave(draft, prompt, { modeIntent: mode });
      onClose();
    } catch (err) {
      setError(err.message || t('chatControls.copy10'));
    }
  }
  const allGroups = new Set(CONTROL_GROUPS.flatMap(([, names]) => names));
  const duration = schema.duration,
    resolution = schema.resolution,
    ratio = schema.aspect_ratio,
    audio = schema.generate_audio;
  const labels = {
    '16:9': t('chatControls.copy11'),
    '9:16': t('chatControls.copy12'),
    '1:1': t('chatControls.copy13'),
  };
  const groupLabels = {
    '音画日程 · 实验项': t('adjunct.experimentalSchedule'),
    参考与时间: t('chatControls.copy14'),
    随机性与采样: t('chatControls.copy15'),
    运行与导出: t('chatControls.copy16'),
  };
  return (
    <ChatDialog
      title={scope === 'next' ? t('chatControls.copy17') : t('chatControls.copy18')}
      onClose={onClose}
      className="qc-controls-dialog"
      eyebrow={t('quickView.advanced')}
    >
      <form onSubmit={save}>
        <div className="qc-controls-body">
          <p className="qc-muted">{t('workspace.copy73')}</p>
          {!recipe ? (
            <p role="alert">{t('workspace.copy74')}</p>
          ) : (
            <>
              {/* Deployment profiles and sampler/VAE/runtime knobs are operator tools; regular
                  users get the common settings and references only. */}
              {operator && (
                <details className="quick-model-details">
                  <summary>{t('quickView.modelDetails')}</summary>
                  <DeploymentProfileSelect
                    capabilities={capabilities}
                    value={draft.deployment_profile_id}
                    mode={recipe?.mode}
                    disabled={busy}
                    onChange={(value) => setDraft({ ...draft, deployment_profile_id: value })}
                  />
                </details>
              )}
              <label className="qc-field">
                <span>{t('workspace.copy75')}</span>
                <select
                  aria-label={t('workspace.copy75')}
                  value={pendingMode?.intent || mode}
                  onChange={(e) => changeMode(e.target.value)}
                >
                  <option
                    value="auto"
                    disabled={
                      effectiveMode('auto') !== recipe.mode &&
                      !modeAvailability(availability, capabilities, draft, effectiveMode('auto'))
                        .available
                    }
                  >
                    {t('workspace.copy76')}
                  </option>
                  {['fl', 'ref']
                    .filter((value) => capabilities.recipes.some((item) => item.mode === value))
                    .map((value) => {
                      const status = modeAvailability(
                        availability,
                        capabilities,
                        draft,
                        value,
                        undefined,
                        locale,
                      );
                      return (
                        <option key={value} value={value} disabled={!status.available}>
                          {value === 'fl' ? t('chatControls.copy19') : t('chatControls.copy20')} ·{' '}
                          {status.label}
                        </option>
                      );
                    })}
                </select>
              </label>
              <GenerationAvailability
                snapshot={availability}
                capabilities={capabilities}
                settings={draft}
                operator={operator}
                onRefresh={onRefreshAvailability}
                checking={availabilityChecking}
              />
              <p className="qc-muted">
                {t('chatControls.copy25')}
                {mode === 'auto'
                  ? t('chatControls.copy26', {
                      mode:
                        recipe.mode === 'fl' ? t('chatControls.copy21') : t('chatControls.copy20'),
                    })
                  : ''}
              </p>
              {pendingMode && (
                <section className="qc-warning" role="alert">
                  <p>{t('chatControls.copy27', { count: pendingMode.count })}</p>
                  <button type="button" onClick={() => applyMode(pendingMode.intent)}>
                    {t('workspace.copy77')}
                  </button>
                  <button type="button" onClick={() => setPendingMode(null)}>
                    {t('workspace.copy78')}
                  </button>
                </section>
              )}
              {scope === 'card' && (
                <label className="qc-field">
                  <span>{t('workspace.copy79')}</span>
                  <textarea
                    aria-label={t('workspace.copy79')}
                    rows={4}
                    maxLength={12000}
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                    required
                  />
                </label>
              )}
              <div className="qc-form-grid">
                {duration &&
                  (duration.type === 'integer' &&
                  Number.isInteger(duration.minimum) &&
                  Number.isInteger(duration.maximum) &&
                  duration.maximum - duration.minimum < 60 ? (
                    <label className="qc-field">
                      <span>{t('workspace.copy80')}</span>
                      <select
                        aria-label={t('workspace.copy80')}
                        disabled={duration.available === false}
                        value={draft.controls?.duration ?? duration.default}
                        onChange={(e) => set('duration', Number(e.target.value))}
                      >
                        {Array.from(
                          { length: Math.max(0, duration.maximum - duration.minimum + 1) },
                          (_, i) => duration.minimum + i,
                        ).map((value) => (
                          <option key={value} value={value}>
                            {t('chatControls.copy28', { value })}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : (
                    fields(['duration'])
                  ))}
                {resolution && (
                  <label className="qc-field">
                    <span>{t('workspace.copy81')}</span>
                    <select
                      aria-label={t('workspace.copy81')}
                      value={draft.controls?.resolution ?? resolution.default}
                      disabled={resolution.available === false}
                      onChange={(e) => set('resolution', e.target.value)}
                    >
                      {[...(resolution.enum || [])]
                        .sort((a, b) => parseInt(b) - parseInt(a))
                        .map((value) => (
                          <option key={value} value={value}>
                            {value === 'custom' ? t('chatControls.copy22') : value}
                          </option>
                        ))}
                    </select>
                  </label>
                )}
                {ratio && (
                  <label className="qc-field">
                    <span>{t('workspace.copy82')}</span>
                    <select
                      aria-label={t('workspace.copy82')}
                      value={draft.controls?.aspect_ratio ?? ratio.default}
                      disabled={ratio.available === false}
                      onChange={(e) => set('aspect_ratio', e.target.value)}
                    >
                      {['16:9', '9:16', '1:1', '21:9', '4:3', '3:4', ...(ratio.enum || [])]
                        .filter(
                          (value, index, all) =>
                            all.indexOf(value) === index && ratio.enum?.includes(value),
                        )
                        .map((value) => (
                          <option key={value} value={value}>
                            {labels[value] || value}
                          </option>
                        ))}
                    </select>
                  </label>
                )}
                <label className="qc-field">
                  <span>{t('workspace.copy83')}</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    max={copiesMax}
                    aria-label={t('workspace.copy83')}
                    value={draft.copies ?? 1}
                    onChange={(e) => setDraft({ ...draft, copies: Number(e.target.value) })}
                  />
                </label>
              </div>
              {audio && (
                <label className={'qc-check ' + (audio.available === false ? 'qc-disabled' : '')}>
                  <input
                    type="checkbox"
                    checked={draft.controls?.generate_audio ?? audio.default ?? false}
                    disabled={audio.available === false}
                    onChange={(e) => set('generate_audio', e.target.checked)}
                  />
                  {t('workspace.copy84')}
                </label>
              )}
              {draft.controls?.resolution === 'custom' && (
                <div className="qc-form-grid">
                  {fields(['width', 'height'])}
                  <p className="qc-muted">{recipe.custom_canvas_constraints?.description}</p>
                </div>
              )}
              {CONTROL_GROUPS.slice(1)
                .filter(([name]) => operator || name === '参考与时间')
                .map(([name, names]) => (
                  <details key={name}>
                    <summary>{groupLabels[name] || name}</summary>
                    <div className="qc-form-grid">{fields(names)}</div>
                    {name === '参考与时间' && (
                      <>
                        <ProfileInputBoundary support={recipe.execution_support} />
                        <p className="qc-muted">{t('workspace.copy136')}</p>
                        {scope === 'card' && onEditMaterials && (
                          <button
                            type="button"
                            disabled={busy || !!pendingMode}
                            onClick={() => onEditMaterials(clone(draft), prompt)}
                          >
                            {t('workspace.copy85')}
                          </button>
                        )}
                      </>
                    )}
                    {name === '随机性与采样' && (
                      <p className="qc-muted">{t('workspace.copy137')}</p>
                    )}
                    {name === '运行与导出' && <p className="qc-muted">{t('workspace.copy138')}</p>}
                  </details>
                ))}
              {operator &&
                Object.keys(schema).some(
                  (name) =>
                    !allGroups.has(name) &&
                    !['guides', 'video_audio', 'width', 'height'].includes(name),
                ) && (
                  <details>
                    <summary>{t('workspace.copy86')}</summary>
                    <div className="qc-form-grid">
                      {fields(
                        Object.keys(schema).filter(
                          (name) =>
                            !allGroups.has(name) &&
                            !['guides', 'video_audio', 'width', 'height'].includes(name),
                        ),
                      )}
                    </div>
                  </details>
                )}
            </>
          )}
          {error && (
            <p className="qc-error" role="alert">
              {error}
            </p>
          )}
        </div>
        <div className="qc-dialog-footer">
          <span>{scope === 'next' ? t('chatControls.copy23') : t('chatControls.copy24')}</span>
          <button className="qc-primary" disabled={busy || !recipe}>
            {t('workspace.copy87')}
          </button>
        </div>
      </form>
    </ChatDialog>
  );
}
