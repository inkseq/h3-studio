import { useI18n } from './i18n/I18nProvider.jsx';
import { legacyReason } from './i18n/legacyReasons.js';
import React, { useEffect, useState } from 'react';
import { store } from './store.js';
import { captionStatus, exportSrt } from './caption-model.js';
import { chapterHandover, shotListCsv } from './handover-model.js';
import './handover.css';
function TextDownload({ text, mime, name, disabled, children }) {
  const { t: lt, locale } = useI18n();
  const [url, setUrl] = useState('');
  useEffect(() => {
    if (disabled) {
      setUrl('');
      return;
    }
    const next = URL.createObjectURL(new Blob([text], { type: mime }));
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [text, mime, disabled]);
  return disabled || !url ? (
    <button disabled>{children}</button>
  ) : (
    <a
      className="handover-download"
      href={url}
      download={name}
      onClick={() => store.notify(lt('handover.ui29'))}
    >
      {children}
    </a>
  );
}
export default function ChapterHandover({ project, chapterId, onFix }) {
  const { t: lt, locale } = useI18n();
  if (!chapterId) return null;
  const manifest = chapterHandover(project, chapterId, {
      cloud: store.getState().workspace.mode === 'cloud',
    }),
    captions = captionStatus(project, chapterId),
    name = (project.title + '-' + manifest.chapter_title).replace(
      /[<>:"/\\|?*\u0000-\u001f]/g,
      '_',
    );
  return (
    <section className="chapter-handover" aria-label={lt('handover.ui30')}>
      <h3>{lt('handover.ui31')}</h3>
      <p>
        {lt('handover.summary', {
          shots: manifest.shots.length,
          assets: manifest.assets.length,
          tracks: manifest.audio_tracks.length,
        })}
      </p>
      <div className="cloud-actions">
        <TextDownload
          disabled={!manifest.shots.length}
          text={shotListCsv(manifest)}
          mime="text/csv;charset=utf-8"
          name={name + '-镜头清单.csv'}
        >
          {lt('handover.ui32')}
        </TextDownload>
        <TextDownload
          disabled={!manifest.shots.length}
          text={JSON.stringify(manifest, null, 2)}
          mime="application/json;charset=utf-8"
          name={name + '-素材引用与审片备注.json'}
        >
          {lt('handover.ui33')}
        </TextDownload>
      </div>
      <p>{lt('handover.ui34')}</p>
      <details>
        <summary>{lt('handover.ui35')}</summary>
        <pre className="handover-preview">{JSON.stringify(manifest, null, 2)}</pre>
      </details>
      <h4>{lt('handover.ui36')}</h4>
      <p>
        {lt('handover.cues', { count: captions.track.cues?.length || 0 })} ·{' '}
        {captions.confirmed ? lt('handover.ui37') : lt('handover.ui38')}{' '}
        {lt('handover.subtitleInfo')}
      </p>
      <div className="cloud-actions">
        <button onClick={() => onFix({ id: 'caption-edit', stage: 6 })}>
          {lt('handover.ui39')}
        </button>
        <TextDownload
          disabled={!captions.confirmed}
          text={captions.confirmed ? exportSrt(project, chapterId) : ''}
          mime="application/x-subrip;charset=utf-8"
          name={name + '-手动字幕.srt'}
        >
          {lt('handover.ui40')}
        </TextDownload>
      </div>
      {captions.confirmed && (
        <details>
          <summary>{lt('handover.ui41')}</summary>
          <pre className="handover-preview">{exportSrt(project, chapterId)}</pre>
        </details>
      )}
    </section>
  );
}
