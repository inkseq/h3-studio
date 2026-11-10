// Waveforms for the Edit sound lane (inkseq-design#232, items 19–20): the loudest sample in each
// short slice of a file, read once per file in this browser tab and kept in memory. A clip's
// own sound is read from its video. Files that cannot be decoded simply show no waveform.
import { useEffect, useState } from 'react';

export const PEAKS_PER_SECOND = 40;
const MAX_PEAKS = 4000;
const cache = new Map();

/** Peaks (0–1) of one channel-mixed signal, `perSecond` per second, at most MAX_PEAKS. */
export function peaksOf(channels, sampleRate, perSecond = PEAKS_PER_SECOND) {
  const length = channels[0]?.length || 0;
  if (!length || !(sampleRate > 0)) return [];
  const count = Math.min(MAX_PEAKS, Math.max(1, Math.ceil((length / sampleRate) * perSecond)));
  const size = length / count,
    out = new Array(count).fill(0);
  for (let i = 0; i < count; i++) {
    const from = Math.floor(i * size),
      to = Math.min(length, Math.floor((i + 1) * size));
    let peak = 0;
    for (const channel of channels)
      for (let j = from; j < to; j++) {
        const value = Math.abs(channel[j]);
        if (value > peak) peak = value;
      }
    out[i] = Math.min(1, peak);
  }
  return out;
}

/** The part of a file's peaks between two source times, as an SVG path in a 100×100 box. */
export function peaksPath(peaks, duration, from, to) {
  if (!peaks?.length || !(duration > 0) || !(to > from)) return '';
  const first = Math.max(0, Math.floor((from / duration) * peaks.length)),
    last = Math.min(peaks.length, Math.ceil((to / duration) * peaks.length));
  const slice = peaks.slice(first, last);
  if (!slice.length) return '';
  const step = 100 / slice.length;
  return slice
    .map((peak, i) => {
      const half = Math.max(0.5, peak * 50);
      return `M${(i * step + step / 2).toFixed(2)} ${(50 - half).toFixed(2)}V${(50 + half).toFixed(2)}`;
    })
    .join('');
}

async function decode(url) {
  const Context = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
  if (!Context) return null;
  const data = await (await fetch(url)).arrayBuffer();
  const buffer = await new Context(1, 1, 44100).decodeAudioData(data);
  const channels = Array.from({ length: buffer.numberOfChannels }, (_, i) =>
    buffer.getChannelData(i),
  );
  return { peaks: peaksOf(channels, buffer.sampleRate), duration: buffer.duration };
}

/** { peaks, duration } for a media URL once decoded, else null. */
export function usePeaks(url) {
  const [value, setValue] = useState(() => (url && cache.get(url)?.value) || null);
  useEffect(() => {
    if (!url) return undefined;
    let alive = true;
    let entry = cache.get(url);
    if (!entry) {
      entry = { value: null, promise: decode(url).catch(() => null) };
      entry.promise.then((result) => (entry.value = result));
      cache.set(url, entry);
    }
    entry.promise.then((result) => alive && setValue(result));
    return () => {
      alive = false;
    };
  }, [url]);
  return url ? value : null;
}
