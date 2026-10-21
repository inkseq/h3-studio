import React, { useEffect, useRef, useState } from 'react';
import { Pause, Play } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { cutFps } from './video-cut-model.js';
import { snap } from './film-edit-model.js';
import './trim-bar.css';

const pct = (value, total) => (total > 0 ? (value / total) * 100 : 0) + '%';
const sec = (value) => value.toFixed(2);

/**
 * One take's whole source on a bar with start/end handles (owner, 2026-10-21: always visible,
 * no Apply/Cancel). Dragging previews the frame in `getVideo()`; releasing commits the range.
 */
export default function TrimBar({ duration, start, end, getVideo, onCommit, disabled = false }) {
  const { t } = useI18n();
  const bar = useRef(null),
    [range, setRange] = useState([start, end]),
    [now, setNow] = useState(start),
    [playing, setPlaying] = useState(false),
    dragging = useRef(false);
  const frame = 1 / cutFps,
    total = Number.isFinite(duration) && duration > 0 ? duration : null;
  useEffect(() => {
    if (!dragging.current) setRange([start, end]);
  }, [start, end]);
  // Follow the video's own time; stop at the end of the selection while playing it.
  useEffect(() => {
    const node = getVideo?.();
    if (!node) return;
    const tick = () => {
      setNow(node.currentTime);
      if (playing && node.currentTime >= range[1]) {
        node.pause();
        node.currentTime = range[1];
      }
    };
    const stop = () => setPlaying(false);
    node.addEventListener('timeupdate', tick);
    node.addEventListener('pause', stop);
    return () => {
      node.removeEventListener('timeupdate', tick);
      node.removeEventListener('pause', stop);
    };
  });
  const seek = (value) => {
    const node = getVideo?.();
    const v = Math.min(Math.max(0, value), total ?? value);
    if (node) node.currentTime = v;
    setNow(v);
  };
  const at = (clientX) => {
    const r = bar.current.getBoundingClientRect();
    return snap(Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * (total || 0));
  };
  const clampIn = (value, [, b]) => [Math.min(Math.max(0, snap(value)), b - frame), b];
  const clampOut = (value, [a]) => [a, Math.max(a + frame, Math.min(total ?? value, snap(value)))];
  const commit = (next) => {
    if (Math.abs(next[0] - start) > 1e-6 || Math.abs(next[1] - end) > 1e-6)
      onCommit(next[0], next[1], total);
  };
  const drag = (side) => (event) => {
    if (!total || disabled) return;
    event.preventDefault();
    event.stopPropagation();
    const el = event.currentTarget;
    el.setPointerCapture(event.pointerId);
    getVideo?.()?.pause();
    dragging.current = true;
    let latest = range;
    const move = (e) => {
      const v = at(e.clientX);
      latest = side === 'in' ? clampIn(v, latest) : clampOut(v, latest);
      setRange(latest);
      seek(v);
    };
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      dragging.current = false;
      commit(latest);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  };
  const toggle = () => {
    const node = getVideo?.();
    if (!node) return;
    if (!node.paused) return node.pause();
    if (node.currentTime < range[0] || node.currentTime >= range[1] - frame / 2)
      node.currentTime = range[0];
    setPlaying(true);
    node.play().catch(() => setPlaying(false));
  };
  const [inPoint, outPoint] = range;
  return (
    <div className={'trim-bar' + (disabled || !total ? ' off' : '')}>
      <div
        className="trim-bar-track"
        ref={bar}
        onPointerDown={(event) => total && seek(at(event.clientX))}
      >
        <span className="trim-bar-cut" style={{ left: 0, width: pct(inPoint, total) }} />
        <span className="trim-bar-cut" style={{ left: pct(outPoint, total), right: 0 }} />
        <span
          className="trim-bar-keep"
          style={{ left: pct(inPoint, total), width: pct(outPoint - inPoint, total) }}
        />
        <span className="trim-bar-head" style={{ left: pct(now, total) }} />
        {['in', 'out'].map((side) => (
          <span
            key={side}
            role="slider"
            tabIndex={0}
            aria-label={t(side === 'in' ? 'editor.trimHandleIn' : 'editor.trimHandleOut')}
            aria-valuemin={0}
            aria-valuemax={total || 0}
            aria-valuenow={side === 'in' ? inPoint : outPoint}
            className={'trim-bar-handle ' + side}
            style={{ left: pct(side === 'in' ? inPoint : outPoint, total) }}
            onPointerDown={drag(side)}
            onKeyDown={(event) => {
              const step =
                event.key === 'ArrowLeft' ? -frame : event.key === 'ArrowRight' ? frame : 0;
              if (!step || !total) return;
              event.preventDefault();
              const v = (side === 'in' ? inPoint : outPoint) + step;
              const next = side === 'in' ? clampIn(v, range) : clampOut(v, range);
              setRange(next);
              seek(v);
              commit(next);
            }}
          />
        ))}
      </div>
      <div className="trim-bar-row">
        <button type="button" className="small" disabled={!total} onClick={toggle}>
          {playing ? <Pause size={13} /> : <Play size={13} />}
          {t(playing ? 'editor.pause' : 'editor.playRange')}
        </button>
        <span className="trim-bar-times muted">
          {total
            ? t('editor.trimTimes', {
                in: sec(inPoint),
                out: sec(outPoint),
                len: sec(outPoint - inPoint),
                total: sec(total),
              })
            : t('editor.loading')}
        </span>
        <button
          type="button"
          className="small ghost"
          disabled={!total}
          onClick={() => {
            const next = clampIn(now, range);
            setRange(next);
            commit(next);
          }}
        >
          [ {t('editor.setIn')}
        </button>
        <button
          type="button"
          className="small ghost"
          disabled={!total}
          onClick={() => {
            const next = clampOut(now, range);
            setRange(next);
            commit(next);
          }}
        >
          {t('editor.setOut')} ]
        </button>
        <button
          type="button"
          className="small ghost"
          disabled={!total}
          onClick={() => {
            const next = [0, snap(total)];
            setRange(next);
            commit(next);
          }}
        >
          {t('editor.wholeClip')}
        </button>
      </div>
    </div>
  );
}
