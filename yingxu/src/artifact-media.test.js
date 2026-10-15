import test from 'node:test';
import assert from 'node:assert/strict';
import {artifactMedia,nativeArtifactAttachment,assertArtifactAttachmentTarget} from './artifact-media.js';
import {createCloudClient} from './cloud-client.js';

test('video and FLAC get separate private preview/download routes and correct names',()=>{
  const job={id:'12345678-aaaa',simulation:true};
  for(const [kind,mime,extension] of [['video','video/mp4','mp4'],['audio','audio/flac','flac']]){
    const value=artifactMedia({id:kind,kind,mime,content_url:`/v1/artifacts/${kind}/content`,metadata:{duration:12}},job,{title:'雨夜/信箱'});
    assert.equal(value.type,kind);assert.equal(value.previewURL,`/v1/artifacts/${kind}/content`);assert.equal(value.downloadURL,`/v1/artifacts/${kind}/content?download=1`);
    assert.equal(value.filename,`雨夜_信箱-模拟来源-粗剪-12345678.${extension}`);assert.equal(value.duration,12);
  }
});
test('artifact controls refuse external, signed, mismatched or unsupported resource links',()=>{
  const good={id:'a',kind:'audio',mime:'audio/flac',content_url:'/v1/artifacts/a/content'};
  for(const patch of [{content_url:'https://store.invalid/file?signature=hidden'},{content_url:'/v1/artifacts/b/content'},{content_url:'/v1/artifacts/a/content?token=hidden'},{content_url:'/v1/assets/a/content'},{id:'../a'},{mime:'application/octet-stream'},{kind:'image'}])assert.equal(artifactMedia({...good,...patch},{}),null);
});

test('artifact attachment targets freeze distinct owner-bound video/audio links without reading the files',()=>{
  let calls=0;const client=createCloudClient({fetcher:()=>{calls++;throw Error('must not fetch');}});client.setAccount('original-owner');
  const video=nativeArtifactAttachment(client,{id:'video',content_url:'/v1/artifacts/video/content'},{account:'original-owner',sessionId:'original-session'}),audio=nativeArtifactAttachment(client,{id:'audio',download_url:'/v1/artifacts/audio/content?download=1'},{account:'original-owner',sessionId:'original-session'});
  assert.equal(video.href,'/v1/artifacts/video/content?expected_account=original-owner&download=1');assert.equal(audio.href,'/v1/artifacts/audio/content?expected_account=original-owner&download=1');assert.equal(Object.isFrozen(video),true);
  client.setAccount('new-owner');assert.equal(new URL(video.href,'https://studio.invalid').searchParams.get('expected_account'),'original-owner');assert.equal(calls,0);
});

test('native attachment click fences reject switched accounts, sessions and busy operations but accept the original context',()=>{
  const client=createCloudClient();client.setAccount('owner');const source=nativeArtifactAttachment(client,{id:'result'},{account:'owner',sessionId:'session'}),live={account:'owner',session:{id:'session'},busy:false};
  assert.doesNotThrow(()=>assertArtifactAttachmentTarget(source,live));
  for(const changed of [{...live,account:'other'},{...live,account:null},{...live,session:{id:'other'}},{...live,session:null},{...live,busy:true}])assert.throws(()=>assertArtifactAttachmentTarget(source,changed));
  client.setAccount('other');assert.throws(()=>nativeArtifactAttachment(client,{id:'result'},{account:'owner',sessionId:'session'}),/账户已经改变/);
});

test('native attachment links refuse missing context and resource or query substitutions',()=>{
  const client=createCloudClient();client.setAccount('owner');const context={account:'owner',sessionId:'session'};
  assert.throws(()=>nativeArtifactAttachment(client,{id:'result'},{}));assert.throws(()=>nativeArtifactAttachment(client,{id:'../result'},context));
  for(const download_url of ['/v1/artifacts/other/content?download=1','/v1/assets/result/content','https://store.invalid/file?signature=secret','/v1/artifacts/result/content?expected_account=other','/v1/artifacts/result/content?download=1&token=secret'])assert.throws(()=>nativeArtifactAttachment(client,{id:'result',download_url},context));
});
