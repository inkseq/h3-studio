export const THREAD_GAP=18;
export const threadEntryKey=entry=>`${entry.kind}:${entry.record.id}`;
export const threadCardId=entry=>entry.kind==='card'?entry.record.id:entry.record.card_id;

// Heights include the row gap, so a spacer replaces exactly the same extent.
export function threadLayout(entries,sizes=new Map()){
  let offset=0;
  const rows=entries.map(entry=>{const key=threadEntryKey(entry),measured=sizes.get(key),height=Number.isFinite(measured)&&measured>0?measured:(entry.kind==='turn'?900:720)+THREAD_GAP;
    const row={key,entry,offset,height};offset+=height;return row;});
  return {rows,height:offset};
}

export function threadWindow(layout,{top=Infinity,height=800,overscan=1000,pinned=new Set(),threshold=24}={}){
  const start=Number.isFinite(top)?Math.max(0,top-overscan):Math.max(0,layout.height-height-overscan),end=Number.isFinite(top)?top+height+overscan:Infinity;
  const chosen=layout.rows.map((row,index)=>({row,index})).filter(({row})=>layout.rows.length<=threshold||pinned.has(row.key)||row.offset+row.height>=start&&row.offset<=end);
  const result=[];let position=0;
  for(const {row,index}of chosen){if(row.offset>position)result.push({kind:'spacer',key:'gap-before:'+row.key,height:row.offset-position});result.push({kind:'row',...row,index});position=row.offset+row.height;}
  if(position<layout.height)result.push({kind:'spacer',key:'gap-tail',height:layout.height-position});
  return result;
}

// No timer/retry loop. A failed page waits for the explicit retry control;
// disconnect also fences a queued observer notification after an owner switch.
export function observeEarlierCards(element,root,{snapshot,load,Observer=globalThis.IntersectionObserver}){
  if(!element||!root||typeof Observer!=='function')return ()=>{};
  let disposed=false,inflight=false,lastAttempt=null;
  const observer=new Observer(entries=>{
    const state=snapshot(),key=JSON.stringify([state.scope,state.cursor]);
    if(disposed||inflight||!entries.some(entry=>entry.isIntersecting)||!state.enabled||!state.hasMore||state.loading||state.error||key===lastAttempt)return;
    lastAttempt=key;inflight=true;
    Promise.resolve().then(()=>{if(!disposed)return load();}).catch(()=>{}).finally(()=>{inflight=false;});
  },{root,rootMargin:'240px 0px 0px 0px',threshold:0});
  observer.observe(element);
  return ()=>{disposed=true;observer.disconnect();};
}
