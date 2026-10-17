import React,{useRef,useState} from 'react';
import {Check,FileText,RotateCcw,Scissors,ArrowDown,Trash2,Plus,AlertCircle} from 'lucide-react';
import {store} from './store.js';
import {applyBreakdown,breakdownIssues,createBreakdown,importUndoIssues,mergeBreakdownSegment,replaceBreakdown,sourceLocation,splitBreakdownSegment,undoBreakdownImport} from './script-breakdown-model.js';
import './script-breakdown.css';

function ImportReceipt({project,record,onInspect}){
  const [asked,setAsked]=useState(false),receipt=record.receipt;
  if(!receipt)return null;
  const issues=importUndoIssues(project,receipt),scenes=receipt.entities.filter(e=>e.type==='scene');
  return <section className="breakdown-receipt" aria-label="本次剧本导入记录">
    <strong>{receipt.undone?'已撤销这次导入':`已导入 ${scenes.length} 场戏`}</strong>
    <p>{receipt.undone?'本次新增内容已移除，其它章节、素材和修改保留。可以重新分析原文，再核对一次。':'原文与核对稿已保存。继续编辑正式场戏时，会保护后续修改。'}</p>
    <details className="breakdown-original"><summary>回看这次导入的原文</summary><pre>{record.source}</pre></details>
    {!receipt.undone&&<><div className="breakdown-actions">{scenes.map(scene=>project.entities.some(e=>e.id===scene.id)&&<button key={scene.id} onClick={()=>onInspect?.(scene.id)}>打开 {project.entities.find(e=>e.id===scene.id)?.title||scene.title}</button>)}<button onClick={()=>setAsked(!asked)}><RotateCcw size={14}/>撤销本次导入</button></div>
    {asked&&(issues.length?<div className="breakdown-warning" role="alert"><strong>保留后续修改，不能直接撤销导入</strong><p>以下内容已改变。请先检查相关场戏，或使用顶部撤销逐步回退。</p><ul>{issues.map((issue,index)=><li key={index}>{issue.title}：{issue.reason}{issue.id&&<button onClick={()=>onInspect?.(issue.id)}>查看相关内容</button>}</li>)}</ul></div>:<div className="breakdown-warning"><p>只移除这次新增的 {scenes.length} 场戏{receipt.createdChapter?'和新章节':''}，保留其它项目内容。</p><button onClick={()=>{if(store.editProject(p=>undoBreakdownImport(p,record.batchId)))setAsked(false)}}>确认撤销这次导入</button><button onClick={()=>setAsked(false)}>保留导入</button></div>)}</>}
  </section>;
}

