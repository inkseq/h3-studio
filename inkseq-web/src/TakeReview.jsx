// Review one take (h3-studio#626): a whole-take verdict — usable or redo — and range marks
// that say which second to keep or change. Picks, stars and parts never set either one.
// Read and saved through the shared data layer: only this panel's own buttons wait.
import React, { useEffect, useEffectEvent, useRef, useState } from 'react';
import { Button } from './ui/Button.jsx';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Play, X } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { FloatingMessage } from './floating-notice.js';
import {
  CHOICES,
  TAGS,
  choiceOf,
  fmtSeconds,
  keptAfterCuts,
  marksBody,
  newMark,
  nextVerdict,
  rangeFrom,
  withChoice,
} from './take-review-model.js';
import { playRanges, segmentsAction, snapRange } from './shot-segments.js';
import { inOutRange, timeBarKey } from './storyboard-a11y.js';
import './take-review.css';

function useDuration(mediaRef) {
  const [duration, setDuration] = useState(null);
  useEffect(() => {
    const node = mediaRef.current?.querySelector('video');
    if (!node) return;
    const read = () => Number.isFinite(node.duration) && setDuration(node.duration);
    read();
    node.addEventListener('loadedmetadata', read);
    return () => node.removeEventListener('loadedmetadata', read);
  });
  return duration;
}

// Where the take's video is now, for the playhead on a time bar.
export function usePlayhead(getVideo) {
  const [now, setNow] = useState(0);
  useEffect(() => {
    const node = getVideo();
    if (!node) return undefined;
    const tick = () => setNow(node.currentTime || 0);
    tick();
    const names = ['timeupdate', 'seeked', 'loadedmetadata', 'play', 'pause'];
    for (const name of names) node.addEventListener(name, tick);
    return () => {
      for (const name of names) node.removeEventListener(name, tick);
    };
  });
  return [now, setNow];
}

