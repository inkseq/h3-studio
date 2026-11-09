// The caption lane under the clips in the film editor, and the caption bar: add or build cues,
// edit the selected one, and confirm this version for burn-in. The burn-in export only takes a
// confirmed version (caption-model.js); confirming is here again since the subtitle studio was
// removed (#514), with the same snapshot as before (story audit 2026-11-09).
import React, { useState } from 'react';
import { AlertTriangle, Check, Plus, Trash2, Wand2 } from 'lucide-react';
import { Button, IconButton } from './ui/Button.jsx';
import { Popover } from './ui/Popover.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { CAPTION_LINE, CAPTION_LINES, MAX_CUE_TEXT, captionLines } from './caption-edit.js';

// The export's own wording (caption-model.js) is Chinese; each kind gets a short line in both
// languages. Anything not listed is shown as written.
const PROBLEM_WORDS = [
  ['最多2行', 'editor.problemLines'],
  ['花括号', 'editor.problemChars'],
  ['不足1帧', 'editor.problemShort'],
  ['重叠', 'editor.problemOverlap'],
  ['章节', 'editor.problemPastEnd'],
  ['1–2000', 'editor.problemEmpty'],
  ['纯文字', 'editor.problemMarkup'],
  ['起止时间', 'editor.problemTime'],
  ['编号', 'editor.problemId'],
  ['先添加', 'editor.problemNone'],
];
function problemText(t, problem) {
  const key = PROBLEM_WORDS.find(([words]) => problem.message.includes(words))?.[1],
    text = key ? t(key) : problem.message;
  return problem.number ? t('editor.problemAt', { n: problem.number, text }) : text;
}

export function CaptionLane({ cues, x, width, selected, problems = [], onSelect }) {
  const { t } = useI18n();
  const flagged = new Set(problems.map((problem) => problem.cueId).filter(Boolean));
  return (
    <div
      className="film-caption-lane"
      style={{ width }}
      role="group"
      aria-label={t('editor.captionLane')}
      onPointerDown={(event) => event.target === event.currentTarget && onSelect(null)}
    >
      {!cues.length && <span className="film-audio-empty muted">{t('editor.noCaptions')}</span>}
      {cues.map((cue, index) => {
        const left = x(cue.start);
        return (
          <Button
            key={cue.id}
            data-cue-id={cue.id}
            data-timeline-item="caption"
            className={
              'film-caption-cue' +
              (selected === cue.id ? ' on' : '') +
              (flagged.has(cue.id) ? ' warn' : '')
            }
            style={{ left, width: Math.max(14, x(cue.end) - left) }}
            title={cue.text}
            aria-label={t('editor.cueLabel', { n: index + 1, text: cue.text || '…' })}
            aria-pressed={selected === cue.id}
            onClick={() => onSelect(cue.id)}
          >
            {String(cue.text || '…').replace(/\n/g, ' / ')}
          </Button>
        );
      })}
    </div>
  );
}

// The selected caption's text: two lines at most (Shift+Enter starts the second), a counter for
// each line, and Enter to finish and go back to the cue on the lane.
function CaptionText({ cue, busy, onSet, onDone }) {
  const { t } = useI18n();
  const lines = captionLines(cue.text);
  return (
    <div className="film-caption-text-box">
      <textarea
        className="film-caption-text"
        rows={CAPTION_LINES}
        // One line on screen per caption line, so the counters beside it line up.
        wrap="off"
        value={cue.text}
        maxLength={MAX_CUE_TEXT}
        placeholder={t('editor.captionText')}
        aria-label={t('editor.captionText')}
        aria-describedby={'cue-count-' + cue.id}
        aria-invalid={lines.some((line) => !line.ok) || undefined}
        disabled={busy}
        onChange={(event) => onSet(cue.id, { text: event.target.value })}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || event.nativeEvent.isComposing) return;
          if (!event.shiftKey) {
            event.preventDefault();
            onDone(cue.id);
          } else if (lines.length >= CAPTION_LINES) event.preventDefault();
        }}
      />
      <span className="film-caption-count" id={'cue-count-' + cue.id}>
        {lines.slice(0, Math.max(CAPTION_LINES, lines.length)).map((line, index) => (
          <span key={index} className={line.ok ? 'muted' : 'over'}>
            {t('editor.lineCount', { n: line.length, max: CAPTION_LINE })}
          </span>
        ))}
      </span>
    </div>
  );
}

