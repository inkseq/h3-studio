import { useI18n } from './i18n/I18nProvider.jsx';
import React from 'react';
import { store, roleLabels } from './store.js';
import { LocalRangePreview, RangeFields } from './media-controls.jsx';
import { validateRange } from './media-timing.js';
import './reference-editor.css';

const uses = {
  image: ['firstFrame', 'lastFrame', 'identity', 'reference'],
  video: ['motion', 'reference'],
  audio: ['audio', 'reference'],
};
function ReferenceCard({ entity, link, shot, onInspect }) {
  const { t, locale } = useI18n();
  const saved = shot.data.referenceRanges?.[link.id],
    timed = entity.type !== 'image';
  const [duration, setDuration] = React.useState(null),
    [start, setStart] = React.useState(String(saved?.start ?? 0)),
    [end, setEnd] = React.useState(saved?.end === undefined ? '' : String(saved.end)),
    [error, setError] = React.useState(''),
    [message, setMessage] = React.useState('');
  React.useEffect(() => {
    setStart(String(saved?.start ?? 0));
    setEnd(saved?.end === undefined ? '' : String(saved.end));
    setError('');
  }, [saved?.start, saved?.end, entity.data.fileId]);
  React.useEffect(() => setDuration(null), [entity.data.fileId]);
  const metadata = (d) => {
    setDuration(d);
    setEnd((value) => (value === '' ? String(Number(d.toFixed(3))) : value));
  };
  const change = (setter) => (value) => {
    setter(value);
    setError('');
    setMessage(t('h3more.copy81'));
  };
  const save = () => {
    const problem = validateRange(start, end, duration, locale);
    if (problem) {
      setError(problem);
      return;
    }
    const ok = store.editProject((p) => {
      const s = p.entities.find((e) => e.id === shot.id);
      if (!s || !p.links.some((l) => l.id === link.id)) return false;
      s.data.referenceRanges = {
        ...(s.data.referenceRanges || {}),
        [link.id]: { start: Number(start), end: Number(end), duration, fileId: entity.data.fileId },
      };
      s.status = 'review';
    });
    if (ok) {
      setMessage(t('h3more.copy82'));
      setError('');
    }
  };
  const unlink = () =>
    store.editProject((p) => {
      p.links = p.links.filter((l) => l.id !== link.id);
      const s = p.entities.find((e) => e.id === shot.id);
      if (s) {
        const ranges = { ...(s.data.referenceRanges || {}) };
        delete ranges[link.id];
        s.data.referenceRanges = ranges;
        s.status = 'review';
      }
    });
  return (
    <article className="reference-card" data-testid="reference-card">
      <header>
        <div>
          <strong>{entity.title}</strong>
          <span>{t('story.role.' + link.role)}</span>
        </div>
        <button type="button" onClick={unlink}>
          {t('h3more.copy83')}
        </button>
      </header>
      {saved?.fileId && saved.fileId !== entity.data.fileId && (
        <p className="media-control-error" role="alert">
          {t('h3more.copy84')}
        </p>
      )}
      <LocalRangePreview
        entity={entity}
        start={start}
        end={end}
        duration={duration}
        onDuration={metadata}
      />
      {timed && (
        <>
          <RangeFields
            start={start}
            end={end}
            setStart={change(setStart)}
            setEnd={change(setEnd)}
            duration={duration}
          />
          <div className="media-control-row">
            <button type="button" onClick={save} disabled={!duration}>
              {t('h3more.copy85')}
            </button>
            <span className="media-control-hint">
              {saved ? t('h3more.copy86', { p0: saved.start, p1: saved.end }) : t('h3more.copy87')}
            </span>
          </div>
        </>
      )}
      {error && (
        <p role="alert" className="media-control-error">
          {error}
        </p>
      )}
      {message && (
        <p role="status" className="media-control-hint">
          {message}
        </p>
      )}
      {onInspect && (
        <button type="button" className="media-text-button" onClick={() => onInspect(entity.id)}>
          {t('h3more.copy88')}
        </button>
      )}
    </article>
  );
}

export default function ReferenceEditor({ project, shotId, onInspect }) {
  const { t, locale } = useI18n();
  const shot = project.entities.find((e) => e.id === shotId),
    assets = project.entities.filter((e) => ['image', 'video', 'audio'].includes(e.type)),
    [assetId, setAssetId] = React.useState(''),
    [role, setRole] = React.useState('reference'),
    [error, setError] = React.useState('');
  const asset = assets.find((e) => e.id === assetId),
    links = project.links.filter(
      (l) => l.target === shotId && assets.some((a) => a.id === l.source),
    );
  const selectAsset = (id) => {
    setAssetId(id);
    const e = assets.find((a) => a.id === id);
    setRole(uses[e?.type]?.[0] || 'reference');
    setError('');
  };
  if (!shot) return null;
  const attach = () => {
    if (!asset) {
      setError(t('h3more.copy89'));
      return;
    }
    const result = store.addLink(asset.id, shot.id, role);
    if (result.ok) {
      setAssetId('');
      setError('');
    } else setError(result.error);
  };
  return (
    <section className="reference-editor" aria-label={t('h3more.copy90', { p0: shot.title })}>
      <h4>{t('h3more.copy91')}</h4>
      <p className="media-control-hint">{t('h3more.copy92')}</p>
      {assets.length ? (
        <div className="reference-add">
          <label>
            {t('h3more.copy93')}
            <select
              aria-label={t('h3more.copy93')}
              value={assetId}
              onChange={(e) => selectAsset(e.target.value)}
            >
              <option value="">{t('h3more.copy94')}</option>
              {assets.map((a) => (
                <option key={a.id} value={a.id}>
                  {
                    {
                      image: t('h3more.copy1'),
                      video: t('h3more.copy2'),
                      audio: t('h3more.copy3'),
                    }[a.type]
                  }{' '}
                  · {a.title}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t('h3more.copy95')}
            <select
              aria-label={t('h3more.copy95')}
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              {(uses[asset?.type] || ['reference']).map((r) => (
                <option key={r} value={r}>
                  {t('story.role.' + r)}
                </option>
              ))}
            </select>
          </label>
          <button type="button" onClick={attach}>
            {t('h3more.copy96')}
          </button>
        </div>
      ) : (
        <p className="media-control-empty">{t('h3more.copy97')}</p>
      )}
      {error && (
        <p className="media-control-error" role="alert">
          {error}
        </p>
      )}
      <div className="reference-list">
        {links.map((link) => (
          <ReferenceCard
            key={link.id}
            entity={assets.find((a) => a.id === link.source)}
            link={link}
            shot={shot}
            onInspect={onInspect}
          />
        ))}
      </div>
      {!!links.length && <p className="media-control-hint">{t('h3more.copy98')}</p>}
    </section>
  );
}
