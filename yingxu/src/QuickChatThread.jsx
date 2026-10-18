import React,{createContext,forwardRef,useCallback,useContext,useImperativeHandle,useLayoutEffect,useMemo,useRef,useState} from 'react';
import {captureThreadAnchor,restoreThreadAnchor} from './quick-chat-history.js';
import {threadLayout,threadWindow,threadEntryKey,threadCardId} from './quick-thread-window.js';

const RowContext=createContext(null);
export function useThreadKeepAlive(value){const row=useContext(RowContext);useLayoutEffect(()=>{row?.pin('editor',!!value);return()=>row?.pin('editor',false);},[row,value]);}
// Only ephemeral view selection is remembered between virtual unmounts. Drafts,
// mutations and accepted records remain in their existing original authorities.
export function useThreadViewState(key,initial){
  const row=useContext(RowContext),[value,setValue]=useState(()=>row?.memory.has(key)?row.memory.get(key):initial);
  const change=useCallback(next=>setValue(previous=>{const value=typeof next==='function'?next(previous):next;row?.memory.set(key,value);return value;}),[row,key]);
  return [value,change];
}

function ThreadRow({row,render,attach,pin,memory}){
  const media=useRef(new Set()),context=useMemo(()=>({memory,pin:(reason,value)=>pin(row.key,reason,value)}),[memory,pin,row.key]);
  const ref=useCallback(node=>attach(row.key,node),[attach,row.key]);
  function playing(event,active){if(!['VIDEO','AUDIO'].includes(event.target.tagName))return;if(active)media.current.add(event.target);else media.current.delete(event.target);pin(row.key,'media',media.current.size>0);}
  return <div ref={ref} className="quick-thread-row" data-thread-key={row.key} data-history-entry={row.key} data-virtual-row={row.index}
    onFocusCapture={()=>pin(row.key,'focus',true)} onBlurCapture={event=>{if(!event.currentTarget.contains(event.relatedTarget))pin(row.key,'focus',false);}}
    onPlayCapture={event=>playing(event,true)} onPauseCapture={event=>playing(event,false)} onEndedCapture={event=>playing(event,false)}
    onToggleCapture={event=>pin(row.key,'details',!!event.currentTarget.querySelector('details[open]'))}>
    <RowContext.Provider value={context}>{render(row.entry)}</RowContext.Provider>
  </div>;
}

