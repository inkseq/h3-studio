import { legacyReason } from './i18n/legacyReasons.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React from 'react';
import { generatedTrackState, unlinkGeneratedSound } from './generated-sound-model.js';
import SelectedVideoCuts from './SelectedVideoCuts.jsx';
import { videoCut } from './video-cut-model.js';
import { store } from './store.js';
import { useLocalMedia, LocalRangePreview, RangeFields } from './media-controls.jsx';
import {
  validateRange,
  chapterTiming,
  trackPlacement,
  trackAtTime,
  secondsLabel,
} from './media-timing.js';
import { storyboardUrl } from './storyboard.js';
import './reference-editor.css';
import './sound-studio.css';

const soundRoleNames = (lt) => ({
  dialogue: lt('sound.ui13'),
  music: lt('sound.ui34'),
  ambience: lt('sound.ui26'),
  generated: lt('sound.ui10'),
});
const show = (value, lt) => (Number.isFinite(value) ? Number(value.toFixed(4)) : lt('sound.ui22'));
function GeneratedTrackForm({ track, asset, onSave, onCancel }) {
  const { t: lt, locale } = useI18n();
  const roleNames = soundRoleNames(lt);
  const [gain, setGain] = React.useState(String(track.gain ?? 0.7)),
    [error, setError] = React.useState('');
  return (
    <form
      className="sound-track-form"
      onSubmit={(e) => {
        e.preventDefault();
        const value = Number(gain);
        if (gain === '' || !Number.isFinite(value) || value < 0 || value > 1) {
          setError(lt('sound.ui37'));
          return;
        }
        const { generatedIssue, ...saved } = track;
        onSave({ ...saved, gain: value });
      }}
    >
      <p>{lt('sound.linkedRange', { start: show(track.start, lt), end: show(track.end, lt) })}</p>
      <label>
        {lt('legacy.panel34')}
        <input
          aria-label={lt('legacy.panel54')}
          type="number"
          min="0"
          max="1"
          step="0.05"
          value={gain}
          onChange={(e) => setGain(e.target.value)}
        />
      </label>
      {asset && !track.generatedIssue && (
        <LocalRangePreview
          entity={asset}
          start={track.start}
          end={track.end}
          duration={track.duration}
        />
      )}
      <div className="media-control-row">
        <button type="submit">{lt('legacy.panel35')}</button>
        <button type="button" onClick={onCancel}>
          {lt('legacy.panel36')}
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}
function TrackForm({ track, assets, timeline, onSave, onCancel }) {
  const { t: lt, locale } = useI18n();
  const roleNames = soundRoleNames(lt);
  const [assetId, setAssetId] = React.useState(track?.assetId || ''),
    [role, setRole] = React.useState(track?.role || 'dialogue'),
    [shotId, setShotId] = React.useState(track?.shotId || ''),
    [offset, setOffset] = React.useState(String(track?.offset || 0)),
    [start, setStart] = React.useState(String(track?.start || 0)),
    [end, setEnd] = React.useState(track?.end === undefined ? '' : String(track.end)),
    [gain, setGain] = React.useState(String(track?.gain ?? 0.7)),
    [muted, setMuted] = React.useState(!!track?.muted),
    [duration, setDuration] = React.useState(null),
    [error, setError] = React.useState('');
  const asset = assets.find((a) => a.id === assetId),
    total = timeline.at(-1)?.end || 0;
  const metadata = (d) => {
    setDuration(d);
    setEnd((v) => (v === '' ? String(Number(d.toFixed(3))) : v));
  };
  const submit = (e) => {
    e.preventDefault();
    if (!asset) {
      setError(lt('sound.ui30'));
      return;
    }
    const invalid = validateRange(start, end, duration, locale);
    if (invalid) {
      setError(invalid);
      return;
    }
    if (offset === '' || !Number.isFinite(Number(offset)) || Number(offset) < 0) {
      setError(lt('sound.ui16'));
      return;
    }
    if (gain === '' || !Number.isFinite(Number(gain)) || Number(gain) < 0 || Number(gain) > 1) {
      setError(lt('sound.ui36'));
      return;
    }
    const next = {
        id: track?.id || `sound-${crypto.randomUUID()}`,
        assetId,
        role,
        shotId,
        offset: Number(offset),
        start: Number(start),
        end: Number(end),
        gain: Number(gain),
        muted,
        duration,
        fileId: asset.data.fileId,
      },
      placement = trackPlacement(next, timeline),
      shot = timeline.find((s) => s.id === shotId);
    if (!placement) {
      setError(lt('sound.ui6'));
      return;
    }
    if (shot && Number(offset) >= shot.end - shot.start) {
      setError(lt('sound.ui5'));
      return;
    }
    if (placement.start >= total) {
      setError(lt('sound.ui12'));
      return;
    }
    if (onSave(next) !== false) setError('');
  };
  return (
    <form noValidate className="sound-track-form" onSubmit={submit}>
      <div className="sound-fields">
        <label>
          {lt('legacy.panel37')}
          <select
            aria-label={lt('legacy.panel37')}
            value={role}
            onChange={(e) => setRole(e.target.value)}
          >
            {Object.entries(roleNames)
              .filter(([key]) => key !== 'generated')
              .map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
          </select>
        </label>
        <label>
          {lt('legacy.panel38')}
          <select
            aria-label={lt('legacy.panel38')}
            value={assetId}
            onChange={(e) => {
              setAssetId(e.target.value);
              setStart('0');
              setEnd('');
              setDuration(null);
              setError('');
            }}
          >
            <option value="">{lt('legacy.panel39')}</option>
            {assets.map((a) => (
              <option key={a.id} value={a.id}>
                {a.title}
              </option>
            ))}
          </select>
        </label>
      </div>
      {asset && (
        <>
          <LocalRangePreview
            key={asset.data.fileId || asset.id}
            entity={asset}
            start={start}
            end={end}
            duration={duration}
            onDuration={metadata}
          />
          <RangeFields
            start={start}
            end={end}
            setStart={setStart}
            setEnd={setEnd}
            duration={duration}
            prefix={lt('sound.source')}
          />
        </>
      )}
      <div className="sound-fields">
        <label>
          {lt('legacy.panel40')}
          <select
            aria-label={lt('legacy.panel55')}
            value={shotId}
            onChange={(e) => setShotId(e.target.value)}
          >
            <option value="">{lt('legacy.panel41')}</option>
            {track?.shotId && !timeline.some((s) => s.id === track.shotId) && (
              <option value={track.shotId}>{lt('legacy.panel42')}</option>
            )}
            {timeline.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title} · {secondsLabel(s.start)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {lt('legacy.panel43')}
          <input
            aria-label={lt('legacy.panel56')}
            type="number"
            min="0"
            step="0.1"
            value={offset}
            onChange={(e) => setOffset(e.target.value)}
          />
        </label>
        <label>
          {lt('legacy.panel44')}
          <input
            aria-label={lt('legacy.panel57')}
            type="number"
            min="0"
            max="1"
            step="0.05"
            value={gain}
            onChange={(e) => setGain(e.target.value)}
          />
        </label>
        <label className="sound-check">
          <input type="checkbox" checked={muted} onChange={(e) => setMuted(e.target.checked)} />
          {lt('legacy.panel45')}
        </label>
      </div>
      <p className="media-control-hint">{lt('legacy.explain13')}</p>
      {error && (
        <p role="alert" className="media-control-error">
          {error}
        </p>
      )}
      <div className="media-control-row">
        <button type="submit" className="sound-primary">
          {track ? lt('sound.ui4') : lt('sound.ui24')}
        </button>
        <button type="button" onClick={onCancel}>
          {lt('legacy.panel36')}
        </button>
      </div>
    </form>
  );
}

function TimelineAudio({ track, entity, register, onProblem }) {
  const { t: lt, locale } = useI18n();
  const roleNames = soundRoleNames(lt);
  const { url, error } = useLocalMedia(entity),
    ref = React.useRef(null);
  React.useEffect(() => {
    const el = ref.current;
    if (url && el) register(track.id, el);
    return () => {
      el?.pause();
      register(track.id, null);
    };
  }, [url, track.id]);
  React.useEffect(() => {
    if (error && !track.muted) onProblem(`${entity?.title || lt('sound.ui38')}：${error}`);
  }, [error, track.muted]);
  return url ? (
    <audio
      ref={ref}
      src={url}
      preload="auto"
      onError={() => onProblem(lt('sound.playError', { title: entity?.title || lt('sound.ui38') }))}
    />
  ) : null;
}
function TimelineFrame({ shot, project, time }) {
  const { t: lt, locale } = useI18n();
  const roleNames = soundRoleNames(lt);
  const asset = project.entities.find((e) => e.id === shot?.data.selectedAssetId),
    { url } = useLocalMedia(asset),
    ref = React.useRef(null),
    choice = shot?.data.uxReview?.chosen,
    cut = videoCut(shot, asset);
  React.useEffect(() => {
    const el = ref.current;
    if (el && Number.isFinite(el.duration))
      el.currentTime = Math.min(
        Math.max(0, (cut.sourceStart || 0) + time),
        Math.max(0, el.duration - 0.01),
      );
  }, [time, url, cut.sourceStart]);
  if (asset?.type === 'image' && url)
    return <img src={url} alt={lt('sound.currentShot', { title: shot.title })} />;
  if (asset?.type === 'video' && shot?.data.selectedVideoRange && cut.issue)
    return (
      <div className="sound-storyboard-placeholder">
        <strong>{shot.title}</strong>
        <p>{legacyReason(cut.issue, locale)}</p>
      </div>
    );
  if (asset?.type === 'video' && url)
    return (
      <video
        ref={ref}
        src={url}
        muted
        playsInline
        preload="auto"
        onLoadedMetadata={(e) => {
          const el = e.currentTarget;
          el.currentTime = Math.min(
            Math.max(0, (cut.sourceStart || 0) + time),
            Math.max(0, el.duration - 0.01),
          );
        }}
      />
    );
  if (choice)
    return (
      <>
        <img src={storyboardUrl(shot, choice)} alt={lt('sound.example', { choice })} />
        <span>{lt('legacy.panel46')}</span>
      </>
    );
  return (
    <div className="sound-storyboard-placeholder">
      <strong>{shot?.title || lt('sound.ui32')}</strong>
      <p>{shot?.description || lt('sound.ui25')}</p>
      <span>{lt('legacy.panel47')}</span>
    </div>
  );
}

export default function SoundStudio({ project, chapterId, shots = [] }) {
  const { t: lt, locale } = useI18n();
  const roleNames = soundRoleNames(lt);
  const tracks = (project.journey?.soundTracks?.[chapterId] || []).map((track) => {
      const state = generatedTrackState(project, track);
      return { ...state.track, generatedIssue: state.issue };
    }),
    assets = project.entities.filter((e) => e.type === 'audio'),
    timeline = chapterTiming(shots),
    total = timeline.at(-1)?.end || 0,
    silent = project.journey?.sound?.mode === 'silent';
  const [editing, setEditing] = React.useState(null),
    [adding, setAdding] = React.useState(false),
    [cursor, setCursor] = React.useState(0),
    [playing, setPlaying] = React.useState(false),
    [error, setError] = React.useState('');
  const audios = React.useRef(new Map()),
    frame = React.useRef(0),
    playRef = React.useRef(false),
    cursorRef = React.useRef(0),
    configRef = React.useRef({ tracks, timeline, total, silent }),
    epoch = React.useRef(0),
    base = React.useRef(0),
    requests = React.useRef(new Set());
  configRef.current = { tracks, timeline, total, silent };
  const pause = React.useCallback(() => {
    playRef.current = false;
    setPlaying(false);
    cancelAnimationFrame(frame.current);
    audios.current.forEach((el) => el.pause());
    requests.current.clear();
  }, []);
  const move = React.useCallback((value) => {
    const next = Math.max(0, Math.min(configRef.current.total, value));
    cursorRef.current = next;
    setCursor(next);
    base.current = next;
    epoch.current = performance.now();
  }, []);
  React.useEffect(() => {
    pause();
    move(0);
    setEditing(null);
    setAdding(false);
    setError('');
    return () => {
      playRef.current = false;
      cancelAnimationFrame(frame.current);
      audios.current.forEach((el) => el.pause());
    };
  }, [chapterId]);
  const trackKey = JSON.stringify(tracks),
    shotKey = JSON.stringify(
      timeline.map((s) => [
        s.id,
        s.start,
        s.end,
        s.shot.data.selectedAssetId,
        s.shot.data.selectedVideoRange,
      ]),
    );
  React.useEffect(() => {
    pause();
    setError('');
  }, [trackKey, shotKey, silent]);
  React.useEffect(() => {
    const stop = () => pause();
    window.addEventListener('yingxu-stop-local-media', stop);
    return () => {
      window.removeEventListener('yingxu-stop-local-media', stop);
      playRef.current = false;
      cancelAnimationFrame(frame.current);
      audios.current.forEach((el) => el.pause());
    };
  }, [pause]);
  const register = React.useCallback((id, el) => {
    if (el) audios.current.set(id, el);
    else audios.current.delete(id);
  }, []);
  const problem = React.useCallback(
    (message) => {
      pause();
      setError(message);
    },
    [pause],
  );
  const sync = (time) => {
    const { tracks: liveTracks, timeline: liveTimeline } = configRef.current;
    for (const track of liveTracks) {
      const el = audios.current.get(track.id),
        sourceTime =
          configRef.current.silent || track.generatedIssue
            ? null
            : trackAtTime(track, liveTimeline, time);
      if (!el) continue;
      if (sourceTime === null) {
        el.pause();
        continue;
      }
      el.volume = track.gain ?? 0.7;
      if (Math.abs(el.currentTime - sourceTime) > 0.2) el.currentTime = sourceTime;
      if (el.paused && !requests.current.has(track.id)) {
        requests.current.add(track.id);
        el.play()
          .then(() => {
            requests.current.delete(track.id);
            if (!playRef.current) el.pause();
          })
          .catch(() => {
            requests.current.delete(track.id);
            problem(lt('sound.ui23'));
          });
      }
    }
  };
  const tick = () => {
    if (!playRef.current) return;
    const time = Math.min(
      configRef.current.total,
      base.current + (performance.now() - epoch.current) / 1000,
    );
    cursorRef.current = time;
    setCursor(time);
    sync(time);
    if (time >= configRef.current.total) {
      pause();
      return;
    }
    frame.current = requestAnimationFrame(tick);
  };
  const toggle = () => {
    if (playing) {
      pause();
      return;
    }
    setError('');
    window.dispatchEvent(new Event('yingxu-stop-local-media'));
    for (const track of tracks.filter((t) => !silent && !t.muted)) {
      if (track.generatedIssue) {
        setError(legacyReason(track.generatedIssue, locale));
        return;
      }
      if (
        !Number.isFinite(track.gain ?? 0.7) ||
        (track.gain ?? 0.7) < 0 ||
        (track.gain ?? 0.7) > 1
      ) {
        setError(lt('sound.ui35'));
        return;
      }
      if (!trackPlacement(track, timeline)) {
        setError(lt('sound.ui21'));
        return;
      }
      const el = audios.current.get(track.id);
      if (!el || el.readyState < 1) {
        setError(lt('sound.ui20'));
        return;
      }
      const bad = validateRange(track.start, track.end, el.duration, locale);
      if (bad) {
        setError(lt('sound.rangeError', { reason: bad }));
        return;
      }
    }
    if (cursorRef.current >= total) move(0);
    base.current = cursorRef.current;
    epoch.current = performance.now();
    playRef.current = true;
    setPlaying(true);
    sync(cursorRef.current);
    frame.current = requestAnimationFrame(tick);
  };
  const changeTracks = (mutator) =>
    store.editProject((p) => {
      p.journey ||= {};
      p.journey.soundTracks ||= {};
      const current = p.journey.soundTracks[chapterId] || [];
      p.journey.soundTracks[chapterId] = mutator(current);
    });
  const save = (next) => {
    const ok = changeTracks((current) =>
      current.some((t) => t.id === next.id)
        ? current.map((t) => (t.id === next.id ? next : t))
        : [...current, next],
    );
    if (ok) {
      setEditing(null);
      setAdding(false);
      setError('');
    }
    return ok;
  };
  const jump = (time) => {
    move(time);
    if (playRef.current) sync(time);
  };
  const current = timeline.find((s) => cursor >= s.start && cursor < s.end) || timeline.at(-1);
  if (!chapterId) return <p className="media-control-empty">{lt('legacy.panel48')}</p>;
  return (
    <>
      <SelectedVideoCuts key={chapterId} project={project} shots={shots} onChange={pause} />
      <section className="sound-studio" aria-label={lt('legacy.panel58')}>
        <div className="sound-heading">
          <div>
            <h3>{lt('legacy.panel49')}</h3>
            <p>{lt('legacy.explain12')}</p>
          </div>
          <button
            type="button"
            disabled={!assets.length || !shots.length}
            onClick={() => {
              pause();
              setAdding(true);
              setEditing(null);
            }}
          >
            {lt('legacy.panel50')}
          </button>
        </div>
        {!assets.length && <p className="media-control-empty">{lt('legacy.explain16')}</p>}
        {silent && !!tracks.length && (
          <p className="media-control-empty">{lt('legacy.explain15')}</p>
        )}
        <div className="sound-track-list">
          {tracks.map((track) => {
            const asset = assets.find((a) => a.id === track.assetId),
              placement = trackPlacement(track, timeline);
            return (
              <article key={track.id} className="sound-track-card" data-testid="sound-track-card">
                <div className="sound-track-title">
                  <strong>
                    {roleNames[track.role] || lt('sound.ui11')} · {asset?.title || lt('sound.ui28')}
                  </strong>
                  <span>
                    {track.muted
                      ? lt('sound.ui15')
                      : lt('sound.gain', { percent: Math.round((track.gain ?? 0.7) * 100) })}
                  </span>
                </div>
                <p>
                  {placement
                    ? lt('sound.placement', {
                        time: secondsLabel(placement.start),
                        start: show(track.start, lt),
                        end: show(track.end, lt),
                      })
                    : lt('sound.ui7')}
                  {placement?.end > total + 1e-6 && lt('sound.ui1')}
                </p>
                {track.generatedFrom && (
                  <p className={track.generatedIssue ? 'cloud-warning' : 'muted'}>
                    {legacyReason(track.generatedIssue, locale) || lt('sound.ui3')}
                    {track.muted && lt('sound.ui2')}
                  </p>
                )}
                {(!asset || !asset.data.fileId || asset.data.missingFile) && (
                  <p className="media-control-error">{lt('legacy.explain14')}</p>
                )}
                <div className="media-control-row">
                  <button
                    type="button"
                    onClick={() => {
                      pause();
                      setEditing(track.id);
                      setAdding(false);
                    }}
                  >
                    {track.generatedFrom ? lt('sound.ui31') : lt('sound.ui29')}
                  </button>
                  {track.generatedFrom && (
                    <button
                      type="button"
                      onClick={() => {
                        pause();
                        if (
                          store.editProject((p) => {
                            unlinkGeneratedSound(p, chapterId, track.id);
                          })
                        ) {
                          setEditing(null);
                          store.notify(lt('sound.ui14'));
                        }
                      }}
                    >
                      {lt('legacy.panel51')}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() =>
                      changeTracks((current) =>
                        current.map((t) => (t.id === track.id ? { ...t, muted: !t.muted } : t)),
                      )
                    }
                  >
                    {track.muted ? lt('sound.ui8') : lt('sound.ui33')}
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      changeTracks((current) => current.filter((t) => t.id !== track.id))
                    }
                  >
                    {lt('legacy.panel52')}
                  </button>
                </div>
                {editing === track.id &&
                  (track.generatedFrom ? (
                    <GeneratedTrackForm
                      key={`${track.id}-${trackKey}`}
                      track={track}
                      asset={asset}
                      onSave={save}
                      onCancel={() => setEditing(null)}
                    />
                  ) : (
                    <TrackForm
                      key={`${track.id}-${trackKey}`}
                      track={track}
                      assets={assets}
                      timeline={timeline}
                      onSave={save}
                      onCancel={() => setEditing(null)}
                    />
                  ))}
                <TimelineAudio
                  track={{ ...track, muted: track.muted || !!track.generatedIssue }}
                  entity={asset}
                  register={register}
                  onProblem={problem}
                />
              </article>
            );
          })}
        </div>
        {adding && (
          <TrackForm
            assets={assets}
            timeline={timeline}
            onSave={save}
            onCancel={() => setAdding(false)}
          />
        )}
        <div className="sound-preview">
          <div className="sound-frame">
            <TimelineFrame
              shot={current?.shot}
              project={project}
              time={cursor - (current?.start || 0)}
            />
          </div>
          <div className="sound-preview-details">
            <strong>{current?.title || lt('sound.ui27')}</strong>
            <output aria-label={lt('legacy.panel59')}>
              {secondsLabel(cursor)} / {secondsLabel(total)}
            </output>
            <input
              type="range"
              aria-label={lt('legacy.panel60')}
              min="0"
              max={total || 1}
              step="0.1"
              value={Math.min(cursor, total)}
              onChange={(e) => jump(Number(e.target.value))}
              disabled={!total}
            />
            <div className="media-control-row">
              <button type="button" onClick={toggle} disabled={!total}>
                {playing ? lt('sound.ui19') : lt('sound.ui18')}
              </button>
              <button
                type="button"
                onClick={() => {
                  pause();
                  move(0);
                }}
              >
                {lt('legacy.panel53')}
              </button>
            </div>
            <p className="media-control-hint">
              {!silent && tracks.some((t) => !t.muted) ? lt('sound.ui9') : lt('sound.ui17')}
              {lt('sound.previewHelp')}
            </p>
          </div>
        </div>
        {!!timeline.length && (
          <div className="sound-shot-jumps" aria-label={lt('legacy.panel61')}>
            {timeline.map((s) => (
              <button
                type="button"
                key={s.id}
                aria-pressed={current?.id === s.id}
                onClick={() => jump(s.start)}
              >
                {s.title}
                <small>{secondsLabel(s.start)}</small>
              </button>
            ))}
          </div>
        )}
        {error && (
          <p role="alert" className="media-control-error">
            {error}
          </p>
        )}
      </section>
    </>
  );
}
