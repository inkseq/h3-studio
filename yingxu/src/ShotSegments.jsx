import React, { useEffect, useRef, useState } from 'react';
import { Play, X } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import { Button } from './ui/Button.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { Note } from './ui/Note.jsx';
import { FloatingMessage } from './floating-notice.js';
import { segmentsAction, segmentsTotal, snapRange } from './shot-segments.js';
import './shot-segments.css';

const secs = (value) => String(Math.round(value * 10) / 10);

// Play the kept parts one after another, skipping what lies between them.
function playParts(video, parts) {
  if (!video || !parts.length) return;
  let index = 0;
  const step = () => {
    const part = parts[index];
    if (video.currentTime < part.end) return;
    index += 1;
    if (index >= parts.length) {
      video.pause();
      video.removeEventListener('timeupdate', step);
      return;
    }
    video.currentTime = parts[index].start;
  };
  video.currentTime = parts[0].start;
  video.addEventListener('timeupdate', step);
  video.addEventListener('pause', () => video.removeEventListener('timeupdate', step), {
    once: true,
  });
  Promise.resolve(video.play()).catch(() => {});
}

const MIN_PART = 0.25;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/**
 * One time bar for the kept parts (#232 #23, #38-#43): drag on empty space to keep a new
 * part, drag a part to move it, drag its edges to resize, click anywhere to jump there,
 * select a part and press Delete (or ×) to remove it. Parts never overlap and play in time
 * order.
 */
function PartsBar({ duration, parts, getVideo, busy, onChange, t }) {
  const box = useRef(null),
    bar = useRef(null),
    [draft, setDraft] = useState(null),
    [selected, setSelected] = useState(null),
    [now, setNow] = useState(0);
  const list = draft || parts;
  const total =
    Number.isFinite(duration) && duration > 0
      ? duration
      : Math.max(1, ...parts.map((part) => part.end));
  useEffect(() => {
    const video = getVideo();
    if (!video) return;
    const tick = () => setNow(video.currentTime || 0);
    tick();
    for (const name of ['timeupdate', 'seeked', 'loadedmetadata'])
      video.addEventListener(name, tick);
    return () => {
      for (const name of ['timeupdate', 'seeked', 'loadedmetadata'])
        video.removeEventListener(name, tick);
    };
  });
  const pct = (value) => (value / total) * 100 + '%';
  const at = (x) => {
    const rect = bar.current.getBoundingClientRect();
    return clamp(((x - rect.left) / rect.width) * total, 0, total);
  };
  const seek = (value) => {
    const video = getVideo();
    if (!video) return;
    video.pause();
    video.currentTime = value;
    setNow(value);
  };
  // The free room around a moment (or around one part), so parts never overlap.
  const room = (others, point) => {
    let lo = 0,
      hi = total;
    for (const part of others) {
      if (part.end <= point) lo = Math.max(lo, part.end);
      if (part.start >= point) hi = Math.min(hi, part.start);
    }
    return [lo, hi];
  };
  function begin(event, index, mode) {
    if (busy || event.button > 0) return;
    event.preventDefault();
    event.stopPropagation();
    box.current?.focus({ preventScroll: true });
    const el = bar.current,
      start = at(event.clientX),
      base = parts.map((part) => ({ ...part }));
    // A press inside a part on the track means "this part", not a new one.
    if (mode === 'new') {
      const hit = base.findIndex((part) => start >= part.start && start <= part.end);
      if (hit >= 0) return begin(event, hit, 'move');
    }
    el.setPointerCapture(event.pointerId);
    let next = null;
    setSelected(index);
    const others = index === null ? base : base.filter((_, i) => i !== index),
      [lo, hi] = room(others, index === null ? start : base[index].start);
    const move = (e) => {
      const x = at(e.clientX),
        delta = x - start,
        part = base[index];
      let value;
      if (mode === 'new') {
        const edge = clamp(x, lo, hi);
        value = { start: Math.min(start, edge), end: Math.max(start, edge) };
      } else if (mode === 'move') {
        const length = part.end - part.start,
          from = clamp(part.start + delta, lo, Math.max(lo, hi - length));
        value = { start: from, end: Math.min(hi, from + length) };
      } else if (mode === 'start')
        value = { start: clamp(part.start + delta, lo, part.end - MIN_PART), end: part.end };
      else
        value = {
          start: part.start,
          end: clamp(part.end + delta, part.start + MIN_PART, hi),
        };
      next = mode === 'new' ? [...base, value] : base.map((r, i) => (i === index ? value : r));
      setDraft(next);
      seek(mode === 'end' ? value.end : value.start);
    };
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      setDraft(null);
      // A plain click (no drag) jumps the player to that moment.
      if (!next) return seek(start);
      const added = mode === 'new' ? next.at(-1) : null;
      if (added && added.end - added.start < MIN_PART) return seek(start);
      const sorted = next
        .map((part) => snapRange(part.start, part.end))
        .filter(Boolean)
        .sort((a, b) => a.start - b.start);
      onChange(sorted);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  }
  const remove = (index) => {
    setSelected(null);
    onChange(parts.filter((_, i) => i !== index));
  };
  // The strip above the parts only moves the playhead (owner, 2026-10-26), so seeking never
  // fights with dragging a part.
  function scrub(event) {
    if (event.button > 0) return;
    event.preventDefault();
    const el = event.currentTarget;
    el.setPointerCapture(event.pointerId);
    seek(at(event.clientX));
    const move = (e) => seek(at(e.clientX));
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  }
  return (
    <div
      ref={box}
      className={'parts-bar' + (busy ? ' busy' : '')}
      tabIndex={0}
      title={t('segments.barHelp')}
      onKeyDown={(event) => {
        if (selected === null || !['Delete', 'Backspace'].includes(event.key)) return;
        event.preventDefault();
        remove(selected);
      }}
    >
      <div
        className="parts-bar-scrub"
        role="slider"
        aria-label={t('segments.scrub')}
        aria-valuemin={0}
        aria-valuemax={Math.round(total * 10) / 10}
        aria-valuenow={Math.round(now * 10) / 10}
        onPointerDown={scrub}
      >
        <span className="parts-bar-knob" style={{ left: pct(clamp(now, 0, total)) }} />
      </div>
      <div className="parts-bar-track" ref={bar} onPointerDown={(e) => begin(e, null, 'new')}>
        {list.map((part, index) => (
          <span
            key={index}
            className={'parts-bar-part' + (selected === index ? ' selected' : '')}
            style={{ left: pct(part.start), width: pct(part.end - part.start) }}
            onPointerDown={(e) => begin(e, index, 'move')}
          >
            <i className="edge start" onPointerDown={(e) => begin(e, index, 'start')} />
            <b>{index + 1}</b>
            <i className="edge end" onPointerDown={(e) => begin(e, index, 'end')} />
          </span>
        ))}
        <span className="parts-bar-head" style={{ left: pct(clamp(now, 0, total)) }} />
      </div>
      {/* The × sits above its part, clear of the resize edges. */}
      {selected !== null && list[selected] && (
        <button
          type="button"
          className="parts-bar-x"
          style={{ left: pct((list[selected].start + list[selected].end) / 2) }}
          aria-label={t('segments.remove', { n: selected + 1 })}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => remove(selected)}
        >
          <X size={11} />
        </button>
      )}
      {list.length <= 1 && <small className="parts-bar-hint muted">{t('segments.moreHint')}</small>}
    </div>
  );
}

