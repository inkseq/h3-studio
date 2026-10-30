import test from 'node:test';
import assert from 'node:assert/strict';
import { createCloudClient } from './cloud-client.js';
import { nativeContentRequest, createMediaReadGate } from './media-read-gate.js';

const origin = 'https://staging.inkseq.com',
  path = '/v1/assets/reference_1/content';
test('rendition URLs retain the captured owner and originals remain explicit downloads', () => {
  const client = createCloudClient();
  client.setAccount('superdan');
  const thumb = client.nativeContentURL(path + '?rendition=thumb');
  assert.equal(thumb, path + '?expected_account=superdan&rendition=thumb');
  assert.equal(
    client.nativeContentURL(path, { rendition: 'preview' }),
    path + '?expected_account=superdan&rendition=preview',
  );
  assert.equal(
    client.nativeContentURL(path, { download: true }),
    path + '?expected_account=superdan&download=1',
  );
  client.setAccount('supervan');
  assert.equal(thumb.includes('expected_account=superdan'), true);
  assert.equal(
    client.nativeContentURL(path, { rendition: 'thumb' }),
    path + '?expected_account=supervan&rendition=thumb',
  );
  client.setAccount(null);
  assert.throws(() => client.nativeContentURL(path, { rendition: 'thumb' }));
});
test('rendition callers cannot replace owner, smuggle a query, or turn a thumbnail into a download', () => {
  const client = createCloudClient();
  client.setAccount('superdan');
  for (const query of [
    '?expected_account=supervan',
    '?rendition=original',
    '?rendition=thumb&rendition=preview',
    '?rendition=thumb&download=1',
    '?url=https://other.invalid',
  ])
    assert.throws(() => client.nativeContentURL(path + query));
  for (const options of [
    { rendition: 'thumb', download: true },
    { rendition: 'invalid' },
    { rendition: 123 },
    { rendition: 'preview' },
  ])
    assert.throws(() => client.nativeContentURL(path + '?rendition=thumb', options));
  assert.throws(() =>
    client.nativeContentURL('https://other.invalid' + path, { rendition: 'thumb' }),
  );
});
test('eight thumbnails bypass occupied heavy slots without widening original or preview admission', async () => {
  let started = 0;
  const gate = createMediaReadGate({
    origin,
    fetcher: async () => {
      started++;
      return new Response('bytes');
    },
  });
  const first = await gate(path),
    second = await gate(path);
  let previewStarted = false;
  const preview = gate(path + '?rendition=preview').then((value) => {
    previewStarted = true;
    return value;
  });
  const reads = await Promise.all(Array.from({ length: 8 }, () => gate(path + '?rendition=thumb')));
  assert.equal(started, 10);
  assert.equal(previewStarted, false);
  for (const response of reads) assert.equal(await response.text(), 'bytes');
  assert.equal(nativeContentRequest(path + '?rendition=thumb', origin), false);
  assert.equal(nativeContentRequest(path + '?rendition=preview', origin), true);
  assert.equal(nativeContentRequest(path + '?rendition=thumb&rendition=thumb', origin), true);
  const artifact = '/v1/artifacts/result_1/content';
  assert.equal(nativeContentRequest(artifact + '?rendition=thumb', origin), false);
  assert.equal(nativeContentRequest(artifact + '?rendition=preview', origin), true);
  const client = createCloudClient();
  client.setAccount('superdan');
  assert.equal(
    client.nativeContentURL(artifact, { rendition: 'thumb' }),
    artifact + '?expected_account=superdan&rendition=thumb',
  );
  await first.text();
  assert.equal(await (await preview).text(), 'bytes');
  await second.text();
});
