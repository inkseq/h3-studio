import React,{useEffect,useMemo,useRef,useState} from 'react';
import {BookOpen,Check,ChevronDown,Clapperboard,Cloud,HardDrive,Plus,RefreshCw,Search,Trash2,X} from 'lucide-react';
import {store} from './store.js';
import {cloudController as cloud} from './cloud-controller.js';
import {useCloud} from './CloudStudio.jsx';
import {createLocalStory,openLocalStory,readLocalStoryLibrary,storyCoverFileId,storyEditedTimestamp,storyLibrarySummary} from './story-navigation.js';
import {createCloudClient} from './cloud-client.js';
import {checkedProjectRecord} from './cloud-model.js';
import {getFile} from './media.js';
import './story-navigation.css';

export default function StoryNavigation({state,onOpenCloud,onOpenLocal,onSelect}){
  const c=useCloud(),[creating,setCreating]=useState(false),[title,setTitle]=useState(''),[source,setSource]=useState('local'),[query,setQuery]=useState(''),[error,setError]=useState(''),[pending,setPending]=useState(''),[library,setLibrary]=useState(()=>readLocalStoryLibrary()),inputRef=useRef();
  const refreshLocal=()=>setLibrary(readLocalStoryLibrary());
  useEffect(()=>{refreshLocal();},[state.project.id,state.project.title,state.workspace.mode]);
  useEffect(()=>{const refresh=e=>{if(!e.key||e.key==='yingxu-project-library-v1')refreshLocal();};window.addEventListener('storage',refresh);window.addEventListener('focus',refreshLocal);return()=>{window.removeEventListener('storage',refresh);window.removeEventListener('focus',refreshLocal);};},[]);
  useEffect(()=>{setQuery('');setError('');setCreating(false);setTitle('');},[c.account]);
  useEffect(()=>{if(creating)inputRef.current?.focus();},[creating]);
  const local=store.getLocalProjectSummary(),locals=[...(local?[local]:[]),...library.projects.filter(p=>p.id!==local?.id)];
  const cloudProjects=c.account?[...c.projects]:[];
  if(c.account&&state.workspace.mode==='cloud'){
    const index=cloudProjects.findIndex(p=>p.id===state.project.id),current={id:state.project.id,title:state.project.title};
    if(index<0)cloudProjects.unshift(current);else cloudProjects[index]={...cloudProjects[index],...current};
  }
  const matches=p=>p.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),visibleCloud=cloudProjects.filter(matches),visibleLocal=locals.filter(matches),working=c.busy||!!pending;
  async function act(label,fn){setPending(label);setError('');try{return await fn();}catch(e){setError(e.message);return false;}finally{setPending('');}}
  const openCloud=id=>act('正在打开故事…',async()=>{const done=await cloud.open(id);if(!done)onOpenCloud();else onSelect();return done;});
  const openLocal=id=>act('正在打开故事…',()=>{const done=openLocalStory(id,{store,cloud});if(done)onSelect();return done;});
  const begin=()=>{setTitle('');setSource(c.account?'cloud':'local');setError('');setCreating(true);};
  const create=e=>{e.preventDefault();void act('正在创建故事…',async()=>{
    const done=source==='cloud'?await cloud.create(title.trim()):createLocalStory(title,{store,cloud});
    refreshLocal();if(done){setCreating(false);setTitle('');setQuery('');store.notify('新故事已创建，原故事保留。');onSelect();}else if(cloud.getState().draftOffer)onOpenCloud();return done;
  });};
  const item=(p,kind)=>{const active=state.workspace.mode===kind&&state.project.id===p.id,I=kind==='cloud'?Cloud:HardDrive;return <button key={kind+':'+p.id} type="button" className="story-nav-item" aria-current={active?'page':undefined} disabled={working} onClick={()=>active?onSelect():kind==='cloud'?openCloud(p.id):openLocal(p.id)} title={p.title}><I size={14}/><span><b>{p.title}</b><small>{kind==='cloud'?'云故事':'此浏览器'}{active?' · 当前':''}</small></span>{active&&<Check size={13}/>}</button>;};
  return <section className="story-navigation" aria-label="我的故事"><div className="story-nav-heading"><h2 id="story-library-title" tabIndex={-1}><BookOpen size={17}/>我的故事</h2><button type="button" className="story-nav-refresh" disabled={working} aria-label="刷新故事列表" title="刷新故事列表" onClick={()=>act('正在刷新故事…',async()=>{refreshLocal();if(c.account)await cloud.list();})}><RefreshCw size={13}/></button></div>
    <button type="button" className="story-nav-create" disabled={working} onClick={begin}><Plus size={15}/>新建故事</button>
    {creating&&<form className="story-nav-form" onSubmit={create}><div><label htmlFor="new-sidebar-story-title">故事名称</label><button type="button" disabled={working} aria-label="取消新建故事" onClick={()=>setCreating(false)}><X size={14}/></button></div><input ref={inputRef} id="new-sidebar-story-title" required maxLength={80} placeholder="给新故事起个名字" value={title} disabled={working} onChange={e=>setTitle(e.target.value)}/><label htmlFor="new-sidebar-story-source">保存位置</label><select id="new-sidebar-story-source" value={source} disabled={working} onChange={e=>setSource(e.target.value)}>{c.account&&<option value="cloud">云端 · {c.account}</option>}<option value="local">仅此浏览器</option></select><small>{source==='cloud'?'从空白故事开始，已有故事分别保留。':'仅保存在此浏览器，记得下载完整备份。'}</small><button type="submit" disabled={working||!title.trim()}>创建故事</button></form>}
    <div id="sidebar-story-list"><label className="story-nav-search"><span className="visually-hidden">搜索故事</span><input type="search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜索故事"/></label><nav className="story-nav-list" aria-label="切换故事">{c.account&&<><p className="story-nav-group">云端 · {c.account}</p>{visibleCloud.map(p=>item(p,'cloud'))}{!c.projectsLoaded&&<p className="story-nav-note">故事列表尚未载入，可刷新重试。</p>}{c.projectsLoaded&&!cloudProjects.length&&<p className="story-nav-note">还没有云故事，点击上方新建。</p>}</>}<p className="story-nav-group">本机故事</p>{visibleLocal.map(p=>item(p,'local'))}{query&&!visibleCloud.length&&!visibleLocal.length&&<p className="story-nav-note">已载入的故事中没有匹配项。</p>}</nav>{c.moreProjects&&<button type="button" className="story-nav-more" disabled={working} onClick={()=>act('正在载入更多故事…',()=>cloud.loadMoreProjects())}>加载更多云故事</button>}{query&&c.moreProjects&&<p className="story-nav-note">搜索只包含已载入的故事。</p>}{library.error&&<p className="story-nav-error" role="alert">本机故事列表读取失败，原始数据未覆盖。<button type="button" onClick={onOpenLocal}>查看本机备份</button></p>}{!c.account&&<button type="button" className="story-nav-more" onClick={onOpenCloud}>登录，查看云故事</button>}</div>
    {state.workspace.mode==='cloud'&&state.workspace.dirty&&<p className="story-nav-note">{state.saveState.startsWith('保存失败')?'当前草稿仅暂存在本页面，请先下载备份，勿刷新。':'当前修改已留为本机草稿；切换后可恢复。'}</p>}{pending&&<p className="story-nav-note" role="status">{pending}</p>}{error&&<p className="story-nav-error" role="alert">{error}</p>}
  </section>;
}


