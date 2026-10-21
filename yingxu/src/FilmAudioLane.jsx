import React, { useRef, useState } from 'react';
import { Music, Plus, Trash2, Upload, Volume2, VolumeX } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';

// Seconds ↔ pixels along the clip track, measured from the rendered clip cards so the sound
// lane lines up with clips of any width (cards have a minimum width).
export function timeScale(segments, fallbackPx) {
  const x = (time) => {
    if (!segments.length) return time * fallbackPx;
    for (const seg of segments)
      if (time <= seg.end || seg === segments.at(-1)) {
        const span = seg.end - seg.start || 1;
        if (time >= seg.start && time <= seg.end)
          return seg.left + ((time - seg.start) / span) * seg.width;
        if (time < seg.start) return seg.left;
      }
    const last = segments.at(-1);
    return last.left + last.width + (time - last.end) * fallbackPx;
  };
  return x;
}

function Block({ block, x, selected, onSelect, onMove, onTrim, pxPerSecond }) {
  const { t } = useI18n();
  const drag = useRef(null),
    [dx, setDx] = useState({ kind: null, value: 0 });
  const { placement, track } = block;
  if (!placement) return null;
  const left = x(placement.start),
    width = Math.max(18, x(placement.end) - left),
    fixed = block.generated;
  const start = (kind) => (event) => {
    if (fixed && kind !== 'select') return;
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { kind, x: event.clientX, moved: false };
    onSelect(track.id);
  };
  const move = (event) => {
    if (!drag.current) return;
    const value = event.clientX - drag.current.x;
    if (Math.abs(value) > 3) drag.current.moved = true;
    setDx({ kind: drag.current.kind, value });
  };
  const end = (event) => {
    const current = drag.current;
    drag.current = null;
    setDx({ kind: null, value: 0 });
    if (!current?.moved) return;
    const seconds = (event.clientX - current.x) / pxPerSecond;
    if (current.kind === 'move') onMove(track.id, placement.start + seconds);
    else onTrim(track.id, current.kind, seconds);
  };
  const live =
    dx.kind === 'move'
      ? { left: left + dx.value, width }
      : dx.kind === 'start'
        ? { left: left + dx.value, width: Math.max(18, width - dx.value) }
        : dx.kind === 'end'
          ? { left, width: Math.max(18, width + dx.value) }
          : { left, width };
  return (
    <div
      className={
        'film-audio' +
        (fixed ? ' generated' : '') +
        (track.muted ? ' muted' : '') +
        (selected ? ' selected' : '')
      }
      style={live}
      title={fixed ? t('editor.followsClip') : t('editor.audioDragHint')}
      onPointerDown={fixed ? () => onSelect(track.id) : start('move')}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={() => {
        drag.current = null;
        setDx({ kind: null, value: 0 });
      }}
    >
      {!fixed && (
        <span
          className="film-audio-trim start"
          onPointerDown={start('start')}
          onPointerMove={move}
          onPointerUp={end}
        />
      )}
      <span className="film-audio-label">
        {track.muted ? <VolumeX size={12} /> : fixed ? <Volume2 size={12} /> : <Music size={12} />}
        {block.title}
      </span>
      {!fixed && (
        <span
          className="film-audio-trim end"
          onPointerDown={start('end')}
          onPointerMove={move}
          onPointerUp={end}
        />
      )}
    </div>
  );
}

const FADES = [0, 0.5, 1, 2, 3, 5];

