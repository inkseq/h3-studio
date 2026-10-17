import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {Module} from 'node:module';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {createCloudClient} from './cloud-client.js';

const component=(async()=>{
  const path=fileURLToPath(new URL('./QuickChatMaterials.jsx',import.meta.url)),contents=await readFile(path,'utf8');
  const result=await build({stdin:{contents:contents+"\nexport {I18nProvider as TestI18nProvider} from './i18n/I18nProvider.jsx';\nexport {StoryboardAssetImage,StoryboardVideo,StoryboardPlayButton,StoryboardDownloads} from './StoryboardMedia.jsx';\nexport {setCloudMediaHandlers as setTestMediaHandlers} from './media.js';\n",resolveDir:path.replace(/[/\\][^/\\]+$/,''),loader:'jsx'},bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}});
  const module=new Module(path);module.filename=path;module.paths=Module._nodeModulePaths(path.replace(/[/\\][^/\\]+$/,''));module._compile(result.outputFiles[0].text,path);
  const wrap=Component=>props=>React.createElement(module.exports.TestI18nProvider,null,React.createElement(Component,props));
  return Object.assign(wrap(module.exports.PrivateMedia),{story:Object.fromEntries(['StoryboardAssetImage','StoryboardVideo','StoryboardPlayButton','StoryboardDownloads'].map(name=>[name,wrap(module.exports[name])])),rawVideo:module.exports.StoryboardVideo,setMediaHandlers:module.exports.setTestMediaHandlers});
})();

// Exercise the real hook and its media event handlers without a DOM or network.
// React is pinned; this dispatcher only supplies hook storage/effect cleanup.
function mediaLifecycle(Component,initialProps){
 const slots=[];let cursor=0,props=initialProps,effects=[];
 const same=(a,b)=>a?.length===b?.length&&a.every((v,i)=>Object.is(v,b[i]));
 const hooks={useContext:()=>({t:key=>key}),useState(initial){const i=cursor++;slots[i]??={value:typeof initial==='function'?initial():initial};return [slots[i].value,value=>{slots[i].value=typeof value==='function'?value(slots[i].value):value;}];},useRef(value){return hooks.useMemo(()=>({current:value}),[]);},useMemo(fn,deps){const i=cursor++;if(!slots[i]||!same(slots[i].deps,deps))slots[i]={deps,value:fn()};return slots[i].value;},useCallback(fn,deps){return hooks.useMemo(()=>fn,deps);},useEffect(fn,deps){const i=cursor++;if(!slots[i]||!same(slots[i].deps,deps))effects.push(()=>{slots[i]?.cleanup?.();slots[i]={deps,cleanup:fn()};});}};
 const flatten=value=>Array.isArray(value)?value.flatMap(flatten):!value||typeof value!=='object'?[]:[value,...flatten(value.props?.children)];
 return {render(next=props){props=next;cursor=0;effects=[];const internals=React.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE,previous=internals.H;let tree;internals.H=hooks;try{tree=Component(props);}finally{internals.H=previous;}const nodes=flatten(tree);for(const effect of effects)effect();return {nodes,video:nodes.find(node=>node.type==='video'),retry:nodes.find(node=>node.type==='button'),alert:nodes.find(node=>node.props?.role==='alert')};},unmount(){for(const slot of slots)slot?.cleanup?.();}};
}

test('a transient Story player error retains its native video and only explicit retry reloads the same source',async()=>{
 const kit=await component;let reads=0,resolutions=0,loads=0,metadata=0,ready=0,scope='original';
 kit.setMediaHandlers({read:async()=>{reads++;throw Error('playback must not fetch media bytes');},native:()=>{resolutions++;const captured=scope;return {href:'/v1/artifacts/original/content?expected_account=story-owner',assertCurrent(){if(scope!==captured)throw Error('client.accountChanged');}};}});
 const ref={current:null},h=mediaLifecycle(kit.rawVideo,{asset:{fileId:'cloud_artifact_original',kind:'video'},controls:true,videoRef:ref,onLoadedMetadata:()=>metadata++,onCanPlay:()=>ready++});
 try{
  let view=h.render();const source=view.video.props.src;view.video.props.ref({load(){loads++;}});assert.equal(ref.current.load instanceof Function,true);
  view.video.props.onError({currentTarget:{error:{code:2}}});view=h.render();assert.equal(view.video.props.src,source);assert.ok(view.alert);assert.ok(view.retry);assert.equal(loads,0);assert.equal(resolutions,1);assert.equal(reads,0);
  const event={preventDefault(){},stopPropagation(){}};view.retry.props.onClick(event);view=h.render();assert.equal(loads,1);assert.equal(view.alert,undefined);assert.equal(view.video.props.src,source);assert.equal(resolutions,1);
  view.video.props.onError({});view=h.render();view.video.props.onLoadedMetadata({});view=h.render();assert.equal(view.alert,undefined);assert.equal(metadata,1);
  view.video.props.onError({});view=h.render();view.video.props.onCanPlay({});view=h.render();assert.equal(view.alert,undefined);assert.equal(ready,1);
  view.video.props.onError({});const oldRetry=h.render().retry;scope='different-account';view=h.render();assert.equal(view.video,undefined);assert.equal(view.retry,undefined);oldRetry.props.onClick(event);assert.equal(loads,1);assert.equal(resolutions,1);assert.equal(reads,0);
 }finally{h.unmount();kit.setMediaHandlers();}
});