/**
 * Several kept parts of the chosen take (h3-studio#512). Without parts the shot keeps the
 * single trim above; "Keep parts" starts a list from that trim. Each change saves the whole
 * ordered list through POST /actions (`shot.segments`), which the server snaps and binds.
 */
export default function ShotSegments({ shot, getVideo, duration, trim }) {
  const { t } = useI18n(),
    { state, dispatch, adapter } = useStoryboardUI();
  const list = shot.segments || [];
  const [saving, setSaving] = useState(false),
    [error, setError] = useState('');
  // Parts are saved on the cloud story (POST /actions), like choosing a take.
  if (!shot.chosen || !adapter?.readVersions) return null;
  const busy = saving || state.busy || !!state.workbenchPending;
  async function save(next) {
    setSaving(true);
    setError('');
    try {
      const result = await dispatch({
        type: 'workbenchAction',
        action: segmentsAction(shot.id, next),
      });
      if (result?.ok === false) throw Error('segments');
    } catch {
      setError(t('segments.failed'));
    } finally {
      setSaving(false);
    }
  }
  const cut = list.length > 0;
  // One switch between the whole take and cut parts (owner, 2026-10-26); the bar shows only
  // for parts. Switching to parts starts from the whole take (#232 #39).
  const mode = (
    <Segmented
      className="segments-mode"
      label={t('segments.title')}
      value={cut ? 'parts' : 'whole'}
      disabled={busy}
      onChange={(value) => {
        if (value === 'whole') return void save([]);
        const first = duration > 0 ? snapRange(0, duration) : trim && snapRange(trim[0], trim[1]);
        if (first) void save([first]);
      }}
      options={[
        { value: 'whole', label: t('segments.clear') },
        { value: 'parts', label: t('segments.start'), disabled: !(duration > 0 || trim) },
      ]}
    />
  );
  if (!cut)
    return (
      <div className="shot-segments start">
        {mode}
        <FloatingMessage text={error || null} onDismiss={() => setError('')} />
      </div>
    );
  return (
    <section className="shot-segments" aria-label={t('segments.title')}>
      <header>
        {mode}
        <Button
          size="sm"
          variant="ghost"
          className="keep-label"
          onClick={() => playParts(getVideo(), list)}
        >
          <Play size={13} />
          {t('segments.playAll')}
        </Button>
      </header>
      <PartsBar
        duration={duration}
        parts={list}
        getVideo={getVideo}
        busy={busy}
        onChange={(next) => void save(next)}
        t={t}
      />
      <small className="muted">
        {saving
          ? t('segments.saving')
          : t('segments.total', { count: list.length, len: secs(segmentsTotal(list)) })}
      </small>
      <FloatingMessage text={error || null} onDismiss={() => setError('')} />
    </section>
  );
}