function useStoryLibrary(state){
  const c=useCloud(),[library,setLibrary]=useState(()=>readLocalStoryLibrary()),[pending,setPending]=useState(''),[error,setError]=useState('');
  const pendingRef=useRef(false),refreshLocal=()=>setLibrary(readLocalStoryLibrary());
  useEffect(()=>{refreshLocal();},[state.project.id,state.project.title,state.project.updatedAt,state.workspace.mode]);
  useEffect(()=>{const refresh=event=>{if(!event?.key||event.key==='yingxu-project-library-v1')refreshLocal();};window.addEventListener('storage',refresh);window.addEventListener('focus',refreshLocal);return()=>{window.removeEventListener('storage',refresh);window.removeEventListener('focus',refreshLocal);};},[]);
  useEffect(()=>{setError('');setPending('');},[c.account]);
  const local=store.getLocalProjectSummary(),currentLocal=state.workspace.mode==='local'?state.project:local;
  const locals=[...(currentLocal?[currentLocal]:[]),...library.projects.filter(project=>project.id!==currentLocal?.id)];
  const cloudProjects=c.account?[...c.projects]:[];
  if(c.account&&state.workspace.mode==='cloud'&&state.workspace.account===c.account){const index=cloudProjects.findIndex(project=>project.id===state.project.id);if(index<0)cloudProjects.unshift(state.project);else cloudProjects[index]={...cloudProjects[index],...state.project};}
  const stories=[...cloudProjects.map(project=>({project,mode:'cloud'})),...locals.map(project=>({project,mode:'local'}))];
  const working=c.busy||!!pending;
  async function act(message,fn){if(working||pendingRef.current)return false;pendingRef.current=true;setPending(message);setError('');try{return await fn();}catch(error){setError(error.message);return false;}finally{pendingRef.current=false;setPending('');}}
  async function openStory(story,onSelect,onOpenCloud){return act('正在打开故事…',async()=>{
    const active=state.workspace.mode===story.mode&&state.project.id===story.project.id;
    const done=active?true:story.mode==='cloud'?await cloud.open(story.project.id):openLocalStory(story.project.id,{store,cloud});
    if(done)onSelect?.();else onOpenCloud?.();return done;
  });}
  return {c,library,stories,working,pending,error,act,openStory,refreshLocal};
}

