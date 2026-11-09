// Story-bible sections of the profile editor (story-bible.js): what the best-practice flow
// needs from a character, place or prop, as fields instead of one long description. The
// speaker number lives in the voice section (VoicePicker.jsx), with the voices.
import React, { useRef, useState } from 'react';
import { Plus, Upload, X } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { Field } from './ui/Field.jsx';
import { Note } from './ui/Note.jsx';
import { Popover, closePopover } from './ui/Popover.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { StoryboardAssetImage } from './StoryboardMedia.jsx';
import {
  COMPASS,
  EXPRESSIONS,
  PROFILE_FIELDS,
  PROP_SCALES,
  profileChecklist,
  speakingPlaces,
  voiceGaps,
} from './story-bible.js';
import './story-bible.css';

/** Where this character may speak in the story (story-bible.js speakingPlaces). */
export function useSpeaking(p) {
  const { state } = useStore();
  return speakingPlaces(p, {
    scenes: state.scenes || [],
    shots: state.shots || [],
    profiles: state.profiles || [],
  });
}

/** One line of small dots: how much of the flow's checklist this profile has. */
export function BibleDots({ p, used }) {
  const { t } = useI18n();
  const items = profileChecklist(p, { used });
  const gap = voiceGaps(p, useSpeaking(p)).length > 0;
  const done = items.filter((item) => item.done).length;
  const label = t('bible.dotsLabel', { done, total: items.length });
  return (
    <span
      className="bible-dots"
      role="img"
      aria-label={gap ? label + ' · ' + t('bible.dotsVoiceGap') : label}
      title={[
        ...items.map(
          (item) => (item.done ? '✓ ' : '· ') + t('bible.check.' + p.type + '.' + item.key),
        ),
        ...(gap ? ['! ' + t('bible.dotsVoiceGap')] : []),
      ].join('\n')}
    >
      {items.map((item) => (
        <i key={item.key} className={item.done ? 'on' : item.part ? 'part' : ''} />
      ))}
      {gap && <i className="gap" />}
    </span>
  );
}

// A list of short fixed lines (anchors), each row labelled "Anchor 1", "Anchor 2"…
function Lines({ value, onChange, placeholder, max = 12 }) {
  const { t } = useI18n();
  return (
    <div className="bible-lines">
      {value.map((line, index) => (
        <div key={index} className="bible-line">
          <input
            value={line}
            maxLength={600}
            placeholder={placeholder}
            aria-label={t('bible.anchorN', { n: index + 1 })}
            onChange={(e) => onChange(value.map((x, i) => (i === index ? e.target.value : x)))}
          />
          <IconButton
            size="sm"
            label={t('bible.removeLine') + ' · ' + t('bible.anchorN', { n: index + 1 })}
            onClick={() => onChange(value.filter((_, i) => i !== index))}
          >
            <X size={13} />
          </IconButton>
        </div>
      ))}
      {value.length < max && (
        <Button size="sm" variant="ghost" onClick={() => onChange([...value, ''])}>
          <Plus size={13} aria-hidden="true" />
          {t('bible.addLine')}
        </Button>
      )}
    </div>
  );
}

