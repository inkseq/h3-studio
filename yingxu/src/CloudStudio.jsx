import { TrashMenu } from './UniversalTrash.jsx';
import { FloatingMessage } from './floating-notice.js';
import HostedSignIn from './HostedSignIn.jsx';
import { translate } from './i18n/locale.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import './i18n/workspace-language.css';
import { deferredPanel } from './deferred-panel.js';
const ProductionQueue = deferredPanel(() => import('./ProductionQueue.jsx'), '制作队列');
import React, { useEffect, useState, useSyncExternalStore } from 'react';
import { Cloud, LogOut, Upload, RefreshCw, Save, ChevronRight } from 'lucide-react';
import { store } from './store.js';
import { cloudController as cloud } from './cloud-controller.js';
import {
  cloudSystemMessage,
  executionPresetControls,
  controlsForRecipe,
  selectedProfileRecipe,
  jobStatusLabel,
  jobMessage,
  activeJobStatuses,
  estimateLabel,
  shotSnapshot,
  stableJSON,
} from './cloud-model.js';
import { download } from './media.js';
import './cloud-studio.css';
import ShotPromptDraft from './ShotPromptDraft.jsx';
import RecipeExecutionNotice from './RecipeExecutionNotice.jsx';
import GenerationAvailability from './GenerationAvailability.jsx';
import { modeAvailability } from './generation-availability.js';
import ApiKeys from './ApiKeys.jsx';
import GenerationControl from './GenerationControl.jsx';
import { DeploymentProfileSelect } from './DeploymentProfile.jsx';
import GenerationFailure from './GenerationFailure.jsx';
import { ServiceStatus, InputUsage } from './ServiceStatus.jsx';

export const useCloud = () => useSyncExternalStore(cloud.subscribe, cloud.getState, cloud.getState);
const run = (promise) => promise.catch(() => {});
const issueText = (item) =>
  typeof item === 'string' ? item : item?.message || item?.detail || JSON.stringify(item);
const byteLabel = (value, locale = 'zh') =>
  typeof value !== 'number'
    ? translate(locale, 'cloud.unknown')
    : value >= 1024 ** 3
      ? (value / 1024 ** 3).toFixed(2) + ' GiB'
      : (value / 1024 ** 2).toFixed(2) + ' MiB';