export default function ScriptBreakdown({project,onInspect}){
  const [reanalyse,setReanalyse]=useState(false),[staleAck,setStaleAck]=useState(''),[message,setMessage]=useState('');
  const cursors=useRef({});
  const source=String(project.journey?.brief?.sourceScript||''),draft=project.journey?.breakdown;
  const active=draft?.segments?.filter(s=>!s.omitted)||[],omitted=draft?.segments?.filter(s=>s.omitted)||[],chapters=project.entities.filter(e=>e.type==='chapter').sort((a,b)=>a.order-b.order);
  const editable=draft?.status==='draft',stale=!!draft&&source!==draft.source,issues=editable?breakdownIssues(project,draft):[];
  const edit=(mutator,key)=>store.editProject(p=>{const current=p.journey?.breakdown;if(!current||current.batchId!==draft.batchId||current.status!=='draft')throw Error('草案状态已更新，请重新核对。');mutator(current);},{coalesceKey:key||null});
  const analyse=()=>{try{const next=createBreakdown(source,{chapterTitle:draft?.chapterTitle||'剧本拆解',targetChapterId:chapters.some(c=>c.id===draft?.targetChapterId)?draft.targetChapterId:''});if(store.editProject(p=>replaceBreakdown(p,next))){setReanalyse(false);setStaleAck('');setMessage(`已整理 ${next.segments.length} 段待核对草案，尚未添加到正式章节。`);}}catch(error){setMessage(error.message);}};
  const apply=()=>{if(stale&&staleAck!==draft.batchId)return;let receipt;const done=store.editProject(p=>{receipt=applyBreakdown(p,draft.batchId);});if(done){store.setScope(receipt.chapterId);store.select(null);setMessage(`已确认导入 ${active.length} 场戏，接下来将制作这个章节。`);}};
  return <section className="script-breakdown" data-testid="script-breakdown" aria-label="剧本拆解与核对">
    <header><FileText size={22}/><div><h3>先核对拆场，再放进章节</h3><p>空行只提供起点。按地点、时间和剧情变化拆场；你决定哪些内容属于同一场。</p></div></header>
    {!draft?<div className="breakdown-start"><p>保留原文，先预览可修改的分场草案。最多一次核对 30 场，不会自动改写或覆盖正式章节。</p><button className="primary" disabled={!source.trim()} onClick={analyse} data-testid="breakdown-analyse">预览剧本拆解</button>{!source.trim()&&<p>先在第 1 步粘贴已有剧本。</p>}</div>:<>
      <div className="breakdown-summary"><span>{active.length} 场保留 · {omitted.length} 段暂不导入</span><span>{editable?'待核对，尚未导入':draft.status==='undone'?'本次导入已撤销':'已确认导入'}</span><button onClick={()=>setReanalyse(!reanalyse)}>重新分析原文</button></div>
      {reanalyse&&<div className="breakdown-warning"><strong>重新从简报原文整理草案</strong><p>{editable?'当前核对稿中的改名、拆分和合并会被重置；正式章节与简报原文保留。可用顶部撤销恢复本次草案重置。':'会建立新的待核对草案；已导入章节保持不动，导入记录保留。'}</p><button onClick={analyse}>确认重新分析</button><button onClick={()=>setReanalyse(false)}>保留当前草案</button></div>}
      {stale&&<div className="breakdown-warning"><strong>简报原文后来有更新</strong><p>下面仍基于拆解时保存的原文。可以重新分析，也可以确认继续使用这份原文快照。</p>{editable&&<label><input type="checkbox" checked={staleAck===draft.batchId} onChange={e=>setStaleAck(e.target.checked?draft.batchId:'')}/>这次继续使用当前草案的原文快照</label>}</div>}
      <details className="breakdown-original"><summary>查看本次拆解的完整原文（保留不改写）</summary><pre>{draft.source}</pre></details>
      <div className="breakdown-scene-list">{active.map((segment,index)=><article className="breakdown-scene" key={segment.id} data-testid="breakdown-scene">
        <div className="breakdown-scene-heading"><span>场 {index+1}</span>{editable&&<button onClick={()=>edit(current=>{current.segments.find(s=>s.id===segment.id).omitted=true})} aria-label={`暂不导入第 ${index+1} 场`}><Trash2 size={14}/>暂不导入</button>}</div>
        <label>第 {index+1} 场名称<input aria-label={`第 ${index+1} 场名称`} value={segment.title} maxLength={160} readOnly={!editable} onChange={e=>edit(current=>{current.segments.find(s=>s.id===segment.id).title=e.target.value},`breakdown:title:${segment.id}`)}/></label>
        <label>第 {index+1} 场动作与台词<textarea aria-label={`第 ${index+1} 场动作与台词`} rows={4} value={segment.script} maxLength={120000} readOnly={!editable} onSelect={e=>{cursors.current[segment.id]=e.currentTarget.selectionStart}} onClick={e=>{cursors.current[segment.id]=e.currentTarget.selectionStart}} onChange={e=>{const value=e.target.value;edit(current=>{const item=current.segments.find(s=>s.id===segment.id);item.script=value;item.mapping=item.ranges.length===1&&current.source.slice(item.ranges[0].start,item.ranges[0].end)===value?'exact':'shared'},`breakdown:script:${segment.id}`)}}/></label>
        <details className="breakdown-source"><summary>回看原句 · {segment.ranges.map(range=>sourceLocation(draft.source,range)).join('；')}</summary>{segment.ranges.map((range,i)=><div key={i}><small>{sourceLocation(draft.source,range)}</small><blockquote>{draft.source.slice(range.start,range.end)}</blockquote></div>)}<p>{segment.mapping==='shared'?'这场经过人工拆分、合并或改写。原文范围是依据，不表示当前文字逐字对应。':'编辑只改变核对稿，上面的原句保持不变。'}</p></details>
        {editable&&<div className="breakdown-split"><p>需要再拆一场？先把光标放在上方台词中间，再点“从光标处拆场”。</p><div className="breakdown-actions"><button onClick={()=>edit(current=>splitBreakdownSegment(current,segment.id,cursors.current[segment.id]))}><Scissors size={14}/>从光标处拆场</button><button disabled={index===active.length-1} onClick={()=>edit(current=>mergeBreakdownSegment(current,segment.id))}><ArrowDown size={14}/>与下一场合并</button></div></div>}
      </article>)}</div>
      {omitted.length>0&&<details className="breakdown-omitted" open><summary>暂不导入的 {omitted.length} 段 · 原文仍保留</summary>{omitted.map(segment=><div key={segment.id}><div><strong>{segment.title||'未命名场戏'}</strong><p>{segment.script.slice(0,160)}{segment.script.length>160?'…':''}</p></div>{editable&&<button onClick={()=>edit(current=>{current.segments.find(s=>s.id===segment.id).omitted=false})}><Plus size={14}/>恢复这一段</button>}</div>)}</details>}
      {editable&&<div className="breakdown-confirm"><h4>核对完成后放在哪里？</h4><label>导入目标<select aria-label="导入目标" value={draft.targetChapterId||''} onChange={e=>edit(current=>{current.targetChapterId=e.target.value})}><option value="">新建一个章节</option>{chapters.map(chapter=><option key={chapter.id} value={chapter.id}>追加到：{chapter.title}</option>)}{draft.targetChapterId&&!chapters.some(c=>c.id===draft.targetChapterId)&&<option value={draft.targetChapterId}>原目标已被删除，请重新选择</option>}</select></label>{!draft.targetChapterId&&<label>新章节名称<input aria-label="新章节名称" value={draft.chapterTitle} maxLength={160} onChange={e=>edit(current=>{current.chapterTitle=e.target.value},'breakdown:chapter-title')}/></label>}
        <p>将{draft.targetChapterId?'在所选章节末尾追加':'新建章节并加入'} {active.length} 场戏；{omitted.length?`${omitted.length} 段暂不导入；`:''}已有章节和简报原文保留。</p>
        {issues.length>0&&<ul className="breakdown-issues">{issues.map(issue=><li key={issue}><AlertCircle size={14}/>{issue}</li>)}</ul>}
        <button className="primary" disabled={issues.length>0||(stale&&staleAck!==draft.batchId)} onClick={apply} data-testid="breakdown-apply"><Check size={16}/>确认导入 {active.length} 场戏</button><small>一次确认作为一项修改保存。同一批次不会重复导入。</small>
      </div>}
      {draft.receipt&&<ImportReceipt key={draft.batchId} project={project} record={draft} onInspect={onInspect}/>}
    </>}
    {message&&<p role="status" className="breakdown-message">{message}</p>}
    {!!project.journey?.breakdownImports?.length&&<details className="breakdown-history"><summary>以前的导入记录（{project.journey.breakdownImports.length}）</summary>{project.journey.breakdownImports.map(record=><ImportReceipt key={record.batchId} project={project} record={record} onInspect={onInspect}/>)}</details>}
  </section>;
}
