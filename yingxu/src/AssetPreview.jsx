import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useState } from 'react';
import { getFile } from './media.js';
export default function AssetPreview({ entity, alt, muted = false, compact = false, onMetadata }) {
  const { t: lt } = useI18n();
  const [url, setUrl] = useState(''),
    [error, setError] = useState('');
  useEffect(() => {
    let active = true,
      objectUrl;
    setUrl('');
    setError('');
    if (!entity?.data.fileId) {
      setError('legacy.mediaMissing');
      return;
    }
    getFile(entity.data.fileId)
      .then((blob) => {
        if (!active) return;
        if (!blob) {
          setError('legacy.mediaMissing');
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => active && setError('legacy.mediaRead'));
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [entity?.data.fileId]);
  if (error) return <p role="alert">{lt(error)}</p>;
  if (!url) return <p>{lt('legacy.copy25')}</p>;
  const failed = () => setError('legacy.mediaFormat');
  const metadata = (event) => {
    const media = event.currentTarget;
    if (Number.isFinite(media.duration) && media.duration > 0)
      onMetadata?.({
        duration: media.duration,
        ...(entity.type === 'video' ? { width: media.videoWidth, height: media.videoHeight } : {}),
      });
  };
  return entity.type === 'video' ? (
    <video
      src={url}
      controls={!compact}
      muted={muted || compact}
      playsInline
      preload="metadata"
      aria-label={alt || entity.title}
      onLoadedMetadata={metadata}
      onError={failed}
    />
  ) : entity.type === 'audio' ? (
    <audio
      src={url}
      controls
      preload="metadata"
      aria-label={alt || entity.title}
      onLoadedMetadata={metadata}
      onError={failed}
    />
  ) : (
    <img src={url} alt={alt || entity.title} onError={failed} />
  );
}