// Pick a picture for a slot (an expression, a prop state) in a popover: this card's own
// pictures first, all of the story's on request, or a new upload filed on this card.
// The shared Popover moves focus in, closes on Escape without closing the editor, and
// returns focus to the slot.
function PictureSlot({ label, assetId, p, onPick }) {
  const { t } = useI18n();
  const { state, db, dispatch } = useStore();
  const ownIds = [...new Set(p.variants.flatMap((v) => v.images))];
  const own = ownIds.map((id) => db.asset(id)).filter(Boolean);
  const [filter, setFilter] = useState(own.length ? 'own' : 'all'),
    [busy, setBusy] = useState(false),
    [failed, setFailed] = useState(false);
  const input = useRef(null);
  const asset = assetId && db.asset(assetId);
  const choices =
    filter === 'own'
      ? own
      : [...own, ...state.assets.filter((a) => a.kind === 'image' && !ownIds.includes(a.id))];
  const title = t('bible.pickFor', { label });

  async function upload(files) {
    if (!files.length) return;
    setBusy(true);
    setFailed(false);
    const node = input.current;
    const result = await dispatch({
      type: 'uploadFiles',
      files,
      target: { kind: 'image', pid: p.id, vid: p.def || p.variants[0]?.id },
    });
    setBusy(false);
    const id = result?.ids?.[0];
    if (result?.ok === false || !id) {
      setFailed(true);
      return;
    }
    onPick(id);
    closePopover(node);
  }

  return (
    <div className="bible-slot">
      <Popover
        align="start"
        className="bible-picker"
        label={title}
        onOpenChange={(open) => !open && setFailed(false)}
        trigger={
          <Button
            className={'bible-slot-face' + (asset ? ' filled' : '')}
            aria-label={asset ? title + ' · ' + asset.name : title}
          >
            {asset ? <StoryboardAssetImage asset={asset} /> : <Plus size={16} aria-hidden="true" />}
          </Button>
        }
      >
        <div className="bible-picker-head" data-keep-open>
          <Segmented
            label={title}
            options={[
              { value: 'own', label: t('bible.pickerOwn') },
              { value: 'all', label: t('bible.pickerAll') },
            ]}
            value={filter}
            onChange={setFilter}
          />
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => input.current?.click()}>
            <Upload size={13} aria-hidden="true" />
            {t(busy ? 'bible.pickerUploading' : 'bible.pickerUpload')}
          </Button>
          <input
            ref={input}
            type="file"
            accept="image/*"
            hidden
            onChange={(e) => {
              void upload([...e.target.files]);
              e.target.value = '';
            }}
          />
        </div>
        {failed && (
          <Note tone="warn" role="alert">
            {t('bible.pickerUploadFailed')}
          </Note>
        )}
        {choices.length ? (
          <div className="bible-picker-grid">
            {choices.slice(0, 60).map((choice) => (
              <Button
                key={choice.id}
                className="bible-picker-item"
                aria-label={choice.name}
                aria-pressed={choice.id === assetId}
                title={choice.name}
                onClick={() => onPick(choice.id)}
              >
                <StoryboardAssetImage asset={choice} />
              </Button>
            ))}
          </div>
        ) : (
          <small className="muted">
            {t(filter === 'own' ? 'bible.noOwnPictures' : 'bible.noPictures')}
          </small>
        )}
        {asset && (
          <Button size="sm" variant="ghost" onClick={() => onPick('')}>
            {t('bible.clearPicture')}
          </Button>
        )}
      </Popover>
      <small>{label}</small>
    </div>
  );
}