export default function TakeReview({ shotId, artifactId, mediaRef, chosen = false }) {
  const { t } = useI18n(),
    { state, adapter, dispatch } = useStoryboardUI(),
    client = useQueryClient(),
    duration = useDuration(mediaRef),
    bar = useRef(null);
  const key = ['take-review', state.contextId, shotId, artifactId];
  const read = useQuery({
    queryKey: key,
    queryFn: () => adapter.readTakeReview(shotId, artifactId),
    enabled: !!adapter.readTakeReview,
  });
  const [editing, setEditing] = useState(null),
    [drag, setDrag] = useState(null),
    [message, setMessage] = useState(''),
    // A verdict pressed while another saves waits here, and already shows as chosen (#1032).
    [queued, setQueued] = useState(null),
    // Keyboard marking (audit 2026-11-09): I and O set these at the playhead, Enter marks.
    [inPoint, setInPoint] = useState(null),
    [outPoint, setOutPoint] = useState(null);
  const save = useMutation({
    mutationFn: ({ body }) =>
      adapter.saveTakeReview(shotId, artifactId, body, 'take-review-' + crypto.randomUUID()),
    onSuccess: (receipt, { next }) => {
      client.setQueryData(key, (old) => ({
        ...old,
        ...next,
        revision: receipt.revision,
        verdict: receipt.verdict,
        actor_kind: receipt.actor_kind,
        updated_at: receipt.updated_at,
      }));
      setMessage(t('review.saved'));
      void client.invalidateQueries({
        queryKey: ['take-review-summaries', state.contextId, shotId],
      });
    },
    onError: (error) => {
      setMessage(
        t(
          error?.status === 409
            ? 'review.conflict'
            : error?.status === 404
              ? 'review.unavailable'
              : 'review.failed',
        ),
      );
      setQueued(null);
      if (error?.status === 409) void read.refetch();
    },
  });
  const data = read.data,
    shown = {
      ...data,
      ...(save.isPending ? save.variables.next : null),
      ...queued,
    },
    marks = shown?.marks || [],
    saving = !data || save.isPending;
  const sendQueued = useEffectEvent(() => {
    if (save.isPending || !queued || !data) return;
    setQueued(null);
    commit(queued);
  });
  useEffect(() => sendQueued(), [save.isPending]);
  function commit(next) {
    if (save.isPending && 'verdict' in next && !('marks' in next)) {
      setQueued(next);
      return;
    }
    try {
      const body = { expected_revision: data.revision };
      if ('verdict' in next) body.verdict = next.verdict;
      if ('marks' in next) body.marks = marksBody(next.marks, duration);
      save.mutate({ body, next: 'marks' in next ? { ...next, marks: body.marks } : next });
    } catch (error) {
      setMessage(t(error.message.startsWith('review.') ? error.message : 'review.failed'));
    }
  }
  const at = (x) => {
    const rect = bar.current.getBoundingClientRect();
    return Math.max(0, Math.min(duration, ((x - rect.left) / rect.width) * duration));
  };
  function begin(event) {
    if (saving || !duration || event.button > 0) return;
    event.preventDefault();
    const el = event.currentTarget,
      start = at(event.clientX);
    el.setPointerCapture(event.pointerId);
    setDrag(rangeFrom(start, start, duration));
    const move = (e) => setDrag(rangeFrom(start, at(e.clientX), duration));
    const up = (e) => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      setDrag(null);
      const range = rangeFrom(start, at(e.clientX), duration);
      if (range) setEditing({ mark: newMark(range, new Set(marks.map((m) => m.id))), isNew: true });
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  }
  const video = () => mediaRef.current?.querySelector('video');
  const [now, setNow] = usePlayhead(video);
  const seek = (time) => {
    const node = video();
    if (!node) return;
    node.pause();
    node.currentTime = time;
    setNow(time);
  };
  const ids = () => new Set(marks.map((m) => m.id));
  function saveDraft() {
    commit({
      marks: editing.isNew
        ? [...marks, editing.mark]
        : marks.map((m) => (m.id === editing.mark.id ? editing.mark : m)),
    });
    setEditing(null);
  }
  // A new mark over the in–out range (or in point to the playhead), optionally with a choice.
  function markRange(choice) {
    const span = inOutRange(inPoint, outPoint, now),
      range = span && rangeFrom(span.start, span.end, duration);
    if (!range) return false;
    const mark = newMark(range, ids());
    setEditing({ mark: choice ? withChoice(mark, choice) : mark, isNew: true });
    setInPoint(null);
    setOutPoint(null);
    return true;
  }
  // The keys of review tools (timeBarKey): I/O, Enter, 1–4, arrows, J/K/L, Escape.
  function keys(event) {
    const action = timeBarKey(event),
      onBar = event.target === event.currentTarget;
    if (!action || !duration) return;
    const node = video(),
      clampTime = (value) => Math.max(0, Math.min(duration, value));
    switch (action.type) {
      case 'step':
        seek(clampTime(now + action.by));
        break;
      case 'seek':
        seek(action.to === 'start' ? 0 : duration);
        break;
      case 'toggle':
        if (!onBar && event.key === ' ') return;
        if (node?.paused) void node.play()?.catch?.(() => {});
        else node?.pause();
        break;
      case 'play':
        void node?.play()?.catch?.(() => {});
        break;
      case 'in':
        setInPoint(now);
        break;
      case 'out':
        // O without I marks from the start of the take.
        setOutPoint(now);
        if (inPoint === null) setInPoint(0);
        break;
      case 'enter':
        if (!onBar) return;
        if (editing) {
          if (saving) return;
          saveDraft();
        } else if (!markRange()) return;
        break;
      case 'choice':
        if (editing)
          setEditing((old) => ({ ...old, mark: withChoice(old.mark, CHOICES[action.index]) }));
        else if (!markRange(CHOICES[action.index])) return;
        break;
      case 'cancel':
        if (editing) setEditing(null);
        else if (inPoint !== null || outPoint !== null) {
          setInPoint(null);
          setOutPoint(null);
        } else return;
        break;
      case 'delete':
        if (!editing || editing.isNew || saving) return;
        commit({ marks: marks.filter((m) => m.id !== editing.mark.id) });
        setEditing(null);
        break;
      default:
        return;
    }
    event.preventDefault();
  }
  const pending = inOutRange(inPoint, outPoint, now);
  const pct = (value) => (duration ? (value / duration) * 100 : 0) + '%';
  const draft = editing?.mark;
  const setDraft = (patch) => setEditing((old) => ({ ...old, mark: { ...old.mark, ...patch } }));
  return (
    <section
      className={'take-review' + (editing || drag || inPoint !== null ? ' marking' : '')}
      aria-label={t('review.title')}
    >
      <header className="take-review-head">
        <span className="take-review-title">{t('review.title')}</span>
        <div className="take-review-verdict" role="group" aria-label={t('review.verdict')}>
          {['keep', 'redo'].map((value) => (
            <Button
              key={value}
              className={'take-review-pick ' + value + (shown?.verdict === value ? ' on' : '')}
              aria-pressed={shown?.verdict === value}
              disabled={!data}
              onClick={() => commit({ verdict: nextVerdict(shown.verdict, value) })}
            >
              {t('review.' + value)}
            </Button>
          ))}
        </div>
      </header>
      <div
        ref={bar}
        className={'take-review-bar' + (duration ? '' : ' unknown')}
        role="group"
        tabIndex={0}
        aria-label={t('review.barLabel', { at: fmtSeconds(now) })}
        aria-describedby={'take-review-keys-' + artifactId}
        aria-keyshortcuts="I O Enter 1 2 3 4 J K L"
        onPointerDown={begin}
        onKeyDown={keys}
      >
        {marks.map((mark) => (
          <Button
            key={mark.id}
            className={
              'take-review-mark ' +
              choiceOf(mark) +
              (editing && !editing.isNew && editing.mark.id === mark.id ? ' editing' : '')
            }
            style={{ left: pct(mark.start_s), width: pct(mark.end_s - mark.start_s) }}
            aria-label={t('review.markLabel', {
              from: fmtSeconds(mark.start_s),
              to: fmtSeconds(mark.end_s),
              verdict: t('review.range.' + choiceOf(mark)),
            })}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => {
              seek(mark.start_s);
              setEditing({ mark: { ...mark }, isNew: false });
            }}
          />
        ))}
        {/* An in point set with I (and O) shows until Enter makes it a mark. */}
        {!drag && !editing && pending && (
          <span
            className="take-review-mark draft pending"
            style={{ left: pct(pending.start), width: pct(pending.end - pending.start) }}
          />
        )}
        {duration > 0 && (
          <span
            className="take-review-playhead"
            aria-hidden="true"
            style={{ left: pct(Math.max(0, Math.min(duration, now))) }}
          />
        )}
        {/* The range stays on the bar while it is dragged and while its note is open. */}
        {(drag || editing?.isNew) && (
          <span
            className="take-review-mark draft"
            style={{
              left: pct((drag || draft).start_s),
              width: pct((drag || draft).end_s - (drag || draft).start_s),
            }}
          />
        )}
      </div>
      <small className="take-review-hint muted">
        {drag
          ? fmtSeconds(drag.start_s) + ' – ' + fmtSeconds(drag.end_s)
          : t(duration ? (marks.length ? 'review.hintMore' : 'review.hint') : 'review.waiting')}
      </small>
      <small className="take-review-keys muted" id={'take-review-keys-' + artifactId}>
        {t('review.keys')}
      </small>

      {draft && (
        <div className="take-review-editor">
          <div className="take-review-editor-row">
            <span className="take-review-range">
              {fmtSeconds(draft.start_s)} – {fmtSeconds(draft.end_s)}
            </span>
            {/* Play only the marked stretch (owner, 2026-11-09). */}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => playRanges(video(), [{ start: draft.start_s, end: draft.end_s }])}
            >
              <Play size={13} />
              {t('review.play')}
            </Button>
            <Segmented
              label={t('review.rangeVerdict')}
              value={choiceOf(draft)}
              onChange={(choice) =>
                setEditing((old) => ({ ...old, mark: withChoice(old.mark, choice) }))
              }
              options={CHOICES.map((value) => ({ value, label: t('review.range.' + value) }))}
            />
          </div>
          <div className="take-review-tags" role="group" aria-label={t('review.tags')}>
            {TAGS.map((tag) => (
              <Button
                key={tag}
                className={'take-review-tag' + (draft.tags.includes(tag) ? ' on' : '')}
                aria-pressed={draft.tags.includes(tag)}
                onClick={() =>
                  setDraft({
                    tags: draft.tags.includes(tag)
                      ? draft.tags.filter((x) => x !== tag)
                      : [...draft.tags, tag],
                  })
                }
              >
                {t('review.tag.' + tag)}
              </Button>
            ))}
          </div>
          <label className="take-review-note">
            <span className="visually-hidden">{t('review.note')}</span>
            <input
              value={draft.note}
              maxLength={500}
              placeholder={t('review.note')}
              onChange={(e) => setDraft({ note: e.target.value })}
            />
          </label>
          <div className="take-review-actions">
            {!editing.isNew && (
              <Button
                variant="ghost-danger"
                disabled={saving}
                onClick={() => {
                  commit({ marks: marks.filter((m) => m.id !== draft.id) });
                  setEditing(null);
                }}
              >
                {t('review.delete')}
              </Button>
            )}
            <span className="spacer" />
            <Button variant="ghost" onClick={() => setEditing(null)}>
              {t('review.cancel')}
            </Button>
            <Button variant="primary" disabled={saving} onClick={saveDraft}>
              {t('review.save')}
            </Button>
          </div>
        </div>
      )}

      {chosen && marks.some((mark) => choiceOf(mark) === 'cut') && duration && (
        <div className="take-review-apply">
          <span className="muted small">{t('review.cutHelp')}</span>
          <Button
            size="sm"
            onClick={async () => {
              const parts = keptAfterCuts(duration, marks)
                .map((part) => snapRange(part.start, part.end))
                .filter(Boolean);
              if (!parts.length) return setMessage(t('review.cutAll'));
              const result = await dispatch({
                type: 'workbenchAction',
                action: segmentsAction(shotId, parts),
              }).catch(() => ({ ok: false }));
              setMessage(t(result?.ok === false ? 'review.failed' : 'review.cutApplied'));
            }}
          >
            {t('review.applyCuts')}
          </Button>
        </div>
      )}

      {marks.length > 0 && (
        <ol className="take-review-list">
          {marks.map((mark) => (
            <li key={mark.id}>
              <Button
                className="take-review-row"
                onClick={() => {
                  seek(mark.start_s);
                  setEditing({ mark: { ...mark }, isNew: false });
                }}
              >
                <span className={'take-review-dot ' + choiceOf(mark)} aria-hidden="true" />
                <span className="take-review-range">
                  {fmtSeconds(mark.start_s)} – {fmtSeconds(mark.end_s)}
                </span>
                <b>{t('review.range.' + choiceOf(mark))}</b>
                <span className="take-review-row-tags">
                  {mark.tags.map((tag) => t('review.tag.' + tag)).join(' · ')}
                </span>
                {mark.note && <span className="take-review-row-note">{mark.note}</span>}
              </Button>
              <Button
                variant="ghost"
                className="round"
                aria-label={t('review.delete')}
                disabled={saving}
                onClick={() => commit({ marks: marks.filter((m) => m.id !== mark.id) })}
              >
                <X size={13} />
              </Button>
            </li>
          ))}
        </ol>
      )}
      <FloatingMessage text={message || null} onDismiss={() => setMessage('')} />
    </section>
  );
}
