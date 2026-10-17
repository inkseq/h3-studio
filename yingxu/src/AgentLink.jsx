import React,{useEffect,useState} from 'react';
import {readAgentLink} from './agent-navigation.js';
import {cloudController as cloud} from './cloud-controller.js';
import {useCloud} from './CloudStudio.jsx';

export default function AgentLink({state,onOpenCloud,onLocate,onActivity}){
  const c=useCloud(),[search,setSearch]=useState(()=>window.location.search),[hidden,setHidden]=useState(false),[error,setError]=useState('');
  useEffect(()=>{const update=()=>{setSearch(window.location.search);setHidden(false);setError('');};window.addEventListener('popstate',update);return()=>window.removeEventListener('popstate',update);},[]);
  const target=readAgentLink(search);
  if(!target||hidden)return null;
  const here=state.workspace.mode==='cloud'&&state.workspace.account===c.account&&state.project.id===target.projectId;
  async function follow(){
    setError('');
    if(!c.account){onOpenCloud();return;}
    if(!here){try{const done=await cloud.open(target.projectId);if(!done)onOpenCloud();}catch{setError('无法打开链接中的故事。请核对当前账户是否有权限；现有作品与草稿均保留。');}return;}
    if(target.activity)onActivity();
    else if(target.entityId){if(!onLocate(target.entityId)){setError('这项内容不在当前版本中。可查看云端新版或创作动态；它也可能已经被删除。');return;}}
    else onActivity();
    setHidden(true);
  }
  return <aside className="agent-link-banner" aria-label="来自 Agent 的作品链接"><div><b>{here?'已打开链接中的故事':'收到一个云故事链接'}</b><p>链接只定位作品，访问仍取决于你的账户。{!here?'打开前会保留当前草稿。':''}</p></div><div><button className="primary" disabled={c.busy} onClick={follow}>{!c.account?'登录并打开':here?target.activity?'查看创作动态':target.entityId?'前往对应内容':'查看故事动态':'打开链接中的故事'}</button><button onClick={()=>setHidden(true)}>暂不打开</button></div>{error&&<p role="alert">{error}</p>}</aside>;
}
