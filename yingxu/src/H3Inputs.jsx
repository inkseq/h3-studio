import { useI18n } from './i18n/I18nProvider.jsx';
import { FloatingMessage } from './floating-notice.js';
import React, { useRef, useState, useSyncExternalStore } from 'react';
import { Upload, Image, Video, Music, Film, Plus, Trash2 } from 'lucide-react';
import { store } from './store.js';
import { cloudController as cloud } from './cloud-controller.js';
import { modeAvailability } from './generation-availability.js';
import { uploadFile } from './media.js';
import { createUploadGate } from './workspace-context.js';
import { freestyleRoles, freestyleInputIds, uploadFreestyleFiles } from './media-operations.js';
import {
  controlsForRecipe,
  referenceSpecs,
  effectiveReferenceCount,
  removeFreestyleInput,
  selectedProfileRecipe,
} from './cloud-model.js';
import { roleSupported } from './quick-chat-model.js';
import { effectiveLook, bindLook } from './character-model.js';
import { bindLocation } from './location-model.js';
import AssetPreview from './AssetPreview.jsx';
import ReferenceEditor from './ReferenceEditor.jsx';
import RecipeExecutionNotice from './RecipeExecutionNotice.jsx';
import './media-workspace.css';

const namesFor = (t) => ({
    image: t('h3more.copy1'),
    video: t('h3more.copy2'),
    audio: t('h3more.copy3'),
  }),
  icons = { image: Image, video: Video, audio: Music };
const mediaAccept = {
  image: 'image/png,image/jpeg,image/webp,image/gif',
  video: 'video/mp4,video/webm,video/quicktime',
  audio: 'audio/mpeg,audio/wav,audio/x-wav,audio/mp4,audio/ogg,audio/flac,audio/x-flac,audio/webm',
};
const find = (project, id) => project.entities.find((entity) => entity.id === id);

