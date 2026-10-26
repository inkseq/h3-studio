import { TrashMenu } from './UniversalTrash.jsx';
import { Button } from './ui/Button.jsx';
import { FloatingMessage } from './floating-notice.js';
import HostedSignIn from './HostedSignIn.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import './i18n/workspace-language.css';
import { deferredPanel } from './deferred-panel.js';
const ProductionQueue = deferredPanel(() => import('./ProductionQueue.jsx'), '制作队列');
import React, { useEffect, useState, useSyncExternalStore } from 'react';
import { LogOut, RefreshCw, ChevronRight } from 'lucide-react';
import { store } from './store.js';
import { cloudController as cloud } from './cloud-controller.js';
import { jobStatusLabel, jobMessage, activeJobStatuses } from './cloud-model.js';
import { download } from './media.js';
import './cloud-studio.css';
import ApiKeys from './ApiKeys.jsx';
import GenerationFailure from './GenerationFailure.jsx';
import { ServiceStatus } from './ServiceStatus.jsx';

export const useCloud = () => useSyncExternalStore(cloud.subscribe, cloud.getState, cloud.getState);
const run = (promise) => promise.catch(() => {});
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
            <Button onClick={() => run(cloud.initialize())}>{t('account.copy75')}</Button>
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
            <Button
              variant="primary"
              type="submit"
              disabled={c.busy || c.auth.auth_ready === false}
            >
              {t('account.copy79')}
            </Button>
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
        <Button
          disabled={c.busy}
          onClick={() => {
            setPassword('');
            run(cloud.logout());
          }}
        >
          <LogOut size={14} />
          {t('account.copy81')}
        </Button>
      </div>
      <details open={!!initialGrant} className="cloud-compatibility-access">
        <summary>{t('quickView.accessDetails')}</summary>
        <Button aria-expanded={showKeys} onClick={() => setShowKeys(!showKeys)}>
          {showKeys ? t('account.interface1') : t('account.interface2')}
        </Button>
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
        <Button disabled={c.busy} onClick={() => run(cloud.reloadCapabilities())}>
          {t('account.copy72')}
        </Button>
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
            <Button variant="primary" type="submit" disabled={c.busy || !title.trim()}>
              {t('account.copy84')}
            </Button>
            <Button
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
            </Button>
          </div>
        </form>
        <p className="muted">{t('account.copy86')}</p>
        <div className="cloud-section-head">
          <h3>{t('account.copy87')}</h3>
          <Button disabled={c.busy} onClick={() => run(cloud.list())}>
            <RefreshCw size={14} />
            {t('account.copy88')}
          </Button>
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
              <Button disabled={c.busy} onClick={() => open(p.id)}>
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
              </Button>
              <TrashMenu
                target={{ kind: 'project', project_id: p.id, id: p.id }}
                onEdit={() => open(p.id)}
              />
            </div>
          ))}
        </div>
        {c.moreProjects && (
          <Button disabled={c.busy} onClick={() => run(cloud.loadMoreProjects())}>
            {t('account.copy92')}
          </Button>
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
            <Button
              disabled={c.busy || c.draftOffer.draft.serverVersion !== c.draftOffer.record.version}
              onClick={() => open(c.draftOffer.record.id, { restoreDraft: true })}
            >
              {t('account.copy95')}
            </Button>
            <Button
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
            </Button>
            <Button
              disabled={c.busy}
              onClick={() => open(c.draftOffer.record.id, { keepDraft: true })}
            >
              {t('storyOpen.openCloud')}
            </Button>
            <Button onClick={() => cloud.dismissDraft()}>{t('account.copy97')}</Button>
          </div>
          <small>{t('storyOpen.draftBody')}</small>
        </section>
      )}
      <FloatingMessage tone="error" text={c.error ? c.error : null} />
    </div>
  );
}

const basics = new Set(['duration', 'resolution', 'aspect_ratio', 'generate_audio', 'seed']);
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
                <Button disabled={c.busy} onClick={() => run(cloud.cancel(job.id))}>
                  {t('account.copy131')}
                </Button>
              )}
              {shot && onInspect && ['failed', 'blocked', 'cancelled'].includes(job.status) && (
                <Button onClick={() => onInspect(shot.id)}>{t('account.copy132')}</Button>
              )}
              {shot && onReview && job.status === 'succeeded' && (
                <Button onClick={() => onReview(shot.id)}>{t('account.copy133')}</Button>
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
