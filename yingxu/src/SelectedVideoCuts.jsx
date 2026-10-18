import { useI18n } from './i18n/I18nProvider.jsx';
import { legacyReason } from './i18n/legacyReasons.js';
import GeneratedSoundAction from './GeneratedSoundAction.jsx';
import React, { useState } from 'react';
import { store } from './store.js';
import { LocalRangePreview, RangeFields } from './media-controls.jsx';
import {
  alignVideoRange,
  applyVideoRange,
  boundVideoRange,
  videoBinding,
  videoCut,
} from './video-cut-model.js';

const show = (value) => (Number.isFinite(value) ? Number(value.toFixed(4)) : '—');
function CutEditor({ shot, asset, onDone }) {
  const { t: lt, locale } = useI18n();
  const existing = boundVideoRange(shot.data.selectedVideoRange, asset)
    ? shot.data.selectedVideoRange
    : null;
  const [start, setStart] = useState(String(existing?.start ?? 0)),
    [end, setEnd] = useState(existing ? String(existing.end) : ''),
    [duration, setDuration] = useState(null),
    [position, setPosition] = useState(0),
    [error, setError] = useState('');
  const aligned = start !== '' && end !== '' ? alignVideoRange(Number(start), Number(end)) : null;
  const save = (useRangeDuration) => {
    if (start === '' || end === '') {
      setError(lt('handover.ui1'));
      return;
    }
    const range = { ...videoBinding(asset), start: Number(start), end: Number(end) };
    let result;
    const ok = store.editProject((p) => {
      result = applyVideoRange(p, shot.id, range, { duration, useRangeDuration });
    });
    if (!ok) {
      setError(legacyReason(store.getState().notice, locale));
      return;
    }
    store.notify(
      useRangeDuration
        ? lt('handover.ui2')
        : lt('cut.saved', { start: show(result.sourceStart), end: show(result.sourceEnd) }),
    );
    onDone();
  };
  return (
    <div className="selected-cut-editor">
      <h4>{lt('cut.title', { title: shot.title })}</h4>
      <p>{lt('handover.ui3')}</p>
      <LocalRangePreview
        entity={asset}
        start={start}
        end={end}
        duration={duration}
        onDuration={(value) => {
          setDuration(value);
          setEnd((old) => (old === '' ? String(value) : old));
        }}
        onPosition={setPosition}
      />
      <p className="muted">{lt('cut.position', { seconds: show(position) })}</p>
      <div className="media-control-row">
        <button
          disabled={!duration}
          type="button"
          onClick={() => setStart(String(Number(position.toFixed(6))))}
        >
          {lt('handover.ui4')}
        </button>
        <button
          disabled={!duration || position <= 0}
          type="button"
          onClick={() => setEnd(String(Number(position.toFixed(6))))}
        >
          {lt('handover.ui5')}
        </button>
      </div>
      <RangeFields
        start={start}
        end={end}
        setStart={setStart}
        setEnd={setEnd}
        prefix={lt('handover.ui6')}
        duration={duration}
      />
      {aligned && aligned.frames > 0 ? (
        <p role="status">
          {lt('cut.aligned', {
            start,
            end,
            actualStart: show(aligned.start),
            actualEnd: show(aligned.end),
            frames: aligned.frames,
            duration: show(aligned.duration),
          })}
        </p>
      ) : (
        <p className="muted">{lt('handover.ui7')}</p>
      )}
      <p className="muted">{lt('handover.ui8')}</p>
      {error && (
        <p className="media-control-error" role="alert">
          {error}
        </p>
      )}
      <div className="media-control-row">
        <button disabled={!duration} type="button" onClick={() => save(false)}>
          {lt('cut.keep', { seconds: show(shot.data.seconds) })}
        </button>
        <button
          disabled={!duration || !aligned || aligned.frames < 1}
          type="button"
          onClick={() => save(true)}
        >
          {lt('handover.ui9')}
        </button>
        <button type="button" onClick={onDone}>
          {lt('handover.ui10')}
        </button>
      </div>
    </div>
  );
}
export default function SelectedVideoCuts({ project, shots, onChange = () => {} }) {
  const { t: lt, locale } = useI18n();
  const [editing, setEditing] = useState(null);
  const selected = shots.filter(
    (shot) =>
      project.entities.find((e) => e.id === shot.data.selectedAssetId)?.type === 'video' ||
      shot.data.selectedVideoRange,
  );
  return (
    <section className="selected-video-cuts" aria-label={lt('handover.ui11')}>
      <h3>{lt('handover.ui12')}</h3>
      <p>{lt('handover.ui13')}</p>
      {!selected.length && <p className="muted">{lt('handover.ui14')}</p>}
      {selected.map((shot) => {
        const asset = project.entities.find((e) => e.id === shot.data.selectedAssetId),
          cut = videoCut(shot, asset);
        return (
          <article className="selected-cut" key={shot.id}>
            <div className="cloud-section-head">
              <b>
                {shot.title} · {asset?.title || lt('handover.ui15')}
              </b>
              <button
                type="button"
                disabled={asset?.type !== 'video' || !asset?.data.fileId || asset.data.missingFile}
                onClick={() => {
                  onChange();
                  setEditing(editing === shot.id ? null : shot.id);
                }}
              >
                {editing === shot.id ? lt('handover.ui16') : lt('handover.ui17')}
              </button>
            </div>
            {cut.issue ? (
              <p className="cloud-warning">{legacyReason(cut.issue, locale)}</p>
            ) : (
              <p>
                {lt('cut.range', {
                  start: show(cut.sourceStart),
                  end: show(cut.sourceEnd),
                  seconds: show(shot.data.seconds),
                })}
                {cut.unusedTailFrames
                  ? lt('cut.tail', { seconds: show(cut.unusedTailFrames / 24) })
                  : ''}
                。{cut.range ? lt('handover.ui18') : lt('handover.ui19')}
              </p>
            )}
            {shot.data.selectedVideoRange && (
              <button
                type="button"
                onClick={() => {
                  onChange();
                  store.editProject((p) => {
                    delete p.entities.find((e) => e.id === shot.id).data.selectedVideoRange;
                  });
                  setEditing(null);
                  store.notify(lt('handover.ui20'));
                }}
              >
                {lt('handover.ui21')}
              </button>
            )}
            {asset?.type === 'video' && (
              <GeneratedSoundAction key={shot.id + ':' + asset.id} project={project} shot={shot} />
            )}
            {editing === shot.id && asset?.type === 'video' && (
              <CutEditor
                key={shot.id + ':' + JSON.stringify(videoBinding(asset))}
                shot={shot}
                asset={asset}
                onDone={() => setEditing(null)}
              />
            )}
          </article>
        );
      })}
    </section>
  );
}
