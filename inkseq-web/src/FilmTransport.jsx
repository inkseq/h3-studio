// The film editor's own transport under the preview: play/pause, time, sound, full screen and
// the key list. Remotion's built-in controls name their buttons in English only and move focus
// to their play button on every play/pause, so the editor draws its own (story audit 2026-11-09).
import React, { useEffect, useState } from 'react';
import { Keyboard, Maximize, Pause, Play, Volume2, VolumeX } from 'lucide-react';
import { IconButton } from './ui/Button.jsx';
import { Dialog } from './ui/Dialog.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';

// m:ss:ff, the timecode editors show, so a one-frame step is visible.
export function timecode(frame, fps) {
  const f = Math.max(0, Math.round(frame)),
    seconds = Math.floor(f / fps);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}:${String(f % fps).padStart(2, '0')}`;
}

export function FilmTransport({ player, total, fps, rate, onToggle, onHelp }) {
  const { t } = useI18n();
  const [frame, setFrame] = useState(0),
    [playing, setPlaying] = useState(false),
    [muted, setMuted] = useState(false);
  useEffect(() => {
    const node = player.current;
    if (!node) return;
    const onFrame = (event) => setFrame(event.detail.frame);
    const onPlay = () => setPlaying(true),
      onPause = () => setPlaying(false),
      onMute = (event) => setMuted(!!event.detail.isMuted);
    node.addEventListener('frameupdate', onFrame);
    node.addEventListener('seeked', onFrame);
    node.addEventListener('play', onPlay);
    node.addEventListener('pause', onPause);
    node.addEventListener('ended', onPause);
    node.addEventListener('mutechange', onMute);
    return () => {
      node.removeEventListener('frameupdate', onFrame);
      node.removeEventListener('seeked', onFrame);
      node.removeEventListener('play', onPlay);
      node.removeEventListener('pause', onPause);
      node.removeEventListener('ended', onPause);
      node.removeEventListener('mutechange', onMute);
    };
  }, [player]);
  return (
    <div className="film-transport" role="group" aria-label={t('editor.transport')}>
      <IconButton
        size="sm"
        label={t(playing ? 'editor.pauseFilm' : 'editor.playFilm')}
        onClick={onToggle}
      >
        {playing ? <Pause size={15} /> : <Play size={15} />}
      </IconButton>
      <span className="film-transport-time">
        {timecode(frame, fps)} / {timecode(Math.max(0, total), fps)}
      </span>
      {/* Always present, so the buttons after it never move when J/L change the speed. */}
      <span className="film-transport-rate" aria-live="polite">
        {playing && rate !== 1 ? t('editor.rate', { rate }) : ''}
      </span>
      <span className="film-transport-spacer" />
      <IconButton
        size="sm"
        label={t(muted ? 'editor.unmuteFilm' : 'editor.muteFilm')}
        aria-pressed={muted}
        onClick={() => (muted ? player.current?.unmute() : player.current?.mute())}
      >
        {muted ? <VolumeX size={15} /> : <Volume2 size={15} />}
      </IconButton>
      <IconButton
        size="sm"
        label={t('editor.fullscreen')}
        onClick={() => {
          try {
            player.current?.requestFullscreen();
          } catch {
            // The browser does not allow full screen here; the preview stays as it is.
          }
        }}
      >
        <Maximize size={15} />
      </IconButton>
      <IconButton size="sm" label={t('editor.keys')} aria-keyshortcuts="?" onClick={onHelp}>
        <Keyboard size={15} />
      </IconButton>
    </div>
  );
}

const KEYS = [
  ['Space', 'editor.keyPlay'],
  ['J · K · L', 'editor.keyShuttle'],
  ['← →', 'editor.keyFrame'],
  ['Shift + ← →', 'editor.keySecond'],
  ['↑ ↓', 'editor.keyCut'],
  ['Home · End', 'editor.keyEnds'],
  ['I · O', 'editor.keyMark'],
  ['Enter', 'editor.keyClipSelect'],
  ['Alt + ← →', 'editor.keyClipMove'],
  ['Delete', 'editor.keyClipOut'],
  ['Ctrl + Z', 'editor.keyUndo'],
  ['← → · Shift', 'editor.keyAudioMove'],
  ['[ ]  · Alt', 'editor.keyAudioTrim'],
  ['Shift + Enter', 'editor.keyCaptionLine'],
  ['?', 'editor.keyHelp'],
];

/** The editor's key list, opened with ? or the keyboard button. */
export function FilmKeysHelp({ onClose }) {
  const { t } = useI18n();
  return (
    <Dialog title={t('editor.keysTitle')} onClose={onClose} portal>
      <dl className="film-keys">
        {KEYS.map(([keys, label]) => (
          <React.Fragment key={label}>
            <dt>
              <kbd>{keys === 'Space' ? t('editor.keySpace') : keys}</kbd>
            </dt>
            <dd>{t(label)}</dd>
          </React.Fragment>
        ))}
      </dl>
    </Dialog>
  );
}
