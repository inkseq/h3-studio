const copy=value=>structuredClone(value);
const uid=prefix=>`${prefix}-${globalThis.crypto.randomUUID()}`;
export const MAX_BREAKDOWN_SCENES=30;

function trimmedRange(source,start,end){
  const text=source.slice(start,end),leading=text.length-text.trimStart().length,trailing=text.length-text.trimEnd().length;
  return {start:start+leading,end:end-trailing};
}

export function createBreakdown(source,{chapterTitle='剧本拆解',targetChapterId=''}={}){
  if(typeof source!=='string'||!source.trim())throw Error('先在故事简报中粘贴剧本，再预览拆场。');
  if(source.length>120000)throw Error('剧本超过 120000 字符，请先分成多份整理；原文未修改。');
  const ranges=[],pattern=/\r?\n[ \t]*(?:\r?\n)+/g;let start=0,match;
  while((match=pattern.exec(source))){const range=trimmedRange(source,start,match.index);if(range.end>range.start)ranges.push(range);start=pattern.lastIndex;}
  const last=trimmedRange(source,start,source.length);if(last.end>last.start)ranges.push(last);
  if(ranges.length>MAX_BREAKDOWN_SCENES)throw Error(`原文包含 ${ranges.length} 段，一次最多核对 ${MAX_BREAKDOWN_SCENES} 场。请在简报中分批整理；没有截断或导入任何内容。`);
  return {batchId:uid('breakdown'),source,createdAt:new Date().toISOString(),status:'draft',chapterTitle,targetChapterId,segments:ranges.map((range,index)=>({id:uid('draftscene'),title:`场 ${index+1}`,script:source.slice(range.start,range.end),ranges:[range],omitted:false,mapping:'exact'}))};
}

export function replaceBreakdown(project,draft){
  project.journey??={};
  const previous=project.journey.breakdown;
  if(previous?.receipt){
    const imports=project.journey.breakdownImports||[];
    project.journey.breakdownImports=[...imports.filter(item=>item.batchId!==previous.batchId),{batchId:previous.batchId,source:previous.source,receipt:copy(previous.receipt),status:previous.status}];
  }
  project.journey.breakdown=copy(draft);
}

function editable(draft){if(!draft||draft.status!=='draft')throw Error('这份草案已经导入，不能重复修改或导入。可重新分析生成新草案。');}
export function splitBreakdownSegment(draft,id,cursor){
  editable(draft);const at=draft.segments.findIndex(s=>s.id===id),segment=draft.segments[at];
  if(!segment||segment.omitted)throw Error('找不到可拆分的场戏。');
  if(draft.segments.filter(s=>!s.omitted).length>=MAX_BREAKDOWN_SCENES)throw Error(`一次最多保留 ${MAX_BREAKDOWN_SCENES} 场。`);
  const first=segment.script.slice(0,cursor),second=segment.script.slice(cursor);
  if(!Number.isInteger(cursor)||cursor<=0||cursor>=segment.script.length||!first.trim()||!second.trim())throw Error('请把光标放在动作与台词中间，光标前后都要有内容。');
  let rangesA=copy(segment.ranges),rangesB=copy(segment.ranges),mapping='shared';
  const range=segment.ranges[0];
  if(segment.ranges.length===1&&draft.source.slice(range.start,range.end)===segment.script){rangesA=[{start:range.start,end:range.start+cursor}];rangesB=[{start:range.start+cursor,end:range.end}];mapping='exact';}
  draft.segments.splice(at,1,{...segment,script:first,ranges:rangesA,mapping},{...segment,id:uid('draftscene'),title:`${segment.title.slice(0,150)}（后段）`,script:second,ranges:rangesB,mapping});
}

function mergedRanges(ranges){
  const sorted=ranges.map(copy).sort((a,b)=>a.start-b.start),result=[];
  for(const range of sorted){const last=result.at(-1);if(last&&range.start<=last.end)last.end=Math.max(last.end,range.end);else result.push(range);}
  return result;
}
export function mergeBreakdownSegment(draft,id){
  editable(draft);const at=draft.segments.findIndex(s=>s.id===id),next=draft.segments.findIndex((s,index)=>index>at&&!s.omitted);
  if(at<0||next<0||draft.segments[at].omitted)throw Error('这场后面没有可合并的场戏。');
  const first=draft.segments[at],second=draft.segments[next];
  if(first.script.length+second.script.length+2>120000)throw Error('合并后的场戏超过 120000 字符，请保留为两场。');
  draft.segments[at]={...first,script:`${first.script}\n\n${second.script}`,ranges:mergedRanges([...first.ranges,...second.ranges]),mapping:'shared'};
  draft.segments.splice(next,1);
}