export function CloudAssetStatus() {
  const { t, locale } = useI18n();
  const c = useCloud(),
    s = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    [expanded, setExpanded] = useState(false),
    labels = {
      ready: t('cloud.ready'),
      failed: t('cloud.failed'),
      storage_unknown: t('cloud.storageUnknown'),
      validating: t('cloud.validating'),
      receiving: t('cloud.receiving'),
    },
    items = c.assets.filter((a) => !a.parent_id || a.status !== 'ready');
  return (
    <details
      className="cloud-storage"
      open={expanded}
      onToggle={(e) => setExpanded(e.currentTarget.open)}
    >
      <summary>
        {t('cloud.storageTitle')}
        {c.assets.some((a) => ['failed', 'storage_unknown'].includes(a.status))
          ? t('cloud.needsRecovery')
          : ''}
      </summary>
      <p>{t('account.copy55')}</p>
      <button disabled={c.busy} onClick={() => run(cloud.assetInventory())}>
        <RefreshCw size={13} />
        {t('account.copy56')}
      </button>
      {c.usage && (
        <p>
          {t('cloud.usage', {
            used: byteLabel(c.usage.accounted_bytes, locale),
            limit: byteLabel(c.usage.limit_bytes, locale),
            active: c.usage.active_uploads,
            max: c.usage.max_active,
          })}
        </p>
      )}
      <FloatingMessage tone="error" text={c.assetError ? c.assetError : null} />
      <div className="cloud-storage-list">
        {items.slice(0, 20).map((a) => (
          <article key={a.id}>
            <div>
              <b>{a.file_name || t('cloud.untitled')}</b>
              <small>
                {labels[a.status] || a.status}
                {a.metadata?.duration
                  ? t('cloud.mediaDuration', { seconds: Number(a.metadata.duration).toFixed(2) })
                  : ''}
              </small>
              {a.error && <small>{a.error}</small>}
            </div>
            {['failed', 'storage_unknown', 'validating'].includes(a.status) ? (
              <button disabled={c.busy} onClick={() => run(cloud.resumeAsset(a.id))}>
                {t('account.copy57')}
              </button>
            ) : a.status === 'ready' &&
              !s.project.entities.some((e) => e.data.cloudAssetId === a.id) ? (
              <button disabled={c.busy} onClick={() => run(cloud.attachAsset(a.id))}>
                {t('account.copy58')}
              </button>
            ) : null}
            {a.project_id && (
              <TrashMenu target={{ kind: 'asset', project_id: a.project_id, id: a.id }} />
            )}
          </article>
        ))}
      </div>
      {items.length > 20 && <small>{t('account.copy59')}</small>}
    </details>
  );
}
export function CloudButton({ onOpen }) {
  const { t } = useI18n();
  const c = useCloud(),
    workspace = useSyncExternalStore(store.subscribe, store.getState, store.getState).workspace;
  useEffect(() => {
    cloud.initialize();
    return cloud.startPolling();
  }, []);
  return (
    <button className="cloud-entry" onClick={onOpen}>
      <Cloud size={16} />
      {c.account
        ? workspace.mode === 'cloud'
          ? t('account.interface7') + c.account
          : t('account.interface8') + c.account + t('account.interface9')
        : t('account.interface10')}
    </button>
  );
}
export function CloudWorkspaceBar({ onOpenCloud }) {
  const { t, locale } = useI18n();
  const c = useCloud(),
    s = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  if (s.workspace.mode !== 'cloud')
    return c.account && (c.resumeOffer || c.resumeError) ? (
      <aside className="cloud-resume" aria-label={t('account.copy61')}>
        <b>{t('account.copy60')}</b>
        <p>{c.resumeError || t('cloud.resume')}</p>
        <div className="cloud-actions">
          {c.resumeOffer && (
            <button disabled={c.busy} onClick={() => run(cloud.resume())}>
              {t('account.copy61')}
            </button>
          )}
          <button disabled={c.busy} onClick={() => cloud.dismissResume()}>
            {t('account.copy62')}
          </button>
          <button disabled={c.busy} onClick={onOpenCloud}>
            {t('account.copy63')}
          </button>
        </div>
      </aside>
    ) : null;
  return (
    <div className="cloud-workspace-bar">
      <div>
        <Cloud size={16} />
        <b>{c.account}</b>
        <span>
          {t('account.fragment3')}
          {s.workspace.serverVersion} ·{' '}
          {s.workspace.dirty ? t('account.interface11') : t('account.interface12')}
        </span>
      </div>
      <div>
        <button disabled={c.busy} onClick={() => run(cloud.save())}>
          <Save size={14} />
          {t('account.copy64')}
        </button>
        <button disabled={c.busy} onClick={() => run(cloud.syncAll())}>
          <Upload size={14} />
          {t('account.copy65')}
        </button>
        <button disabled={c.busy} onClick={() => run(cloud.checkRemoteVersion())}>
          {t('account.copy66')}
        </button>
        <button disabled={c.busy} onClick={() => cloud.leave()}>
          {t('account.copy67')}
        </button>
      </div>
      {c.remoteUpdate && (
        <div className="cloud-remote-update" role="status">
          <span>
            {t('cloud.remoteVersion', { version: c.remoteUpdate.version })}
            {s.workspace.dirty ? t('cloud.keepDraft') : t('cloud.loadResults')}
          </span>
          <button disabled={c.busy} onClick={() => run(cloud.open(s.project.id))}>
            {t('account.copy68')}
          </button>
        </div>
      )}
      {c.remoteUpdateError && <p role="status">{c.remoteUpdateError}</p>}
      {c.conflict && (
        <p role="alert">
          {t('account.copy69')}
          <button disabled={c.busy} onClick={() => run(cloud.saveCopy())}>
            {t('account.copy70')}
          </button>
          <button
            onClick={() =>
              download(
                new Blob([JSON.stringify(store.exportProject(), null, 2)], {
                  type: 'application/json',
                }),
                '云冲突草稿.json',
              )
            }
          >
            {t('account.copy71')}
          </button>
        </p>
      )}
      <FloatingMessage tone="error" text={c.error ? c.error : null} />
      {c.message && <p role="status">{cloudSystemMessage(c.message, locale)}</p>}
      <ServiceStatus capabilities={c.capabilities} projectId={s.project.id} />
      <button disabled={c.busy} onClick={() => run(cloud.reloadCapabilities())}>
        {t('account.copy72')}
      </button>
      <CloudAssetStatus />
    </div>
  );
}
export function CloudProjects({ onClose, quick = false, initialGrant = null }) {
  const { t } = useI18n();
  const c = useCloud(),
    [username, setUsername] = useState(''),
    [password, setPassword] = useState(''),
    [title, setTitle] = useState(''),
    [query, setQuery] = useState(''),
    [trashedProjects, setTrashedProjects] = useState([]),
    [showKeys, setShowKeys] = useState(!!initialGrant),
    s = store.getState();
  useEffect(() => () => setPassword(''), []);
  useEffect(() => {
    setQuery('');
    setTrashedProjects([]);
    const changed = (event) => {
      const r = event.detail;
      if (r?.state !== 'completed') return;
      const ids = r.items
        .filter((row) => row.target.kind === 'project')
        .map((row) => row.target.id);
      if (!ids.length) return;
      setTrashedProjects((old) =>
        r.action === 'trash'
          ? [...new Set([...old, ...ids])]
          : old.filter((id) => !ids.includes(id)),
      );
    };
    window.addEventListener('inkseq-trash-changed', changed);
    return () => window.removeEventListener('inkseq-trash-changed', changed);
  }, [c.account]);
  const visibleProjects = c.projects
    .filter((p) => !trashedProjects.includes(p.id))
    .filter((p) =>
      String(p.title ?? '')
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase()),
    );
  const login = (e) => {
    e.preventDefault();
    const secret = password;
    setPassword('');
    run(cloud.login(username.trim(), secret));
  };
  if (!c.account)
    return (
      <div className="cloud-projects">
        <p>{t('account.copy73')}</p>
        {!c.auth ? (
          <>
            <p role="status">{t('account.copy74')}</p>
            <button onClick={() => run(cloud.initialize())}>{t('account.copy75')}</button>
          </>
        ) : (
          <form onSubmit={login}>
            <HostedSignIn auth={c.auth} />
            <label>
              {t('account.copy76')}
              <input
                autoComplete="username"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                maxLength={100}
              />
            </label>
            {!['username-test', 'username-only-test'].includes(c.auth.authentication) && (
              <label>
                {t('account.copy77')}
                <input
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  maxLength={72}
                />
              </label>
            )}
            {['username-test', 'username-only-test'].includes(c.auth.authentication) && (
              <p className="cloud-warning">{t('account.copy78')}</p>
            )}
            <button className="primary" disabled={c.busy || c.auth.auth_ready === false}>
              {t('account.copy79')}
            </button>
            {c.auth.auth_ready === false && <p role="alert">{t('account.copy80')}</p>}
          </form>
        )}
        <FloatingMessage tone="error" text={c.error ? c.error : null} />
      </div>
    );
  const open = (id, options) => run(cloud.open(id, options).then((done) => done && onClose()));
  return (
    <div className="cloud-projects">
      <div className="cloud-section-head">
        <b>
          {t('account.fragment1')}
          {c.account}
        </b>
        <button
          disabled={c.busy}
          onClick={() => {
            setPassword('');
            run(cloud.logout());
          }}
        >
          <LogOut size={14} />
          {t('account.copy81')}
        </button>
      </div>
      <details open={!!initialGrant} className="cloud-compatibility-access">
        <summary>{t('quickView.accessDetails')}</summary>
        <button type="button" aria-expanded={showKeys} onClick={() => setShowKeys(!showKeys)}>
          {showKeys ? t('account.interface1') : t('account.interface2')}
        </button>
        <a className="account-settings-link" href="/trash">
          {t('trash.title')}
        </a>
        <a className="account-settings-link" href="/settings">
          {t('account.copy151')}
        </a>
        {showKeys && (
          <ApiKeys
            key={c.account}
            initialGrant={initialGrant}
            account={c.account}
            projects={
              c.projects.some((p) => p.id === s.project.id) || s.workspace.mode !== 'cloud'
                ? c.projects
                : [{ id: s.project.id, title: s.project.title }, ...c.projects]
            }
            moreProjects={c.moreProjects}
          />
        )}
        <ServiceStatus capabilities={c.capabilities} />
        <button disabled={c.busy} onClick={() => run(cloud.reloadCapabilities())}>
          {t('account.copy72')}
        </button>
      </details>
      <a className="account-settings-link" href="/">
        {t('quickView.backToWorks')}
      </a>
      <details className="cloud-compatibility-stories">
        <summary>{t('quickView.compatibilityStories')}</summary>
        <p>{t('account.copy82')}</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            run(
              (quick ? cloud.createQuick(title) : cloud.create(title)).then((done) => {
                if (done) {
                  setTitle('');
                  onClose();
                }
              }),
            );
          }}
        >
          <label>
            {t('account.copy83')}
            <input
              value={title}
              required
              maxLength={80}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={quick ? t('account.interface3') : t('account.interface4')}
            />
          </label>
          <div className="cloud-actions">
            <button className="primary" disabled={c.busy || !title.trim()}>
              {t('account.copy84')}
            </button>
            <button
              type="button"
              disabled={c.busy}
              onClick={() =>
                run(
                  cloud
                    .create(title.trim() || s.project.title.slice(0, 75) + ' · 副本', {
                      copy: true,
                    })
                    .then((done) => done && onClose()),
                )
              }
            >
              {t('account.copy85')}
            </button>
          </div>
        </form>
        <p className="muted">{t('account.copy86')}</p>
        <div className="cloud-section-head">
          <h3>{t('account.copy87')}</h3>
          <button disabled={c.busy} onClick={() => run(cloud.list())}>
            <RefreshCw size={14} />
            {t('account.copy88')}
          </button>
        </div>
        <label>
          {t('account.copy89')}
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('account.accessible15')}
          />
        </label>
        <p className="muted">
          {t('cloud.loadedProjects', {
            count: c.projects.length,
            more: c.moreProjects ? t('cloud.moreProjects') : '',
          })}
        </p>
        {!c.projectsLoaded && <p role="status">{t('account.copy90')}</p>}
        {c.projectsLoaded && !c.projects.length && <p>{t('account.copy91')}</p>}
        {query && !visibleProjects.length && (
          <p>
            {t('cloud.noProjectMatch')}
            {t(c.moreProjects ? 'cloud.searchEarlier' : 'cloud.clearSearch')}
          </p>
        )}
        <div className="cloud-project-list">
          {visibleProjects.map((p) => (
            <div key={p.id}>
              <button disabled={c.busy} onClick={() => open(p.id)}>
                <span>
                  <b>{p.title?.trim() || t('legacy.visible24')}</b>
                  <small>
                    {t('cloud.versionLabel', { version: p.version })}
                    {p.updated_at
                      ? new Date(
                          typeof p.updated_at === 'number' ? p.updated_at * 1000 : p.updated_at,
                        ).toLocaleString()
                      : ''}
                  </small>
                </span>
                <ChevronRight size={16} />
              </button>
              <TrashMenu
                target={{ kind: 'project', project_id: p.id, id: p.id }}
                onEdit={() => open(p.id)}
              />
            </div>
          ))}
        </div>
        {c.moreProjects && (
          <button disabled={c.busy} onClick={() => run(cloud.loadMoreProjects())}>
            {t('account.copy92')}
          </button>
        )}
      </details>
      {c.draftOffer && (
        <section className="cloud-warning">
          <h4>{t('account.copy93')}</h4>
          {c.draftOffer.draft.volatile && <p role="alert">{t('account.copy94')}</p>}
          <p>
            {t('cloud.draftVersions', {
              title: c.draftOffer.record.project.title,
              base: c.draftOffer.draft.serverVersion,
              current: c.draftOffer.record.version,
            })}
          </p>
          <div className="cloud-actions">
            <button
              disabled={c.busy || c.draftOffer.draft.serverVersion !== c.draftOffer.record.version}
              onClick={() => open(c.draftOffer.record.id, { restoreDraft: true })}
            >
              {t('account.copy95')}
            </button>
            <button
              onClick={() =>
                download(
                  new Blob([JSON.stringify(c.draftOffer.draft.project, null, 2)], {
                    type: 'application/json',
                  }),
                  '未同步云草稿.json',
                )
              }
            >
              {t('account.copy71')}
            </button>
            <button
              disabled={c.busy}
              onClick={() => open(c.draftOffer.record.id, { keepDraft: true })}
            >
              {t('storyOpen.openCloud')}
            </button>
            <button onClick={() => cloud.dismissDraft()}>{t('account.copy97')}</button>
          </div>
          <small>{t('storyOpen.draftBody')}</small>
        </section>
      )}
      <FloatingMessage tone="error" text={c.error ? c.error : null} />
    </div>
  );
}