export default forwardRef(function QuickChatThread({entries,scrollRef,renderEntry,anchorKey,active=true},ref){
  const node=useRef(null),nodes=useRef(new Map()),sizes=useRef(new Map()),memory=useRef(new Map()),pins=useRef(new Map()),resize=useRef(null),pendingAnchor=useRef(null),frame=useRef(null),pendingJump=useRef(null),lastRows=useRef(new Set()),anchorWindow=useRef(null);
  const [version,setVersion]=useState(0),[viewport,setViewport]=useState({top:Infinity,height:800}),[jumpKey,setJumpKey]=useState(null);
  const layout=useMemo(()=>threadLayout(entries,sizes.current),[entries,version]);
  const live=useRef({layout,entries});live.current={layout,entries};
  const pin=useCallback((key,reason,value)=>{const reasons=pins.current.get(key)||new Set(),had=reasons.has(reason);if(had===value)return;if(value)reasons.add(reason);else reasons.delete(reason);if(reasons.size)pins.current.set(key,reasons);else pins.current.delete(key);setVersion(value=>value+1);},[]);
  const readViewport=useCallback(()=>{const root=scrollRef.current,list=node.current;if(!root||!list)return;const offset=list.getBoundingClientRect().top-root.getBoundingClientRect().top+root.scrollTop;const next={top:root.scrollTop-offset,height:root.clientHeight};setViewport(previous=>previous.top===next.top&&previous.height===next.height?previous:next);},[scrollRef]);
  const measure=useCallback(records=>{
    const root=scrollRef.current;if(!root)return;let changed=false;
    for(const record of records){const key=record.target.dataset.threadKey;if(!key||nodes.current.get(key)!==record.target)continue;const height=record.borderBoxSize?.[0]?.blockSize??record.target.getBoundingClientRect().height;if(Number.isFinite(height)&&height>0&&Math.abs((sizes.current.get(key)||0)-height)>.5){if(!changed)pendingAnchor.current={anchor:captureThreadAnchor(root),bottom:root.scrollHeight-root.scrollTop-root.clientHeight<48};sizes.current.set(key,height);changed=true;}}
    if(changed)setVersion(value=>value+1);
  },[scrollRef]);
  const attach=useCallback((key,value)=>{const previous=nodes.current.get(key);if(previous)resize.current?.unobserve(previous);if(value){nodes.current.set(key,value);resize.current?.observe(value);}else nodes.current.delete(key);},[]);
  useLayoutEffect(()=>{if(!active)return;const root=scrollRef.current;if(!root)return;const update=()=>{if(frame.current!==null)return;frame.current=requestAnimationFrame(()=>{frame.current=null;readViewport();});};root.addEventListener('scroll',update,{passive:true});const observer=new ResizeObserver(records=>{measure(records);readViewport();});resize.current=observer;for(const value of nodes.current.values())observer.observe(value);observer.observe(root);readViewport();return()=>{root.removeEventListener('scroll',update);observer.disconnect();resize.current=null;if(frame.current!==null)cancelAnimationFrame(frame.current);frame.current=null;};},[active,measure,readViewport,scrollRef]);
  useLayoutEffect(()=>{const root=scrollRef.current,pending=pendingAnchor.current;if(root&&pending){if(pending.bottom)root.scrollTop=root.scrollHeight;else restoreThreadAnchor(root,pending.anchor);pendingAnchor.current=null;readViewport();}},[version,readViewport,scrollRef]);
  useImperativeHandle(ref,()=>({synchronize:readViewport,scrollToCard(cardId){const row=live.current.layout.rows.find(row=>threadCardId(row.entry)===cardId);pendingJump.current=cardId;if(row)setJumpKey(row.key);setVersion(value=>value+1);}}),[readViewport]);
  useLayoutEffect(()=>{const wanted=pendingJump.current;if(!wanted||!active)return;const row=layout.rows.find(row=>threadCardId(row.entry)===wanted),root=scrollRef.current,list=node.current;if(!row||!root||!list)return;if(jumpKey!==row.key){setJumpKey(row.key);return;}const offset=list.getBoundingClientRect().top-root.getBoundingClientRect().top+root.scrollTop;root.scrollTop=offset+row.offset-16;pendingJump.current=null;readViewport();},[layout,jumpKey,active,readViewport,scrollRef]);
  const protectedKeys=new Set(pins.current.keys());if(anchorKey)protectedKeys.add(anchorKey);if(jumpKey)protectedKeys.add(jumpKey);
  if(!anchorKey)anchorWindow.current=null;
  else if(anchorWindow.current?.key!==anchorKey)anchorWindow.current={key:anchorKey,rows:new Set(lastRows.current)};
  if(anchorWindow.current)for(const key of anchorWindow.current.rows)protectedKeys.add(key);
  const rows=threadWindow(layout,{...viewport,pinned:protectedKeys});
  lastRows.current=new Set(rows.filter(row=>row.kind==='row').map(row=>row.key));
  // Once the target has entered the normal viewport window it need not remain
  // pinned forever; open editors/media/focus keep their own independent pins.
  useLayoutEffect(()=>{if(!jumpKey||pendingJump.current)return;const target=layout.rows.find(row=>row.key===jumpKey);if(target&&target.offset+target.height>=viewport.top&&target.offset<=viewport.top+viewport.height)setJumpKey(null);},[jumpKey,layout,viewport]);
  return <div ref={node} className="quick-virtual-thread" data-virtual-count={entries.length} data-mounted-count={rows.filter(row=>row.kind==='row').length}>
    {rows.map(row=>{if(row.kind==='spacer')return <div key={row.key} className="quick-thread-spacer" style={{height:row.height}} aria-hidden="true"/>;let saved=memory.current.get(row.key);if(!saved){saved=new Map();memory.current.set(row.key,saved);}return <ThreadRow key={row.key} row={row} render={renderEntry} attach={attach} pin={pin} memory={saved}/>;})}
  </div>;
});