/** The sound lane under the clips, plus add/upload and the selected track's controls. */
export default function FilmAudioLane({
  part,
  blocks,
  x,
  width,
  pxPerSecond,
  audioAssets = [],
  busy,
  selected,
  onSelect,
  onAdd,
  onUpload,
  onMove,
  onTrim,
  onSet,
  onRemove,
  fadeMax = 0,
  onFade,
}) {
  const { t } = useI18n();
  const [menu, setMenu] = useState(false),
    [wholeFilm, setWholeFilm] = useState(false),
    file = useRef(null);
  const current = blocks.find((b) => b.track.id === selected);
  if (part === 'lane') {
    // Stack overlapping blocks onto rows so none hides another.
    const rows = [];
    const placed = blocks
      .filter((b) => b.placement)
      .sort((a, b) => a.placement.start - b.placement.start)
      .map((b) => {
        let row = rows.findIndex((end) => end <= b.placement.start + 1e-6);
        if (row < 0) row = rows.push(0) - 1;
        rows[row] = b.placement.end;
        return { ...b, row };
      });
    return (
      <div
        className="film-audio-lane"
        style={{ width, height: Math.max(1, rows.length) * 30 + 8 }}
        onPointerDown={(event) => event.target === event.currentTarget && onSelect(null)}
      >
        {!placed.length && <span className="film-audio-empty muted">{t('editor.noAudio')}</span>}
        {placed.map((block) => (
          <div key={block.track.id} className="film-audio-row" style={{ top: 4 + block.row * 30 }}>
            <Block
              block={block}
              x={x}
              pxPerSecond={pxPerSecond}
              selected={selected === block.track.id}
              onSelect={onSelect}
              onMove={onMove}
              onTrim={onTrim}
            />
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className="film-audio-bar">
      <div className="film-audio-add">
        <button
          type="button"
          className="small"
          disabled={busy}
          aria-expanded={menu}
          onClick={() => setMenu((v) => !v)}
        >
          <Plus size={13} />
          {t('editor.addAudio')}
        </button>
        {menu && (
          <div className="film-audio-menu" role="menu">
            <label className="film-audio-whole">
              <input
                type="checkbox"
                checked={wholeFilm}
                onChange={(e) => setWholeFilm(e.target.checked)}
              />
              {t('editor.wholeFilm')}
            </label>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenu(false);
                file.current?.click();
              }}
            >
              <Upload size={13} />
              {t('editor.uploadAudio')}
            </button>
            {audioAssets.map((asset) => (
              <button
                key={asset.id}
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenu(false);
                  onAdd(asset, { wholeFilm });
                }}
              >
                <Music size={13} />
                {asset.title}
              </button>
            ))}
          </div>
        )}
        <input
          ref={file}
          type="file"
          accept="audio/*"
          hidden
          onChange={(event) => {
            const files = Array.from(event.target.files || []);
            event.target.value = '';
            if (files.length) onUpload(files, { wholeFilm });
          }}
        />
      </div>
      {current ? (
        <div className="film-audio-controls">
          <b>{current.title}</b>
          {current.generated && <small className="muted">{t('editor.followsClip')}</small>}
          {current.detached && <small className="muted">{t('editor.detachedTrack')}</small>}
          <label>
            {t('editor.volume')}
            <input
              type="range"
              min="0"
              max="100"
              step="1"
              value={Math.round((current.track.gain ?? 0.7) * 100)}
              onChange={(e) => onSet(current.track.id, { gain: Number(e.target.value) / 100 })}
            />
            <span className="muted">{Math.round((current.track.gain ?? 0.7) * 100)}%</span>
          </label>
          {fadeMax > 0 &&
            ['in', 'out'].map((side) => (
              <label key={side}>
                {t(side === 'in' ? 'editor.fadeIn' : 'editor.fadeOut')}
                <select
                  value={current.track[side === 'in' ? 'fade_in_s' : 'fade_out_s'] || 0}
                  disabled={busy}
                  onChange={(e) => onFade(current.track.id, side, Number(e.target.value))}
                >
                  {FADES.filter((value) => value <= fadeMax).map((value) => (
                    <option key={value} value={value}>
                      {value ? t('editor.seconds', { value }) : t('editor.crossfadeOff')}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          <button
            type="button"
            className="small"
            onClick={() => onSet(current.track.id, { muted: !current.track.muted })}
          >
            {current.track.muted ? <Volume2 size={13} /> : <VolumeX size={13} />}
            {t(current.track.muted ? 'editor.unmute' : 'editor.mute')}
          </button>
          <button
            type="button"
            className="small ghost"
            onClick={() => {
              onRemove(current.track.id);
              onSelect(null);
            }}
          >
            <Trash2 size={13} />
            {t('editor.removeAudio')}
          </button>
        </div>
      ) : (
        <small className="muted">{t('editor.audioHint')}</small>
      )}
    </div>
  );
}
