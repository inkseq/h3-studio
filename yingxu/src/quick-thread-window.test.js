import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {threadLayout,threadWindow,observeEarlierCards} from './quick-thread-window.js';

const entries=Array.from({length:66},(_,i)=>({kind:'card',record:{id:'card-'+String(i).padStart(3,'0')}}));
test('66 actual variable-height rows render a small viewport and disjoint pinned rows with exact spacers',()=>{
  const sizes=new Map(entries.map((row,i)=>['card:'+row.record.id,350+(i%7)*100])),layout=threadLayout(entries,sizes),target=layout.rows[31];
  const rows=threadWindow(layout,{top:target.offset,height:812,overscan:600,pinned:new Set(['card:card-000','card:card-065'])});
  assert.ok(rows.some(row=>row.kind==='row'&&row.key==='card:card-000'));
  assert.ok(rows.some(row=>row.kind==='row'&&row.key==='card:card-065'));
  assert.ok(rows.some(row=>row.kind==='row'&&row.key===target.key));
  assert.ok(rows.filter(row=>row.kind==='row').length<12);
  assert.equal(rows.some(row=>row.kind==='row'&&row.key==='card:card-010'),false);
  assert.equal(rows.reduce((sum,row)=>sum+row.height,0),layout.height);
  assert.equal(rows.filter(row=>row.kind==='row').map(row=>row.index).sort((a,b)=>a-b).join(','),rows.filter(row=>row.kind==='row').map(row=>row.index).join(','));
});

test('newly prepended cards preserve measured row identities and do not force all remote media to mount',()=>{
  const sizes=new Map(entries.slice(10).map(row=>['card:'+row.record.id,600]));
  const before=threadLayout(entries.slice(10),sizes),after=threadLayout(entries,sizes),anchor='card:card-040';
  const shift=after.rows.find(row=>row.key===anchor).offset-before.rows.find(row=>row.key===anchor).offset;
  assert.equal(shift,10*738);
  const rows=threadWindow(after,{top:after.rows[40].offset,height:900,pinned:new Set([anchor])});
  assert.ok(rows.some(row=>row.key===anchor));assert.equal(rows.filter(row=>row.kind==='row').length<15,true);
});

