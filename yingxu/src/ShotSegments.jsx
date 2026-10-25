import React, { useEffect, useRef, useState } from 'react';
import { Play, X } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import { Button } from './ui/Button.jsx';
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
 * One time bar for the kept parts (#232 #23): drag on empty space to keep a new part, drag a
 * part to move it, drag its edges to resize, select it and press Delete (or ×) to remove it.
 * Parts play in time order.
 */
function PartsBar({ duration, parts, getVideo, busy, onChange, t }) {
  const bar = useRef(null),
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
    video.addEventListener('timeupdate', tick);
    return () => video.removeEventListener('timeupdate', tick);
  });
  const pct = (value) => (value / total) * 100 + '%';
  const at = (x) => {
    const box = bar.current.getBoundingClientRect();
    return clamp(((x - box.left) / box.width) * total, 0, total);
  };
  const seek = (value) => {
    const video = getVideo();
    if (!video) return;
    video.pause();
    video.currentTime = value;
  };
  function begin(event, index, mode) {
    if (busy || event.button > 0) return;
    event.preventDefault();
    event.stopPropagation();
    const el = bar.current,
      start = at(event.clientX),
      base = parts.map((part) => ({ ...part }));
    el.setPointerCapture(event.pointerId);
    let next = null;
    if (index !== null) setSelected(index);
    const move = (e) => {
      const x = at(e.clientX),
        delta = x - start,
        part = base[index];
      let value;
      if (mode === 'new') value = { start: Math.min(start, x), end: Math.max(start, x) };
      else if (mode === 'move') {
        const length = part.end - part.start,
          from = clamp(part.start + delta, 0, total - length);
        value = { start: from, end: from + length };
      } else if (mode === 'start')
        value = { start: clamp(part.start + delta, 0, part.end - MIN_PART), end: part.end };
      else
        value = { start: part.start, end: clamp(part.end + delta, part.start + MIN_PART, total) };
      next = mode === 'new' ? [...base, value] : base.map((r, i) => (i === index ? value : r));
      setDraft(next);
      seek(mode === 'end' ? value.end : value.start);
    };
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      setDraft(null);
      if (!next) {
        // A plain press on empty space just moves the playhead there.
        if (mode === 'new') seek(start);
        return;
      }
      const added = mode === 'new' ? next.at(-1) : null;
      if (added && added.end - added.start < MIN_PART) return seek(start);
      const sorted = next
        .map((part) => snapRange(part.start, part.end))
        .filter(Boolean)
        .sort((a, b) => a.start - b.start);
      setSelected(null);
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
  return (
    <div
      className={'parts-bar' + (busy ? ' busy' : '')}
      tabIndex={0}
      title={t('segments.barHelp')}
      onKeyDown={(event) => {
        if (selected === null || !['Delete', 'Backspace'].includes(event.key)) return;
        event.preventDefault();
        remove(selected);
      }}
    >
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
            <button
              type="button"
              className="parts-bar-x"
              aria-label={t('segments.remove', { n: index + 1 })}
              onPointerDown={(e) => e.stopPropagation()}
              onClick={() => remove(index)}
            >
              <X size={10} />
            </button>
            <i className="edge end" onPointerDown={(e) => begin(e, index, 'end')} />
          </span>
        ))}
        <span className="parts-bar-head" style={{ left: pct(clamp(now, 0, total)) }} />
      </div>
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
  if (!list.length)
    return (
      <div className="shot-segments start">
        <Button
          size="sm"
          variant="ghost"
          className="keep-label"
          disabled={busy || !trim}
          onClick={() => {
            const first = trim && snapRange(trim[0], trim[1]);
            if (first) void save([first]);
          }}
        >
          {t('segments.start')}
        </Button>
        <FloatingMessage text={error || null} onDismiss={() => setError('')} />
      </div>
    );
  return (
    <section className="shot-segments" aria-label={t('segments.title')}>
      <header>
        <b>{t('segments.title')}</b>
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
      <footer>
        <span className="muted">
          {saving
            ? t('segments.saving')
            : t('segments.total', { count: list.length, len: secs(segmentsTotal(list)) })}
        </span>
        <Button
          size="sm"
          variant="ghost"
          className="keep-label"
          disabled={busy}
          onClick={() => void save([])}
        >
          {t('segments.clear')}
        </Button>
      </footer>
      <FloatingMessage text={error || null} onDismiss={() => setError('')} />
    </section>
  );
}
