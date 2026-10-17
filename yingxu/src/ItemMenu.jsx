import React,{useEffect,useRef,useState} from 'react';
import {MoreHorizontal} from 'lucide-react';
import {useI18n} from './i18n/I18nProvider.jsx';
// One open menu owns only presentation. Business operations stay with callers.
export default function ItemMenu({children,label,disabled=false}){
 const {t}=useI18n(),[open,setOpen]=useState(false),root=useRef(null),trigger=useRef(null);
 useEffect(()=>{if(!open)return;const outside=e=>{if(!root.current?.contains(e.target))setOpen(false);};const key=e=>{if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();setOpen(false);trigger.current?.focus();}};const scroll=()=>setOpen(false);document.addEventListener('pointerdown',outside,true);document.addEventListener('keydown',key,true);window.addEventListener('scroll',scroll,true);return()=>{document.removeEventListener('pointerdown',outside,true);document.removeEventListener('keydown',key,true);window.removeEventListener('scroll',scroll,true);};},[open]);
 useEffect(()=>{if(disabled)setOpen(false);},[disabled]);
 return <span className="item-menu" ref={root} onPointerDown={e=>e.stopPropagation()}><button type="button" ref={trigger} disabled={disabled} aria-label={label||t('trash.menu')} aria-expanded={open} aria-haspopup="menu" onClick={e=>{e.stopPropagation();setOpen(v=>!v);}}><MoreHorizontal size={16} aria-hidden="true"/></button>{open&&<span className="item-menu-popover" role="menu" onClick={e=>e.stopPropagation()}>{typeof children==='function'?children(()=>setOpen(false)):children}</span>}</span>;
}