export function sourceLocation(source,range){
  const first=source.slice(0,range.start).split('\n').length,last=source.slice(0,Math.max(range.start,range.end-1)).split('\n').length;
  return `原文第 ${first}${last===first?'':`–${last}`} 行 · 字符 ${range.start+1}–${range.end}`;
}

export function breakdownIssues(project,draft){
  if(!draft||draft.status!=='draft')return ['这份草案已经导入，不能再次应用。'];
  const scenes=draft.segments.filter(s=>!s.omitted),issues=[];
  if(!scenes.length)issues.push('至少保留一场戏。');
  if(scenes.length>MAX_BREAKDOWN_SCENES)issues.push(`一次最多保留 ${MAX_BREAKDOWN_SCENES} 场。`);
  scenes.forEach((scene,index)=>{if(!scene.title.trim())issues.push(`第 ${index+1} 场需要场名。`);if(!scene.script.trim())issues.push(`第 ${index+1} 场需要动作与台词。`);});
  if(draft.targetChapterId&&!project.entities.some(e=>e.id===draft.targetChapterId&&e.type==='chapter'))issues.push('选择的章节已不存在，请重新选择或新建章节。');
  if(!draft.targetChapterId&&!draft.chapterTitle.trim())issues.push('填写新章节名称。');
  if(project.entities.length+scenes.length+(draft.targetChapterId?0:1)>5000)issues.push('项目节点超过上限，无法导入；草案已保留。');
  return issues;
}

function nextOrder(project,parentId){const siblings=project.entities.filter(e=>e.parentId===parentId);return siblings.length?Math.max(...siblings.map(e=>e.order))+1:0;}
function entity(type,parentId,title,data,order){return {id:uid(type),type,parentId,title,description:'',data,order,version:1,status:'draft'};}
export function applyBreakdown(project,batchId){
  const draft=project.journey?.breakdown;
  if(!draft||draft.batchId!==batchId)throw Error('草案已更新，请重新核对后导入。');
  const issues=breakdownIssues(project,draft);if(issues.length)throw Error(issues.join(' '));
  const created=[],createdChapter=!draft.targetChapterId;
  let chapter=project.entities.find(e=>e.id===draft.targetChapterId);
  if(createdChapter){chapter=entity('chapter',null,draft.chapterTitle.trim(),{},nextOrder(project,null));created.push(chapter);}
  let order=nextOrder(project,chapter.id);
  for(const segment of draft.segments.filter(s=>!s.omitted))created.push(entity('scene',chapter.id,segment.title.trim(),{script:segment.script,importSource:{batchId:draft.batchId,segmentId:segment.id,ranges:copy(segment.ranges),mapping:segment.mapping}},order++));
  project.entities.push(...created);
  const receipt={chapterId:chapter.id,createdChapter,createdAt:new Date().toISOString(),entities:copy(created),undone:false};
  draft.status='applied';draft.receipt=receipt;
  return receipt;
}

export function importUndoIssues(project,receipt){
  if(!receipt||receipt.undone)return [{id:null,title:'本次导入',reason:'这次导入已经撤销。'}];
  const ids=new Set(receipt.entities.map(e=>e.id)),issues=[];
  for(const original of receipt.entities){const current=project.entities.find(e=>e.id===original.id);if(!current)issues.push({id:null,title:original.title,reason:'已被删除或合并'});else if(JSON.stringify(current)!==JSON.stringify(original))issues.push({id:current.id,title:current.title,reason:'导入后有修改'});}
  for(const node of project.entities)if(!ids.has(node.id)&&ids.has(node.parentId))issues.push({id:node.id,title:node.title,reason:'在导入后添加了内容'});
  for(const link of project.links)if(ids.has(link.source)||ids.has(link.target)){const node=project.entities.find(e=>e.id===(ids.has(link.target)?link.target:link.source));issues.push({id:node?.id||null,title:node?.title||'关联内容',reason:'导入后建立了参考关系'});}
  return issues.filter((issue,index,items)=>items.findIndex(item=>item.id===issue.id&&item.title===issue.title&&item.reason===issue.reason)===index);
}

export function undoBreakdownImport(project,batchId){
  const record=project.journey?.breakdown?.batchId===batchId?project.journey.breakdown:project.journey?.breakdownImports?.find(item=>item.batchId===batchId);
  if(!record?.receipt)throw Error('找不到这次导入记录。');
  const issues=importUndoIssues(project,record.receipt);
  if(issues.length)throw Error('本次导入后的内容已有变化，已保护这些修改。请打开相关场戏检查，或按顶部撤销逐步回退。');
  const ids=new Set(record.receipt.entities.map(e=>e.id));
  project.entities=project.entities.filter(e=>!ids.has(e.id));
  for(const id of ids)delete project.layout.positions[id];
  record.status='undone';record.receipt.undone=true;
  return true;
}
