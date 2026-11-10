import { useI18n } from './i18n/I18nProvider.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import React, {
  useCallback,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  useEffectEvent,
} from 'react';
import { Player } from '@remotion/player';
import { AbsoluteFill, Html5Audio, Html5Video, Sequence } from 'remotion';
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import {
  SortableContext,
  horizontalListSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { Clapperboard, EyeOff, Gauge, Undo2, Unlink, Volume2, VolumeX } from 'lucide-react';
import { store } from './store.js';
import { useStoryboardMediaUrl } from './StoryboardMedia.jsx';
import { cutFps } from './video-cut-model.js';
import { chapterShots, shotsAspect } from './delivery-model.js';
import { captionProblems, captionStatus, captionTrack, orderedCues } from './caption-model.js';
import {
  addCue,
  confirmCaptions,
  cuesFromLines,
  fillCuesFromLines,
  removeCue,
  setCue,
} from './caption-edit.js';
import { CaptionControls, CaptionLane } from './FilmCaptionLane.jsx';
import { FilmKeysHelp, FilmTransport } from './FilmTransport.jsx';
import {
  clipName,
  cutPoints,
  markRange,
  shuttle,
  stepToCut,
  transportCommand,
} from './film-editor-keys.js';
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
import { cloudController } from './cloud-controller.js';
import './film-editor.css';
import { Note } from './ui/Note.jsx';

// Linear fade in/out over the given number of frames, as the exporter's afade.
const envelope = (frame, frames, fadeIn, fadeOut) =>
  Math.max(
    0,
    Math.min(1, fadeIn > 0 ? frame / fadeIn : 1, fadeOut > 0 ? (frames - frame) / fadeOut : 1),
  );

const PREMOUNT = cutFps;
// Clip and cue ids go into attribute selectors.
const CSS_ESCAPE = (id) => (globalThis.CSS?.escape ? globalThis.CSS.escape(id) : String(id));

// The timeline's playhead line follows the player on its own, so the editor around it does
// not re-render on every frame.
function FilmPlayhead({ player, pps }) {
  const [frame, setFrame] = useState(0);
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
  }, [player]);
  return <span className="film-playhead" style={{ left: (frame / cutFps) * pps }} />;
}

const SIZES = { '9:16': [720, 1280], '16:9': [1280, 720], '1:1': [960, 960], '4:3': [960, 720] };
// A clip whose file cannot play (an expired link, a dropped connection) shows black for its
// time instead of throwing out of the player (Remotion throws when no onError is given).
const keepPlaying = () => {};