const controlLabels = (t) => ({
  duration: t('cloud.shot22'),
  resolution: t('cloud.shot6'),
  aspect_ratio: t('cloud.shot26'),
  generate_audio: t('cloud.shot21'),
  seed: t('cloud.shot50'),
  steps: t('cloud.shot47'),
  cfg_video: t('cloud.shot38'),
  cfg_audio: t('cloud.shot54'),
  denoise: t('cloud.shot49'),
  sampler_name: t('cloud.shot46'),
  scheduler: t('cloud.shot42'),
  shift_video: t('cloud.shot39'),
  shift_audio: t('cloud.shot55'),
  ref_image_size: t('cloud.shot7'),
  video_decode: t('cloud.shot33'),
  audio_decode: t('cloud.shot51'),
  encoder_device: t('cloud.shot29'),
  tile_size: t('cloud.shot34'),
  tile_overlap: t('cloud.shot36'),
  audio_tile_size: t('cloud.shot52'),
  audio_overlap: t('cloud.shot53'),
  width: t('cloud.shot30'),
  height: t('cloud.shot32'),
  video_tile_size: t('cloud.shot35'),
  video_overlap: t('cloud.shot36'),
  video_temporal_size: t('cloud.shot18'),
  video_temporal_overlap: t('cloud.shot19'),
  export_crf: t('cloud.shot37'),
});
const basics = new Set(['duration', 'resolution', 'aspect_ratio', 'generate_audio', 'seed']);
function Control(props) {
  const { t, locale } = useI18n();
  return <GenerationControl {...props} className="cloud-control" />;
}
function DeploymentPreset({ recipe, values, onApply }) {
  const { t } = useI18n();
  const automatic = executionPresetControls(recipe);
  if (Object.keys(automatic).length)
    return (
      <section className="cloud-selected-mode" aria-label={t('account.accessible16')}>
        <b>{t('account.copy99')}</b>
        <p>{t('account.copy100')}</p>
      </section>
    );
  const preset = recipe?.deployment_preset;
  if (preset?.applies_to !== 'unset_controls_only') return null;
  const controls = Object.fromEntries(
    Object.entries(preset.controls || {}).filter(
      ([field, value]) =>
        ['encoder_device', 'video_decode'].includes(field) &&
        recipe.controls?.[field]?.enum?.includes(value),
    ),
  );
  if (!Object.keys(controls).length) return null;
  const different = Object.entries(controls).some(([field, value]) => values[field] !== value);
  return (
    <section className="cloud-warning" aria-label={t('account.accessible17')}>
      <b>{preset.label || t('cloud.shot13')}</b>
      <p>
        {Object.entries(controls)
          .map(
            ([field, value]) =>
              `${controlLabels(t)[field]}: ${value === 'cpu' ? 'CPU' : value === 'tiled' ? t('cloud.shot5') : value}`,
          )
          .join(' · ')}
        {t('cloud.presetTail')}
      </p>
      {different ? (
        <>
          <p>{t('account.copy101')}</p>
          <button type="button" onClick={() => onApply(controls)}>
            {t('account.copy102')}
          </button>
        </>
      ) : (
        <p>{t('account.copy103')}</p>
      )}
    </section>
  );
}
export function CloudShotPanel({
  project,
  shot,
  compact = false,
  showRecipeSelector = true,
  draftOnly = false,
}) {
  const { t, locale } = useI18n();
  const c = useCloud(),
    h3 = shot.data.h3 || {},
    recipes = c.capabilities?.recipes || [],
    recipe = selectedProfileRecipe(c.capabilities, h3),
    plan = c.plans[shot.id],
    values = controlsForRecipe(recipe, h3.controls, shot.data.seconds),
    [guideAsset, setGuideAsset] = useState(''),
    [guideTime, setGuideTime] = useState('0'),
    [guideAudio, setGuideAudio] = useState(false),
    [guideStart, setGuideStart] = useState(''),
    [guideEnd, setGuideEnd] = useState(''),
    [guideError, setGuideError] = useState('');
  const availabilitySettings = {
      deployment_profile_id: h3.deploymentProfileId || null,
      recipe_id: recipe?.id,
    },
    availability = modeAvailability(
      c.availability,
      c.capabilities,
      availabilitySettings,
      undefined,
      Date.now(),
      locale,
    );
  const stalePlan = !!plan && plan.sourceSnapshot !== stableJSON(shotSnapshot(project, shot.id)),
    expires =
      plan?.expires_at &&
      (typeof plan.expires_at === 'number' ? plan.expires_at * 1000 : Date.parse(plan.expires_at)),
    expired = !!expires && expires <= Date.now();
  const update = (patch) => {
    const live = store.getState().project.entities.find((e) => e.id === shot.id);
    store.updateEntity(shot.id, { data: { h3: { ...live.data.h3, ...patch } } });
  };
  const automatic = executionPresetControls(recipe),
    rawGuideFrames = Math.max(5, Math.round((values.duration || 15) * 24)),
    guideDuration =
      recipe?.limits?.input_count_semantics === 'native_roles_v2'
        ? (rawGuideFrames + ((5 - (rawGuideFrames % 17) + 17) % 17)) / 24
        : values.duration || 15;
  const control = (field, value) => {
    const controls = { ...h3.controls };
    if (value === undefined) delete controls[field];
    else controls[field] = value;
    update({ controls });
  };
  const media = project.entities.filter((e) => ['image', 'video', 'audio'].includes(e.type)),
    jobs = c.jobs.filter((j) => j.client_ref?.shot_id === shot.id),
    guides = h3.guides || [],
    held = jobs.some((j) => j.status === 'recovery_hold'),
    submitted = !!plan && jobs.some((j) => j.plan_id === plan.plan_id);
  const addGuide = () => {
    const entity = media.find((e) => e.id === guideAsset),
      hasRange = entity?.type !== 'image' && (guideStart !== '' || guideEnd !== '');
    if (
      hasRange &&
      (guideStart === '' ||
        guideEnd === '' ||
        !Number.isFinite(Number(guideStart)) ||
        !Number.isFinite(Number(guideEnd)) ||
        Number(guideStart) < 0 ||
        Number(guideEnd) <= Number(guideStart))
    ) {
      setGuideError(t('cloud.shot41'));
      return;
    }
    const raw = Math.max(5, Math.round((values.duration || 15) * 24)),
      guideEndTime =
        recipe?.limits?.input_count_semantics === 'native_roles_v2'
          ? (raw + ((5 - (raw % 17) + 17) % 17)) / 24
          : values.duration || 15;
    if (Number(guideTime) >= guideEndTime) {
      setGuideError(t('cloud.shot48'));
      return;
    }
    update({
      guides: [
        ...guides,
        {
          media_id: guideAsset,
          time_seconds: Number(guideTime),
          use_audio: entity?.type === 'video' && guideAudio,
          ...(hasRange
            ? {
                source_range: {
                  start: Number(guideStart),
                  end: Number(guideEnd),
                  fileId: entity.data.fileId,
                },
              }
            : {}),
        },
      ],
    });
    setGuideAsset('');
    setGuideStart('');
    setGuideEnd('');
    setGuideError('');
  };
  return (
    <section
      className={'cloud-shot-panel ' + (compact ? 'compact' : '')}
      aria-label={t('cloud.shotHeading', { title: shot.title })}
    >
      <div className="cloud-section-head">
        <h3>{draftOnly ? t('cloud.shot59') : t('cloud.shot23')}</h3>
        <span className="cloud-badge">{draftOnly ? t('cloud.shot3') : t('cloud.shot2')}</span>
      </div>
      <p>{t('account.copy104')}</p>
      {!draftOnly && (
        <GenerationAvailability
          snapshot={c.availability}
          capabilities={c.capabilities}
          settings={availabilitySettings}
          operator={c.operatorAccess === true}
          onRefresh={() => run(cloud.reloadAvailability())}
          checking={c.availabilityChecking}
        />
      )}{' '}
      {!recipe ? (
        <p role="alert">{t('account.copy105')}</p>
      ) : (
        <>
          <DeploymentProfileSelect
            capabilities={c.capabilities}
            value={h3.deploymentProfileId}
            mode={recipe.mode}
            disabled={c.busy}
            onChange={(value) => update({ deploymentProfileId: value })}
          />
          {showRecipeSelector ? (
            <label className="cloud-control">
              <span>{t('account.copy106')}</span>
              <select
                value={recipe.id}
                onChange={(e) => {
                  if (!draftOnly && e.target.value !== recipe.id) {
                    const live = cloud.getState(),
                      status = modeAvailability(
                        live.availability,
                        live.capabilities,
                        {
                          deployment_profile_id: h3.deploymentProfileId || null,
                          recipe_id: e.target.value,
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
                  update({ recipeId: e.target.value });
                }}
              >
                {recipes.map((r) => (
                  <option
                    key={r.id}
                    value={r.id}
                    disabled={
                      !draftOnly &&
                      r.id !== recipe.id &&
                      !modeAvailability(
                        c.availability,
                        c.capabilities,
                        { deployment_profile_id: h3.deploymentProfileId || null, recipe_id: r.id },
                        undefined,
                        Date.now(),
                        locale,
                      ).available
                    }
                  >
                    {r.label}
                    {!draftOnly
                      ? ' · ' +
                        modeAvailability(
                          c.availability,
                          c.capabilities,
                          {
                            deployment_profile_id: h3.deploymentProfileId || null,
                            recipe_id: r.id,
                          },
                          undefined,
                          Date.now(),
                          locale,
                        ).label
                      : ''}
                  </option>
                ))}
              </select>
              <small>
                {recipe.model_id} · {recipe.mode === 'fl' ? t('cloud.shot28') : t('cloud.shot9')}
              </small>
            </label>
          ) : (
            <p className="cloud-selected-mode">
              {t('cloud.currentMode', {
                mode:
                  recipe.mode === 'fl'
                    ? t('cloud.shot16')
                    : recipe.mode === 'ref'
                      ? t('cloud.shot4')
                      : recipe.label,
              })}
            </p>
          )}
          {showRecipeSelector && (
            <RecipeExecutionNotice recipe={recipe} project={project} shot={shot} />
          )}
          <label className="cloud-control">
            <span>{t('account.copy107')}</span>
            <textarea
              rows={4}
              maxLength={12000}
              value={shot.data.prompt ?? shot.description}
              onChange={(e) =>
                store.updateEntity(
                  shot.id,
                  { data: { prompt: e.target.value } },
                  { coalesceKey: 'cloud-prompt:' + shot.id },
                )
              }
              placeholder={t('account.accessible18')}
            />
            <small>{draftOnly ? t('cloud.shot15') : t('cloud.shot45')}</small>
          </label>
          <ShotPromptDraft key={shot.id} project={project} shot={shot} />
          <InputUsage
            project={project}
            shot={shot}
            recipe={recipe}
            uploadMax={c.capabilities?.upload_max_bytes}
          />
          <DeploymentPreset
            recipe={recipe}
            values={values}
            onApply={(controls) => update({ controls: { ...h3.controls, ...controls } })}
          />
          <div className="cloud-control-grid">
            {Object.entries(recipe.controls || {})
              .filter(([field]) => basics.has(field))
              .map(([field, schema]) => (
                <Control
                  key={field}
                  field={field}
                  schema={
                    Object.hasOwn(automatic, field)
                      ? { ...schema, available: false, reason: t('cloud.shot25') }
                      : schema
                  }
                  value={values[field]}
                  onChange={(value) => control(field, value)}
                />
              ))}
          </div>
          {project.journey?.workspace !== 'freestyle' &&
            (project.journey?.brief?.aspect || '9:16') !== values.aspect_ratio && (
              <p className="cloud-warning">
                {t('cloud.aspectNote', {
                  planned: project.journey?.brief?.aspect || '9:16',
                  current: values.aspect_ratio,
                })}
                {recipe.controls?.aspect_ratio?.enum?.includes(
                  project.journey?.brief?.aspect || '9:16',
                ) && (
                  <button
                    type="button"
                    onClick={() =>
                      control('aspect_ratio', project.journey?.brief?.aspect || '9:16')
                    }
                  >
                    {t('account.copy108')}
                  </button>
                )}
              </p>
            )}
          <p className="muted">
            {t('cloud.editDuration', {
              seconds: Number(Number(shot.data.seconds || 5).toFixed(3)),
            })}
          </p>
          <details>
            <summary>{t('account.copy109')}</summary>
            <div className="cloud-control-grid">
              {Object.entries(recipe.controls || {})
                .filter(([field]) => !basics.has(field))
                .map(([field, schema]) => (
                  <Control
                    key={field}
                    field={field}
                    schema={
                      Object.hasOwn(automatic, field)
                        ? { ...schema, available: false, reason: t('cloud.shot25') }
                        : schema
                    }
                    value={values[field]}
                    onChange={(value) => control(field, value)}
                  />
                ))}
            </div>
            <p className="muted">
              {recipe.custom_canvas_constraints?.description || t('cloud.shot31')}
              {t('cloud.customHelp')}
            </p>
            <h4>{t('account.copy110')}</h4>
            {project.links
              .filter((l) => l.target === shot.id)
              .map((l) => project.entities.find((e) => e.id === l.source))
              .filter((e) => e?.type === 'video')
              .map((e) => (
                <label className="cloud-checkbox" key={e.id}>
                  <input
                    type="checkbox"
                    checked={h3.video_audio?.[e.id] !== false}
                    onChange={(ev) =>
                      update({ video_audio: { ...h3.video_audio, [e.id]: ev.target.checked } })
                    }
                  />
                  {e.title}
                  {t('cloud.useSourceAudio')}
                </label>
              ))}
            <h4>{t('account.copy111')}</h4>
            <p>{t('account.copy112')}</p>
            {guides.map((g, i) => (
              <div className="cloud-guide" key={i}>
                <span>
                  {t('cloud.guide', {
                    title: media.find((m) => m.id === g.media_id)?.title || t('cloud.shot27'),
                    seconds: g.time_seconds,
                    range: g.source_range
                      ? t('cloud.sourceRange', {
                          start: g.source_range.start,
                          end: g.source_range.end,
                        })
                      : '',
                    audio:
                      media.find((m) => m.id === g.media_id)?.type === 'audio'
                        ? t('cloud.shot56')
                        : g.use_audio
                          ? t('cloud.shot8')
                          : t('cloud.shot17'),
                  })}
                </span>
                <button
                  onClick={() => update({ guides: guides.filter((_, index) => index !== i) })}
                >
                  {t('account.copy113')}
                </button>
              </div>
            ))}
            <div className="cloud-guide-add">
              <label>
                {t('account.copy114')}
                <select
                  value={guideAsset}
                  onChange={(e) => {
                    setGuideAsset(e.target.value);
                    setGuideAudio(false);
                  }}
                >
                  <option value="">{t('account.copy115')}</option>
                  {media.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.title}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t('account.copy116')}
                <input
                  type="number"
                  min="0"
                  max={guideDuration}
                  step="0.01"
                  value={guideTime}
                  onChange={(e) => setGuideTime(e.target.value)}
                />
              </label>
              {media.find((m) => m.id === guideAsset)?.type !== 'image' && guideAsset && (
                <>
                  <label>
                    {t('account.copy117')}
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      value={guideStart}
                      onChange={(e) => setGuideStart(e.target.value)}
                    />
                  </label>
                  <label>
                    {t('account.copy118')}
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      value={guideEnd}
                      onChange={(e) => setGuideEnd(e.target.value)}
                    />
                  </label>
                </>
              )}
              <label>
                <input
                  type="checkbox"
                  disabled={media.find((m) => m.id === guideAsset)?.type !== 'video'}
                  checked={guideAudio}
                  onChange={(e) => setGuideAudio(e.target.checked)}
                />
                {t('account.copy119')}
              </label>
              <button
                disabled={
                  !guideAsset ||
                  !Number.isFinite(Number(guideTime)) ||
                  Number(guideTime) < 0 ||
                  guides.length >= (recipe.limits?.max_guides ?? 0)
                }
                onClick={addGuide}
              >
                {t('account.copy120')}
              </button>
            </div>
            <FloatingMessage tone="error" text={guideError ? guideError : null} />
            {recipe.limits && (
              <p className="muted">
                {t('cloud.inputLimits', {
                  images: recipe.limits.max_images,
                  videos: recipe.limits.max_videos,
                  audios: recipe.limits.max_audios,
                  total: recipe.limits.max_total_files,
                  min: recipe.limits.min_clip_duration,
                  max: recipe.limits.max_clip_duration,
                })}
              </p>
            )}
          </details>
        </>
      )}
      {draftOnly ? (
        <p className="cloud-selected-mode">{t('account.copy121')}</p>
      ) : (
        <>
          {!c.capabilities ? (
            <p className="cloud-warning" role="status">
              {c.capabilityError || t('cloud.shot20')}
              <button disabled={c.busy} onClick={() => run(cloud.reloadCapabilities())}>
                {t('account.copy122')}
              </button>
            </p>
          ) : (
            !c.capabilities.execution_enabled && (
              <p className="cloud-warning">
                {t('account.copy123')}
                <button disabled={c.busy} onClick={() => run(cloud.reloadCapabilities())}>
                  {t('account.copy124')}
                </button>
              </p>
            )
          )}
          {c.capabilities?.simulation && <p className="cloud-warning">{t('account.copy125')}</p>}
          <FloatingMessage tone="error" text={c.error ? c.error : null} />
          <div className="cloud-actions">
            <button
              disabled={c.busy || held || !recipe}
              onClick={() => run(cloud.prepare(shot.id))}
            >
              {t('account.copy126')}
            </button>
            <button
              className="primary"
              disabled={
                c.busy ||
                held ||
                submitted ||
                (!c.unknown[shot.id] &&
                  (stalePlan ||
                    expired ||
                    plan?.status !== 'ready' ||
                    !c.capabilities?.execution_enabled ||
                    !availability.available))
              }
              onClick={() => run(cloud.submit(shot.id))}
            >
              ②{' '}
              {c.busy
                ? t('cloud.shot10')
                : submitted
                  ? t('cloud.shot11')
                  : c.unknown[shot.id]
                    ? t('cloud.shot24')
                    : t('cloud.shot14')}
            </button>
          </div>
          {plan && (
            <div className={'cloud-plan ' + plan.status}>
              <b>
                {submitted
                  ? t('cloud.shot44')
                  : stalePlan
                    ? t('cloud.shot43')
                    : expired
                      ? t('cloud.shot40')
                      : plan.status === 'ready'
                        ? t('cloud.shot58')
                        : t('cloud.shot57')}
              </b>
              {plan.execution?.admission_state === 'waiting_capacity' && (
                <p className="cloud-warning">{t('account.copy127')}</p>
              )}
              {(plan.blockers || []).map((item, i) => (
                <p key={i}>{issueText(item)}</p>
              ))}
              {(plan.warnings || []).map((item, i) => (
                <p className="cloud-warning" key={i}>
                  {issueText(item)}
                </p>
              ))}
              <p>{estimateLabel(plan.estimate, locale)}</p>
              {plan.output_spec && (
                <p>
                  {t('cloud.output', {
                    width: plan.output_spec.width,
                    height: plan.output_spec.height,
                    frames: plan.output_spec.frames,
                    seconds: Number(plan.output_spec.actual_duration).toFixed(2),
                  })}
                </p>
              )}
              <details>
                <summary>{t('account.copy128')}</summary>
                <pre>{JSON.stringify(plan.effective_request, null, 2)}</pre>
              </details>
              <small>{t('account.copy129')}</small>
            </div>
          )}
          {!c.busy && c.unknown[shot.id] && (
            <p role="alert" className="cloud-warning">
              {t('account.copy130')}
            </p>
          )}
          {jobs.length > 0 && <JobList jobs={jobs} project={project} />}
        </>
      )}
    </section>
  );
}
export function JobList({ jobs, project, onInspect, onReview }) {
  const { t, locale } = useI18n();
  const c = useCloud();
  return (
    <div className="cloud-job-list">
      {jobs.map((job) => {
        const shot = project.entities.find((e) => e.id === job.client_ref?.shot_id),
          old = shot && job.client_ref.shot_version !== shot.version;
        return (
          <article key={job.id}>
            <div>
              <b>
                {shot?.title || t('cloud.shot12')} · v{job.client_ref?.shot_version}
              </b>
              <span>
                {jobStatusLabel(job.status, locale)}
                {old ? t('cloud.shot1') : ''}
              </span>
              <small>
                {job.status === 'waiting_capacity'
                  ? ''
                  : job.status === 'recovery_hold'
                    ? jobMessage(job, locale)
                    : (job.phase !== job.status ? job.phase : '') || jobMessage(job, locale)}
              </small>
              <code>{job.id}</code>
            </div>
            <GenerationFailure job={job} />
            <div className="cloud-actions">
              {activeJobStatuses.has(job.status) && (
                <button disabled={c.busy} onClick={() => run(cloud.cancel(job.id))}>
                  {t('account.copy131')}
                </button>
              )}
              {shot && onInspect && ['failed', 'blocked', 'cancelled'].includes(job.status) && (
                <button onClick={() => onInspect(shot.id)}>{t('account.copy132')}</button>
              )}
              {shot && onReview && job.status === 'succeeded' && (
                <button onClick={() => onReview(shot.id)}>{t('account.copy133')}</button>
              )}
            </div>
            {job.status === 'succeeded' && <small>{t('account.copy134')}</small>}
            {job.status === 'waiting_capacity' && (
              <p role="status" className="cloud-warning">
                {jobMessage(job, locale)}
              </p>
            )}
            {job.status === 'recovery_hold' && <small>{t('account.copy135')}</small>}
            {job.status === 'submission_unknown' && <small>{t('account.copy136')}</small>}
          </article>
        );
      })}
    </div>
  );
}
export function CloudProduction(props) {
  const { locale } = useI18n();
  return <ProductionQueue locale={locale} key={props.project.id} {...props} />;
}