class Observer{
  static instances=[];
  constructor(callback,options){this.callback=callback;this.options=options;Observer.instances.push(this);}
  observe(element){this.element=element;}
  disconnect(){this.disconnected=true;}
  enter(){this.callback([{isIntersecting:true}]);}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('upward observer waits for initial bottom, single-flights a page, stops on 429/error and fences cleanup',async()=>{
  let calls=0,release;const state={scope:'owner/session',cursor:'first',enabled:false,hasMore:true,loading:false,error:null};
  const stop=observeEarlierCards({}, {},{Observer,snapshot:()=>state,load:()=>{calls++;return new Promise(resolve=>release=resolve);}}),observer=Observer.instances.at(-1);
  observer.enter();await tick();assert.equal(calls,0);
  state.enabled=true;observer.enter();observer.enter();await tick();assert.equal(calls,1);
  release();await tick();observer.enter();await tick();assert.equal(calls,1,'same cursor cannot form a polling loop');
  state.cursor='second';state.error={status:429};observer.enter();await tick();assert.equal(calls,1);
  state.error=null;observer.enter();await tick();assert.equal(calls,2);release();await tick();
  state.cursor='third';observer.enter();stop();await tick();assert.equal(calls,2,'queued notification cannot read after unmount/account switch');assert.equal(observer.disconnected,true);
});

test('exhaustion and pending mutation gates never auto-fetch and absent observer keeps the accessible fallback',async()=>{
  let calls=0,state={scope:'owner/session',cursor:'first',enabled:true,hasMore:false,loading:false,error:null};
  const stop=observeEarlierCards({}, {},{Observer,snapshot:()=>state,load:async()=>{calls++;}});Observer.instances.at(-1).enter();await tick();
  state={...state,hasMore:true,enabled:false};Observer.instances.at(-1).enter();await tick();assert.equal(calls,0);stop();
  observeEarlierCards({}, {},{Observer:null,snapshot:()=>state,load:async()=>{calls++;}})();assert.equal(calls,0);
});

test('the actual virtual thread component omits offscreen media while keeping an explicit anchor row mounted',async()=>{
  const directory=fileURLToPath(new URL('.',import.meta.url)),filename=directory+'thread-window-test.cjs';
  const compiled=await build({stdin:{contents:"import React from 'react';import Thread from './QuickChatThread.jsx';export const View=props=><Thread {...props}/>;",resolveDir:directory,loader:'jsx'},bundle:true,write:false,format:'cjs',platform:'node',external:['react','react-dom']});
  const module=new Module(filename);module.filename=filename;module.paths=Module._nodeModulePaths(directory);module._compile(compiled.outputFiles[0].text,filename);
  const markup=renderToStaticMarkup(React.createElement(module.exports.View,{entries,scrollRef:{current:null},anchorKey:'card:card-001',renderEntry:entry=>React.createElement('article',{'data-card-id':entry.record.id},React.createElement('video',{'data-test-media':entry.record.id}))}));
  const mounted=Number(markup.match(/data-mounted-count="(\d+)"/)[1]);assert.ok(mounted<10);assert.ok(mounted>1);
  assert.match(markup,/data-card-id="card-001"/);assert.match(markup,/data-card-id="card-065"/);assert.doesNotMatch(markup,/data-card-id="card-030"/);
  assert.equal((markup.match(/<video /g)||[]).length,mounted);assert.match(markup,/aria-hidden="true"/);
});


test('pre-mutation thread snapshot preserves the visible row across 20-to-30 prepend and measured-window unpin',async()=>{
  const directory=fileURLToPath(new URL('.',import.meta.url)),filename=directory+'thread-snapshot-test.cjs';
  const compiled=await build({stdin:{contents:"export {ThreadScrollSnapshot} from './QuickChatThread.jsx';",resolveDir:directory,loader:'jsx'},bundle:true,write:false,format:'cjs',platform:'node',external:['react','react-dom']});
  const module=new Module(filename);module.filename=filename;module.paths=Module._nodeModulePaths(directory);module._compile(compiled.outputFiles[0].text,filename);
  const {ThreadScrollSnapshot}=module.exports;
  let contentTop=158.796875,connected=true;
  const row={get isConnected(){return connected;},getAttribute:()=> 'card:card-047',getBoundingClientRect:()=>({top:contentTop-root.scrollTop,bottom:contentTop-root.scrollTop+320})};
  let mounted=[row];
  const root={scrollTop:0,scrollHeight:20*738,clientHeight:700,getBoundingClientRect:()=>({top:0}),querySelectorAll:()=>mounted,contains:node=>mounted.includes(node)};
  const view=new ThreadScrollSnapshot({scrollRef:{current:root}}),initialOffset=row.getBoundingClientRect().top;
  // First commit: prepend ten rows while all old rows remain pinned.
  const prepend=view.getSnapshotBeforeUpdate();contentTop+=10*738;root.scrollHeight+=10*738;view.componentDidUpdate(null,null,prepend);
  assert.equal(row.getBoundingClientRect().top,initialOffset);
  // Next commit: unpin the old window and replace measured/estimated prefixes
  // by spacers. Capturing only in ResizeObserver would see the shifted 243px.
  const unpin=view.getSnapshotBeforeUpdate();contentTop+=84.203125;root.scrollHeight+=84.203125;
  assert.equal(row.getBoundingClientRect().top,243);
  view.componentDidUpdate(null,null,unpin);assert.equal(row.getBoundingClientRect().top,initialOffset);
  // A stable row key also survives a DOM-node replacement during virtualization.
  const replaced=view.getSnapshotBeforeUpdate();connected=false;contentTop+=53.5;
  const replacement={isConnected:true,getAttribute:()=> 'card:card-047',getBoundingClientRect:row.getBoundingClientRect};mounted=[replacement];
  view.componentDidUpdate(null,null,replaced);assert.equal(replacement.getBoundingClientRect().top,initialOffset);
  // Capture happens at commit time, after a user's actual scroll, not at request
  // start: a later viewport change is respected instead of restoring stale state.
  root.scrollTop+=125;const scrolledOffset=replacement.getBoundingClientRect().top,snapshot=view.getSnapshotBeforeUpdate();contentTop+=30;
  view.componentDidUpdate(null,null,snapshot);assert.equal(replacement.getBoundingClientRect().top,scrolledOffset);
  view.props={...view.props,active:false};assert.equal(view.getSnapshotBeforeUpdate({active:true}),null,'a route-cached hidden thread must not reset its scroll');
  view.props={...view.props,active:true};assert.equal(view.getSnapshotBeforeUpdate({active:false}),null,'reactivation must not snapshot zero-sized hidden geometry');
});
