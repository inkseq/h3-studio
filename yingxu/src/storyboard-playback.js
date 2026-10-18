// A source swap can abort an older play() promise after the next clip starts.
// Only the current element and play intent may change the timeline's controls.
export function createStoryboardPlayback() {
  let active = null,
    wantsPlay = false,
    pending = null,
    generation = 0,
    onFailure = null;
  const invalidate = () => {
    generation++;
    pending = null;
  };
  const current = (request) =>
    request.video === active && request.generation === generation && wantsPlay;
  const failed = (request) => {
    if (!current(request)) return;
    pending = null;
    wantsPlay = false;
    request.video.pause();
    onFailure?.();
  };
  return {
    sync({ video, time, playing, onFailure: failure }) {
      if (video !== active) {
        const previous = active;
        invalidate();
        active = video;
        previous?.pause();
      }
      wantsPlay = playing === true;
      onFailure = failure;
      if (!video) return;
      if (
        Number.isFinite(video.duration) &&
        Number.isFinite(time) &&
        Math.abs(video.currentTime - time) > 0.4
      )
        video.currentTime = Math.max(0, Math.min(time, video.duration));
      if (!wantsPlay) {
        invalidate();
        if (!video.paused) video.pause();
        return;
      }
      if (video.readyState < 1 || !video.paused || pending) return;
      const request = { video, generation: ++generation };
      pending = request;
      try {
        Promise.resolve(video.play())
          .then(() => {
            if (current(request)) pending = null;
            else if (video !== active || !wantsPlay) video.pause();
          })
          .catch(() => failed(request));
      } catch {
        failed(request);
      }
    },
    reset() {
      const previous = active;
      invalidate();
      active = null;
      wantsPlay = false;
      onFailure = null;
      previous?.pause();
    },
  };
}
