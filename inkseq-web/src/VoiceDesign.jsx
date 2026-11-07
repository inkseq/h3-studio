// Design a character's voice from words (h3-studio#1124). Inky drafts the description from the
// card; three previews come back; the one kept joins the character's voice samples, the audio
// reference that keeps the voice the same across shots. Free while new.
import React, { useEffect, useRef, useState } from 'react';
import { Pause, Play } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';

const MAX_DESCRIPTION = 400,
  MAX_SAMPLE = 120;
const newRequest = () => 'voice-' + crypto.randomUUID().replaceAll('-', '').slice(0, 24);
const failureOf = (failure) =>
  ({
    voice_design_refused: 'refused',
    voice_design_daily_limit: 'limit',
  })[failure?.detail?.detail?.code] || 'failed';

// One sample plays at a time, and leaving the editor stops it.
export function useSample() {
  const audio = useRef(null),
    [playing, setPlaying] = useState('');
  const stop = () => {
    audio.current?.pause();
    audio.current = null;
    setPlaying('');
  };
  useEffect(() => () => audio.current?.pause(), []);
  const toggle = (id, src) => {
    const same = playing === id;
    stop();
    if (same || !src) return;
    const next = new Audio(src);
    next.onended = () => setPlaying('');
    audio.current = next;
    setPlaying(id);
    next.play().catch(() => setPlaying(''));
  };
  return { playing, toggle };
}

function Previews({ design, kept, busy, onKeep }) {
  const { t } = useI18n();
  const { playing, toggle } = useSample();
  return (
    <ul className="voice-list">
      {design.previews.map((preview) => {
        const id = preview.asset_id,
          label = t('voiceDesign.preview', { n: preview.index + 1 }),
          done = kept.includes(preview.index);
        return (
          <li key={id} className="voice-item voice-design-item">
            <IconButton
              size="sm"
              label={t('voice.listen', { name: label })}
              onClick={() => toggle(id, `/v1/assets/${encodeURIComponent(id)}/content`)}
            >
              {playing === id ? <Pause size={14} /> : <Play size={14} />}
            </IconButton>
            <span className="voice-name">{label}</span>
            <Button
              size="sm"
              variant={done ? 'primary' : 'secondary'}
              disabled={done || !!busy}
              onClick={() => onKeep(preview)}
            >
              {t(done ? 'voiceDesign.kept' : 'voiceDesign.keep')}
            </Button>
          </li>
        );
      })}
    </ul>
  );
}

export function VoiceDesign({ profile, language }) {
  const { t } = useI18n();
  const { adapter, dispatch } = useStore();
  const [design, setDesign] = useState(null),
    [description, setDescription] = useState(''),
    [sample, setSample] = useState(''),
    [busy, setBusy] = useState(''),
    [failed, setFailed] = useState(''),
    [kept, setKept] = useState([]);
  const run = async (fromText) => {
    setBusy('design');
    setFailed('');
    try {
      const value = await adapter.designVoice({
        request_id: newRequest(),
        character_id: profile.id,
        language,
        ...(fromText ? { description: description.trim(), sample: sample.trim() } : {}),
      });
      setDesign(value);
      setDescription(value.description);
      setSample(value.sample);
      setKept([]);
    } catch (failure) {
      setFailed(failureOf(failure));
    } finally {
      setBusy('');
    }
  };
  const keep = async (preview) => {
    setBusy('keep');
    setFailed('');
    try {
      const { entity_id: entity } = await adapter.keepVoiceDesign({
        request_id: design.request_id,
        index: preview.index,
        asset_id: preview.asset_id,
        title: t('voiceDesign.clipTitle', { name: profile.name || '' }).slice(0, 80),
      });
      await Promise.resolve(dispatch({ type: 'addVoice', pid: profile.id, asset: entity }));
      setKept((list) => [...list, preview.index]);
    } catch {
      setFailed('keep');
    } finally {
      setBusy('');
    }
  };
  return (
    <div className="voice-design">
      <div className="voice-line-row">
        <span className="muted">{t('voiceDesign.hint')}</span>
        {!design && (
          <Button size="sm" variant="primary" disabled={!!busy} onClick={() => void run(false)}>
            {t(busy === 'design' ? 'voiceDesign.working' : 'voiceDesign.start')}
          </Button>
        )}
      </div>
      {design && (
        <>
          <label className="field">
            <span>{t('voiceDesign.description')}</span>
            <textarea
              rows={2}
              maxLength={MAX_DESCRIPTION}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          <label className="field">
            <span>{t('voiceDesign.sample')}</span>
            <input
              maxLength={MAX_SAMPLE}
              value={sample}
              onChange={(event) => setSample(event.target.value)}
            />
          </label>
          <Previews design={design} kept={kept} busy={busy} onKeep={(p) => void keep(p)} />
          <div className="voice-line-row">
            {design.partial ? (
              <small className="muted">{t('voiceDesign.partial')}</small>
            ) : (
              <span />
            )}
            <Button
              size="sm"
              variant="secondary"
              disabled={!!busy || !description.trim() || !sample.trim()}
              onClick={() => void run(true)}
            >
              {t(busy === 'design' ? 'voiceDesign.working' : 'voiceDesign.again')}
            </Button>
          </div>
        </>
      )}
      {failed && <p role="alert">{t('voiceDesign.failed.' + failed)}</p>}
    </div>
  );
}