// The whole cut as one Remotion composition: each included clip is a Sequence that plays
// its source from the trimmed in-point, so the preview matches the exporter frame for frame.
function Cut({ clips, sources, cues = [], audio = [] }) {
  return (
    <AbsoluteFill style={{ background: '#000' }}>
      {clips.map((clip) => (
        // Each clip mounts a second early, hidden, so its video has loaded when its turn comes
        // (Remotion premounting) instead of starting to load at the cut.
        <Sequence
          key={clip.id}
          from={clip.from}
          durationInFrames={clip.frames}
          premountFor={PREMOUNT}
        >
          {sources[clip.asset?.id] && clip.parts ? (
            clip.parts.map((part, index) => (
              <Sequence
                key={index}
                from={clip.parts.slice(0, index).reduce((sum, p) => sum + p.frames, 0)}
                durationInFrames={part.frames}
                premountFor={PREMOUNT}
              >
                <Html5Video
                  src={sources[clip.asset.id]}
                  trimBefore={part.startFrame}
                  pauseWhenBuffering
                  muted
                  onError={keepPlaying}
                  style={{ width: '100%', height: '100%', objectFit: 'contain' }}
                />
              </Sequence>
            ))
          ) : sources[clip.asset?.id] ? (
            <Html5Video
              src={sources[clip.asset.id]}
              trimBefore={clip.startFrame}
              playbackRate={clip.rate}
              pauseWhenBuffering
              muted
              onError={keepPlaying}
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
              {clip.name}
            </AbsoluteFill>
          )}
        </Sequence>
      ))}
      {/* The film's sound is exactly the chapter's sound tracks, as in the export. */}
      {audio.map((a) =>
        sources[a.assetId] ? (
          <Sequence
            key={'audio-' + a.id}
            from={a.from}
            durationInFrames={a.frames}
            premountFor={PREMOUNT}
          >
            <Html5Audio
              src={sources[a.assetId]}
              trimBefore={a.trimBefore}
              playbackRate={a.rate}
              pauseWhenBuffering
              onError={keepPlaying}
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
// Keyboard (story audit 2026-11-09): Enter selects, Enter on the selected clip picks it up for
// dnd-kit's keyboard drag, Alt+←/→ moves it one place, Delete leaves it out of the cut.
function TimelineClip({
  clip,
  pps,
  selected,
  playing,
  busy,
  sound,
  dragging,
  onSelect,
  onMove,
  onLeaveOut,
}) {
  const { t } = useI18n();
  const sortable = useSortable({
    id: clip.id,
    disabled: busy,
    attributes: { roleDescription: t('editor.clipRole') },
  });
  // True length on the ruler (#232 #13): a minimum width would push every later clip off time.
  const width = (clip.frames / cutFps) * pps,
    seconds = (clip.frames / cutFps).toFixed(1);
  const onKeyDown = (event) => {
    if (busy || dragging.current) return;
    if (event.key === 'Enter' && !selected) {
      event.preventDefault();
      onSelect(clip);
    } else if (event.altKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
      // Alt+arrow is also the browser's back/forward; the editor keeps it here.
      event.preventDefault();
      onMove(clip, event.key === 'ArrowLeft' ? -1 : 1);
    } else if (event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault();
      onLeaveOut(clip);
    } else if (event.key === 'Enter') sortable.listeners?.onKeyDown?.(event);
  };
  return (
    <div
      ref={sortable.setNodeRef}
      data-clip-id={clip.id}
      data-timeline-item="clip"
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
      title={`${clip.index + 1}. ${clip.name} · ${seconds}s`}
      onClick={() => onSelect(clip)}
      {...sortable.attributes}
      {...sortable.listeners}
      aria-label={t('editor.clipLabel', { n: clip.index + 1, seconds })}
      aria-pressed={selected}
      onKeyDown={onKeyDown}
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
      {/* The number needs room for its pill; a sliver of a clip shows only its picture. */}
      {width >= 30 && (
        <span className="film-tclip-label">
          <b>{clip.index + 1}</b>
          {width >= 72 && <span>{clip.name}</span>}
        </span>
      )}
      {clip.rate !== 1 && width >= 30 && (
        <span className="film-tclip-rate">{t('editor.rate', { rate: clip.rate })}</span>
      )}
      {width >= 40 && sound?.state === 'on' && (
        <Volume2 className="film-tclip-sound" size={12} aria-label={t('editor.clipSoundOn')} />
      )}
    </div>
  );
}

export default function FilmEditor({
  chapterId,
  chapterPicker = null,
  initialTrim = null,
  onClose,
  onUploadAudio,
}) {
  const { t } = useI18n();
  const s = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    project = s.project;
  const chapter = project.entities.find((e) => e.id === chapterId);
  // Each clip carries the name it shows: its title, else its first script line, else "Shot N".
  const clips = useMemo(() => {
    const shots = new Map(project.entities.map((e) => [e.id, e]));
    return editorClips(project, chapterId).map((clip) => ({
      ...clip,
      name: clipName(shots.get(clip.id)) || t('editor.shotN', { n: clip.index + 1 }),
    }));
  }, [project, chapterId, t]);
  const included = useMemo(() => clips.filter((clip) => clip.included), [clips]),
    excluded = clips.filter((clip) => !clip.included),
    total = cutFrames(clips),
    seconds = total / cutFps;
  const [sources, setSources] = useState({}),
    [durations, setDurations] = useState({}),
    [error, setError] = useState(''),
    [selectedId, setSelectedId] = useState(initialTrim),
    [playingId, setPlayingId] = useState(null),
    [zoom, setZoom] = useState(null),
    [fitWidth, setFitWidth] = useState(800),
    [captions, setCaptions] = useState(true),
    [selectedAudio, setSelectedAudio] = useState(null),
    [selectedCue, setSelectedCue] = useState(null),
    [rate, setRate] = useState(1),
    [help, setHelp] = useState(false),
    [announce, setAnnounce] = useState('');
  const root = useRef(null),
    player = useRef(null),
    scroller = useRef(null),
    sourceVideo = useRef(null),
    // dnd-kit's keyboard drag owns the arrows and Enter while a clip is picked up.
    dragging = useRef(false),
    // The current frame lives in a ref, not state: re-rendering the whole editor on every
    // frame starved the player, so each clip froze after a fraction of a second
    // (owner, 2026-11-09). Only the playhead line and the playing clip's highlight follow it.
    frameRef = useRef(0);
  // The inspector only shows a clip that is in the cut (story audit 2026-11-09); a left-out
  // clip is put back from the list under the preview instead.
  const selected = included.find((clip) => clip.id === selectedId) || included[0] || null;
  // A newly selected clip shows from its name down, even on a short screen.
  React.useEffect(() => {
    const node = root.current?.querySelector('.film-inspector');
    if (node) node.scrollTop = 0;
  }, [selected?.id]);
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
  const follow = (f) => {
    frameRef.current = f;
    const id = included.find((c) => f >= c.from && f < c.from + c.frames)?.id ?? null;
    setPlayingId((old) => (old === id ? old : id));
  };
  React.useEffect(() => {
    const node = player.current;
    if (!node) return;
    const onFrame = (event) => follow(event.detail.frame);
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
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    // Enter picks the selected clip up, arrows move it, Enter or Space drops, Escape cancels.
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
      keyboardCodes: { start: ['Enter'], cancel: ['Escape'], end: ['Enter', 'Space'] },
    }),
  );
  const busy = !!s.busy;
  const aspect = shotsAspect(project, chapterShots(project, chapterId)),
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
    return !failure && ok !== false;
  };
  // Keep the playhead in view when the keyboard moves it along a zoomed-in timeline.
  const reveal = (f) => {
    const node = scroller.current;
    if (!node) return;
    const left = (f / cutFps) * pps;
    if (left < node.scrollLeft + 12 || left > node.scrollLeft + node.clientWidth - 36)
      node.scrollLeft = Math.max(0, left - node.clientWidth / 3);
  };
  const seekFrame = (frame) => {
    const f = Math.max(0, Math.min(Math.max(0, total - 1), Math.round(frame)));
    player.current?.seekTo(f);
    follow(f);
    reveal(f);
  };
  const seekTime = (time) => seekFrame(time * cutFps);
  const select = (clip) => {
    setSelectedId(clip.id);
    if (clip.included && Number.isInteger(clip.from)) seekTime(clip.from / cutFps);
  };
  const clipNode = (id) => root.current?.querySelector(`[data-clip-id="${CSS_ESCAPE(id)}"]`);
  const focusClip = (id) => requestAnimationFrame(() => clipNode(id)?.focus());
  // Alt+←/→: one place along the cut, past left-out clips, as the drag does (moveShot).
  const moveClip = (clip, direction) => {
    const at = included.findIndex((c) => c.id === clip.id),
      next = included[at + direction];
    if (!next) return;
    const to = clips.findIndex((c) => c.id === next.id);
    if (edit((p) => moveShot(p, chapterId, clip.id, to))) {
      setSelectedId(clip.id);
      setAnnounce(t('editor.movedTo', { at: at + direction + 1 }));
      focusClip(clip.id);
    }
  };
  // Delete leaves a clip out (Ctrl+Z puts it back); the selection moves to its neighbour.
  const leaveOut = (clip) => {
    const at = included.findIndex((c) => c.id === clip.id),
      neighbour = included[at + 1] || included[at - 1];
    if (!edit((p) => setIncluded(p, chapterId, clip.id, false))) return;
    setAnnounce(t('editor.leftOut', { n: clip.index + 1 }));
    if (neighbour) {
      // The playhead goes with the selection: the next clip now starts where this one did.
      setSelectedId(neighbour.id);
      seekTime((neighbour === included[at + 1] ? clip.from : neighbour.from) / cutFps);
      focusClip(neighbour.id);
    }
  };
  const putBack = (clip) => {
    if (!edit((p) => setIncluded(p, chapterId, clip.id, true))) return;
    setSelectedId(clip.id);
    setAnnounce(t('editor.putBackDone', { n: clip.index + 1 }));
  };
  const assets = [...new Map(clips.filter((c) => c.playable).map((c) => [c.asset.id, c])).values()];
  // Sound follows the same cut timeline as the picture, so the preview matches what you see.
  const cutTimeline = included.map((c) => ({
    id: c.id,
    title: c.name,
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
  // Remotion: a new inputProps object re-renders the whole composition, so keep it stable.
  const audioKey = JSON.stringify(audio);
  const inputProps = useMemo(
    () => ({ clips: included, sources, cues: captions ? cues : [], audio: JSON.parse(audioKey) }),
    [included, sources, captions, cues, audioKey],
  );
  const x = useMemo(() => timeScale([], pps), [pps]);
  // Caption cues as stored (seconds), for the editable lane.
  const rawCues = orderedCues(captionTrack(project, chapterId).cues);
  const hasLines = useMemo(
    () => cuesFromLines(project, included, cutFps).length > 0,
    [project, included],
  );
  // Burn-in needs this version confirmed; any later change to captions or cut asks again.
  const problems = useMemo(
    () =>
      captionTrack(project, chapterId).cues?.length ? captionProblems(project, chapterId) : [],
    [project, chapterId],
  );
  const captionState = useMemo(() => {
    const status = captionStatus(project, chapterId);
    return status.confirmed ? 'confirmed' : status.previouslyConfirmed ? 'stale' : 'unconfirmed';
  }, [project, chapterId]);
  const laneWidth = Math.max(seconds * pps, 200);
  const playhead = () => frameRef.current / cutFps;
  const jumpToCue = (id) => {
    const cue = rawCues.find((item) => item.id === id);
    if (!cue) return;
    setSelectedCue(id);
    seekTime(Number(cue.start) || 0);
    // After the problems popover has closed and handed focus back to its button.
    setTimeout(() => {
      root.current
        ?.querySelector(`[data-cue-id="${CSS_ESCAPE(id)}"]`)
        ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      root.current?.querySelector('.film-caption-text')?.focus();
    }, 60);
  };
  // I/O: the selected clip's start or end goes to the playhead.
  const mark = (side) => {
    if (!selected) return;
    const range = markRange(selected, frameRef.current, cutFps, side);
    if (range.error) return setError(t(range.error));
    const duration = selected.sourceSeconds ?? durations[selected.asset?.id];
    if (!Number.isFinite(duration)) return setError(t('editor.trimUnknown'));
    edit((p) => trimClip(p, selected.id, range.start, range.end, duration));
  };
  const commands = {
    toggle: () => {
      if (player.current?.isPlaying()) player.current.pause();
      else {
        setRate(1);
        player.current?.play();
      }
    },
    pause: () => {
      player.current?.pause();
      setRate(1);
    },
    forward: () => {
      setRate((old) => shuttle(old, !!player.current?.isPlaying(), 1));
      player.current?.play();
    },
    reverse: () => {
      setRate((old) => shuttle(old, !!player.current?.isPlaying(), -1));
      player.current?.play();
    },
    backFrame: () => (player.current?.pause(), seekFrame(frameRef.current - 1)),
    forwardFrame: () => (player.current?.pause(), seekFrame(frameRef.current + 1)),
    back1s: () => (player.current?.pause(), seekFrame(frameRef.current - cutFps)),
    forward1s: () => (player.current?.pause(), seekFrame(frameRef.current + cutFps)),
    previousCut: () => seekFrame(stepToCut(frameRef.current, cutPoints(included), -1)),
    nextCut: () => seekFrame(stepToCut(frameRef.current, cutPoints(included), 1)),
    start: () => seekFrame(0),
    end: () => seekFrame(total - 1),
    markIn: () => mark('in'),
    markOut: () => mark('out'),
    help: () => setHelp(true),
  };
  const onKey = useEffectEvent((event) => {
    if (dragging.current || help) return;
    const node = root.current,
      inEditor = !!node && (event.target === document.body || node.contains(event.target));
    const command = transportCommand(event, { inEditor });
    if (!command) return;
    event.preventDefault();
    commands[command]();
  });
  React.useEffect(() => {
    // `?` is caught before the page's own help shortcut so the editor's key list opens instead.
    const early = (event) => {
      if (event.key !== '?') return;
      onKey(event);
      if (event.defaultPrevented) event.stopPropagation();
    };
    const late = (event) => event.key !== '?' && onKey(event);
    window.addEventListener('keydown', early, true);
    window.addEventListener('keydown', late);
    return () => {
      window.removeEventListener('keydown', early, true);
      window.removeEventListener('keydown', late);
    };
  }, []);
  const addAudio = async (asset, file, { wholeFilm = false, at = null } = {}) => {
    let duration = asset.data.metadata?.duration ?? asset.data.metadata?.duration_s;
    if (!Number.isFinite(duration) && file) duration = await probeDuration(file);
    edit((p) => {
      const track = addAudioTrack(
        p,
        chapterId,
        p.entities.find((e) => e.id === asset.id),
        wholeFilm ? 0 : (at ?? playhead()),
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
  const title = t('editor.title', { chapter: chapter?.title || '' });
  const position = (id) => included.findIndex((clip) => clip.id === id) + 1,
    number = (id) => (clips.find((clip) => clip.id === id)?.index ?? 0) + 1;
  // A page of its own under the story's top bar, which keeps the steps, save status and report.
  return (
    <section className="film-editor" ref={root} aria-label={title}>
      <header className="film-editor-head">
        <div>
          <div className="film-editor-title">
            <h2>{chapterPicker ? t('editor.open') : title}</h2>
            {chapterPicker}
          </div>
          <small className="muted">
            {t('editor.total', { seconds: seconds.toFixed(1), count: included.length })}
          </small>
        </div>
        {/* Always present, so a message never pushes the preview and timeline down. */}
        <p className="film-editor-error qc-error" role="alert">
          {error}
        </p>
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
          <div className="film-monitor">
            <div className="film-editor-stage">
              <Player
                ref={player}
                component={Cut}
                inputProps={inputProps}
                durationInFrames={Math.max(1, total)}
                fps={cutFps}
                compositionWidth={width}
                compositionHeight={height}
                playbackRate={rate}
                clickToPlay
                doubleClickToFullscreen
                spaceKeyToPlayOrPause={false}
                style={{ width: '100%', height: '100%' }}
              />
            </div>
            <FilmTransport
              player={player}
              total={total}
              fps={cutFps}
              rate={rate}
              onToggle={commands.toggle}
              onHelp={() => setHelp(true)}
            />
          </div>
          <section className="film-inspector" aria-label={t('editor.selectedClip')}>
            {selected && (
              <>
                {/* One row: the clip's name and its actions as icons, so the source and the
                    trim bar keep their room on a short screen. */}
                <div className="film-inspector-head">
                  <b title={selected.name}>
                    {selected.index + 1}. {selected.name}
                  </b>
                  <IconButton
                    size="sm"
                    label={t('editor.excludeHint')}
                    disabled={busy}
                    onClick={() => leaveOut(selected)}
                  >
                    <EyeOff size={14} />
                  </IconButton>
                  <IconButton
                    size="sm"
                    className={selectedSound?.state === 'on' ? 'on' : ''}
                    label={
                      selectedSound?.state === 'none'
                        ? selectedSound.issue
                        : t(
                            selectedSound?.state === 'on'
                              ? 'editor.clipSoundOn'
                              : 'editor.clipSoundOff',
                          )
                    }
                    aria-pressed={selectedSound?.state === 'on'}
                    disabled={
                      busy ||
                      !selectedSound ||
                      selectedSound.state === 'none' ||
                      selectedSound.state === 'detached'
                    }
                    onClick={() => edit((p) => toggleClipSound(p, chapterId, selected.id))}
                  >
                    {selectedSound?.state === 'on' ? <Volume2 size={14} /> : <VolumeX size={14} />}
                  </IconButton>
                  {canDetach && selectedSound?.track && selectedSound.state !== 'detached' && (
                    <IconButton
                      size="sm"
                      label={t('editor.detachHint')}
                      disabled={busy}
                      onClick={() => {
                        const id = selectedSound.track.id;
                        edit((p) => detachClipSound(p, chapterId, id, cutTimeline));
                        setSelectedAudio(id);
                      }}
                    >
                      <Unlink size={14} />
                    </IconButton>
                  )}
                  {selected.playable && rates && !selected.parts && (
                    <label className="film-speed" title={t('editor.speed')}>
                      <Gauge size={14} aria-hidden="true" />
                      <select
                        className="field-sm"
                        aria-label={t('editor.speed')}
                        value={selected.rate}
                        disabled={busy}
                        onChange={(event) => {
                          const value = Number(event.target.value);
                          if (selected.rate !== value)
                            edit((p) =>
                              setClipSpeed(
                                p,
                                selected.id,
                                value,
                                selected.sourceSeconds ?? durations[selected.asset.id],
                              ),
                            );
                        }}
                      >
                        {rates.map((value) => (
                          <option key={value} value={value}>
                            {t('editor.rate', { rate: value })}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                </div>
                {selected.playable ? (
                  <>
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
                    {selectedSound?.slow && selectedSound.state === 'off' && (
                      <small className="muted">{t('editor.slowSoundMuted')}</small>
                    )}
                    {selectedSound?.state === 'detached' && (
                      <small className="muted">{t('editor.soundIsDetached')}</small>
                    )}
                  </>
                ) : (
                  <p className="muted film-inspector-empty">{t('editor.trimNeedsVideo')}</p>
                )}
              </>
            )}
          </section>
          <div className="film-zoom">
            {excluded.length > 0 && (
              <div className="film-excluded" role="group" aria-label={t('editor.excludedList')}>
                <small className="muted">{t('editor.excludedList')}</small>
                {excluded.map((clip) => (
                  <Button
                    key={clip.id}
                    size="sm"
                    className="excluded-clip"
                    disabled={busy}
                    title={t('editor.putBack', { n: clip.index + 1 })}
                    onClick={() => putBack(clip)}
                  >
                    <Undo2 size={13} aria-hidden="true" />
                    <span className="film-excluded-name">
                      {clip.index + 1}. {clip.name}
                    </span>
                  </Button>
                ))}
              </div>
            )}
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
            <Button
              variant="ghost"
              size="sm"
              title={t('editor.dragHint')}
              onClick={() => setZoom(null)}
            >
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
                accessibility={{
                  screenReaderInstructions: { draggable: t('editor.dragInstructions') },
                  announcements: {
                    onDragStart: ({ active }) => t('editor.dragStart', { n: number(active.id) }),
                    onDragOver: ({ active, over }) =>
                      over
                        ? t('editor.dragOver', { n: number(active.id), at: position(over.id) })
                        : t('editor.dragOutside', { n: number(active.id) }),
                    onDragEnd: ({ active, over }) =>
                      over
                        ? t('editor.dragEnd', { n: number(active.id), at: position(over.id) })
                        : t('editor.dragCancel', { n: number(active.id) }),
                    onDragCancel: ({ active }) => t('editor.dragCancel', { n: number(active.id) }),
                  },
                }}
                onDragStart={() => (dragging.current = true)}
                onDragCancel={() => (dragging.current = false)}
                onDragEnd={({ active, over }) => {
                  dragging.current = false;
                  if (!over || active.id === over.id) return;
                  const to = clips.findIndex((clip) => clip.id === over.id);
                  if (edit((p) => moveShot(p, chapterId, active.id, to))) focusClip(active.id);
                }}
              >
                <SortableContext
                  items={included.map((clip) => clip.id)}
                  strategy={horizontalListSortingStrategy}
                >
                  <div className="film-vtrack" role="group" aria-label={t('editor.pictureTrack')}>
                    {included.map((clip) => (
                      <TimelineClip
                        key={clip.id}
                        clip={clip}
                        pps={pps}
                        busy={busy}
                        dragging={dragging}
                        selected={selected?.id === clip.id}
                        playing={playingId === clip.id}
                        sound={clipSound(project, chapterId, clip.id)}
                        onSelect={select}
                        onMove={moveClip}
                        onLeaveOut={leaveOut}
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
                onRemove={(id) => edit((p) => removeTrack(p, chapterId, id))}
                sources={sources}
                busy={busy}
                onDropFiles={
                  onUploadAudio ? (files, at) => void uploadAudio(files, { at }) : undefined
                }
              />
              <CaptionLane
                cues={rawCues}
                x={x}
                width={laneWidth}
                selected={selectedCue}
                problems={problems}
                onSelect={setSelectedCue}
              />
              <FilmPlayhead player={player} pps={pps} />
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
            fadeMax={fadeMax}
            onFade={(id, side, value) =>
              edit((p) => setTrackFade(p, chapterId, id, side, value, fadeMax))
            }
            onRemove={(id) => edit((p) => removeTrack(p, chapterId, id))}
          />
          <CaptionControls
            cues={rawCues}
            selected={selectedCue}
            busy={busy}
            hasLines={hasLines}
            problems={problems}
            state={captionState}
            onAdd={() => {
              // Type straight away: the new caption's text field takes the keyboard.
              if (
                edit((p) =>
                  setSelectedCue(addCue(p, chapterId, frameRef.current / cutFps, total / cutFps)),
                )
              )
                requestAnimationFrame(() =>
                  root.current?.querySelector('.film-caption-text')?.focus(),
                );
            }}
            onFill={() => edit((p) => fillCuesFromLines(p, chapterId, included, cutFps))}
            onSet={(id, patch) => edit((p) => setCue(p, chapterId, id, patch, total / cutFps))}
            onRemove={(id) => {
              edit((p) => removeCue(p, chapterId, id));
              setSelectedCue(null);
            }}
            onConfirm={() => {
              if (edit((p) => confirmCaptions(p, chapterId)))
                setAnnounce(t('editor.captionsConfirmed'));
            }}
            onJump={jumpToCue}
            onDone={(id) =>
              root.current?.querySelector(`[data-cue-id="${CSS_ESCAPE(id)}"]`)?.focus()
            }
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
      <p className="visually-hidden" aria-live="polite">
        {announce}
      </p>
      {help && <FilmKeysHelp onClose={() => setHelp(false)} />}
    </section>
  );
}