function editedLabel(value){const stamp=storyEditedTimestamp(value);return stamp?new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(stamp):'编辑时间未读取';}
function StoryLocation({mode}){return <span className={'story-location '+mode}>{mode==='cloud'?<Cloud size={12}/>:<HardDrive size={12}/>} {mode==='cloud'?'云端':'本机'}</span>;}

function useLibraryReader(account){
  const reader=useMemo(()=>{const value=createCloudClient({onUnauthorized:()=>{void cloud.recheckSession().catch(()=>{});}});value.setAccount(account);return value;},[account]);
  useEffect(()=>()=>reader.reset(),[reader]);
  const guard=()=>{if(!account||cloud.getState().account!==account)throw Error('账户已改变，旧账户封面未展示。');};
  return useMemo(()=>({
    project:async id=>{guard();const record=checkedProjectRecord(await reader.project(id));guard();if(record.id!==id)throw Error('故事详情标识不一致。');return record.project;},
    cover:async file=>{guard();const blob=await reader.download(file.cloudPath);guard();return blob;},
  }),[reader,account]);
}

function StoryLibraryCard({story,reader,working,onOpen}){
  const ref=useRef(),[visible,setVisible]=useState(false),[loaded,setLoaded]=useState(null),[detailError,setDetailError]=useState(false),[coverUrl,setCoverUrl]=useState('');
  const project=Array.isArray(story.project.entities)?story.project:loaded||story.project,summary=storyLibrarySummary(project),file=storyCoverFileId(summary.cover,{mode:story.mode});
  useEffect(()=>{if(typeof IntersectionObserver==='undefined'){setVisible(true);return;}const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)){setVisible(true);observer.disconnect();}},{rootMargin:'160px'});if(ref.current)observer.observe(ref.current);return()=>observer.disconnect();},[]);
  useEffect(()=>{let alive=true;setLoaded(null);setDetailError(false);if(!visible||story.mode!=='cloud'||Array.isArray(story.project.entities))return;
    reader.project(story.project.id).then(project=>{if(alive)setLoaded(project);}).catch(()=>{if(alive)setDetailError(true);});return()=>{alive=false;};
  },[visible,reader,story.mode,story.project.id,story.project.version,story.project.updated_at,Array.isArray(story.project.entities)]);
  useEffect(()=>{let alive=true,url='';setCoverUrl('');if(!visible||!file)return;
    const read=file.cloudPath?reader.cover(file):getFile(file.fileId);
    read.then(blob=>{if(!alive||!blob?.size||(blob.type&&!blob.type.startsWith(file.kind+'/')))return;url=URL.createObjectURL(blob);setCoverUrl(url);}).catch(()=>{});
    return()=>{alive=false;if(url)URL.revokeObjectURL(url);};
  },[visible,reader,file?.fileId,file?.kind,file?.cloudPath]);
  const known=summary.shotCount!==null;
  return <button ref={ref} type="button" className="story-library-card" disabled={working} onClick={onOpen} aria-label={'打开'+(story.mode==='cloud'?'云端':'本机')+'故事 '+summary.title}>
    <div className="story-library-cover">{coverUrl?file.kind==='video'?<video src={coverUrl} muted playsInline preload="metadata" aria-hidden="true"/>:<img src={coverUrl} alt="" draggable={false}/>:<Clapperboard size={32} aria-hidden="true"/>}<StoryLocation mode={story.mode}/></div>
    <div className="story-library-card-body"><h2 title={summary.title}>{summary.title}</h2><p>{known?<><b>{summary.chosenCount}/{summary.shotCount}</b> 已选定 · {summary.shotCount} 个镜头</>:detailError?'镜头信息暂未读取':'镜头信息未读取'}</p><time dateTime={storyEditedTimestamp(summary.updatedAt)?new Date(storyEditedTimestamp(summary.updatedAt)).toISOString():undefined}>{editedLabel(summary.updatedAt)}</time></div>
  </button>;
}

