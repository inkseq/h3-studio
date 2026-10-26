import { useI18n } from './i18n/I18nProvider.jsx';
import { Button } from './ui/Button.jsx';
import React, {
  useCallback,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  useEffectEvent,
} from 'react';
import { createPortal } from 'react-dom';
import { Player } from '@remotion/player';
import { AbsoluteFill, Html5Audio, Html5Video, Sequence } from 'remotion';
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core';
import { SortableContext, horizontalListSortingStrategy, useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { Clapperboard, Eye, EyeOff, Gauge, Unlink, Volume2, VolumeX, X } from 'lucide-react';
import { store } from './store.js';
import { useStoryboardMediaUrl } from './StoryboardMedia.jsx';
import { cutFps } from './video-cut-model.js';
import { captionTrack, orderedCues } from './caption-model.js';
import {
  CROSSFADES,
  clipSoundCrossfade,
  cutFrames,
  editorClips,
  moveShot,
  setClipSoundCrossfade,
  setClipSpeed,
  setIncluded,
  trimClip,
} from './film-edit-model.js';
import {
  addAudioTrack,
  audioBlocks,
  clipSound,
  detachClipSound,
  moveTrack,
  removeTrack,
  setTrack,
  setTrackFade,
  toggleClipSound,
  trimTrack,
} from './film-audio-model.js';
import FilmAudioLane, { timeScale } from './FilmAudioLane.jsx';
import TrimBar from './TrimBar.jsx';
import { CloudSaveStatus } from './CloudAutosave.jsx';
import { cloudController } from './cloud-controller.js';
import './film-editor.css';
import { Note } from './ui/Note.jsx';

// Linear fade in/out over the given number of frames, as the exporter's afade.
const envelope = (frame, frames, fadeIn, fadeOut) =>
  Math.max(
    0,
    Math.min(1, fadeIn > 0 ? frame / fadeIn : 1, fadeOut > 0 ? (frames - frame) / fadeOut : 1),
  );

const SIZES = { '9:16': [720, 1280], '16:9': [1280, 720], '1:1': [960, 960], '4:3': [960, 720] };

// The whole cut as one Remotion composition: each included clip is a Sequence that plays
// its source from the trimmed in-point, so the preview matches the exporter frame for frame.
function Cut({ clips, sources, cues = [], audio = [] }) {
  return (
    <AbsoluteFill style={{ background: '#000' }}>
      {clips.map((clip) => (
        <Sequence key={clip.id} from={clip.from} durationInFrames={clip.frames}>
          {sources[clip.asset?.id] && clip.parts ? (
            clip.parts.map((part, index) => (
              <Sequence
                key={index}
                from={clip.parts.slice(0, index).reduce((sum, p) => sum + p.frames, 0)}
                durationInFrames={part.frames}
              >
                <Html5Video
                  src={sources[clip.asset.id]}
                  trimBefore={part.startFrame}
                  muted
                  style={{ width: '100%', height: '100%', objectFit: 'contain' }}
                />
              </Sequence>
            ))
          ) : sources[clip.asset?.id] ? (
            <Html5Video
              src={sources[clip.asset.id]}
              trimBefore={clip.startFrame}
              playbackRate={clip.rate}
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
            <Html5Audio
              src={sources[a.assetId]}
              trimBefore={a.trimBefore}
              playbackRate={a.rate}
              volume={(f) => a.volume * envelope(f, a.frames, a.fadeIn, a.fadeOut)}
            />
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
  const onUrlEvent = useEffectEvent((...args) => onUrl(...args));
  React.useEffect(() => {
    if (playbackUrl) onUrlEvent(asset.id, playbackUrl);
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
  // True length on the ruler (#232 #13): a minimum width would push every later clip off time.
  const width = (clip.frames / cutFps) * pps;
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
      {clip.parts?.length > 1 &&
        clip.parts.slice(1).map((_, index) => (
          <span
            key={index}
            className="film-tclip-cut"
            style={{
              left:
                (clip.parts.slice(0, index + 1).reduce((sum, p) => sum + p.frames, 0) /
                  clip.frames) *
                  100 +
                '%',
            }}
          />
        ))}
      <span className="film-tclip-label">
        <b>{clip.index + 1}</b>
        {width >= 72 && <span>{clip.title}</span>}
      </span>
      {clip.rate !== 1 && width >= 30 && (
        <span className="film-tclip-rate">{t('editor.rate', { rate: clip.rate })}</span>
      )}
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
    rate: c.rate,
  }));
  // Audio blocks follow the cut's content, not the array rebuilt on every render.
  const cutKey = JSON.stringify(cutTimeline);
  // Render v5 controls show only when the server advertises them (#408).
  const caps = cloudController.getState().capabilities?.chapter_render,
    v5 = caps?.render_contract_version >= 5,
    rates = (v5 && caps.playback_rates) || null,
    fadeMax = (v5 && caps.audio_fades_max_s) || 0,
    canDetach = v5 && !!caps.detached_generated_audio,
    crossfades = (v5 && caps.clip_sound_crossfade_values_s) || null,
    crossfade = crossfades ? clipSoundCrossfade(project, chapterId) : 0;
  const blocks = useMemo(
    () => audioBlocks(project, chapterId, JSON.parse(cutKey)),
    [project, chapterId, cutKey],
  );
  const audioAssets = project.entities.filter(
    (e) => e.type === 'audio' && e.data.fileId && !e.data.missingFile && !e.data.sourceJobId,
  );
  // Clips' own sounds that are heard, by clip id, for the automatic boundary blend.
  const audibleOwn = new Set(
    blocks
      .filter((b) => b.generated && b.placement && !b.effectiveMuted)
      .map((b) => b.track.shotId),
  );
  const audio = blocks
    .filter((b) => b.placement && !b.effectiveMuted && b.asset)
    .map((b) => {
      const frames = Math.max(1, Math.round((b.placement.end - b.placement.start) * cutFps)),
        at = b.generated ? cutTimeline.findIndex((c) => c.id === b.track.shotId) : -1,
        // Half the blend on each side of a boundary between two heard clip sounds, at most
        // half the clip; a longer manual fade wins.
        edge = Math.min((crossfade / 2) * cutFps, frames / 2),
        before = at > 0 && audibleOwn.has(cutTimeline[at - 1].id) ? edge : 0,
        after =
          at >= 0 && at < cutTimeline.length - 1 && audibleOwn.has(cutTimeline[at + 1].id)
            ? edge
            : 0;
      return {
        id: b.track.id,
        assetId: b.asset.id,
        from: Math.round(b.placement.start * cutFps),
        frames,
        trimBefore: Math.round(Number(b.track.start) * cutFps),
        rate: at >= 0 ? cutTimeline[at].rate || 1 : 1,
        volume: b.track.gain ?? 0.7,
        fadeIn: Math.max(before, (fadeMax ? b.track.fade_in_s || 0 : 0) * cutFps),
        fadeOut: Math.max(after, (fadeMax ? b.track.fade_out_s || 0 : 0) * cutFps),
      };
    });
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
        {/* #232 (#31): export from where the cut is made; the film dialog keeps its own confirm. */}
        <Button
          variant="primary"
          size="sm"
          onClick={() => {
            onClose();
            window.dispatchEvent(new Event('inkseq-open-film'));
          }}
        >
          <Clapperboard size={14} />
          {t('film.open')}
        </Button>
        <Button
          variant="ghost"
          className="round"
          aria-label={t('editor.close')}
          title={t('editor.close')}
          onClick={onClose}
        >
          <X size={18} />
        </Button>
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
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    edit((p) => setIncluded(p, chapterId, selected.id, !selected.included))
                  }
                >
                  {selected.included ? <EyeOff size={13} /> : <Eye size={13} />}
                  {t(selected.included ? 'editor.exclude' : 'editor.include')}
                </Button>
                <Button
                  className={'small ghost' + (selectedSound?.state === 'on' ? ' on' : '')}
                  disabled={
                    busy ||
                    !selectedSound ||
                    selectedSound.state === 'none' ||
                    selectedSound.state === 'detached'
                  }
                  title={selectedSound?.state === 'none' ? selectedSound.issue : undefined}
                  onClick={() => edit((p) => toggleClipSound(p, chapterId, selected.id))}
                >
                  {selectedSound?.state === 'on' ? <Volume2 size={13} /> : <VolumeX size={13} />}
                  {t(selectedSound?.state === 'on' ? 'editor.clipSoundOn' : 'editor.clipSoundOff')}
                </Button>
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
                        title={t('editor.clickToPlay')}
                        onClick={(event) => {
                          const node = event.currentTarget;
                          if (node.paused) void node.play().catch(() => {});
                          else node.pause();
                        }}
                        onLoadedMetadata={(event) => {
                          onDuration(selected.asset.id, event.currentTarget.duration);
                          event.currentTarget.currentTime = selected.startFrame / cutFps;
                        }}
                      />
                    ) : (
                      <span className="muted">{t('editor.loading')}</span>
                    )}
                  </div>
                  {selected.parts ? (
                    <Note>{t('editor.keptParts', { count: selected.parts.length })}</Note>
                  ) : (
                    <TrimBar
                      key={selected.id}
                      duration={selected.sourceSeconds ?? durations[selected.asset.id]}
                      start={selected.startFrame / cutFps}
                      end={(selected.startFrame + selected.sourceFrames) / cutFps}
                      getVideo={() => sourceVideo.current}
                      disabled={busy}
                      onCommit={(start, end, source) =>
                        edit((p) => trimClip(p, selected.id, start, end, source))
                      }
                    />
                  )}
                  {rates && !selected.parts && (
                    <div className="film-speed" role="group" aria-label={t('editor.speed')}>
                      <Gauge size={14} aria-hidden="true" />
                      <span>{t('editor.speed')}</span>
                      {/* #232 (#29): one compact choice instead of a row of six buttons. */}
                      <select
                        className="field-sm"
                        aria-label={t('editor.speed')}
                        value={selected.rate}
                        disabled={busy}
                        onChange={(event) => {
                          const rate = Number(event.target.value);
                          if (selected.rate !== rate)
                            edit((p) =>
                              setClipSpeed(
                                p,
                                selected.id,
                                rate,
                                selected.sourceSeconds ?? durations[selected.asset.id],
                              ),
                            );
                        }}
                      >
                        {rates.map((rate) => (
                          <option key={rate} value={rate}>
                            {t('editor.rate', { rate })}
                          </option>
                        ))}
                      </select>
                      {selectedSound?.slow && selectedSound.state === 'off' && (
                        <small className="muted">{t('editor.slowSoundMuted')}</small>
                      )}
                    </div>
                  )}
                  {canDetach && selectedSound?.track && (
                    <div className="film-detach">
                      {selectedSound.state === 'detached' ? (
                        <small className="muted">{t('editor.soundIsDetached')}</small>
                      ) : (
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={busy}
                          title={t('editor.detachHint')}
                          onClick={() => {
                            const id = selectedSound.track.id;
                            edit((p) => detachClipSound(p, chapterId, id, cutTimeline));
                            setSelectedAudio(id);
                          }}
                        >
                          <Unlink size={13} />
                          {t('editor.detachSound')}
                        </Button>
                      )}
                    </div>
                  )}
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
          <div className="film-zoom" title={t('editor.dragHint')}>
            <span className="film-zoom-spacer" />
            {crossfades && (
              <label title={t('editor.crossfadeHint')}>
                {t('editor.crossfade')}
                <select
                  value={crossfade}
                  disabled={busy}
                  onChange={(e) =>
                    edit((p) => setClipSoundCrossfade(p, chapterId, Number(e.target.value)))
                  }
                >
                  {crossfades
                    .filter((value) => CROSSFADES.includes(value))
                    .map((value) => (
                      <option key={value} value={value}>
                        {value ? t('editor.seconds', { value }) : t('editor.crossfadeOff')}
                      </option>
                    ))}
                </select>
              </label>
            )}
            <Button variant="ghost" size="sm" onClick={() => setZoom(null)}>
              {t('editor.fit')}
            </Button>
          </div>
          <div
            className="film-timeline"
            ref={scroller}
            // #232 (#16): the wheel scrolls a long timeline sideways; Ctrl/⌘ + wheel zooms.
            onWheel={(event) => {
              if (event.ctrlKey || event.metaKey) {
                setZoom(
                  Math.max(MIN_PPS, Math.min(MAX_PPS, pps * (event.deltaY < 0 ? 1.15 : 0.87))),
                );
                return;
              }
              if (Math.abs(event.deltaY) > Math.abs(event.deltaX))
                event.currentTarget.scrollLeft += event.deltaY;
            }}
          >
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
          {excluded.length > 0 && (
            <div className="film-excluded">
              <small className="muted">{t('editor.excludedList')}</small>
              {excluded.map((clip) => (
                <Button
                  key={clip.id}
                  className={'small ghost excluded-clip' + (selected?.id === clip.id ? ' on' : '')}
                  onClick={() => select(clip)}
                >
                  {clip.index + 1}. {clip.title}
                </Button>
              ))}
            </div>
          )}
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
            fadeMax={fadeMax}
            onFade={(id, side, value) =>
              edit((p) => setTrackFade(p, chapterId, id, side, value, fadeMax))
            }
            onRemove={(id) => edit((p) => removeTrack(p, chapterId, id))}
          />
          <footer className="film-editor-foot muted">
            {/* Once the server exports in editor order without left-out shots (#347), say nothing. */}
            {excluded.length > 0 &&
              !cloudController.getState().capabilities?.story_film?.film_cuts && (
                <span>{t('editor.excludedPending')}</span>
              )}
          </footer>
        </>
      )}
    </div>,
    document.body,
  );
}
