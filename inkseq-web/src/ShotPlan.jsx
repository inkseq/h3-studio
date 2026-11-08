// A shot's plan, acting and continuity (shot-plan.js) in the shot editor, and its one-line
// summary on the shot card. The tabs follow the flow: plan → acting → continuity.
import React, { useState } from 'react';
import { Plus, X } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { Field } from './ui/Field.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { Note } from './ui/Note.jsx';
import { cameraArrow } from './shot-look.js';
import { sceneBeats } from './script-beats.js';
import { SIZES, CONTINUITY, planChecklist, planOf, previousExit } from './shot-plan.js';
import './shot-plan.css';

// The beats this shot claims, from its scene.
function claimedBeats(state, shot) {
  const plan = planOf(shot.entity);
  const scene = (state.scenes || []).find((s) => s.id === shot.entity?.parentId);
  const claimed = new Set(plan.beats);
  return sceneBeats(scene).filter((b) => claimed.has(b.id));
}

function useChecklist(shot) {
  const { state } = useStore();
  const beats = claimedBeats(state, shot);
  const speakers = [
    ...new Set(beats.filter((b) => b.kind === 'line' && b.speaker).map((b) => b.speaker)),
  ];
  const characters = state.profiles.filter((p) => p.type === 'character');
  return { beats, speakers, characters, items: planChecklist(shot, { speakers, characters }) };
}

/** On the shot card: size and camera, what the audience learns, the line(s), readiness. */
export function ShotCardPlan({ shot }) {
  const { t } = useI18n();
  const plan = planOf(shot.entity);
  const { beats, characters, items } = useChecklist(shot);
  const done = items.filter((item) => item.done).length;
  const names = new Map(characters.map((c) => [c.id, c]));
  const lines = beats.filter((b) => b.kind === 'line' && b.text.trim());
  if (!plan.purpose && !plan.size && !lines.length && !plan.beats.length) return null;
  return (
    <div className="card-plan">
      <div className="card-plan-row">
        {plan.size && <span className="card-plan-chip">{t('plan.size.' + plan.size)}</span>}
        {shot.camera && (
          <span className="card-plan-chip">
            {cameraArrow(shot.camera)} {t('shotLook.camera.' + shot.camera)}
          </span>
        )}
        <span
          className="card-plan-ring"
          style={{ '--v': Math.round((done / items.length) * 100) }}
          role="img"
          aria-label={t('plan.readyLabel', { done, total: items.length })}
          title={items
            .map((item) => (item.done ? '✓ ' : '· ') + t('plan.check.' + item.key))
            .join('\n')}
        />
      </div>
      {plan.purpose && <div className="card-plan-purpose">{plan.purpose}</div>}
      {lines.slice(0, 2).map((line) => {
        const who = names.get(line.speaker);
        return (
          <div key={line.id} className="card-plan-line">
            {who?.bible?.speaker && <b>{who.bible.speaker}</b>} {who?.name || '?'}：{line.text}
          </div>
        );
      })}
    </div>
  );
}

function Timeline({ rows, onChange }) {
  const { t } = useI18n();
  const set = (index, change) =>
    onChange(rows.map((r, i) => (i === index ? { ...r, ...change } : r)));
  const num = (value) =>
    value === '' ? null : Math.max(0, Math.min(600, Math.round(+value * 10) / 10));
  return (
    <div className="plan-timeline">
      {rows.map((row, index) => (
        <div key={index} className="plan-timeline-row">
          <input
            type="number"
            step="0.1"
            min={0}
            aria-label={t('plan.from')}
            value={row.from ?? ''}
            onChange={(e) => set(index, { from: num(e.target.value) })}
          />
          <span className="muted">–</span>
          <input
            type="number"
            step="0.1"
            min={0}
            aria-label={t('plan.to')}
            value={row.to ?? ''}
            onChange={(e) => set(index, { to: num(e.target.value) })}
          />
          <span className="muted">{t('plan.secondsUnit')}</span>
          <input
            value={row.text}
            maxLength={600}
            aria-label={t('plan.action')}
            placeholder={t('plan.actionHint')}
            onChange={(e) => set(index, { text: e.target.value })}
          />
          <IconButton
            size="sm"
            label={t('plan.removeRow')}
            onClick={() => onChange(rows.filter((_, i) => i !== index))}
          >
            <X size={13} />
          </IconButton>
        </div>
      ))}
      {rows.length < 12 && (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            const last = rows.at(-1);
            onChange([...rows, { from: last?.to ?? 0, to: null, text: '' }]);
          }}
        >
          <Plus size={13} aria-hidden="true" />
          {t('plan.addRow')}
        </Button>
      )}
    </div>
  );
}