export function StoryLibraryHome({state,onOpenCloud,onOpenLocal,onSelect}){
  const library=useStoryLibrary(state),{c,stories,working,act}=library,[query,setQuery]=useState(''),[creating,setCreating]=useState(false),[title,setTitle]=useState(''),[source,setSource]=useState('local'),[visibleLimit,setVisibleLimit]=useState(12),input=useRef(),reader=useLibraryReader(c.account);
  useEffect(()=>{setQuery('');setCreating(false);setTitle('');setVisibleLimit(12);},[c.account]);
  useEffect(()=>setVisibleLimit(12),[query]);
  useEffect(()=>{if(creating)input.current?.focus();},[creating]);
  const begin=()=>{setTitle('');setSource(c.account?'cloud':'local');setCreating(true);};
  const create=event=>{event.preventDefault();void act('正在创建故事…',async()=>{const done=source==='cloud'?await cloud.create(title.trim()):createLocalStory(title,{store,cloud});library.refreshLocal();if(done){setCreating(false);setTitle('');store.notify('新故事已创建，原故事保留。');onSelect?.();}else if(cloud.getState().draftOffer)onOpenCloud?.();return done;});};
  const visible=stories.filter(story=>String(story.project.title||'').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <section className="story-library-home" aria-labelledby="story-library-heading">
    <header className="story-library-heading"><div><h1 id="story-library-heading" tabIndex={-1}>作品库</h1><p>把故事留在这里，随时接着创作。</p></div><button type="button" className="primary" disabled={working} onClick={begin}><Plus size={16}/>新建故事</button></header>
    <div className="story-library-tools"><label className="story-library-search"><Search size={17}/><input type="search" aria-label="搜索作品" value={query} onChange={event=>setQuery(event.target.value)} placeholder="搜索故事名称"/></label><button type="button" className="story-library-refresh" disabled={working} aria-label="刷新作品库" onClick={()=>act('正在刷新作品…',async()=>{library.refreshLocal();if(c.account)await cloud.list();})}><RefreshCw size={16}/></button><button type="button" disabled title="回收站将在后续版本开放"><Trash2 size={15}/>回收站</button><label className="story-library-test-toggle" title="隐藏测试记录将在后续版本开放"><input type="checkbox" disabled/>隐藏测试记录</label></div>
    {creating&&<form className="story-library-create-form" onSubmit={create}><label>故事名称<input ref={input} required maxLength={80} value={title} onChange={event=>setTitle(event.target.value)} placeholder="给新故事起个名字" disabled={working}/></label><label>保存位置<select value={source} onChange={event=>setSource(event.target.value)} disabled={working}>{c.account&&<option value="cloud">云端 · {c.account}</option>}<option value="local">本机 · 此浏览器</option></select></label><button type="submit" className="primary" disabled={working||!title.trim()}>创建故事</button><button type="button" disabled={working} aria-label="取消新建故事" onClick={()=>setCreating(false)}><X size={16}/></button></form>}
    {!c.account&&<p className="story-library-note">当前显示本机作品。<button type="button" onClick={onOpenCloud}>登录查看云端故事</button></p>}
    <div className="story-library-grid">{visible.slice(0,visibleLimit).map(story=><StoryLibraryCard key={(story.mode==='cloud'?c.account:'local')+':'+story.mode+':'+story.project.id} story={story} reader={reader} working={working} onOpen={()=>library.openStory(story,onSelect,onOpenCloud)}/>)}</div>
    {!visible.length&&<div className="story-library-empty"><BookOpen size={28}/><h2>{query?'没有找到这个故事':'从一个故事开始'}</h2><p>{query?'搜索只包含已经载入的作品。':'点击「新建故事」，写下第一个镜头。'}</p></div>}
    {c.account&&!c.projectsLoaded&&<p className="story-library-note">云端作品尚未载入，可刷新重试。</p>}
    {visible.length>visibleLimit?<button type="button" className="story-library-more" disabled={working} onClick={()=>setVisibleLimit(value=>value+12)}>显示更多作品</button>:c.moreProjects&&<button type="button" className="story-library-more" disabled={working} onClick={()=>act('正在载入更多作品…',async()=>{await cloud.loadMoreProjects();setVisibleLimit(value=>value+12);})}>加载更多云端作品</button>}
    {c.moreProjects&&query&&<p className="story-library-note">搜索只包含已载入的云端故事。</p>}
    {library.library.error&&<p className="story-library-error" role="alert">本机作品列表读取失败，原数据保留。<button type="button" onClick={onOpenLocal}>查看本机备份</button></p>}
    {state.workspace.mode==='cloud'&&state.workspace.dirty&&<p className="story-library-note">当前云故事还有未同步草稿；切换故事后仍可恢复。</p>}
    {library.pending&&<p className="story-library-note" role="status">{library.pending}</p>}{library.error&&<p className="story-library-error" role="alert">{library.error}</p>}
  </section>;
}

export function RecentStoriesDropdown({state,onOpenCloud,onOpenLocal,onSelect}){
  const library=useStoryLibrary(state),details=useRef(),stories=[...library.stories].sort((a,b)=>(storyEditedTimestamp(b.project.updatedAt??b.project.updated_at)||0)-(storyEditedTimestamp(a.project.updatedAt??a.project.updated_at)||0)).slice(0,8);
  return <details className="story-recent" ref={details} onKeyDown={event=>{if(event.key==='Escape'){details.current.open=false;details.current.querySelector('summary')?.focus();}}}><summary aria-label={'当前故事 '+state.project.title+'，切换最近故事'}><span className="story-recent-title">{state.project.title}</span><StoryLocation mode={state.workspace.mode}/><ChevronDown size={14}/></summary><div className="story-recent-menu"><b>最近故事</b>{stories.map(story=><button type="button" key={story.mode+':'+story.project.id} disabled={library.working} aria-current={state.workspace.mode===story.mode&&state.project.id===story.project.id?'page':undefined} onClick={()=>library.openStory(story,()=>{details.current.open=false;onSelect?.();},onOpenCloud)}><span>{story.project.title}</span><StoryLocation mode={story.mode}/></button>)}{!library.c.account&&<button type="button" onClick={onOpenCloud}>登录查看云端故事</button>}{library.library.error&&<button type="button" onClick={onOpenLocal}>查看本机备份</button>}{library.pending&&<small role="status">{library.pending}</small>}{library.error&&<small role="alert">{library.error}</small>}</div></details>;
}