export function CaptionControls({
  cues,
  selected,
  busy,
  hasLines,
  problems = [],
  state = 'unconfirmed',
  onAdd,
  onFill,
  onSet,
  onRemove,
  onConfirm,
  onJump,
  onDone,
}) {
  const { t } = useI18n();
  const [armed, setArmed] = useState(false);
  const cue = cues.find((item) => item.id === selected);
  const time = (key) => (
    <input
      type="number"
      step="0.1"
      min={0}
      className="field-sm film-caption-time"
      aria-label={t(key === 'start' ? 'editor.cueStart' : 'editor.cueEnd')}
      value={cue[key]}
      disabled={busy}
      onChange={(event) => onSet(cue.id, { [key]: Number(event.target.value) })}
    />
  );
  const status = !cues.length
    ? t('editor.captionsNone')
    : problems.length
      ? t('editor.captionProblems', { count: problems.length })
      : state === 'confirmed'
        ? t('editor.captionsConfirmed')
        : state === 'stale'
          ? t('editor.captionsStale')
          : t('editor.captionsUnconfirmed');
  return (
    <div className="film-caption-bar">
      <div className="film-caption-tools">
        <Button size="sm" disabled={busy} onClick={onAdd}>
          <Plus size={13} aria-hidden="true" />
          {t('editor.addCaption')}
        </Button>
        {hasLines && (
          <Button
            size="sm"
            variant={armed ? 'primary' : 'ghost'}
            disabled={busy}
            title={t('editor.captionsFromLinesHint')}
            onClick={() => {
              // Replacing existing captions takes a second click on the same button.
              if (cues.length && !armed) return setArmed(true);
              setArmed(false);
              onFill();
            }}
            onBlur={() => setArmed(false)}
          >
            <Wand2 size={13} aria-hidden="true" />
            {t(armed ? 'editor.captionsReplace' : 'editor.captionsFromLines')}
          </Button>
        )}
      </div>
      {/* Reserved: the editor for the picked caption, or a hint in the same space. */}
      <div className="film-caption-edit">
        {cue ? (
          <>
            {time('start')}
            <span className="muted">–</span>
            {time('end')}
            <CaptionText cue={cue} busy={busy} onSet={onSet} onDone={onDone} />
            <IconButton
              size="sm"
              label={t('editor.removeCaption')}
              disabled={busy}
              onClick={() => onRemove(cue.id)}
            >
              <Trash2 size={13} />
            </IconButton>
          </>
        ) : (
          <small className="muted">{cues.length ? t('editor.captionPick') : ''}</small>
        )}
      </div>
      <div className="film-caption-check">
        <Button
          size="sm"
          variant={
            state === 'confirmed' || problems.length || !cues.length ? 'secondary' : 'primary'
          }
          disabled={busy || !cues.length || !!problems.length || state === 'confirmed'}
          title={t('editor.confirmCaptionsHint')}
          onClick={onConfirm}
        >
          <Check size={13} aria-hidden="true" />
          {t('editor.confirmCaptions')}
        </Button>
        {/* Last in the row, so its changing text never moves the button before it. */}
        {problems.length ? (
          <Popover
            align="end"
            className="film-caption-problems"
            trigger={
              <Button size="sm" variant="ghost" className="film-caption-status warn">
                <AlertTriangle size={13} aria-hidden="true" />
                {status}
              </Button>
            }
          >
            <p className="muted">{t('editor.problemsIntro')}</p>
            <ul>
              {problems.map((problem, index) => (
                <li key={index}>
                  {problem.cueId ? (
                    <Button variant="link" size="sm" onClick={() => onJump(problem.cueId)}>
                      {problemText(t, problem)}
                    </Button>
                  ) : (
                    <span>{problemText(t, problem)}</span>
                  )}
                </li>
              ))}
            </ul>
          </Popover>
        ) : (
          <span
            className={'film-caption-status' + (state === 'stale' && cues.length ? ' warn' : '')}
            role="status"
          >
            {status}
          </span>
        )}
      </div>
    </div>
  );
}
