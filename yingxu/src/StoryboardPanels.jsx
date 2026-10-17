import React,{useEffect,useRef,useState} from 'react';

export function useStoryboardPanelSize(key,initial,min,max){
  const storageKey='sixnine-storyboard-panel:'+key,cleanup=useRef(()=>{});
  const clamp=value=>Math.max(min,Math.min(max,value));
  const [size,setSize]=useState(()=>{try{const saved=Number(localStorage.getItem(storageKey));return saved>=min&&saved<=max?saved:initial;}catch{return initial;}});
  useEffect(()=>{try{localStorage.setItem(storageKey,String(size));}catch{}},[storageKey,size]);
  useEffect(()=>()=>cleanup.current(),[]);
  return {size,min,max,reset:()=>setSize(initial),adjust:delta=>setSize(value=>clamp(value+delta)),start:(event,axis,sign)=>{
    event.preventDefault();cleanup.current();const origin=axis==='x'?event.clientX:event.clientY,base=size;
    const move=e=>setSize(clamp(base+sign*((axis==='x'?e.clientX:e.clientY)-origin)));
    const done=()=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',done);window.removeEventListener('pointercancel',done);document.body.classList.remove('sb-resizing','sb-resizing-y');cleanup.current=()=>{};};
    window.addEventListener('pointermove',move);window.addEventListener('pointerup',done);window.addEventListener('pointercancel',done);document.body.classList.add(axis==='x'?'sb-resizing':'sb-resizing-y');cleanup.current=done;
  }};
}
export function StoryboardSplitter({panel,axis='x',sign=1,className='',label='调整面板大小'}){
  return <div className={'splitter '+axis+' '+className} tabIndex={0} role="separator" aria-label={label} aria-orientation={axis==='x'?'vertical':'horizontal'} aria-valuemin={panel.min} aria-valuemax={panel.max} aria-valuenow={Math.round(panel.size)} title="拖动调整大小 · 双击恢复默认 · 方向键调整"
    onPointerDown={event=>panel.start(event,axis,sign)} onDoubleClick={panel.reset} onKeyDown={event=>{if(event.key==='Home'){event.preventDefault();panel.reset();}if((axis==='x'?['ArrowLeft','ArrowRight']:['ArrowUp','ArrowDown']).includes(event.key)){event.preventDefault();panel.adjust((['ArrowRight','ArrowDown'].includes(event.key)?1:-1)*sign*(event.shiftKey?40:10));}}}><i/></div>;
}
