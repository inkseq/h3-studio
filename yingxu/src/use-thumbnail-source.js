import { useEffect, useRef, useState } from 'react';
import { thumbnailQueue } from './library-thumbnails.js';

// Admission is for small, immutable renditions only. It never fetches an
// original, persists a URL or retries a failed read under a later identity.
export function useThumbnailSource(owner, url, { enabled = true } = {}) {
  const ref = useRef(null),
    release = useRef(null),
    [visible, setVisible] = useState(null),
    [started, setStarted] = useState(null);
  useEffect(() => {
    setVisible(null);
    if (!enabled || !url || !ref.current) return;
    if (typeof IntersectionObserver === 'undefined') {
      setVisible({ owner, url });
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible({ owner, url });
          observer.disconnect();
        }
      },
      { rootMargin: '160px' },
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [owner, url, enabled]);
  useEffect(() => {
    setStarted(null);
    release.current = null;
    if (!enabled || visible?.owner !== owner || visible?.url !== url || !owner || !url) return;
    let live = true;
    const cancel = thumbnailQueue(owner).enqueue((done) => {
      if (!live) {
        done();
        return;
      }
      const entry = { owner, url, done };
      release.current = entry;
      setStarted(entry);
    });
    return () => {
      live = false;
      cancel();
      release.current = null;
    };
  }, [owner, url, enabled, visible]);
  const current = started?.owner === owner && started?.url === url ? started : null;
  return {
    ref,
    src: current ? url : undefined,
    finish: () => {
      if (!current || release.current !== current) return;
      current.done();
      release.current = null;
    },
  };
}
