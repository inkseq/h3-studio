// Story-bible sections of the profile editor (story-bible.js): what the best-practice flow
// needs from a character, place or prop, as fields instead of one long description.
import React, { useState } from 'react';
import { Plus, X } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { Field } from './ui/Field.jsx';
import { Note } from './ui/Note.jsx';
import { StoryboardAssetImage } from './StoryboardMedia.jsx';
import {
  COMPASS,
  EXPRESSIONS,
  PROFILE_FIELDS,
  PROP_SCALES,
  nextSpeaker,
  profileChecklist,
} from './story-bible.js';
import './story-bible.css';

/** One line of small dots: how much of the flow's checklist this profile has. */
export function BibleDots({ p, used }) {
  const { t } = useI18n();
  const items = profileChecklist(p, { used });
  const done = items.filter((item) => item.done).length;
  return (
    <span
      className="bible-dots"
      role="img"
      aria-label={t('bible.dotsLabel', { done, total: items.length })}
      title={items
        .map((item) => (item.done ? '✓ ' : '· ') + t('bible.check.' + p.type + '.' + item.key))
        .join('\n')}
    >
      {items.map((item) => (
        <i key={item.key} className={item.done ? 'on' : item.part ? 'part' : ''} />
      ))}
    </span>
  );
}

// A list of short fixed lines (anchors): each line is sent word for word in every shot.
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
            onChange={(e) => onChange(value.map((x, i) => (i === index ? e.target.value : x)))}
          />
          <IconButton
            size="sm"
            label={t('bible.removeLine')}
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

// Pick one of the story's pictures for a slot (an expression, a prop state).
function PictureSlot({ label, assetId, choices, onPick }) {
  const { t } = useI18n();
  const { db } = useStore();
  const [open, setOpen] = useState(false);
  const asset = assetId && db.asset(assetId);
  return (
    <div className="bible-slot">
      <Button
        className={'bible-slot-face' + (asset ? ' filled' : '')}
        aria-label={t('bible.pickFor', { label })}
        onClick={() => setOpen((v) => !v)}
      >
        {asset ? <StoryboardAssetImage asset={asset} /> : <Plus size={16} aria-hidden="true" />}
      </Button>
      <small>{label}</small>
      {open && (
        <div className="bible-picker" role="dialog" aria-label={t('bible.pickFor', { label })}>
          {choices.length ? (
            choices.slice(0, 48).map((choice) => (
              <Button
                key={choice.id}
                className="bible-picker-item"
                aria-label={choice.name}
                onClick={() => {
                  onPick(choice.id);
                  setOpen(false);
                }}
              >
                <StoryboardAssetImage asset={choice} />
              </Button>
            ))
          ) : (
            <small className="muted">{t('bible.noPictures')}</small>
          )}
          {asset && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                onPick('');
                setOpen(false);
              }}
            >
              {t('bible.clearPicture')}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

export function ProfileBible({ p }) {
  const { t } = useI18n();
  const { state, dispatch, db } = useStore();
  const b = p.bible;
  const set = (patch) => dispatch({ type: 'bible', pid: p.id, patch });
  // Own pictures first, then the rest of the story's pictures.
  const own = [...new Set(p.variants.flatMap((v) => v.images))];
  const choices = [
    ...own.map((id) => db.asset(id)).filter(Boolean),
    ...state.assets.filter((a) => a.kind === 'image' && !own.includes(a.id)),
  ];
  const characters = state.profiles.filter((x) => x.type === 'character');
  const speakerTaken = (tag) => characters.some((c) => c.id !== p.id && c.bible?.speaker === tag);

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
          <h3>{t('bible.speakerTitle')}</h3>
          <p className="muted small">{t('bible.speakerHelp')}</p>
          <div className="bible-row">
            <Field label={t('bible.speaker')}>
              <div className="bible-inline">
                <select value={b.speaker} onChange={(e) => set({ speaker: e.target.value })}>
                  <option value="">{t('bible.noSpeaker')}</option>
                  {Array.from({ length: 19 }, (_, i) => 'S' + (i + 1)).map((tag) => (
                    <option key={tag} value={tag} disabled={speakerTaken(tag)}>
                      {tag}
                      {speakerTaken(tag)
                        ? ' · ' + characters.find((c) => c.bible?.speaker === tag)?.name
                        : ''}
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
                choices={choices}
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
                choices={choices}
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
