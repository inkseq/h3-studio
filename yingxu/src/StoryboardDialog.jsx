import React,{useEffect,useRef,useState} from 'react';
import {createPortal} from 'react-dom';

// HTML dialogs keep keyboard focus inside the page instead of opening a browser
// JavaScript prompt. Closing a dialog never commits its draft.
function StoryboardDialog({title,children,onClose,busy=false}){
  const dialog=useRef(),opener=useRef(document.activeElement);
  useEffect(()=>{
    const node=dialog.current;
    node.showModal();
    return()=>{node.close();queueMicrotask(()=>{if(opener.current?.isConnected)opener.current.focus();});};
  },[]);
  return createPortal(<dialog ref={dialog} className="sb-portal sb-dialog" aria-label={title}
    onCancel={event=>{event.preventDefault();if(!busy)onClose();}}
    onClick={event=>{if(event.target===event.currentTarget&&!busy)onClose();}}>
    <div className="picker-head"><h2>{title}</h2><button type="button" className="x big" aria-label="关闭弹窗" disabled={busy} onClick={onClose}>×</button></div>
    {children}
  </dialog>,document.body);
}

export function StoryboardNameForm({label,name,onName,onSubmit,onCancel,busy=false,error=''}){
  const valid=!!name.trim();
  const submit=event=>{event.preventDefault();if(valid&&!busy)onSubmit(name.trim());};
  return <form onSubmit={submit} className="sb-name-form">
    <label>{label}<input autoFocus required value={name} disabled={busy} onChange={event=>onName(event.target.value)}/></label>
    {error&&<p role="alert" className="warn">{error}</p>}
    <div className="row sb-dialog-actions"><button type="button" disabled={busy} onClick={onCancel}>取消</button><button type="submit" className="primary" disabled={!valid||busy}>{busy?'正在创建…':'创建'}</button></div>
  </form>;
}

export function StoryboardNameDialog({title,label='名称',onSubmit,onClose}){
  const [name,setName]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),pending=useRef(false);
  async function submit(value){
    if(pending.current)return;
    pending.current=true;setBusy(true);setError('');
    try{
      const receipt=await onSubmit(value);
      if(receipt?.ok===false){setError(receipt.error||'创建没有完成，名称仍保留。');return;}
      onClose();
    }catch(problem){setError(problem?.message||'创建尚未确认，请核对原记录。');}
    finally{pending.current=false;setBusy(false);}
  }
  return <StoryboardDialog title={title} onClose={onClose} busy={busy}><StoryboardNameForm label={label} name={name} onName={setName} onSubmit={submit} onCancel={onClose} busy={busy} error={error}/></StoryboardDialog>;
}

export function StoryboardDeleteDialog({onConfirm,onClose}){
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),pending=useRef(false);
  async function confirm(){
    if(pending.current)return;
    pending.current=true;setBusy(true);setError('');
    try{
      const receipt=await onConfirm();
      if(receipt?.ok===false){setError(receipt.error||'删除没有完成，原记录仍保留。');return;}
      onClose();
    }catch(problem){setError(problem?.message||'删除尚未确认，请核对原记录。');}
    finally{pending.current=false;setBusy(false);}
  }
  return <StoryboardDialog title="删除镜头前确认" onClose={onClose} busy={busy}>
    <p>删除这个镜头的编辑卡片？原任务、成片和费用记录会保留。</p>
    {error&&<p role="alert" className="warn">{error}</p>}
    <div className="row sb-dialog-actions"><button type="button" autoFocus disabled={busy} onClick={onClose}>取消</button><button type="button" className="danger" disabled={busy} onClick={confirm}>{busy?'正在删除…':'确认删除镜头'}</button></div>
  </StoryboardDialog>;
}
