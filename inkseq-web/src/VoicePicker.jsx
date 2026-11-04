// A character's voice from our catalog (h3-studio#997). The story keeps only our voice id; which
// provider speaks it is the server's business, and people only ever see voice names. Hidden while
// the server has voice switched off. Choosing is a saved edit that shows at once.
import React, { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Pause, Play } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';

const sampleOf = (voice, lang) => voice.samples?.[lang] || voice.samples?.en;

// One sample plays at a time, and leaving the editor stops it.
function useSample() {
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

export function VoicePicker({ profile }) {
  const { t, locale } = useI18n();
  const { adapter, dispatch } = useStore();
  const read = adapter?.readVoices;
  const { data } = useQuery({
    queryKey: ['voices'],
    queryFn: () => read(),
    enabled: !!read,
    staleTime: 600000,
    retry: false,
  });
  const { playing, toggle } = useSample();
  if (!data?.enabled || !data.voices?.length) return null;
  const language = locale === 'en' ? 'en' : 'zh';
  const choose = (id) => dispatch({ type: 'profile', id: profile.id, patch: { voiceId: id } });
  return (
    <section className="pe-section voice-picker" aria-label={t('voice.title')}>
      <h3>{t('voice.title')}</h3>
      <ul className="voice-list">
        {data.voices.map((voice) => {
          const chosen = profile.voiceId === voice.id,
            name = voice.name?.[language] || voice.id;
          return (
            <li key={voice.id} className={chosen ? 'voice-item chosen' : 'voice-item'}>
              <IconButton
                size="sm"
                label={t('voice.listen', { name })}
                onClick={() => toggle(voice.id, sampleOf(voice, language))}
              >
                {playing === voice.id ? <Pause size={14} /> : <Play size={14} />}
              </IconButton>
              <span className="voice-name">{name}</span>
              <small className="muted">{t('voice.gender.' + voice.gender)}</small>
              <Button
                size="sm"
                variant={chosen ? 'primary' : 'secondary'}
                aria-pressed={chosen}
                onClick={() => choose(chosen ? '' : voice.id)}
              >
                {chosen ? t('voice.chosen') : t('voice.choose')}
              </Button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
