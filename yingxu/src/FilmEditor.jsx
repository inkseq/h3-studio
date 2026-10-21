import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useCallback, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Player } from '@remotion/player';
import { AbsoluteFill, Html5Video, Sequence } from 'remotion';
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core';
import { SortableContext, horizontalListSortingStrategy, useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import {
  Check,
  CloudUpload,
  Eye,
  EyeOff,
  GripVertical,
  Mic,
  Pause,
  Play,
  Scissors,
  X,
} from 'lucide-react';
import { store } from './store.js';
import { cloudController as cloud } from './cloud-controller.js';
import { useStoryboardMediaUrl } from './StoryboardMedia.jsx';
import { cutFps } from './video-cut-model.js';
import { captionTrack, orderedCues } from './caption-model.js';
import {
  cutFrames,
  editorClips,
  moveShot,
  setIncluded,
  snap,
  trimClip,
} from './film-edit-model.js';
import './film-editor.css';

const SIZES = { '9:16': [720, 1280], '16:9': [1280, 720], '1:1': [960, 960], '4:3': [960, 720] };
const PX_PER_SECOND = 56;

// The whole cut as one Remotion composition: each included clip is a Sequence that plays
// its source from the trimmed in-point, so the preview matches the exporter frame for frame.
function Cut({ clips, sources, cues = [] }) {
  return (
    <AbsoluteFill style={{ background: '#000' }}>
      {clips.map((clip) => (
        <Sequence key={clip.id} from={clip.from} durationInFrames={clip.frames}>
          {sources[clip.asset?.id] ? (
            <Html5Video
              src={sources[clip.asset.id]}
              trimBefore={clip.startFrame}
              style={{ width: '100%', height: '100%', objectFit: 'contain' }}
            />
          ) : (
            <AbsoluteFill
              style={{
                alignItems: 'center',
                justifyContent: 'center',
                color: '#9aa39c',
                fontSize: 40,
              }}
            >
              {clip.title}
            </AbsoluteFill>
          )}
        </Sequence>
      ))}
      {cues.map((cue) => (
        <Sequence key={'cue-' + cue.id} from={cue.from} durationInFrames={cue.frames}>
          <AbsoluteFill
            style={{ justifyContent: 'flex-end', alignItems: 'center', paddingBottom: '7%' }}
          >
            <span
              style={{
                maxWidth: '86%',
                padding: '0.2em 0.5em',
                borderRadius: 6,
                background: 'rgba(0,0,0,.55)',
                color: '#fff',
                fontSize: 44,
                lineHeight: 1.3,
                textAlign: 'center',
                whiteSpace: 'pre-line',
              }}
            >
              {cue.text}
            </span>
          </AbsoluteFill>
        </Sequence>
      ))}
    </AbsoluteFill>
  );
}

// Resolves a clip's playable URL (local file or cloud content) and, when the story does not
// know the source length yet, reads it from the video's metadata.
function ClipSource({ asset, needsDuration, onUrl, onDuration }) {
  const { playbackUrl } = useStoryboardMediaUrl({ entity: asset, fileId: asset.data.fileId });
  React.useEffect(() => {
    if (playbackUrl) onUrl(asset.id, playbackUrl);
  }, [asset.id, playbackUrl]);
  return needsDuration && playbackUrl ? (
    <video
      hidden
      preload="metadata"
      src={playbackUrl}
      onLoadedMetadata={(event) => onDuration(asset.id, event.currentTarget.duration)}
    />
  ) : null;
}

// Cloud clips show their thumbnail rendition; local files have none, so they keep a plain tile.
function ClipThumb({ asset }) {
  const { url, thumbnailImage } = useStoryboardMediaUrl(
    { entity: asset, fileId: asset.data.fileId },
    { thumbnail: true },
  );
  return url && thumbnailImage ? (
    <img className="film-clip-thumb" src={url} alt="" loading="lazy" />
  ) : null;
}

function TrimHandle({ side, disabled, title, onDrag, onDone }) {
  const start = useRef(null);
  return (
    <span
      className={'film-trim film-trim-' + side + (disabled ? ' off' : '')}
      title={title}
      role="separator"
      aria-label={title}
      onPointerDown={(event) => {
        if (disabled) return;
        event.stopPropagation();
        event.currentTarget.setPointerCapture(event.pointerId);
        start.current = event.clientX;
      }}
      onPointerMove={(event) => {
        if (start.current !== null) onDrag(event.clientX - start.current);
      }}
      onPointerUp={(event) => {
        if (start.current === null) return;
        const dx = event.clientX - start.current;
        start.current = null;
        onDone(dx);
      }}
      onPointerCancel={() => {
        start.current = null;
        onDrag(0);
      }}
    />
  );
}

const pct = (value, total) => (total > 0 ? (value / total) * 100 : 0) + '%';
const sec = (value) => value.toFixed(2);

// A large single-clip trimmer: the whole source on one wide bar with start/end handles,
// its own video, and frame-accurate keys. Nothing is written until Apply.
function ClipTrimmer({ clip, url, source, onApply, onCancel }) {
  const { t } = useI18n();
  const video = useRef(null),
    bar = useRef(null),
    box = useRef(null);
  const [probed, setProbed] = useState(null);
  const total = Number.isFinite(source) ? source : probed;
  const [range, setRange] = useState([
    clip.startFrame / cutFps,
    (clip.startFrame + clip.frames) / cutFps,
  ]);
  const [now, setNow] = useState(range[0]),
    [playing, setPlaying] = useState(false);
  const [inPoint, outPoint] = range,
    frame = 1 / cutFps;
  React.useEffect(() => box.current?.focus(), []);
  const clampIn = (value) => (old) => [Math.min(Math.max(0, snap(value)), old[1] - frame), old[1]];
  const clampOut = (value) => (old) => [
    old[0],
    Math.max(old[0] + frame, Math.min(total ?? value, snap(value))),
  ];
  const seekTo = (value) => {
    const v = Math.min(Math.max(0, value), total ?? value);
    if (video.current) video.current.currentTime = v;
    setNow(v);
  };
  const at = (clientX) => {
    const r = bar.current.getBoundingClientRect();
    return snap(Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * (total || 0));
  };
  const dragHandle = (side) => (event) => {
    if (!total) return;
    event.preventDefault();
    event.stopPropagation();
    const el = event.currentTarget;
    el.setPointerCapture(event.pointerId);
    video.current?.pause();
    const move = (e) => {
      const v = at(e.clientX);
      setRange(side === 'in' ? clampIn(v) : clampOut(v));
      seekTo(v);
    };
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  };
  const toggle = () => {
    const node = video.current;
    if (!node) return;
    if (!node.paused) return node.pause();
    if (node.currentTime < inPoint || node.currentTime >= outPoint - frame / 2)
      node.currentTime = inPoint;
    node.play().catch(() => {});
  };
  const onKey = (event) => {
    if (event.target.closest('button')) return;
    const key = event.key.toLowerCase();
    if (key === ' ') toggle();
    else if (key === 'i') setRange(clampIn(now));
    else if (key === 'o') setRange(clampOut(now));
    else if (key === 'arrowleft') seekTo(snap(now - frame));
    else if (key === 'arrowright') seekTo(snap(now + frame));
    else return;
    event.preventDefault();
  };
  return (
    <div className="clip-trimmer" ref={box} tabIndex={-1} onKeyDown={onKey}>
      <div className="clip-trimmer-head">
        <b>{t('editor.trimTitle', { title: clip.index + 1 + '. ' + clip.title })}</b>
        <small className="muted">{t('editor.trimKeys')}</small>
      </div>
      <div className="clip-trimmer-video">
        <video
          ref={video}
          src={url}
          preload="auto"
          playsInline
          onClick={toggle}
          onLoadedMetadata={(event) => {
            setProbed(event.currentTarget.duration);
            event.currentTarget.currentTime = inPoint;
          }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onTimeUpdate={(event) => {
            const node = event.currentTarget;
            setNow(node.currentTime);
            if (!node.paused && node.currentTime >= outPoint) {
              node.pause();
              node.currentTime = outPoint;
            }
          }}
        />
      </div>
      <div
        className={'clip-trimmer-bar' + (total ? '' : ' off')}
        ref={bar}
        onPointerDown={(event) => total && seekTo(at(event.clientX))}
      >
        <span className="clip-trimmer-cut" style={{ left: 0, width: pct(inPoint, total) }} />
        <span className="clip-trimmer-cut" style={{ left: pct(outPoint, total), right: 0 }} />
        <span
          className="clip-trimmer-keep"
          style={{ left: pct(inPoint, total), width: pct(outPoint - inPoint, total) }}
        />
        <span className="clip-trimmer-playhead" style={{ left: pct(now, total) }} />
        {['in', 'out'].map((side) => (
          <span
            key={side}
            role="slider"
            tabIndex={0}
            aria-label={t(side === 'in' ? 'editor.trimHandleIn' : 'editor.trimHandleOut')}
            aria-valuemin={0}
            aria-valuemax={total || 0}
            aria-valuenow={side === 'in' ? inPoint : outPoint}
            className={'clip-trimmer-handle ' + side}
            style={{ left: pct(side === 'in' ? inPoint : outPoint, total) }}
            onPointerDown={dragHandle(side)}
            onKeyDown={(event) => {
              const step =
                event.key === 'ArrowLeft' ? -frame : event.key === 'ArrowRight' ? frame : 0;
              if (!step) return;
              event.preventDefault();
              event.stopPropagation();
              const v = (side === 'in' ? inPoint : outPoint) + step;
              setRange(side === 'in' ? clampIn(v) : clampOut(v));
              seekTo(v);
            }}
          />
        ))}
      </div>
      <p className="clip-trimmer-times muted">
        {total
          ? t('editor.trimTimes', {
              in: sec(inPoint),
              out: sec(outPoint),
              len: sec(outPoint - inPoint),
              total: sec(total),
            })
          : t('editor.loading')}
      </p>
      <div className="clip-trimmer-actions">
        <button className="small" disabled={!total} onClick={toggle}>
          {playing ? <Pause size={14} /> : <Play size={14} />}
          {t(playing ? 'editor.pause' : 'editor.playRange')}
        </button>
        <button className="small" disabled={!total} onClick={() => setRange(clampIn(now))}>
          [ {t('editor.setIn')}
        </button>
        <button className="small" disabled={!total} onClick={() => setRange(clampOut(now))}>
          {t('editor.setOut')} ]
        </button>
        <button
          className="small ghost"
          disabled={!total}
          onClick={() => setRange([0, snap(total)])}
        >
          {t('editor.wholeClip')}
        </button>
        <span className="clip-trimmer-spacer" />
        <button className="small ghost" onClick={onCancel}>
          {t('editor.cancel')}
        </button>
        <button
          className="small primary"
          disabled={!total}
          onClick={() => onApply(inPoint, outPoint, total)}
        >
          {t('editor.apply')}
        </button>
      </div>
    </div>
  );
}

function ClipCard({ clip, duration, active, busy, onSeek, onToggle, onTrim, onFineTrim }) {
  const { t } = useI18n();
  const sortable = useSortable({ id: clip.id, disabled: busy });
  const [drag, setDrag] = useState({ side: null, dx: 0 });
  const source = clip.sourceSeconds ?? duration ?? null,
    start = clip.startFrame / cutFps,
    end = (clip.startFrame + clip.frames) / cutFps;
  // Live trim while dragging, clamped to the source and at least one frame.
  const range = (side, dx) => {
    const shift = dx / PX_PER_SECOND;
    if (side === 'start')
      return [Math.min(Math.max(0, snap(start + shift)), end - 1 / cutFps), end];
    return [start, Math.max(start + 1 / cutFps, Math.min(source, snap(end + shift)))];
  };
  const [liveStart, liveEnd] = drag.side ? range(drag.side, drag.dx) : [start, end];
  const width = Math.max(88, (liveEnd - liveStart) * PX_PER_SECOND);
  const canTrim = !!clip.asset && Number.isFinite(source) && !busy;
  return (
    <div
      ref={sortable.setNodeRef}
      className={
        'film-clip' +
        (clip.included ? '' : ' excluded') +
        (active ? ' active' : '') +
        (sortable.isDragging ? ' dragging' : '')
      }
      style={{
        width,
        transform: CSS.Translate.toString(sortable.transform),
        transition: sortable.transition,
      }}
      {...sortable.attributes}
      {...sortable.listeners}
    >
      <TrimHandle
        side="start"
        disabled={!canTrim}
        title={canTrim ? t('editor.trimStart') : t('editor.trimUnknown')}
        onDrag={(dx) => setDrag({ side: 'start', dx })}
        onDone={(dx) => {
          setDrag({ side: null, dx: 0 });
          const [s, e] = range('start', dx);
          if (s !== start) onTrim(clip, s, e, source);
        }}
      />
      {clip.asset && <ClipThumb asset={clip.asset} />}
      <button type="button" className="film-clip-body" onClick={() => onSeek(clip)}>
        <span className="film-clip-grip" aria-hidden="true">
          <GripVertical size={13} />
        </span>
        <b>
          {clip.index + 1}. {clip.title}
        </b>
        <small>
          {clip.asset ? (liveEnd - liveStart).toFixed(1) + 's' : t('editor.noVideo')}
          {!clip.included && ' · ' + t('editor.excluded')}
        </small>
      </button>
      <button
        type="button"
        className="film-clip-toggle"
        disabled={busy}
        title={t(clip.included ? 'editor.exclude' : 'editor.include')}
        aria-label={t(clip.included ? 'editor.exclude' : 'editor.include')}
        onClick={() => onToggle(clip)}
      >
        {clip.included ? <Eye size={13} /> : <EyeOff size={13} />}
      </button>
      <button
        type="button"
        className="film-clip-scissors"
        disabled={busy || !clip.playable}
        title={clip.playable ? t('editor.trim') : t('editor.trimNeedsVideo')}
        aria-label={t('editor.trim')}
        onClick={() => onFineTrim(clip)}
      >
        <Scissors size={13} />
      </button>
      <TrimHandle
        side="end"
        disabled={!canTrim}
        title={canTrim ? t('editor.trimEnd') : t('editor.trimUnknown')}
        onDrag={(dx) => setDrag({ side: 'end', dx })}
        onDone={(dx) => {
          setDrag({ side: null, dx: 0 });
          const [s, e] = range('end', dx);
          if (e !== end) onTrim(clip, s, e, source);
        }}
      />
    </div>
  );
}

export default function FilmEditor({ chapterId, initialTrim = null, onClose }) {
  const { t } = useI18n();
  const s = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    project = s.project;
  const chapter = project.entities.find((e) => e.id === chapterId);
  const clips = useMemo(() => editorClips(project, chapterId), [project, chapterId]);
  const included = clips.filter((clip) => clip.included),
    total = cutFrames(clips);
  const [sources, setSources] = useState({}),
    [durations, setDurations] = useState({}),
    [error, setError] = useState(''),
    [current, setCurrent] = useState(null),
    [trimming, setTrimming] = useState(initialTrim);
  const player = useRef(null);
  const trimTarget = clips.find((clip) => clip.id === trimming && clip.playable);
  // The overlay owns scrolling while open; the storyboard behind it must not move.
  React.useEffect(() => {
    const old = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = old;
    };
  }, []);
  const [captions, setCaptions] = useState(true);
  const cues = useMemo(
    () =>
      orderedCues(captionTrack(project, chapterId).cues)
        .map((cue) => {
          const from = Math.ceil(Number(cue.start) * cutFps - 1e-7),
            to = Math.floor(Number(cue.end) * cutFps + 1e-7);
          return { id: cue.id, text: String(cue.text || ''), from, frames: to - from };
        })
        .filter((cue) => cue.text && Number.isFinite(cue.from) && cue.frames > 0),
    [project, chapterId],
  );
  // Follow the playhead: highlight the clip that is on screen.
  React.useEffect(() => {
    const node = player.current;
    if (!node) return;
    const onFrame = (event) => {
      const frame = event.detail.frame,
        clip = included.find((c) => frame >= c.from && frame < c.from + c.frames);
      if (clip) setCurrent((old) => (old === clip.id ? old : clip.id));
    };
    node.addEventListener('frameupdate', onFrame);
    return () => node.removeEventListener('frameupdate', onFrame);
  }, [included]);
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }));
  const busy = !!s.busy;
  const cloudStory = s.workspace?.mode === 'cloud',
    dirty = !!s.workspace?.dirty,
    [saving, setSaving] = useState(false);
  const saveCloud = async () => {
    setSaving(true);
    setError('');
    try {
      await cloud.save();
    } catch (e) {
      setError(e?.message || t('editor.saveFailed'));
    } finally {
      setSaving(false);
    }
  };
  const aspect = project.journey?.delivery?.aspect || project.journey?.brief?.aspect || '16:9',
    [width, height] = SIZES[aspect] || SIZES['16:9'];
  const onUrl = useCallback(
    (id, url) => setSources((old) => (old[id] === url ? old : { ...old, [id]: url })),
    [],
  );
  const onDuration = useCallback(
    (id, value) =>
      Number.isFinite(value) &&
      setDurations((old) => (old[id] === value ? old : { ...old, [id]: value })),
    [],
  );
  const edit = (fn) => {
    setError('');
    let failure = null;
    const ok = store.editProject((p) => {
      try {
        fn(p);
      } catch (e) {
        failure = e;
        return false;
      }
    });
    if (failure)
      setError(
        String(failure.message).startsWith('editor.') ? t(failure.message) : failure.message,
      );
    else if (!ok) setError(t('editor.saveFailed'));
  };
  const seek = (clip) => {
    setCurrent(clip.id);
    if (clip.included && Number.isInteger(clip.from)) player.current?.seekTo(clip.from);
  };
  const assets = [...new Map(clips.filter((c) => c.playable).map((c) => [c.asset.id, c])).values()];
  return (
    <div
      className="film-editor"
      role="dialog"
      aria-modal="true"
      aria-label={t('editor.title', { chapter: chapter?.title || '' })}
    >
      <header className="film-editor-head">
        <div>
          <h2>{t('editor.title', { chapter: chapter?.title || '' })}</h2>
          <small className="muted">
            {t('editor.total', { seconds: (total / cutFps).toFixed(1), count: included.length })}
          </small>
          {cues.length > 0 && (
            <label className="film-editor-captions">
              <input
                type="checkbox"
                checked={captions}
                onChange={(e) => setCaptions(e.target.checked)}
              />
              {t('editor.captions', { count: cues.length })}
            </label>
          )}
        </div>
        {cloudStory &&
          (dirty ? (
            <button className="primary small" disabled={saving || busy} onClick={saveCloud}>
              <CloudUpload size={14} />
              {t(saving ? 'editor.saving' : 'editor.save')}
            </button>
          ) : (
            <span className="film-editor-saved muted">
              <Check size={13} />
              {t('editor.saved')}
            </span>
          ))}
        <button
          className="ghost round"
          aria-label={t('editor.close')}
          title={t('editor.close')}
          onClick={onClose}
        >
          <X size={18} />
        </button>
      </header>
      {assets.map((clip) => (
        <ClipSource
          key={clip.asset.id}
          asset={clip.asset}
          needsDuration={!Number.isFinite(clip.sourceSeconds)}
          onUrl={onUrl}
          onDuration={onDuration}
        />
      ))}
      {!clips.length ? (
        <p className="film-editor-empty">{t('editor.empty')}</p>
      ) : (
        <>
          <div className={'film-editor-stage' + (trimTarget ? ' trimming' : '')}>
            {trimTarget && sources[trimTarget.asset.id] ? (
              <ClipTrimmer
                key={trimTarget.id}
                clip={trimTarget}
                url={sources[trimTarget.asset.id]}
                source={trimTarget.sourceSeconds ?? durations[trimTarget.asset.id]}
                onCancel={() => setTrimming(null)}
                onApply={(start, end, source) => {
                  edit((p) => trimClip(p, trimTarget.id, start, end, source));
                  setTrimming(null);
                }}
              />
            ) : trimTarget ? (
              <p className="muted">{t('editor.loading')}</p>
            ) : (
              <Player
                ref={player}
                component={Cut}
                inputProps={{ clips: included, sources, cues: captions ? cues : [] }}
                durationInFrames={Math.max(1, total)}
                fps={cutFps}
                compositionWidth={width}
                compositionHeight={height}
                controls
                style={{ width: '100%', height: '100%' }}
              />
            )}
          </div>
          <p className="film-editor-hint muted">{t('editor.dragHint')}</p>
          {error && (
            <p className="qc-error" role="alert">
              {error}
            </p>
          )}
          <div className="film-editor-tracks">
            <DndContext
              sensors={sensors}
              collisionDetection={closestCenter}
              onDragEnd={({ active, over }) => {
                if (!over || active.id === over.id) return;
                const to = clips.findIndex((clip) => clip.id === over.id);
                edit((p) => moveShot(p, chapterId, active.id, to));
              }}
            >
              <SortableContext
                items={clips.map((clip) => clip.id)}
                strategy={horizontalListSortingStrategy}
              >
                <div className="film-track">
                  {clips.map((clip) => (
                    <ClipCard
                      key={clip.id}
                      clip={clip}
                      duration={clip.asset ? durations[clip.asset.id] : null}
                      active={current === clip.id}
                      busy={busy}
                      onSeek={seek}
                      onToggle={(c) => edit((p) => setIncluded(p, chapterId, c.id, !c.included))}
                      onTrim={(c, start, end, source) =>
                        edit((p) => trimClip(p, c.id, start, end, source))
                      }
                      onFineTrim={(c) => {
                        player.current?.pause();
                        setCurrent(c.id);
                        setTrimming(c.id);
                      }}
                    />
                  ))}
                </div>
              </SortableContext>
            </DndContext>
            <div className="film-track film-track-voices" aria-disabled="true">
              <span className="film-track-label">
                <Mic size={13} />
                {t('editor.voices')}
              </span>
              <span className="film-voices-soon">{t('editor.voicesSoon')}</span>
            </div>
          </div>
          <footer className="film-editor-foot muted">
            <span>{t('editor.exportHint')}</span>
            {cloudStory && <span>{t('editor.exportSaveHint')}</span>}
            {clips.some((clip) => !clip.included) && <span>{t('editor.excludedPending')}</span>}
          </footer>
        </>
      )}
    </div>
  );
}
