// Review one take (h3-studio#626): a whole-take verdict — usable or redo — and range marks
// that say which second to keep or change. Picks, stars and parts never set either one.
// Read and saved through the shared data layer: only this panel's own buttons wait.
import React, { useEffect, useRef, useState } from 'react';
import { Button } from './ui/Button.jsx';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { FloatingMessage } from './floating-notice.js';
import {
  TAGS,
  fmtSeconds,
  marksBody,
  newMark,
  nextVerdict,
  rangeFrom,
} from './take-review-model.js';
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

export default function TakeReview({ shotId, artifactId, mediaRef }) {
  const { t } = useI18n(),
    { state, adapter } = useStoryboardUI(),
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
    [message, setMessage] = useState('');
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
      if (error?.status === 409) void read.refetch();
    },
  });
  const data = read.data,
    shown = save.isPending ? { ...data, ...save.variables.next } : data,
    marks = shown?.marks || [],
    saving = !data || save.isPending;
  function commit(next) {
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
  const seek = (time) => {
    const video = mediaRef.current?.querySelector('video');
    if (!video) return;
    video.pause();
    video.currentTime = time;
  };
  const pct = (value) => (duration ? (value / duration) * 100 : 0) + '%';
  const draft = editing?.mark;
  const setDraft = (patch) => setEditing((old) => ({ ...old, mark: { ...old.mark, ...patch } }));
  return (
    <section className="take-review" aria-label={t('review.title')}>
      <header className="take-review-head">
        <span className="take-review-title">{t('review.title')}</span>
        <div className="take-review-verdict" role="group" aria-label={t('review.verdict')}>
          {['keep', 'redo'].map((value) => (
            <Button
              key={value}
              className={'take-review-pick ' + value + (shown?.verdict === value ? ' on' : '')}
              aria-pressed={shown?.verdict === value}
              disabled={saving}
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
        role="presentation"
        onPointerDown={begin}
      >
        {marks.map((mark) => (
          <Button
            key={mark.id}
            className={'take-review-mark ' + mark.verdict}
            style={{ left: pct(mark.start_s), width: pct(mark.end_s - mark.start_s) }}
            aria-label={t('review.markLabel', {
              from: fmtSeconds(mark.start_s),
              to: fmtSeconds(mark.end_s),
              verdict: t('review.range.' + mark.verdict),
            })}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => {
              seek(mark.start_s);
              setEditing({ mark: { ...mark }, isNew: false });
            }}
          />
        ))}
        {drag && (
          <span
            className="take-review-mark draft"
            style={{ left: pct(drag.start_s), width: pct(drag.end_s - drag.start_s) }}
          />
        )}
      </div>
      <small className="take-review-hint muted">
        {t(duration ? (marks.length ? 'review.hintMore' : 'review.hint') : 'review.waiting')}
      </small>

      {draft && (
        <div className="take-review-editor">
          <div className="take-review-editor-row">
            <span className="take-review-range">
              {fmtSeconds(draft.start_s)} – {fmtSeconds(draft.end_s)}
            </span>
            <Segmented
              label={t('review.rangeVerdict')}
              value={draft.verdict}
              onChange={(verdict) => setDraft({ verdict })}
              options={[
                { value: 'change', label: t('review.range.change') },
                { value: 'keep', label: t('review.range.keep') },
              ]}
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
            <Button
              variant="primary"
              disabled={saving}
              onClick={() => {
                commit({
                  marks: editing.isNew
                    ? [...marks, draft]
                    : marks.map((m) => (m.id === draft.id ? draft : m)),
                });
                setEditing(null);
              }}
            >
              {t('review.save')}
            </Button>
          </div>
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
                <span className={'take-review-dot ' + mark.verdict} aria-hidden="true" />
                <span className="take-review-range">
                  {fmtSeconds(mark.start_s)} – {fmtSeconds(mark.end_s)}
                </span>
                <b>{t('review.range.' + mark.verdict)}</b>
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