export function InputZone({
  project,
  shot,
  kind,
  role,
  title,
  description,
  maximum = 1,
  disabled = false,
  guide = false,
}) {
  const { t, locale } = useI18n();
  const names = namesFor(t);
  const ref = useRef(),
    uploadGate = useRef(createUploadGate()),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [dragging, setDragging] = useState(false),
    Icon = icons[kind] || Film;
  const roles = freestyleRoles(kind, role),
    ids = freestyleInputIds(project, shot.id, { kind, role, guide }),
    assets = [...new Set(ids)].map((id) => find(project, id)).filter(Boolean),
    directCount = ids.length,
    countAllReferences = !!kind && !guide && !['firstFrame', 'lastFrame'].includes(role),
    count = countAllReferences ? effectiveReferenceCount(project, shot.id, kind) : directCount;
  async function accept(files) {
    if (uploadGate.current.busy) {
      setError(t('h3more.copy4'));
      return;
    }
    setError('');
    if (disabled) {
      setError(t('h3more.copy5'));
      return;
    }
    if (!files.length) return;
    await uploadGate.current.run(async () => {
      setBusy(true);
      try {
        await uploadFreestyleFiles({
          store,
          shotId: shot.id,
          files,
          kind,
          role,
          guide,
          maximum,
          countAllReferences,
          upload: uploadFile,
          locale,
        });
      } catch (error) {
        setError(error.message);
      } finally {
        setBusy(false);
        if (ref.current) ref.current.value = '';
      }
    });
  }
  const rememberMetadata = (asset, metadata) => {
    const current = store.getState().project;
    if (current.id !== project.id) return;
    const live = find(current, asset.id);
    if (
      !live ||
      live.data.fileId !== asset.data.fileId ||
      Number.isFinite(live.data.metadata?.duration) ||
      Number.isFinite(live.data.metadata?.duration_s)
    )
      return;
    store.updateEntity(asset.id, {
      data: { metadata: { ...live.data.metadata, ...metadata, source: 'browser_preview' } },
    });
  };
  return (
    <section
      className={
        'h3-input-zone ' + (dragging ? 'is-dragging ' : '') + (disabled ? 'is-disabled' : '')
      }
      aria-label={title}
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled && !uploadGate.current.busy) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        accept([...event.dataTransfer.files]);
      }}
    >
      <header>
        <Icon size={19} />
        <h4>{title}</h4>
        <span>
          {count}/{maximum}
        </span>
      </header>
      <p>
        {description}
        {count > directCount ? t('h3more.copy6', { p0: count - directCount }) : ''}
      </p>
      <input
        ref={ref}
        type="file"
        tabIndex={-1}
        disabled={disabled || busy || count >= maximum}
        className="visually-hidden"
        aria-label={t('h3more.copy7') + title}
        multiple={maximum > 1}
        accept={kind ? mediaAccept[kind] : Object.values(mediaAccept).join(',')}
        onChange={(event) => accept([...event.target.files])}
      />
      <button
        type="button"
        className="h3-drop-button"
        disabled={disabled || busy || count >= maximum}
        onClick={() => ref.current.click()}
      >
        <Upload size={18} />
        {busy
          ? t('h3more.copy8')
          : count >= maximum
            ? t('h3more.copy9')
            : t('h3more.copy10', { p0: kind ? names[kind] : t('h3more.copy11') })}
      </button>
      <FloatingMessage tone="error" text={error ? error : null} />
      {!!assets.length && (
        <div className="h3-media-grid">
          {assets.map((asset) => (
            <article key={asset.id}>
              <div className={'h3-media-preview ' + asset.type}>
                <AssetPreview
                  entity={asset}
                  onMetadata={(metadata) => rememberMetadata(asset, metadata)}
                />
              </div>
              <div className="h3-media-caption">
                <b>{asset.title}</b>
                <small>
                  {names[asset.type]}
                  {asset.data.metadata?.duration
                    ? t('h3more.copy12', { p0: Number(asset.data.metadata.duration).toFixed(1) })
                    : ''}
                </small>
                {asset.type === 'video' && !guide && (
                  <label className="h3-video-audio">
                    <input
                      type="checkbox"
                      checked={shot.data.h3?.video_audio?.[asset.id] !== false}
                      onChange={(event) => {
                        const live = store
                          .getState()
                          .project.entities.find((entity) => entity.id === shot.id);
                        store.updateEntity(shot.id, {
                          data: {
                            h3: {
                              ...live.data.h3,
                              video_audio: {
                                ...live.data.h3?.video_audio,
                                [asset.id]: event.target.checked,
                              },
                            },
                          },
                        });
                      }}
                    />
                    {t('h3more.copy13')}
                  </label>
                )}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    store.editProject((project) =>
                      removeFreestyleInput(project, shot.id, asset.id, { guide, roles }),
                    )
                  }
                >
                  <Trash2 size={13} />
                  {t('h3more.copy14')}
                  {guide ? t('h3more.copy15') : t('h3more.copy16')}
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function GuideTiming({ project, shot, recipe }) {
  const { t, locale } = useI18n();
  const guides = shot.data.h3?.guides || [],
    seconds =
      controlsForRecipe(recipe, shot.data.h3?.controls, shot.data.seconds).duration ||
      shot.data.seconds ||
      5,
    raw = Math.max(5, Math.round(seconds * 24)),
    duration =
      recipe?.limits?.input_count_semantics === 'native_roles_v2'
        ? (raw + ((5 - (raw % 17) + 17) % 17)) / 24
        : seconds,
    [error, setError] = useState('');
  const update = (index, patch) =>
    store.editProject((project) => {
      const live = find(project, shot.id);
      if (!live?.data.h3?.guides?.[index]) return false;
      live.data.h3.guides[index] = { ...live.data.h3.guides[index], ...patch };
      live.version++;
      live.status = 'review';
    });
  const number = (text, { minimum = 0, maximum = Infinity } = {}) =>
    text !== '' &&
    Number.isFinite(Number(text)) &&
    Number(text) >= minimum &&
    Number(text) < maximum;
  return (
    !!guides.length && (
      <div className="h3-guide-timing">
        {guides.map((guide, index) => {
          const asset = find(project, guide.media_id);
          return (
            <article key={`${guide.media_id}:${index}`}>
              <strong>
                {index + 1}. {asset?.title || t('h3more.copy17')}
              </strong>
              <div className="h3-guide-fields">
                <label>
                  {t('h3more.copy18')}
                  <input
                    aria-label={t('h3more.copy19', { p0: index + 1 })}
                    type="number"
                    min="0"
                    max={duration}
                    step="0.01"
                    value={guide.time_seconds}
                    onChange={(event) => {
                      if (number(event.target.value, { maximum: duration })) {
                        update(index, { time_seconds: Number(event.target.value) });
                        setError('');
                      } else setError(t('h3more.copy20'));
                    }}
                  />
                </label>
                {asset?.type === 'video' && (
                  <label className="h3-guide-checkbox">
                    <input
                      type="checkbox"
                      checked={!!guide.use_audio}
                      onChange={(event) => update(index, { use_audio: event.target.checked })}
                    />
                    {t('h3more.copy21')}
                  </label>
                )}
                {asset && asset.type !== 'image' && (
                  <fieldset>
                    <legend>{t('h3more.copy22')}</legend>
                    <label>
                      {t('h3more.copy23')}
                      <input
                        aria-label={t('h3more.copy24', { p0: index + 1 })}
                        type="number"
                        min="0"
                        step="0.01"
                        value={guide.source_range?.start ?? ''}
                        onChange={(event) => {
                          if (number(event.target.value)) {
                            update(index, {
                              source_range: {
                                ...guide.source_range,
                                start: Number(event.target.value),
                                end: guide.source_range?.end ?? Number(event.target.value) + 1,
                                fileId: asset.data.fileId,
                              },
                            });
                            setError('');
                          }
                        }}
                      />
                    </label>
                    <label>
                      {t('h3more.copy25')}
                      <input
                        aria-label={t('h3more.copy26', { p0: index + 1 })}
                        type="number"
                        min="0"
                        step="0.01"
                        value={guide.source_range?.end ?? ''}
                        onChange={(event) => {
                          if (
                            number(event.target.value) &&
                            Number(event.target.value) > (guide.source_range?.start ?? 0)
                          ) {
                            update(index, {
                              source_range: {
                                start: guide.source_range?.start ?? 0,
                                end: Number(event.target.value),
                                fileId: asset.data.fileId,
                              },
                            });
                            setError('');
                          } else setError(t('h3more.copy27'));
                        }}
                      />
                    </label>
                    {guide.source_range && (
                      <button type="button" onClick={() => update(index, { source_range: null })}>
                        {t('h3more.copy28')}
                      </button>
                    )}
                  </fieldset>
                )}
              </div>
              <button
                type="button"
                onClick={() =>
                  store.editProject((project) => {
                    const live = find(project, shot.id);
                    live.data.h3.guides = live.data.h3.guides.filter((_, i) => i !== index);
                    live.version++;
                    live.status = 'review';
                  })
                }
              >
                {t('h3more.copy29')}
              </button>
            </article>
          );
        })}
        <FloatingMessage tone="error" text={error ? error : null} />
        <p>{t('h3more.copy30')}</p>
      </div>
    )
  );
}

export function ShotSharedReferences({ project, shot }) {
  const { t, locale } = useI18n();
  const scene = find(project, shot.parentId),
    characters = project.entities.filter((entity) => entity.type === 'character'),
    locations = project.entities.filter((entity) => entity.type === 'location');
  const { references } = referenceSpecs(project, shot.id),
    directIds = new Set(
      project.links
        .filter(
          (link) =>
            link.target === shot.id &&
            ['image', 'video', 'audio'].includes(find(project, link.source)?.type),
        )
        .map((link) => link.source),
    ),
    inherited = references.filter((ref) => !directIds.has(ref.entity.id));
  const commit = (mutate) => {
    let result;
    const ok = store.editProject((project) => {
      result = mutate(project);
      return result?.ok !== false;
    });
    if (!ok) store.notify(result?.error || store.getState().notice);
  };
  return (
    <section className="h3-shared-references">
      <h4>{t('h3more.copy31')}</h4>
      <p>{t('h3more.copy32')}</p>
      <div className="h3-cast-fields">
        {!!locations.length && (
          <label>
            {t('h3more.copy33')}
            <select
              aria-label={t('h3more.copy34', { p0: shot.title })}
              value={shot.data.locationId || ''}
              onChange={(event) =>
                commit((project) => bindLocation(project, shot.id, event.target.value, locale))
              }
            >
              <option value="">
                {scene?.data.locationId
                  ? t('h3more.copy35', {
                      p0: find(project, scene.data.locationId)?.title || t('h3more.copy36'),
                    })
                  : t('h3more.copy37')}
              </option>
              {locations.map((location) => (
                <option key={location.id} value={location.id}>
                  {location.title}
                </option>
              ))}
            </select>
          </label>
        )}
        {characters.map((character) => {
          const binding = effectiveLook(project, shot, character.id),
            looks = character.data.looks || [];
          return (
            !!looks.length && (
              <label key={character.id}>
                {character.title} {t('h3more.copy38')}
                <select
                  aria-label={t('h3more.copy39', { p0: shot.title, p1: character.title })}
                  value={
                    shot.data.cast?.find((cast) => cast.characterId === character.id)?.lookId || ''
                  }
                  onChange={(event) =>
                    commit((project) =>
                      bindLook(project, shot.id, character.id, event.target.value, locale),
                    )
                  }
                >
                  <option value="">
                    {binding?.inherited
                      ? t('h3more.copy35', {
                          p0:
                            looks.find((look) => look.id === binding.lookId)?.name ||
                            t('h3more.copy40'),
                        })
                      : t('h3more.copy41')}
                  </option>
                  {looks.map((look) => (
                    <option key={look.id} value={look.id}>
                      {look.name}
                    </option>
                  ))}
                </select>
              </label>
            )
          );
        })}
      </div>
      {!!inherited.length && (
        <div className="h3-inherited-grid">
          {inherited.map((ref) => (
            <article key={ref.key}>
              <AssetPreview entity={ref.entity} />
              <span>
                {ref.purpose === 'identity' ? t('h3more.copy42') : t('h3more.copy43')} ·{' '}
                {ref.entity.title}
              </span>
            </article>
          ))}
        </div>
      )}
      {!characters.length && !locations.length && (
        <p className="media-workspace-hint">{t('h3more.copy44')}</p>
      )}
    </section>
  );
}

export default function H3Inputs({
  project,
  shot,
  recipes = [],
  onInspect,
  local = false,
  showShared = true,
}) {
  const { t, locale } = useI18n();
  const capacity = useSyncExternalStore(cloud.subscribe, cloud.getState, cloud.getState);
  const recipe = selectedProfileRecipe({ ...capacity.capabilities, recipes }, shot.data.h3),
    mode = recipe?.mode || (local ? shot.data.h3?.inputMode || 'fl' : null),
    limits = recipe?.limits || {},
    draftLimits = local && !recipe,
    maximum = (key, fallback) => limits[key] ?? (draftLimits ? fallback : 0);
  const options = recipes.length
    ? recipes
    : local
      ? [
          { mode: 'fl', label: t('h3more.copy45') },
          { mode: 'ref', label: t('h3more.copy46') },
        ]
      : [];
  const choose = (option) => {
    if (!local && option.id !== recipe?.id) {
      const live = cloud.getState(),
        status = modeAvailability(
          live.availability,
          live.capabilities,
          {
            deployment_profile_id: shot.data.h3?.deploymentProfileId || null,
            recipe_id: option.id,
          },
          undefined,
          Date.now(),
          locale,
        );
      if (!status.available) {
        store.notify(status.message);
        return;
      }
    }
    const h3 = { ...shot.data.h3, inputMode: option.mode };
    if (option.id) h3.recipeId = option.id;
    else delete h3.recipeId;
    store.updateEntity(shot.id, { data: { h3 } });
  };
  const frameCount = project.links.filter(
      (link) => link.target === shot.id && ['firstFrame', 'lastFrame'].includes(link.role),
    ).length,
    referenceCount = project.links.filter(
      (link) =>
        link.target === shot.id &&
        ['image', 'video', 'audio'].includes(find(project, link.source)?.type) &&
        !['firstFrame', 'lastFrame'].includes(link.role),
    ).length;
  return (
    <section className="h3-input-workspace" aria-label={t('h3more.copy47', { p0: shot.title })}>
      <fieldset className="h3-mode-picker">
        <legend>{t('h3more.copy48')}</legend>
        <div>
          {options.map((option) => (
            <label
              key={option.id || option.mode}
              className={
                (recipe ? recipe.id === option.id : mode === option.mode) ? 'selected' : ''
              }
            >
              <input
                type="radio"
                name={'h3-input-mode-' + shot.id}
                disabled={
                  !local &&
                  option.id !== recipe?.id &&
                  !modeAvailability(
                    capacity.availability,
                    capacity.capabilities,
                    {
                      deployment_profile_id: shot.data.h3?.deploymentProfileId || null,
                      recipe_id: option.id,
                    },
                    undefined,
                    Date.now(),
                    locale,
                  ).available
                }
                checked={recipe ? recipe.id === option.id : mode === option.mode}
                onChange={() => choose(option)}
              />
              <span>
                <b>
                  {option.mode === 'fl'
                    ? t('h3more.copy45')
                    : option.mode === 'ref'
                      ? t('h3more.copy46')
                      : option.label}
                  {!local
                    ? ' · ' +
                      modeAvailability(
                        capacity.availability,
                        capacity.capabilities,
                        {
                          deployment_profile_id: shot.data.h3?.deploymentProfileId || null,
                          recipe_id: option.id,
                        },
                        undefined,
                        Date.now(),
                        locale,
                      ).label
                    : ''}
                </b>
                <small>{option.mode === 'fl' ? t('h3more.copy49') : t('h3more.copy50')}</small>
              </span>
            </label>
          ))}
        </div>
        {!options.length && <p role="status">{t('h3more.copy51')}</p>}
        {draftLimits && <p className="media-workspace-hint">{t('h3more.copy52')}</p>}
      </fieldset>
      {recipe && <RecipeExecutionNotice recipe={recipe} project={project} shot={shot} />}
      <div className="h3-input-title">
        <h3>{t('h3more.copy53')}</h3>
        <p>{mode === 'fl' ? t('h3more.copy54') : t('h3more.copy55')}</p>
        {mode === 'fl' && showShared && (
          <p className="media-workspace-hint">{t('h3more.copy56')}</p>
        )}
      </div>
      {(mode === 'fl' ||
        (mode === 'ref' &&
          (roleSupported(recipe, 'first_frame', 'image') ||
            roleSupported(recipe, 'last_frame', 'image')))) && (
        <>
          <div className="h3-frame-grid">
            <InputZone
              project={project}
              shot={shot}
              kind="image"
              role="firstFrame"
              title={t('h3more.copy57')}
              description={t('h3more.copy58')}
              disabled={!!recipe && !roleSupported(recipe, 'first_frame', 'image')}
            />
            <InputZone
              project={project}
              shot={shot}
              kind="image"
              role="lastFrame"
              title={t('h3more.copy59')}
              description={t('h3more.copy60')}
              disabled={!!recipe && !roleSupported(recipe, 'last_frame', 'image')}
            />
          </div>
          {mode === 'fl' &&
            referenceCount > 0 &&
            recipe?.limits?.input_count_semantics !== 'native_roles_v2' && (
              <p className="cloud-warning">
                {t('h3more.copy61')} {referenceCount} {t('h3more.copy62')}
              </p>
            )}
        </>
      )}
      {mode === 'ref' && showShared && <ShotSharedReferences project={project} shot={shot} />}
      {mode && (
        <div className="h3-reference-grid">
          {(mode === 'ref' || (recipe && roleSupported(recipe, 'images', 'image'))) && (
            <InputZone
              project={project}
              shot={shot}
              kind="image"
              role="reference"
              title={t('h3more.copy63')}
              description={t('h3more.copy64')}
              maximum={maximum('max_images', 9)}
            />
          )}
          {(mode === 'ref' || (recipe && roleSupported(recipe, 'videos', 'video'))) && (
            <InputZone
              project={project}
              shot={shot}
              kind="video"
              role="motion"
              title={mode === 'fl' ? t('h3more.copy65') : t('h3more.copy66')}
              description={mode === 'fl' ? t('h3more.copy67') : t('h3more.copy68')}
              maximum={maximum('max_videos', 3)}
            />
          )}
          {(mode === 'ref' || (recipe && roleSupported(recipe, 'audios', 'audio'))) && (
            <InputZone
              project={project}
              shot={shot}
              kind="audio"
              role="audio"
              title={mode === 'fl' ? t('h3more.copy69') : t('h3more.copy70')}
              description={mode === 'fl' ? t('h3more.copy71') : t('h3more.copy72')}
              maximum={maximum('max_audios', 3)}
            />
          )}
        </div>
      )}
      {mode === 'ref' && frameCount > 0 && !roleSupported(recipe, 'first_frame', 'image') && (
        <p className="cloud-warning">
          {t('h3more.copy61')} {frameCount} {t('h3more.copy73')}
        </p>
      )}
      {mode && (
        <details className="h3-time-guides">
          <summary>{t('h3more.copy74')}</summary>
          <p>
            {recipe?.limits?.input_count_semantics === 'native_roles_v2'
              ? t('h3more.copy75')
              : t('h3more.copy76')}
          </p>
          <InputZone
            project={project}
            shot={shot}
            title={t('h3more.copy77')}
            description={t('h3more.copy78')}
            maximum={maximum('max_guides', 8)}
            guide
          />
          <GuideTiming project={project} shot={shot} recipe={recipe} />
        </details>
      )}
      <details className="h3-reuse-details">
        <summary>{t('h3more.copy79')}</summary>
        <ReferenceEditor project={project} shotId={shot.id} onInspect={onInspect} />
      </details>
      <p className="media-workspace-hint">{t('h3more.copy80')}</p>
    </section>
  );
}
