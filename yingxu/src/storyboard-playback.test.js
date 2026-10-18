import test from 'node:test';
import assert from 'node:assert/strict';
import { createStoryboardPlayback } from './storyboard-playback.js';
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const settle = async () => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};
function media({ readyState = 4 } = {}) {
  const requests = [];
  return {
    paused: true,
    currentTime: 0,
    duration: 5.167,
    readyState,
    requests,
    pauseCalls: 0,
    play() {
      const request = deferred();
      requests.push(request);
      return request.promise;
    },
    pause() {
      this.pauseCalls++;
      this.paused = true;
    },
  };
}
function sync(player, video, playing = true, onFailure = () => {}) {
  player.sync({ video, time: 0, playing, onFailure });
}
test('next chosen clip starts automatically and an obsolete AbortError cannot stop it', async () => {
  let failures = 0;
  const playback = createStoryboardPlayback(),
    first = media(),
    second = media(),
    failure = () => failures++;
  sync(playback, first, true, failure);
  assert.equal(first.requests.length, 1);
  sync(playback, null, true, failure); // the next owner-scoped Blob is still being read
  sync(playback, second, true, failure);
  assert.equal(second.requests.length, 1);
  second.paused = false;
  second.requests[0].resolve();
  await settle();
  first.requests[0].reject(Object.assign(new Error('source removed'), { name: 'AbortError' }));
  await settle();
  assert.equal(failures, 0);
  assert.equal(second.paused, false);
});
test('a real current clip play failure pauses the active timeline', async () => {
  let failures = 0;
  const playback = createStoryboardPlayback(),
    video = media();
  sync(playback, video, true, () => failures++);
  video.requests[0].reject(Object.assign(new Error('blocked'), { name: 'NotAllowedError' }));
  await settle();
  assert.equal(failures, 1);
  assert.equal(video.paused, true);
});
test('RAF sync shares one pending play request and never replays an already playing clip', async () => {
  const playback = createStoryboardPlayback(),
    video = media();
  sync(playback, video);
  sync(playback, video);
  sync(playback, video);
  assert.equal(video.requests.length, 1);
  video.paused = false;
  video.requests[0].resolve();
  await settle();
  sync(playback, video);
  assert.equal(video.requests.length, 1);
});
test('pending metadata waits, then the next clip starts with the existing play intent', () => {
  const playback = createStoryboardPlayback(),
    video = media({ readyState: 0 });
  sync(playback, video);
  assert.equal(video.requests.length, 0);
  video.readyState = 1;
  sync(playback, video);
  assert.equal(video.requests.length, 1);
});
test('pause then resume ignores the interrupted promise from the prior play intent', async () => {
  let failures = 0;
  const playback = createStoryboardPlayback(),
    video = media(),
    failure = () => failures++;
  sync(playback, video, true, failure);
  sync(playback, video, false, failure);
  sync(playback, video, true, failure);
  assert.equal(video.requests.length, 2);
  video.requests[0].reject(Object.assign(new Error('paused'), { name: 'AbortError' }));
  await settle();
  assert.equal(failures, 0);
  video.paused = false;
  video.requests[1].resolve();
  await settle();
  assert.equal(video.paused, false);
});
test('an obsolete play resolution cannot leave the previous clip sounding', async () => {
  const playback = createStoryboardPlayback(),
    first = media(),
    second = media();
  sync(playback, first);
  sync(playback, second);
  first.paused = false;
  first.requests[0].resolve();
  await settle();
  assert.equal(first.paused, true);
});
test('cleanup invalidates pending playback and remains usable after effect remount', async () => {
  let failures = 0;
  const playback = createStoryboardPlayback(),
    first = media(),
    second = media();
  sync(playback, first, true, () => failures++);
  playback.reset();
  first.requests[0].reject(new Error('unmounted'));
  await settle();
  assert.equal(failures, 0);
  sync(playback, second);
  assert.equal(second.requests.length, 1);
});
