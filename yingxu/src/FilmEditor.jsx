import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useCallback, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import { Player } from '@remotion/player';
import { AbsoluteFill, Html5Audio, Html5Video, Sequence } from 'remotion';
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core';
import { SortableContext, horizontalListSortingStrategy, useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { Eye, EyeOff, Volume2, VolumeX, X } from 'lucide-react';
import { store } from './store.js';
import { useStoryboardMediaUrl } from './StoryboardMedia.jsx';
import { cutFps } from './video-cut-model.js';
import { captionTrack, orderedCues } from './caption-model.js';
import { cutFrames, editorClips, moveShot, setIncluded, trimClip } from './film-edit-model.js';
import {
  addAudioTrack,
  audioBlocks,
  clipSound,
  moveTrack,
  removeTrack,
  setTrack,
  toggleClipSound,
  trimTrack,
} from './film-audio-model.js';
import FilmAudioLane, { timeScale } from './FilmAudioLane.jsx';
import TrimBar from './TrimBar.jsx';
import { CloudSaveStatus } from './CloudAutosave.jsx';
import './film-editor.css';

const SIZES = { '9:16': [720, 1280], '16:9': [1280, 720], '1:1': [960, 960], '4:3': [960, 720] };

// The whole cut as one Remotion composition: each included clip is a Sequence that plays
// its source from the trimmed in-point, so the preview matches the exporter frame for frame.
function Cut({ clips, sources, cues = [], audio = [] }) {
  return (
    <AbsoluteFill style={{ background: '#000' }}>
      {clips.map((clip) => (
        <Sequence key={clip.id} from={clip.from} durationInFrames={clip.frames}>
          {sources[clip.asset?.id] ? (
            <Html5Video
              src={sources[clip.asset.id]}
              trimBefore={clip.startFrame}
              muted
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
      {/* The film's sound is exactly the chapter's sound tracks, as in the export. */}
      {audio.map((a) =>
        sources[a.assetId] ? (
          <Sequence key={'audio-' + a.id} from={a.from} durationInFrames={a.frames}>
            <Html5Audio src={sources[a.assetId]} trimBefore={a.trimBefore} volume={a.volume} />
          </Sequence>
        ) : null,
      )}
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

// A freshly uploaded file's length, read locally before its server metadata exists.
function probeDuration(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file),
      node = new Audio();
    const done = (value) => {
      URL.revokeObjectURL(url);
      resolve(value);
    };
    node.preload = 'metadata';
    node.onloadedmetadata = () => done(node.duration);
    node.onerror = () => done(NaN);
    node.src = url;
  });
}

const MIN_PPS = 6,
  MAX_PPS = 240;
const clock = (seconds) =>
  `${Math.floor(Math.max(0, seconds) / 60)}:${String(Math.floor(Math.max(0, seconds) % 60)).padStart(2, '0')}`;

// Ruler ticks every 1/2/5/10/15/30/60 s, whichever keeps labels at least ~64 px apart.
function Ruler({ seconds, pps, onSeek }) {
  const step = [1, 2, 5, 10, 15, 30, 60, 120].find((s) => s * pps >= 64) || 120;
  const ticks = [];
  for (let t = 0; t <= seconds + 1e-6; t += step) ticks.push(t);
  const drag = useRef(false);
  const timeAt = (event) => {
    const r = event.currentTarget.getBoundingClientRect();
    return Math.max(0, Math.min(seconds, (event.clientX - r.left) / pps));
  };
  return (
    <div
      className="film-ruler"
      style={{ width: seconds * pps }}
      onPointerDown={(event) => {
        drag.current = true;
        event.currentTarget.setPointerCapture(event.pointerId);
        onSeek(timeAt(event));
      }}
      onPointerMove={(event) => drag.current && onSeek(timeAt(event))}
      onPointerUp={() => (drag.current = false)}
      onPointerCancel={() => (drag.current = false)}
    >
      {ticks.map((t) => (
        <span key={t} className="film-tick" style={{ left: t * pps }}>
          {clock(t)}
        </span>
      ))}
    </div>
  );
}

// One included clip on the timeline: width follows its length; drag to reorder, click to select.
function TimelineClip({ clip, pps, selected, playing, busy, sound, onSelect }) {
  const { t } = useI18n();
  const sortable = useSortable({ id: clip.id, disabled: busy });
  const width = Math.max(14, (clip.frames / cutFps) * pps);
  return (
    <div
      ref={sortable.setNodeRef}
      data-clip-id={clip.id}
      className={
        'film-tclip' +
        (clip.playable ? '' : ' empty') +
        (selected ? ' selected' : '') +
        (playing ? ' playing' : '') +
        (sortable.isDragging ? ' dragging' : '')
      }
      style={{
        width,
        transform: CSS.Translate.toString(sortable.transform),
        transition: sortable.transition,
      }}
      title={`${clip.index + 1}. ${clip.title} · ${(clip.frames / cutFps).toFixed(1)}s`}
      onClick={() => onSelect(clip)}
      {...sortable.attributes}
      {...sortable.listeners}
    >
      {clip.asset && <ClipThumb asset={clip.asset} />}
      <span className="film-tclip-label">
        <b>{clip.index + 1}</b>
        {width >= 72 && <span>{clip.title}</span>}
      </span>
      {width >= 40 && sound?.state === 'on' && (
        <Volume2 className="film-tclip-sound" size={12} aria-label={t('editor.clipSoundOn')} />
      )}
    </div>
  );
}

export default function FilmEditor({ chapterId, initialTrim = null, onClose, onUploadAudio }) {
  const { t } = useI18n();
  const s = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    project = s.project;
  const chapter = project.entities.find((e) => e.id === chapterId);
  const clips = useMemo(() => editorClips(project, chapterId), [project, chapterId]);
  const included = clips.filter((clip) => clip.included),
    excluded = clips.filter((clip) => !clip.included),
    total = cutFrames(clips),
    seconds = total / cutFps;
  const [sources, setSources] = useState({}),
    [durations, setDurations] = useState({}),
    [error, setError] = useState(''),
    [selectedId, setSelectedId] = useState(initialTrim || clips[0]?.id || null),
    [frame, setFrame] = useState(0),
    [zoom, setZoom] = useState(null),
    [fitWidth, setFitWidth] = useState(800),
    [captions, setCaptions] = useState(true),
    [selectedAudio, setSelectedAudio] = useState(null);
  const player = useRef(null),
    scroller = useRef(null),
    sourceVideo = useRef(null);
  const selected = clips.find((clip) => clip.id === selectedId) || clips[0] || null;
  // The overlay owns scrolling while open; the storyboard behind it must not move.
  React.useEffect(() => {
    const old = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = old;
    };
  }, []);
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
  // The playhead follows the player; the timeline scrolls to keep it in view while playing.
  React.useEffect(() => {
    const node = player.current;
    if (!node) return;
    const onFrame = (event) => setFrame(event.detail.frame);
    node.addEventListener('frameupdate', onFrame);
    node.addEventListener('seeked', onFrame);
    return () => {
      node.removeEventListener('frameupdate', onFrame);
      node.removeEventListener('seeked', onFrame);
    };
  });
  React.useLayoutEffect(() => {
    const node = scroller.current;
    if (!node) return;
    const measure = () => setFitWidth(Math.max(200, node.clientWidth - 24));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  const fit = Math.min(MAX_PPS, Math.max(MIN_PPS, fitWidth / Math.max(1, seconds)));
  const pps = zoom ?? fit;
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }));
  const busy = !!s.busy;
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
  const seekTime = (time) => {
    const f = Math.max(0, Math.min(Math.max(0, total - 1), Math.round(time * cutFps)));
    player.current?.seekTo(f);
    setFrame(f);
  };
  const select = (clip) => {
    setSelectedId(clip.id);
    if (clip.included && Number.isInteger(clip.from)) seekTime(clip.from / cutFps);
  };
  const playingId = included.find((c) => frame >= c.from && frame < c.from + c.frames)?.id;
  const assets = [...new Map(clips.filter((c) => c.playable).map((c) => [c.asset.id, c])).values()];
  // Sound follows the same cut timeline as the picture, so the preview matches what you see.
  const cutTimeline = included.map((c) => ({
    id: c.id,
    title: c.title,
    start: c.from / cutFps,
    end: (c.from + c.frames) / cutFps,
  }));
  const blocks = useMemo(
    () => audioBlocks(project, chapterId, cutTimeline),
    [project, chapterId, JSON.stringify(cutTimeline)],
  );
  const audioAssets = project.entities.filter(
    (e) => e.type === 'audio' && e.data.fileId && !e.data.missingFile && !e.data.sourceJobId,
  );
  const audio = blocks
    .filter((b) => b.placement && !b.track.muted && b.asset)
    .map((b) => ({
      id: b.track.id,
      assetId: b.asset.id,
      from: Math.round(b.placement.start * cutFps),
      frames: Math.max(1, Math.round((b.placement.end - b.placement.start) * cutFps)),
      trimBefore: Math.round(Number(b.track.start) * cutFps),
      volume: b.track.gain ?? 0.7,
    }));
  const x = useMemo(() => timeScale([], pps), [pps]);
  const laneWidth = Math.max(seconds * pps, 200);
  const playhead = () => frame / cutFps;
  const addAudio = async (asset, file, { wholeFilm = false } = {}) => {
    let duration = asset.data.metadata?.duration ?? asset.data.metadata?.duration_s;
    if (!Number.isFinite(duration) && file) duration = await probeDuration(file);
    edit((p) => {
      const track = addAudioTrack(
        p,
        chapterId,
        p.entities.find((e) => e.id === asset.id),
        wholeFilm ? 0 : playhead(),
        duration,
      );
      // Music for the whole film starts at 0 and stops where the film ends.
      if (wholeFilm && seconds > 0 && track.end > seconds)
        track.end = Math.round(seconds * 1000) / 1000;
    });
  };
  const uploadAudio = async (files, options) => {
    if (!onUploadAudio) return;
    setError('');
    try {
      const result = await onUploadAudio(files);
      const ids = result?.ids || [];
      for (const [index, id] of ids.entries()) {
        const asset = store.getState().project.entities.find((e) => e.id === id);
        if (asset?.type === 'audio') await addAudio(asset, files[index], options);
      }
    } catch (e) {
      setError(e?.message || t('editor.saveFailed'));
    }
  };
  const selectedSource =
    selected?.playable && sources[selected.asset.id] ? sources[selected.asset.id] : null;
  const selectedSound = selected ? clipSound(project, chapterId, selected.id) : null;
  // Portal to <body>: the editor opens from inside the sticky toolbar, whose stacking context
  // would otherwise let the shot panel draw on top of it.
  return createPortal(
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
            {t('editor.total', { seconds: seconds.toFixed(1), count: included.length })}
          </small>
        </div>
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
        <CloudSaveStatus />
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
      {blocks
        .filter((b) => b.asset?.data.fileId && !assets.some((c) => c.asset.id === b.asset.id))
        .map((b) => (
          <ClipSource
            key={'audio-src-' + b.asset.id}
            asset={b.asset}
            needsDuration={false}
            onUrl={onUrl}
            onDuration={onDuration}
          />
        ))}
      {!clips.length ? (
        <p className="film-editor-empty">{t('editor.empty')}</p>
      ) : (
        <>
          <div className="film-editor-stage">
            <Player
              ref={player}
              component={Cut}
              inputProps={{ clips: included, sources, cues: captions ? cues : [], audio }}
              durationInFrames={Math.max(1, total)}
              fps={cutFps}
              compositionWidth={width}
              compositionHeight={height}
              controls
              style={{ width: '100%', height: '100%' }}
            />
          </div>
          {selected && (
            <section className="film-inspector" aria-label={t('editor.selectedClip')}>
              <div className="film-inspector-head">
                <b>
                  {selected.index + 1}. {selected.title}
                </b>
                <button
                  type="button"
                  className="small ghost"
                  disabled={busy}
                  onClick={() =>
                    edit((p) => setIncluded(p, chapterId, selected.id, !selected.included))
                  }
                >
                  {selected.included ? <EyeOff size={13} /> : <Eye size={13} />}
                  {t(selected.included ? 'editor.exclude' : 'editor.include')}
                </button>
                <button
                  type="button"
                  className={'small ghost' + (selectedSound?.state === 'on' ? ' on' : '')}
                  disabled={busy || !selectedSound || selectedSound.state === 'none'}
                  title={selectedSound?.state === 'none' ? selectedSound.issue : undefined}
                  onClick={() => edit((p) => toggleClipSound(p, chapterId, selected.id))}
                >
                  {selectedSound?.state === 'on' ? <Volume2 size={13} /> : <VolumeX size={13} />}
                  {t(selectedSound?.state === 'on' ? 'editor.clipSoundOn' : 'editor.clipSoundOff')}
                </button>
              </div>
              {selected.playable ? (
                <div className="film-inspector-body">
                  <div className="film-source">
                    {selectedSource ? (
                      <video
                        key={selected.id}
                        ref={sourceVideo}
                        src={selectedSource}
                        preload="auto"
                        playsInline
                        onLoadedMetadata={(event) => {
                          onDuration(selected.asset.id, event.currentTarget.duration);
                          event.currentTarget.currentTime = selected.startFrame / cutFps;
                        }}
                      />
                    ) : (
                      <span className="muted">{t('editor.loading')}</span>
                    )}
                  </div>
                  <TrimBar
                    key={selected.id}
                    duration={selected.sourceSeconds ?? durations[selected.asset.id]}
                    start={selected.startFrame / cutFps}
                    end={(selected.startFrame + selected.frames) / cutFps}
                    getVideo={() => sourceVideo.current}
                    disabled={busy}
                    onCommit={(start, end, source) =>
                      edit((p) => trimClip(p, selected.id, start, end, source))
                    }
                  />
                </div>
              ) : (
                <p className="muted film-inspector-empty">{t('editor.trimNeedsVideo')}</p>
              )}
            </section>
          )}
          {error && (
            <p className="qc-error" role="alert">
              {error}
            </p>
          )}
          <div className="film-zoom">
            <small className="muted">{t('editor.dragHint')}</small>
            <span className="film-zoom-spacer" />
            <label>
              {t('editor.zoom')}
              <input
                type="range"
                min={MIN_PPS}
                max={MAX_PPS}
                step="1"
                value={Math.round(pps)}
                onChange={(e) => setZoom(Number(e.target.value))}
              />
            </label>
            <button type="button" className="small ghost" onClick={() => setZoom(null)}>
              {t('editor.fit')}
            </button>
          </div>
          <div className="film-timeline" ref={scroller}>
            <div className="film-timeline-inner" style={{ width: laneWidth + 24 }}>
              <Ruler seconds={seconds} pps={pps} onSeek={seekTime} />
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
                  items={included.map((clip) => clip.id)}
                  strategy={horizontalListSortingStrategy}
                >
                  <div className="film-vtrack">
                    {included.map((clip) => (
                      <TimelineClip
                        key={clip.id}
                        clip={clip}
                        pps={pps}
                        busy={busy}
                        selected={selected?.id === clip.id}
                        playing={playingId === clip.id}
                        sound={clipSound(project, chapterId, clip.id)}
                        onSelect={select}
                      />
                    ))}
                  </div>
                </SortableContext>
              </DndContext>
              <FilmAudioLane
                part="lane"
                blocks={blocks}
                x={x}
                width={laneWidth}
                pxPerSecond={pps}
                selected={selectedAudio}
                onSelect={setSelectedAudio}
                onMove={(id, start) => edit((p) => moveTrack(p, chapterId, id, start))}
                onTrim={(id, side, delta) =>
                  edit((p) => trimTrack(p, chapterId, id, side, delta, cutTimeline))
                }
              />
              <span className="film-playhead" style={{ left: (frame / cutFps) * pps }} />
            </div>
          </div>
          <FilmAudioLane
            part="controls"
            blocks={blocks}
            audioAssets={audioAssets}
            busy={busy}
            selected={selectedAudio}
            onSelect={setSelectedAudio}
            onAdd={(asset, options) => addAudio(asset, null, options)}
            onUpload={uploadAudio}
            onSet={(id, patch) => edit((p) => setTrack(p, chapterId, id, patch))}
            onRemove={(id) => edit((p) => removeTrack(p, chapterId, id))}
          />
          {excluded.length > 0 && (
            <div className="film-excluded">
              <small className="muted">{t('editor.excludedList')}</small>
              {excluded.map((clip) => (
                <button
                  key={clip.id}
                  type="button"
                  className={'small ghost' + (selected?.id === clip.id ? ' on' : '')}
                  onClick={() => select(clip)}
                >
                  {clip.index + 1}. {clip.title}
                </button>
              ))}
            </div>
          )}
          <footer className="film-editor-foot muted">
            <span>{t('editor.exportHint')}</span>
            {excluded.length > 0 && <span>{t('editor.excludedPending')}</span>}
          </footer>
        </>
      )}
    </div>,
    document.body,
  );
}