export function ProfileBible({ p }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const b = p.bible;
  const set = (patch) => dispatch({ type: 'bible', pid: p.id, patch });
  const characters = state.profiles.filter((x) => x.type === 'character');

  if (p.type === 'character')
    return (
      <div className="bible">
        <section className="bible-section">
          <h3>{t('bible.profile')}</h3>
          <div className="bible-grid">
            {PROFILE_FIELDS.map((key) => (
              <Field key={key} label={t('bible.profile.' + key)}>
                <input
                  value={b.profile[key]}
                  maxLength={600}
                  onChange={(e) => set({ profile: { [key]: e.target.value } })}
                />
              </Field>
            ))}
          </div>
        </section>

        <section className="bible-section">
          <h3>{t('bible.anchors')}</h3>
          <p className="muted small">{t('bible.anchorsHelp')}</p>
          <Lines
            value={b.anchors}
            placeholder={t('bible.anchorsPlaceholder')}
            onChange={(anchors) => set({ anchors })}
          />
        </section>

        <section className="bible-section">
          <h3>{t('bible.body')}</h3>
          <p className="muted small">{t('bible.bodyHelp')}</p>
          <div className="bible-row">
            {['heads', 'arms', 'legs'].map((key) => (
              <Field key={key} label={t('bible.body.' + key)}>
                <input
                  type="number"
                  min={0}
                  max={12}
                  value={b.body[key] ?? ''}
                  onChange={(e) =>
                    set({
                      body: {
                        [key]:
                          e.target.value === ''
                            ? null
                            : Math.max(0, Math.min(12, Math.round(+e.target.value))),
                      },
                    })
                  }
                />
              </Field>
            ))}
          </div>
          <Field label={t('bible.bodyLine')}>
            <textarea
              rows={2}
              maxLength={600}
              value={b.body.line}
              placeholder={t('bible.bodyLinePlaceholder')}
              onChange={(e) => set({ body: { line: e.target.value } })}
            />
          </Field>
        </section>

        <section className="bible-section">
          <h3>{t('bible.expressions')}</h3>
          <p className="muted small">{t('bible.expressionsHelp')}</p>
          <div className="bible-slots">
            {EXPRESSIONS.map((key) => (
              <PictureSlot
                key={key}
                label={t('bible.expression.' + key)}
                assetId={b.expressions[key]}
                p={p}
                onPick={(id) => set({ expressions: { [key]: id } })}
              />
            ))}
          </div>
        </section>
      </div>
    );

  if (p.type === 'location')
    return (
      <div className="bible">
        <section className="bible-section">
          <h3>{t('bible.anchors')}</h3>
          <p className="muted small">{t('bible.placeAnchorsHelp')}</p>
          <Lines
            value={b.anchors}
            placeholder={t('bible.placeAnchorsPlaceholder')}
            onChange={(anchors) => set({ anchors })}
          />
          {b.anchors.filter((x) => x.trim()).length < 3 && (
            <Note tone="info">{t('bible.placeAnchorsFew')}</Note>
          )}
        </section>
        <section className="bible-section">
          <h3>{t('bible.compass')}</h3>
          <p className="muted small">{t('bible.compassHelp')}</p>
          <div className="bible-grid">
            {COMPASS.map((key) => (
              <Field key={key} label={t('bible.compass.' + key)}>
                <input
                  value={b.compass[key]}
                  maxLength={120}
                  onChange={(e) => set({ compass: { [key]: e.target.value } })}
                />
              </Field>
            ))}
          </div>
        </section>
      </div>
    );

  return (
    <div className="bible">
      <section className="bible-section">
        <div className="bible-row">
          <Field label={t('bible.scale')}>
            <select value={b.scale} onChange={(e) => set({ scale: e.target.value })}>
              <option value="">{t('bible.scaleNone')}</option>
              {PROP_SCALES.map((key) => (
                <option key={key} value={key}>
                  {t('bible.scale.' + key)}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('bible.carriedBy')}>
            <select value={b.carriedBy} onChange={(e) => set({ carriedBy: e.target.value })}>
              <option value="">{t('bible.nobody')}</option>
              {characters.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </section>
      <section className="bible-section">
        <h3>{t('bible.fixedLine')}</h3>
        <p className="muted small">{t('bible.fixedLineHelp')}</p>
        <textarea
          rows={2}
          maxLength={600}
          value={b.fixedLine}
          placeholder={t('bible.fixedLinePlaceholder')}
          aria-label={t('bible.fixedLine')}
          onChange={(e) => set({ fixedLine: e.target.value })}
        />
      </section>
      <section className="bible-section">
        <h3>{t('bible.anchors')}</h3>
        <Lines
          value={b.anchors}
          placeholder={t('bible.propAnchorsPlaceholder')}
          onChange={(anchors) => set({ anchors })}
        />
      </section>
      <section className="bible-section">
        <h3>{t('bible.states')}</h3>
        <p className="muted small">{t('bible.statesHelp')}</p>
        <div className="bible-states">
          {b.states.map((state, index) => (
            <div key={index} className="bible-state">
              <PictureSlot
                label={state.name || t('bible.stateUnnamed')}
                assetId={state.imageId}
                p={p}
                onPick={(id) =>
                  set({ states: b.states.map((s, i) => (i === index ? { ...s, imageId: id } : s)) })
                }
              />
              <input
                value={state.name}
                maxLength={80}
                aria-label={t('bible.stateName')}
                onChange={(e) =>
                  set({
                    states: b.states.map((s, i) =>
                      i === index ? { ...s, name: e.target.value } : s,
                    ),
                  })
                }
              />
              <IconButton
                size="sm"
                label={t('bible.removeState')}
                onClick={() => set({ states: b.states.filter((_, i) => i !== index) })}
              >
                <X size={13} />
              </IconButton>
            </div>
          ))}
          {b.states.length < 12 && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => set({ states: [...b.states, { name: '', imageId: '' }] })}
            >
              <Plus size={13} aria-hidden="true" />
              {t('bible.addState')}
            </Button>
          )}
        </div>
      </section>
    </div>
  );
}