test('Story player retry leaves an externally owned local Blob source intact and never revokes it',async()=>{
 const kit=await component,original=URL.revokeObjectURL,revoked=[];URL.revokeObjectURL=url=>revoked.push(url);kit.setMediaHandlers();let loads=0;
 const h=mediaLifecycle(kit.rawVideo,{asset:{src:'blob:owned-by-upload-control',kind:'video'},controls:true});
 try{
  h.render();let view=h.render();assert.equal(view.video.props.src,'blob:owned-by-upload-control');view.video.props.ref({load(){loads++;}});
  view.video.props.onError({});view=h.render();assert.equal(view.video.props.src,'blob:owned-by-upload-control');view.retry.props.onClick({preventDefault(){},stopPropagation(){}});view=h.render();assert.equal(loads,1);assert.equal(view.alert,undefined);h.unmount();assert.deepEqual(revoked,[]);
 }finally{h.unmount();URL.revokeObjectURL=original;}
});

test('Story cloud image/video/audio controls and both attachments render native routes, not Blob downloads',async()=>{
 const kit=await component,client=createCloudClient();client.setAccount('story-owner');let reads=0;
 kit.setMediaHandlers({read:async()=>{reads++;throw Error('views must not read whole media');},native:(id,options)=>{
  const match=/^cloud_(asset|artifact)_([A-Za-z0-9_-]+)$/.exec(id);
  return Object.freeze({href:client.nativeContentURL(`/v1/${match[1]}s/${match[2]}/content`,options),assertCurrent(){}});
 }});
 try{
  for(const [name,kind] of [['StoryboardAssetImage','image'],['StoryboardVideo','video'],['StoryboardPlayButton','audio']]){
   const html=renderToStaticMarkup(React.createElement(kit.story[name],{asset:{fileId:'cloud_asset_reference',kind}}));
   if(kind!=='audio')assert.match(html,/src="\/v1\/assets\/reference\/content\?expected_account=story-owner"/);
   else assert.doesNotMatch(html,/disabled/);
   assert.doesNotMatch(html,/blob:|crossorigin/i);
  }
  const version={artifactId:'video',jobId:'job',asset:{entity:{data:{mime:'video/mp4',cloudContentPath:'/v1/artifacts/video/content'}}},audio:{id:'audio',kind:'audio',mime:'audio/flac',content_url:'/v1/artifacts/audio/content'}};
  const html=renderToStaticMarkup(React.createElement(kit.story.StoryboardDownloads,{version,title:'Story'}));
  assert.match(html,/href="\/v1\/artifacts\/video\/content\?expected_account=story-owner&amp;download=1"/);
  assert.match(html,/href="\/v1\/artifacts\/audio\/content\?expected_account=story-owner&amp;download=1"/);
  assert.match(html,/rel="noopener noreferrer"/);assert.equal(reads,0);
 }finally{kit.setMediaHandlers();}
});

test('Story native source failure stays explicit and cannot fall through to a Blob reader',async()=>{
 const kit=await component;let reads=0;kit.setMediaHandlers({read:async()=>{reads++;throw Error('must not fall back');},native:()=>{throw Error('client.mediaAccount');}});
 try{
  const html=renderToStaticMarkup(React.createElement(kit.story.StoryboardVideo,{asset:{fileId:'cloud_artifact_original',kind:'video'}}));
  assert.doesNotMatch(html,/<video|blob:/);assert.match(html,/role="alert"/);assert.doesNotMatch(html,/client\.mediaAccount/);assert.equal(reads,0);
 }finally{kit.setMediaHandlers();}
});

test('private reference images render native owner-bound routes at thumbnail and full size without Blob reads',async()=>{
  const PrivateMedia=await component;let calls=0;
  const client=createCloudClient({fetcher:()=>{calls++;throw Error('must not read a whole Blob');}});client.setAccount('original-owner');
  for(const compact of [true,false]){
    const html=renderToStaticMarkup(React.createElement(PrivateMedia,{client,path:'/v1/assets/reference/content',kind:'image',name:'Original reference',compact}));
    assert.match(html,/<img /);assert.match(html,/src="\/v1\/assets\/reference\/content\?expected_account=original-owner"/);
    assert.match(html,/referrerPolicy="no-referrer"/);assert.match(html,/alt="Original reference"/);
    assert.match(html,new RegExp(compact?'qc-media-thumb':'qc-media-full'));assert.doesNotMatch(html,/blob:|正在载入预览/);
  }
  assert.equal(calls,0);
});

test('native private video/audio and compact placeholders keep their existing behavior',async()=>{
  const PrivateMedia=await component,client=createCloudClient();client.setAccount('owner');
  for(const kind of ['video','audio']){
    const props={client,path:'/v1/assets/reference/content',kind};
    const full=renderToStaticMarkup(React.createElement(PrivateMedia,props));
    assert.match(full,new RegExp('<'+kind+' '));assert.match(full,/expected_account=owner/);
    const compact=renderToStaticMarkup(React.createElement(PrivateMedia,{...props,compact:true}));
    assert.doesNotMatch(compact,/<video |<audio |\/v1\/assets/);assert.match(compact,/qc-media-placeholder/);
  }
});

test('reference previews fail closed for an unconfirmed owner or an external/substituted URL',async()=>{
  const PrivateMedia=await component,client=createCloudClient();
  const render=path=>renderToStaticMarkup(React.createElement(PrivateMedia,{client,path,kind:'image'}));
  assert.doesNotMatch(render('/v1/assets/reference/content'),/<img /);
  client.setAccount('owner');
  for(const path of ['https://store.invalid/private?signature=secret','/v1/assets/reference/content?expected_account=other','/v1/jobs/reference']){
    const html=render(path);assert.doesNotMatch(html,/<img |signature=secret|expected_account=other/);assert.match(html,/qc-media-placeholder/);
  }
});
