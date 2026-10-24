import React, { useEffect, useState } from 'react';
import { ArrowDown, ArrowUp, GripVertical, Play, X } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import TrimBar from './TrimBar.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { Note } from './ui/Note.jsx';
import { FloatingMessage } from './floating-notice.js';
import { clock, move, segmentsAction, segmentsTotal, snapRange } from './shot-segments.js';
import './shot-segments.css';

const secs = (value) => String(Math.round(value * 10) / 10);

// Play one kept part in the shot's player and stop at its end.
function playRange(video, range) {
  if (!video) return;
  const stop = () => {
    if (video.currentTime >= range.end) {
      video.pause();
      video.removeEventListener('timeupdate', stop);
    }
  };
  video.currentTime = range.start;
  video.addEventListener('timeupdate', stop);
  video.addEventListener('pause', () => video.removeEventListener('timeupdate', stop), {
    once: true,
  });
  Promise.resolve(video.play()).catch(() => {});
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
  const [sel, setSel] = useState(trim),
    [saving, setSaving] = useState(false),
    [error, setError] = useState(''),
    [dragFrom, setDragFrom] = useState(null),
    [src, setSrc] = useState('');
  useEffect(() => setSel(trim), [shot.id, shot.chosen, trim?.[0], trim?.[1]]);
  useEffect(() => {
    const video = getVideo();
    if (video?.currentSrc && video.currentSrc !== src) setSrc(video.currentSrc);
  });
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
  const picked = sel && snapRange(sel[0], sel[1]);
  return (
    <section className="shot-segments" aria-label={t('segments.title')}>
      <header>
        <b>{t('segments.title')}</b>
        <Note as="small">{t('segments.help')}</Note>
      </header>
      <TrimBar
        duration={duration}
        start={sel?.[0] ?? 0}
        end={sel?.[1] ?? duration ?? 1}
        getVideo={getVideo}
        onCommit={(s, e) => setSel([s, e])}
      />
      <div className="shot-segments-add">
        <span className="muted">
          {picked &&
            t('segments.selection', {
              from: clock(picked.start),
              to: clock(picked.end),
              len: secs(picked.end - picked.start),
            })}
        </span>
        <Button
          size="sm"
          variant="primary"
          className="keep-label"
          disabled={busy || !picked}
          onClick={() => void save([...list, picked])}
        >
          {t('segments.add')}
        </Button>
      </div>
      <ol className="shot-segments-list">
        {list.map((range, index) => {
          const n = index + 1;
          return (
            <li
              key={index + ':' + range.start + ':' + range.end}
              draggable={!busy}
              className={dragFrom === index ? 'dragging' : ''}
              onDragStart={() => setDragFrom(index)}
              onDragEnd={() => setDragFrom(null)}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                if (dragFrom !== null && dragFrom !== index) void save(move(list, dragFrom, index));
                setDragFrom(null);
              }}
            >
              <GripVertical size={14} className="shot-segments-grip" aria-hidden="true" />
              <span className="shot-segments-n">{n}</span>
              {src ? (
                <video
                  className="shot-segments-thumb"
                  src={src.split('#')[0] + '#t=' + range.start}
                  preload="metadata"
                  muted
                  playsInline
                  aria-hidden="true"
                />
              ) : (
                <span className="shot-segments-thumb" />
              )}
              <span className="shot-segments-time">
                {t('segments.row', {
                  n,
                  from: clock(range.start),
                  to: clock(range.end),
                  len: secs(range.end - range.start),
                })}
              </span>
              <IconButton
                size="sm"
                label={t('segments.play', { n })}
                onClick={() => playRange(getVideo(), range)}
              >
                <Play size={13} />
              </IconButton>
              <IconButton
                size="sm"
                label={t('segments.up', { n })}
                disabled={busy || index === 0}
                onClick={() => void save(move(list, index, index - 1))}
              >
                <ArrowUp size={13} />
              </IconButton>
              <IconButton
                size="sm"
                label={t('segments.down', { n })}
                disabled={busy || index === list.length - 1}
                onClick={() => void save(move(list, index, index + 1))}
              >
                <ArrowDown size={13} />
              </IconButton>
              <IconButton
                size="sm"
                label={t('segments.remove', { n })}
                disabled={busy}
                onClick={() => void save(list.filter((_, i) => i !== index))}
              >
                <X size={13} />
              </IconButton>
            </li>
          );
        })}
      </ol>
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