/** In the shot editor: plan, acting and continuity tabs plus the pre-generation checklist. */
export function ShotPlanPanel({ shot }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const [tab, setTab] = useState('plan');
  const plan = planOf(shot.entity);
  const { beats, speakers, characters, items } = useChecklist(shot);
  const set = (patch) => dispatch({ type: 'shotPlan', shot: shot.id, patch });
  const text = (key, labelKey, hintKey, rows = 1) => (
    <Field label={t(labelKey)}>
      {rows > 1 ? (
        <textarea
          rows={rows}
          maxLength={600}
          value={plan[key]}
          placeholder={hintKey ? t(hintKey) : undefined}
          onChange={(e) => set({ [key]: e.target.value })}
        />
      ) : (
        <input
          value={plan[key]}
          maxLength={key === 'purpose' ? 600 : 160}
          placeholder={hintKey ? t(hintKey) : undefined}
          onChange={(e) => set({ [key]: e.target.value })}
        />
      )}
    </Field>
  );
  const inFrame = shot.cast.map((c) => characters.find((p) => p.id === c.id)).filter(Boolean);
  const before = previousExit(state.shots, shot.id);
  const done = items.filter((item) => item.done).length;
  return (
    <section className="shot-plan" aria-label={t('plan.title')}>
      <header className="shot-plan-head">
        <Segmented
          tabs
          label={t('plan.title')}
          value={tab}
          onChange={setTab}
          options={['plan', 'acting', 'continuity'].map((value) => ({
            value,
            label: t('plan.tab.' + value),
          }))}
        />
        <span className="muted shot-plan-count">
          {t('plan.readyLabel', { done, total: items.length })}
        </span>
      </header>

      {tab === 'plan' && (
        <div className="shot-plan-body">
          {text('purpose', 'plan.purpose', 'plan.purposeHint', 2)}
          {beats.length > 0 ? (
            <ol className="shot-plan-beats">
              {beats.map((b) => (
                <li key={b.id} className={b.kind}>
                  {b.kind === 'line'
                    ? (characters.find((c) => c.id === b.speaker)?.name || '?') + '：' + b.text
                    : b.text}
                </li>
              ))}
            </ol>
          ) : (
            <Note tone="info">{t('plan.noBeats')}</Note>
          )}
          <div className="shot-plan-grid">
            <Field label={t('plan.sizeLabel')}>
              <select value={plan.size} onChange={(e) => set({ size: e.target.value })}>
                <option value="">{t('plan.notSet')}</option>
                {SIZES.map((size) => (
                  <option key={size} value={size}>
                    {t('plan.size.' + size)}
                  </option>
                ))}
              </select>
            </Field>
            {text('lens', 'plan.lens', 'plan.lensHint')}
            {text('angle', 'plan.angle', 'plan.angleHint')}
            {text('eyeline', 'plan.eyeline', 'plan.eyelineHint')}
          </div>
        </div>
      )}

      {tab === 'acting' && (
        <div className="shot-plan-body">
          <p className="muted small">{t('plan.emotionHelp')}</p>
          <div className="shot-plan-grid">
            {text('emotionFrom', 'plan.emotionFrom', 'plan.emotionFromHint')}
            {text('emotionTo', 'plan.emotionTo', 'plan.emotionToHint')}
          </div>
          <span className="shot-plan-label">{t('plan.timeline')}</span>
          <p className="muted small">{t('plan.timelineHelp')}</p>
          <Timeline rows={plan.timeline} onChange={(timeline) => set({ timeline })} />
          {inFrame.length > 1 && (
            <>
              <span className="shot-plan-label">{t('plan.mouths')}</span>
              <p className="muted small">{t('plan.mouthsHelp')}</p>
              <div className="shot-plan-mouths">
                {inFrame.map((c) => {
                  const speaks = speakers.includes(c.id);
                  const silent = plan.silent.includes(c.id);
                  return (
                    <label key={c.id} className={'shot-plan-mouth' + (speaks ? ' speaks' : '')}>
                      <input
                        type="checkbox"
                        checked={speaks || silent}
                        disabled={speaks}
                        onChange={(e) =>
                          set({
                            silent: e.target.checked
                              ? [...plan.silent, c.id]
                              : plan.silent.filter((id) => id !== c.id),
                          })
                        }
                      />
                      {c.name}
                      <small className="muted">
                        {t(speaks ? 'plan.speaks' : silent ? 'plan.lipsClosed' : 'plan.unsure')}
                      </small>
                    </label>
                  );
                })}
              </div>
            </>
          )}
        </div>
      )}

      {tab === 'continuity' && (
        <div className="shot-plan-body">
          <Field label={t('plan.continuity')}>
            <Segmented
              label={t('plan.continuity')}
              value={plan.continuity || 'fresh'}
              onChange={(continuity) => set({ continuity })}
              options={CONTINUITY.map((value) => ({ value, label: t('plan.mode.' + value) }))}
            />
          </Field>
          <p className="muted small">{t('plan.mode.' + (plan.continuity || 'fresh') + 'Help')}</p>
          {text('entry', 'plan.entry', 'plan.entryHint', 2)}
          {before && before !== plan.entry && (
            <Button size="sm" variant="ghost" onClick={() => set({ entry: before })}>
              {t('plan.copyExit')}
            </Button>
          )}
          {text('exit', 'plan.exit', 'plan.exitHint', 2)}
        </div>
      )}
    </section>
  );
}
