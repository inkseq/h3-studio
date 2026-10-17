import React,{useEffect,useRef,useState} from 'react';
import {Activity,RefreshCw,ChevronRight} from 'lucide-react';
import {cloudController as cloud} from './cloud-controller.js';
import {JobList,useCloud} from './CloudStudio.jsx';
import {emptyActivityWindow,updateActivityWindow} from './activity-window.js';
import './project-activity.css';

export default function ProjectActivity({state,onLocate,onReview,onOpenCloud,onConnect}){
  const c=useCloud(),[history,setHistory]=useState(emptyActivityWindow),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const alive=useRef(false),pending=useRef(false),historyRef=useRef(history),p=state.project,quick=p.journey?.workspace==='freestyle';
  const {events,cursor,loaded,windowReset}=history;
  const connected=state.workspace.mode==='cloud'&&c.account&&state.workspace.account===c.account;
  async function read(older=false){
    if(pending.current||!connected)return;
    pending.current=true;setBusy(true);
    try{const result=await cloud.readProjectActivity(older?{beforeVersion:historyRef.current.cursor}:{});if(!alive.current)return;
      const next=updateActivityWindow(historyRef.current,result,{older});
      historyRef.current=next;setHistory(next);setError('');
    }catch{if(alive.current)setError('创作动态暂时无法读取；已显示记录和当前作品保留。请刷新重试。');}
    finally{pending.current=false;if(alive.current)setBusy(false);}
  }
  useEffect(()=>{alive.current=true;read();const timer=setInterval(()=>{if(document.visibilityState!=='hidden')read();},15000);return()=>{alive.current=false;clearInterval(timer);};},[connected]);
  if(!connected)return <section className="project-activity"><h1>创作动态</h1><p>把作品保存到云端后，Agent 的编辑记录、生成进度和结果都会归到同一个故事。</p><button className="primary" onClick={onOpenCloud}>登录 / 保存到云端</button><p>本机编辑记录不会补造成 Agent 活动。</p></section>;
  return <section className="project-activity" aria-label={quick?'快速创作动态':'故事创作动态'}><header><div><small>ACTIVITY</small><h1>{quick?'一起把这个视频做出来':'一起把故事做出来'}</h1><p>看见 Agent 与网页的修改，回到对应内容继续调整。仅记录此功能上线后的云端保存。</p></div><button disabled={busy} onClick={()=>{read();cloud.refresh().catch(()=>{});cloud.checkRemoteVersion();}}><RefreshCw size={15}/>刷新动态</button></header>
    <div className="activity-intro"><Activity size={22}/><span>动态会注明修改来源，便于你接手调整。为不同助手创建各自的 Key，就能区分它们的操作。</span><button onClick={onConnect}>连接 AI</button></div>
    {state.workspace.dirty&&<p className="cloud-warning">你有尚未同步的编辑。动态可以照常查看，载入云端新版前会让你处理草稿。</p>}
    <h2>生成与结果 <span>{c.jobs.length}</span></h2><p>任务状态沿用服务器记录。完成后可审核候选；调整与重新生成需要你再次确认。</p>
    {c.jobs.length?<JobList jobs={c.jobs} project={p} onInspect={onLocate} onReview={onReview}/>:<p className="activity-empty">当前已载入的任务中还没有生成记录。{quick?'可以先让 Agent 准备提示词和参考素材。':'可以先让 Agent 准备章节和分镜。'}</p>}
    {c.moreJobs&&<button disabled={c.busy} onClick={()=>cloud.loadMoreJobs().catch(()=>{})}>加载更早任务</button>}
    {c.error&&<p role="alert" className="cloud-error">{c.error}</p>}
    <h2>创作记录</h2>{error&&<p role="alert" className="cloud-error">{error}</p>}{!loaded&&!error&&<p role="status">正在读取创作记录…</p>}
    {windowReset&&<p role="status" className="cloud-warning">新记录较多，已切换到最近一页。较早记录仍在云端，可以通过下方「加载更早创作记录」继续查看。</p>}
    {loaded&&!events.length&&<p className="activity-empty">还没有已记录的云端修改。历史作品会保留，但不会补造过去的操作记录。</p>}
    <ol className="activity-events">{events.map(event=><li key={event.id}><article><div className="activity-event-head"><b>{event.summary}</b><span>{event.actor_kind==='api_key'?'API Key':'网页'} · {event.actor_label}</span></div><small>云版本 {event.project_version} · {new Date(typeof event.occurred_at==='number'?event.occurred_at*1000:event.occurred_at).toLocaleString()}</small><div className="activity-targets">{(event.target_entity_ids||[]).slice(0,12).map(id=>{const e=p.entities.find(item=>item.id===id);return e?<button key={id} onClick={()=>onLocate(id)}>{e.title||'未命名内容'}<ChevronRight size={13}/></button>:<span key={id}>内容不在当前版本</span>;})}{(event.target_count||0)>12&&<span>等共 {event.target_count} 项内容</span>}</div>{event.project_version>state.workspace.serverVersion&&<p className="muted">这条修改比你正在查看的版本新，请通过上方「查看云端新版」载入后再调整。</p>}</article></li>)}</ol>
    {cursor!==null&&<button disabled={busy} onClick={()=>read(true)}>加载更早创作记录</button>}
  </section>;
}
