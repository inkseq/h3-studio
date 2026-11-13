// A character's one "Voice" section in the profile editor (one person, one voice): the speaker
// number and group, a note on the voice, a voice from our catalog (h3-studio#997) and the
// voice samples (rendered by the editor and passed in). The story keeps only our voice id;
// which provider speaks it is the server's business, and people only ever see voice names. The
// catalog part is hidden while the server has voice switched off. Choosing is a saved edit; on
// a cloud story the server then makes that voice's own sample the character's voice sample
// (h3-studio#1222), because a shot only sends samples.
import React, { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Pause, Play } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { Field } from './ui/Field.jsx';
import { Note } from './ui/Note.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { VoiceDesign, useSample } from './VoiceDesign.jsx';
import { useSpeaking } from './ProfileBible.jsx';
import { ProfileSplit } from './ProfileSplit.jsx';
import { hasVoiceSample, nextSpeaker, peopleInProfile, voiceGaps } from './story-bible.js';
import { shownVoices, voiceFilters, voiceSpeaks } from './voice-catalog.js';

const DIRECTIONS = ['calm', 'angry', 'sad', 'happy'],
  MAX_LINE = 500;
const newRequest = () => 'line-' + crypto.randomUUID().replaceAll('-', '').slice(0, 24);

// Say a line in this character's voice. The clip joins the character's voices, ready to use as a
// shot's audio reference. A retry after an unknown outcome reuses the id, so it never speaks twice.
function VoiceLine({ profile, language }) {
  const { t } = useI18n();
  const { adapter, dispatch } = useStore();
  const [text, setText] = useState(''),
    [direction, setDirection] = useState('calm'),
    [busy, setBusy] = useState(false),
    [failed, setFailed] = useState(false);
  const request = useRef(null);
  const speak = async () => {
    setBusy(true);
    setFailed(false);
    request.current ||= newRequest();
    try {
      const clip = await adapter.speakLine({
        request_id: request.current,
        text: text.trim(),
        voice_id: profile.voiceId,
        direction,
        language,
      });
      request.current = null;
      setText('');
      // A voice sample needs 3 s; a shorter line stays in the story's files, ready to use elsewhere.
      await Promise.resolve(
        dispatch({ type: 'addVoice', pid: profile.id, asset: clip.entity_id }),
      ).catch(() => setFailed('short'));
    } catch (error) {
      if (error?.detail?.detail?.nothing_charged === true) request.current = null;
      setFailed('failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="voice-line">
      <label className="field">
        <span>{t('voice.line')}</span>
        <textarea
          rows={2}
          maxLength={MAX_LINE}
          value={text}
          placeholder={t('voice.linePlaceholder')}
          onChange={(event) => setText(event.target.value)}
        />
      </label>
      <div className="voice-line-row">
        <Segmented
          label={t('voice.direction')}
          options={DIRECTIONS.map((value) => ({ value, label: t('voice.direction.' + value) }))}
          value={direction}
          onChange={setDirection}
        />
        <Button
          variant="primary"
          size="sm"
          disabled={busy || !text.trim()}
          onClick={() => void speak()}
        >
          {t(busy ? 'voice.speaking' : 'voice.speak')}
        </Button>
      </div>
      {failed && <p role="alert">{t(failed === 'short' ? 'voice.tooShort' : 'voice.failed')}</p>}
    </div>
  );
}

const sampleOf = (voice, lang) => voice.samples?.[lang] || voice.samples?.en;

// The speaker number (S1…S19, one character each) and the group people share.
function SpeakerFields({ profile }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const b = profile.bible;
  const set = (patch) => dispatch({ type: 'bible', pid: profile.id, patch });
  const characters = state.profiles.filter((x) => x.type === 'character');
  const holder = (tag) => characters.find((c) => c.id !== profile.id && c.bible?.speaker === tag);
  return (
    <>
      <div className="bible-row">
        <Field label={t('bible.speaker')}>
          <div className="bible-inline">
            <select value={b.speaker} onChange={(e) => set({ speaker: e.target.value })}>
              <option value="">{t('bible.noSpeaker')}</option>
              {Array.from({ length: 19 }, (_, i) => 'S' + (i + 1)).map((tag) => (
                <option key={tag} value={tag} disabled={!!holder(tag)}>
                  {tag}
                  {holder(tag) ? ' · ' + holder(tag).name : ''}
                </option>
              ))}
            </select>
            {!b.speaker && (
              <Button size="sm" onClick={() => set({ speaker: nextSpeaker(characters) })}>
                {t('bible.giveSpeaker')}
              </Button>
            )}
          </div>
        </Field>
        <Field label={t('bible.group')} help={t('bible.groupHelp')}>
          <input
            value={b.groupId}
            maxLength={80}
            list="bible-groups"
            onChange={(e) => set({ groupId: e.target.value })}
          />
          <datalist id="bible-groups">
            {[...new Set(characters.map((c) => c.bible?.groupId).filter(Boolean))].map((g) => (
              <option key={g} value={g} />
            ))}
          </datalist>
        </Field>
      </div>
      <Field label={t('bible.voiceNote')}>
        <input
          value={b.voiceNote}
          maxLength={600}
          placeholder={t('bible.voiceNotePlaceholder')}
          onChange={(e) => set({ voiceNote: e.target.value })}
        />
      </Field>
    </>
  );
}

// One of our voices for a character. The button changes at once (the saved choice); on a cloud
// story the server then adds that voice's own sample, because a shot only sends samples. A
// failure keeps the choice and says so; nothing waits.
function useCatalogSample(profile, language) {
  const { adapter, dispatch, state } = useStore();
  const [failed, setFailed] = useState(''),
    [giving, setGiving] = useState(false),
    wanted = useRef(''),
    client = useQueryClient();
  const cloud = !!adapter?.chooseCatalogVoice && state.workspaceMode === 'cloud';
  const give = async (id) => {
    wanted.current = id;
    setFailed('');
    setGiving(true);
    try {
      await adapter.chooseCatalogVoice(profile.id, { voice_id: id, language });
      // Shot advice read before the sample existed ("no voice yet") is out of date now.
      void client.invalidateQueries({ queryKey: ['shot-readiness'] });
    } catch {
      if (wanted.current === id) setFailed(id);
    } finally {
      setGiving(false);
    }
  };
  const choose = async (id) => {
    wanted.current = id;
    setFailed('');
    const picked = await dispatch({ type: 'profile', id: profile.id, patch: { voiceId: id } });
    if (id && cloud && picked?.ok !== false) await give(id);
  };
  // A voice chosen before samples existed, or whose sample did not arrive: offer it.
  const lacking = failed === profile.voiceId || !hasVoiceSample(profile);
  return { choose, give, owed: cloud && !giving && !!profile.voiceId && lacking };
}

// Our voices, narrowed by gender and age. The list keeps its height while a filter changes,
// so nothing under it moves; the voice in use always shows.
function VoiceList({ voices, chosenId, language, choose }) {
  const { t } = useI18n();
  const { playing, toggle } = useSample();
  const [filter, setFilter] = useState({ gender: '', age: '' });
  // The voice in use when the list opened leads it; a new choice does not reorder the rows.
  const [first] = useState(chosenId);
  const { genders, ages } = voiceFilters(voices);
  const shown = shownVoices(voices, filter, chosenId, first);
  const options = (key, values) => [
    { value: '', label: t('voice.filter.all') },
    ...values.map((value) => ({ value, label: t(`voice.${key}.${value}`) })),
  ];
  const pick = (key) => (value) => setFilter((current) => ({ ...current, [key]: value }));
  return (
    <>
      {(genders.length > 0 || ages.length > 0) && (
        <div className="voice-filters">
          {genders.length > 0 && (
            <Segmented
              label={t('voice.filter.gender')}
              options={options('gender', genders)}
              value={filter.gender}
              onChange={pick('gender')}
            />
          )}
          {ages.length > 0 && (
            <Segmented
              label={t('voice.filter.age')}
              options={options('age', ages)}
              value={filter.age}
              onChange={pick('age')}
            />
          )}
        </div>
      )}
      <ul className={voices.length > 8 ? 'voice-list long' : 'voice-list'}>
        {shown.map((voice) => {
          const chosen = chosenId === voice.id,
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
              <small className="muted">
                {t('voice.gender.' + voice.gender)}
                {voice.age ? ' · ' + t('voice.age.' + voice.age) : ''}
              </small>
              <Button
                size="sm"
                variant={chosen ? 'primary' : 'secondary'}
                aria-pressed={chosen}
                aria-label={t(chosen ? 'voice.chosenNamed' : 'voice.chooseNamed', { name })}
                onClick={() => void choose(chosen ? '' : voice.id)}
              >
                {chosen ? t('voice.chosen') : t('voice.choose')}
              </Button>
            </li>
          );
        })}
        {shown.length === 0 && <li className="muted small">{t('voice.filter.none')}</li>}
      </ul>
    </>
  );
}

export function VoicePicker({ profile, samples = null }) {
  const { t, locale } = useI18n();
  const { adapter, state } = useStore();
  const read = adapter?.readVoices;
  const { data } = useQuery({
    queryKey: ['voices'],
    queryFn: () => read(),
    enabled: !!read,
    staleTime: 600000,
    retry: false,
  });
  const speaking = useSpeaking(profile);
  const people = peopleInProfile(profile, state.profiles);
  const gaps = voiceGaps(profile, speaking);
  const language = locale === 'en' ? 'en' : 'zh';
  const { choose, give, owed } = useCatalogSample(profile, language);
  const catalog = data?.enabled && data.voices?.length > 0;
  return (
    <section className="pe-section voice-picker voice-all" aria-labelledby={'voice-' + profile.id}>
      <h3 id={'voice-' + profile.id}>{t('bible.voiceTitle')}</h3>
      <p className="muted small">{t('bible.voiceHelp')}</p>
      {people > 1 && speaking > 0 && <ProfileSplit p={profile} count={people} />}
      {/* Stays one line whether or not something is missing, so nothing below moves. */}
      {speaking > 0 && (
        <Note tone={gaps.length ? 'warn' : 'info'}>
          {gaps.length
            ? t('bible.gap.' + (gaps.length > 1 ? 'both' : gaps[0]), { count: speaking })
            : t('bible.gap.none', { count: speaking })}
        </Note>
      )}
      <SpeakerFields profile={profile} />
      {catalog && (
        <div className="voice-catalog">
          <h4>{t('voice.title')}</h4>
          {adapter?.designVoice && <VoiceDesign profile={profile} language={language} />}
          <VoiceList
            key={profile.id}
            voices={data.voices}
            chosenId={profile.voiceId}
            language={language}
            choose={choose}
          />
          {adapter?.speakLine && voiceSpeaks(data.voices, profile.voiceId) && (
            <VoiceLine profile={profile} language={language} />
          )}
          {owed && (
            <div className="voice-line-row" role="alert">
              <span>{t('voice.sampleFailed')}</span>
              <Button size="sm" onClick={() => void give(profile.voiceId)}>
                {t('voice.sampleRetry')}
              </Button>
            </div>
          )}
        </div>
      )}
      {samples}
    </section>
  );
}
