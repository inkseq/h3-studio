import React,{useState} from 'react';
import {cloudController as cloud} from './cloud-controller.js';

export default function AccountPassword({busy}){
  const [open,setOpen]=useState(false),[oldPassword,setOld]=useState(''),[password,setPassword]=useState(''),[confirm,setConfirm]=useState(''),[error,setError]=useState('');
  const clear=()=>{setOld('');setPassword('');setConfirm('');};
  async function submit(e){e.preventDefault();setError('');if(password!==confirm){setError('两次新密码不同，请重新核对。');return;}if(new TextEncoder().encode(password).length>72){setError('新密码最多 72 个 UTF-8 字节，请缩短后重试。');return;}const previous=oldPassword,next=password;clear();try{await cloud.changePassword(previous,next);}catch{setError('未确认密码修改成功。请核对当前密码；网络中断时可尝试用新密码重新登录。');}}
  return <section className="account-password"><button type="button" disabled={busy} aria-expanded={open} onClick={()=>{setOpen(!open);clear();setError('');}}>{open?'收起密码设置':'修改登录密码'}</button>{open&&<form onSubmit={submit}><p>修改后所有旧登录和你创建的 API Key 都会失效，需要重新登录并为 Agent 创建新 Key。故事与素材会保留。</p><label>当前密码<input type="password" autoComplete="current-password" required disabled={busy} value={oldPassword} onChange={e=>setOld(e.target.value)}/></label><label>新密码<input type="password" autoComplete="new-password" minLength={12} maxLength={72} required disabled={busy} value={password} onChange={e=>setPassword(e.target.value)}/></label><label>再次输入新密码<input type="password" autoComplete="new-password" minLength={12} maxLength={72} required disabled={busy} value={confirm} onChange={e=>setConfirm(e.target.value)}/></label><button disabled={busy}>保存密码并重新登录</button>{error&&<p role="alert" className="cloud-error">{error}</p>}</form>}</section>;
}
