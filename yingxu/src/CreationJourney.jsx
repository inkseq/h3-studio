import {chapterTiming} from './media-timing.js';
import React from 'react';
import {deferredPanel} from './deferred-panel.js';
import { ArrowRight, ArrowLeft, ArrowUp, ArrowDown, Plus, Check, Circle, Clapperboard, FileText, Users, Image, Headphones, Download, RotateCcw, Sparkles, Film, CheckCircle2, AlertCircle } from 'lucide-react';
import { store, typeLabels, children } from './store.js';
import './creation-journey.css';
import CandidateReview from './CandidateReview.jsx';
const DeliveryStudio=deferredPanel(()=>import('./DeliveryStudio.jsx'),'交付与粗剪');
import {deliveryIssues} from './delivery-model.js';
import StoryCoach from './StoryCoach.jsx';
const CharacterStudio=deferredPanel(()=>import('./CharacterStudio.jsx'),'角色造型');
const ScriptBreakdown=deferredPanel(()=>import('./ScriptBreakdown.jsx'),'剧本拆分');
import ReferenceEditor from './ReferenceEditor.jsx';
import ShotMediaWorkspace from './ShotMediaWorkspace.jsx';
import LocationStudio,{SceneLocationPicker} from './LocationStudio.jsx';
import AssetPreview from './AssetPreview.jsx';
import {entityCover} from './EntityMediaUpload.jsx';
const SoundStudio=deferredPanel(()=>import('./SoundStudio.jsx'),'声音编辑器');
const SubtitleStudio=deferredPanel(()=>import('./SubtitleStudio.jsx'),'字幕编辑器');

const stages = [
  { title:'故事简报', short:'定方向', icon:FileText, decision:'先确定讲什么、给谁看，以及作品的长度。', next:'安排章节与剧本' },
  { title:'章节剧本', short:'理故事', icon:Clapperboard, decision:'把故事变成章节与场戏，明确每一场的动作和台词。', next:'确定人物与场景' },
  { title:'人物与场景', short:'定形象', icon:Users, decision:'确定跨镜头保持一致的人物身份与地点；细节可以边做边补。', next:'设计镜头与参考' },
  { title:'镜头与参考', short:'拍什么', icon:Image, decision:'逐个说清画面、镜头作用与时长；只添加真正需要的参考。', next:'进入候选审核' },
  { title:'候选审核', short:'选与改', icon:CheckCircle2, decision:'比较版本、保留选择，再把不满意的地方变成具体返修要求。', next:'安排节奏与声音' },
  { title:'节奏与声音', short:'看整体', icon:Headphones, decision:'检查镜头先后与总时长，再决定对白、音乐和字幕。', next:'检查交付计划' },
  { title:'交付检查', short:'查缺项', icon:Download, decision:'回看创作决定与待办，带着明确的计划继续制作。', next:null }
];
const order=(a,b)=>a.order-b.order;
const orderedShots=(p,chapter)=>chapter?children(p,chapter.id).filter(x=>x.type==='scene').flatMap(scene=>children(p,scene.id).filter(x=>x.type==='shot')):[];
const Field=({label,hint,children:content})=><label className="journey-field"><span>{label}</span>{React.isValidElement(content)?React.cloneElement(content,{'aria-label':content.props['aria-label']||label}):content}{hint&&<small>{hint}</small>}</label>;
const Empty=({title,body,action,label})=><div className="journey-empty"><Circle size={24}/><h3>{title}</h3><p>{body}</p>{action&&<button onClick={action}><Plus size={15}/>{label}</button>}</div>;
const reviewNames={selected:'已有选择',revision:'待返修',failed:'演示：失败',retry:'演示：等待重试',cancelled:'演示：已取消'};
function NumberInput({value,min=1/24,max=3600,onValue}) {
  const format=number=>String(Number(Number(number).toFixed(6)));
  const [draft,setDraft]=React.useState(format(value));
  React.useEffect(()=>setDraft(format(value)),[value]);
  const valid=text=>text!==''&&Number.isFinite(Number(text))&&Number(text)>=min&&Number(text)<=max;
  return <input type="number" step="any" min={min} max={max} value={draft} onChange={e=>{const text=e.target.value;setDraft(text);if(valid(text))onValue(Number(text));}} onBlur={()=>{if(!valid(draft)){setDraft(String(value));store.notify(`请输入 ${min} 至 ${max} 秒；已保留上次有效时长。`);}}}/>;
}

export default function CreationJourney({state,onAdd,onInspect,onPreview}) {
  const p=state.project,j=p.journey||{},brief=j.brief||{},sound=j.sound||{};
  const stage=Number.isInteger(j.stage)&&j.stage>=1&&j.stage<=7?j.stage:1;
  const chapters=p.entities.filter(x=>x.type==='chapter').sort(order);
  const chapter=chapters.find(x=>x.id===state.scopeId)||chapters[0]||null;
  const scenes=chapter?children(p,chapter.id).filter(x=>x.type==='scene'):[];
  const shots=orderedShots(p,chapter),allShots=chapters.flatMap(c=>orderedShots(p,c));
  const characters=p.entities.filter(x=>x.type==='character'),locations=p.entities.filter(x=>x.type==='location');
  const currentShot=shots.find(x=>x.id===j.reviewShotId)||shots[0]||null;
  const currentAsset=currentShot&&p.entities.find(x=>x.id===currentShot.data.selectedAssetId);
  const demoChoice=currentShot&&!currentShot.data.selectedAssetId?currentShot.data.uxReview?.chosen:null;
  const usableCandidate=shot=>shot.data.selectedAssetId?p.entities.some(x=>x.id===shot.data.selectedAssetId&&x.data.fileId&&!x.data.missingFile):!!shot.data.uxReview?.chosen;
  const totalSeconds=Number((chapterTiming(shots).at(-1)?.end||0).toFixed(3));
  const setJourney=(patch,key)=>{const current=store.getState().project;return store.updateProject({journey:{...(current.journey||{}),...patch}},{coalesceKey:key?`journey:${key}`:null});};
  const setBrief=(key,value)=>setJourney({brief:{...(store.getState().project.journey?.brief||{}),[key]:value}},`brief:${key}`);
  const setSound=(key,value)=>setJourney({sound:{...(store.getState().project.journey?.sound||{}),[key]:value}},`sound:${key}`);
  const go=next=>{setJourney({stage:next});store.select(null);};
  const add=(kind,parent)=>{if(parent?.type==='chapter')store.setScope(parent.id);if(parent)store.select(parent.id);onAdd(kind);};
  const patchShot=(id,key,value)=>store.updateEntity(id,key==='description'?{description:value}:{data:{[key]:value}},{coalesceKey:`journey:${id}:${key}`});
  const setReview=patch=>{if(!currentShot)return;const latest=store.getState().project.entities.find(x=>x.id===currentShot.id);store.updateEntity(currentShot.id,{...(patch.status==='selected'?{status:'draft'}:{}),data:{...(patch.chosen?{selectedAssetId:''}:{}),uxReview:{...(latest?.data.uxReview||{}),...patch}}},{coalesceKey:patch.note!==undefined?`review:${currentShot.id}:note`:null});};
  const addExample=()=>{
    const n=store.getState().project.entities.filter(e=>e.type==='chapter').length+1;
    const chapterId=store.addEntity('chapter',null,{title:`示例拆解 ${n} · 错过的车票`,description:'示例：她发现一张写着明天日期的旧车票。可改写，也可删除。'});
    if(!chapterId)return;
    const sceneId=store.addEntity('scene',chapterId,{title:'车站 · 雨夜',description:'让观众看到车票上的异常，留下悬念。',data:{script:'林岚走进空荡的车站，捡起一张旧车票。\n她低声说：“这不是……明天吗？”\n广播突然响起，她抬头。',time:'雨夜'}});
    if(!sceneId)return;
    store.addEntity('shot',sceneId,{title:'旧车票的日期',description:'手指拂去车票上的雨水，镜头看清票面日期。',data:{demoType:'ticket',seconds:4,goal:'给出异常线索',shotSize:'特写'}});
    store.addEntity('shot',sceneId,{title:'她抬头看向广播',description:'林岚握着车票抬头，空车站里只有她一人。',data:{demoType:'reaction',seconds:6,goal:'人物反应，留下悬念',shotSize:'中景'}});
    store.setScope(chapterId);store.select(null);store.notify('已追加一章、一场和两个示例镜头。原有内容保留，可逐项修改。');
  };
  const useScript=()=>{
    const project=store.getState().project;
    const text=String(project.journey?.brief?.sourceScript||'').trim();
    if(!text){store.notify('先粘贴一段剧本，再保存为场戏。');return;}
    const previous=project.journey?.scriptImport;
    if(previous?.source===text&&project.entities.some(e=>e.id===previous.chapterId&&e.type==='chapter')){store.setScope(previous.chapterId);go(2);store.notify('这份原文已经保存，已回到对应章节。没有重复创建。');return;}
    const id=store.addEntity('chapter',null,{title:'导入剧本 · 待分章',description:'保留原文，接下来可手动拆成多个章节与场戏。'});
    if(!id)return;
    store.addEntity('scene',id,{title:'原始剧本 · 待拆场',data:{script:text}});setJourney({scriptImport:{source:text,chapterId:id}});store.setScope(id);store.select(null);go(2);store.notify('原文已保存为一章一场。按故事发生地点与时间手动拆场，原有内容未覆盖。');
  };
  const checks=[
    {stage:1,title:'故事方向',ok:!!p.logline.trim(),detail:p.logline.trim()?'已经写下项目一句话':'补一句主角、目标与阻碍，让大家知道要讲什么。'},
    {stage:2,title:'章节与剧本',ok:!!chapter&&scenes.length>0&&scenes.every(s=>String(s.data.script||'').trim()),detail:!chapter?'还没有章节。':!scenes.length?'这一章还没有场戏。':`${scenes.filter(s=>String(s.data.script||'').trim()).length} / ${scenes.length} 场已写剧本`},
    {stage:3,title:'人物与地点设定',ok:characters.length>0&&locations.length>0,optional:true,detail:`${characters.length} 个角色 · ${locations.length} 个地点。无人或抽象作品可以暂不设置。`},
    {stage:4,title:'镜头描述与时长',ok:shots.length>0&&shots.every(s=>String(s.data.prompt||s.description||'').trim()&&s.data.seconds>0),detail:shots.length?`${shots.filter(s=>String(s.data.prompt||s.description||'').trim()&&s.data.seconds>0).length} / ${shots.length} 镜已写清画面与时长`:'这一章还没有镜头。'},
    {stage:5,title:'候选选择与返修',ok:shots.length>0&&shots.every(s=>usableCandidate(s)&&s.status!=='review'&&!['revision','failed','retry','cancelled'].includes(s.data.uxReview?.status)),detail:`${shots.filter(usableCandidate).length} / ${shots.length} 镜已有可用候选选择${shots.some(s=>s.status==='review')?'；内容或参考有改动，原选择保留待复核':''}。示例选择只用于验证审核流程。`},
    {stage:6,title:'声音与节奏决定',ok:!!sound.mode,detail:sound.mode==='silent'?'已选择静音先行。':sound.mode?'已记录声音需求；导出前还需听审素材。':'还未决定声音方案，也可以明确选择静音。'}
  ];
  const incomplete=deliveryIssues(p,chapter?.id).filter(c=>!c.accepted);
  const stageStatus=[p.logline.trim()?'方向已记录':'等待你的想法',chapters.length?`${chapters.length} 章 · ${p.entities.filter(e=>e.type==='scene').length} 场`:'从第一章开始',`${characters.length} 角色 · ${locations.length} 地点`,`${allShots.length} 个镜头`,`${allShots.filter(s=>s.data.uxReview?.chosen||s.data.selectedAssetId).length} 镜已有选择`,sound.mode==='silent'?'静音先行':sound.mode?'声音方案已记录':'可以先做静音版',incomplete.length?`${incomplete.length} 项待补充`:'创作决定已记录'];
  const quickShot=()=>{const ids=['chapter','scene','shot'].map(kind=>kind+'-'+crypto.randomUUID());const ok=store.editProject(q=>{const base={description:'',order:0,version:1,status:'draft',data:{}};q.entities.push({...base,id:ids[0],type:'chapter',title:'单镜练习',parentId:null,order:q.entities.filter(e=>e.type==='chapter').length},{...base,id:ids[1],type:'scene',title:'第一场',parentId:ids[0],data:{script:q.logline}},{...base,id:ids[2],type:'shot',title:'我的第一个镜头',parentId:ids[1],description:q.logline,data:{seconds:6}});q.journey={...q.journey,stage:4,reviewShotId:ids[2]};});if(ok){store.setScope(ids[0]);store.select(null);}};
  const fixIssue=issue=>{const entity=p.entities.find(e=>e.id===issue.entityId);let e=entity;while(e&&e.type!=='chapter')e=p.entities.find(x=>x.id===e.parentId);if(e)store.setScope(e.id);setJourney({stage:issue.stage,focusCharacterId:entity?.type==='character'?entity.id:j.focusCharacterId||null,focusLookId:issue.lookId||null,reviewShotId:entity?.type==='shot'?entity.id:j.reviewShotId||null,returnToDelivery:{issueId:issue.id,entityId:issue.entityId||null,field:issue.field||'',title:issue.title||'回看制作决定'},lastDeliveryIssue:issue.id});store.select(null);};
  React.useEffect(()=>{if(stage===7||!j.returnToDelivery?.entityId)return;let observer;const focus=()=>{const element=stage===3?document.querySelector('.character-studio'):document.getElementById('journey-entity-'+j.returnToDelivery.entityId);if(!element)return false;element.scrollIntoView({block:'center'});element.classList.add('v6-target');if(j.returnToDelivery.field==='referenceRanges'){const details=element.querySelector('details');if(details)details.open=true;}element.querySelector('textarea,input')?.focus({preventScroll:true});observer?.disconnect();return true;};if(!focus()){const panel=document.querySelector('.journey-stage-body');if(panel){observer=new MutationObserver(focus);observer.observe(panel,{childList:true,subtree:true});}}return()=>observer?.disconnect();},[stage,j.returnToDelivery?.entityId,j.returnToDelivery?.field]);
  const StageIcon=stages[stage-1].icon;

  return <div className="creation-journey" data-stage={stage}>
    {stage!==7&&j.returnToDelivery&&<div className="v6-return" role="status"><span>从交付检查来到这里：{j.returnToDelivery.title}</span><button onClick={()=>{setJourney({stage:7,returnToDelivery:null});store.select(null)}}>补完，返回交付检查</button></div>}
    <header className="journey-heading"><div><span className="eyebrow">YOUR STORY, ONE DECISION AT A TIME</span><h1>把想法，变成一部短剧。</h1><p>知道下一步做什么，也随时可以回头改。所有章节、人物和镜头与无限画布共用。</p></div><div className="journey-project-chip"><Film size={16}/><span>{p.title}</span><small>创作计划</small></div></header>
    <nav className="journey-map" aria-label="短剧制作阶段">{stages.map((s,index)=><button key={s.title} data-testid={`journey-stage-${index+1}`} aria-current={stage===index+1?'step':undefined} onClick={()=>go(index+1)}><span className="journey-step-number">{String(index+1).padStart(2,'0')}</span><span><strong>{s.title}</strong><small>{s.short}</small></span></button>)}</nav>
    <section className="journey-panel" data-testid="journey-panel" aria-label={stages[stage-1].title}>
      <header className="journey-panel-heading"><div className="journey-step-icon"><StageIcon size={24}/></div><div><span className="eyebrow">第 {stage} 步 / 共 7 步</span><h2>{stages[stage-1].title}</h2><p><b>这一步决定什么</b> · {stages[stage-1].decision}</p></div><span className="journey-status">{stageStatus[stage-1]}</span></header>

      {stage===1&&<div className="journey-stage-body">
        <div className="journey-entry-options" role="group" aria-label="从哪里开始"><button aria-pressed={(brief.entry||'idea')==='idea'} onClick={()=>setBrief('entry','idea')}><Sparkles size={20}/><span><strong>我只有一个想法</strong><small>先说故事，再安排章节</small></span>{(brief.entry||'idea')==='idea'&&<Check size={16}/>}</button><button aria-pressed={brief.entry==='script'} onClick={()=>setBrief('entry','script')}><FileText size={20}/><span><strong>我已经有剧本</strong><small>保留原文，再手动拆场</small></span>{brief.entry==='script'&&<Check size={16}/>}</button></div>
        <StoryCoach project={p} onQuickShot={quickShot}/>
        <Field label="项目一句话" hint="用自己的话说：谁想得到什么，遇到了什么阻碍？"><textarea rows={3} maxLength={24000} value={p.logline} onChange={e=>store.updateProject({logline:e.target.value},{coalesceKey:'journey:logline'})} placeholder="例如：一个能听见未来广播的女孩，必须在末班车前找到失踪的姐姐。"/></Field>
        <div className="journey-field-grid"><Field label="目标观众"><input value={brief.audience||''} maxLength={300} onChange={e=>setBrief('audience',e.target.value)} placeholder="例如：喜欢都市悬疑的年轻观众"/></Field><Field label="画幅"><select value={brief.aspect||'9:16'} onChange={e=>setBrief('aspect',e.target.value)}><option value="9:16">9:16 · 竖屏短剧</option><option value="16:9">16:9 · 横屏叙事</option><option value="1:1">1:1 · 方形画面</option></select></Field><Field label="单章目标时长（秒）" hint="先定目标；后面按镜头时长回看。"><NumberInput min={5} value={brief.seconds??60} onValue={value=>setBrief('seconds',value)}/></Field></div>
        {brief.entry==='script'&&<div className="journey-soft-panel"><Field label="粘贴已有剧本" hint="原文随项目保存。先预览分场，再确认加入章节；不会自动改写或重复建立一份原文场戏。"><textarea rows={7} maxLength={120000} value={brief.sourceScript||''} onChange={e=>setBrief('sourceScript',e.target.value)} placeholder="粘贴故事梗概、人物动作和台词……"/></Field><button className="primary" disabled={!String(brief.sourceScript||'').trim()} onClick={()=>go(2)}><FileText size={16}/>下一步：预览拆场</button><details><summary>只想把整段剧本当成一场？</summary><p>直接建立一章一场，适合已经整理好的一段连续场景。之后仍可编辑；不要再把同一原文导入一次。</p><button disabled={!String(brief.sourceScript||'').trim()} onClick={useScript}>直接保留为一章一场</button></details></div>}
        <aside className="journey-tip"><span>不需要先写出专业提示词。</span>一句清楚的故事方向就足够开始。角色、参考与镜头语言会在后面逐步补充。</aside>
      </div>}

      {stage===2&&<div className="journey-stage-body">
        <div className="journey-section-top"><div><h3>章节是你自己的故事结构</h3><p>一章可以有多场戏；同一场戏再拆成多个镜头。</p></div><button className="primary" onClick={()=>add('chapter')}><Plus size={16}/>新增章节</button></div>
        {brief.sourceScript&&<ScriptBreakdown project={p} onInspect={onInspect}/>}
        {!chapters.length&&<Empty title="从一个小冲突开始" body="先加第一章，写清开头、变化和结尾钩子。也可以追加示例，看看拆场长什么样。"/>}
        <div className="journey-chapters">{chapters.map((c,index)=><article className="journey-chapter" id={"journey-entity-"+c.id} key={c.id}><div className="journey-chapter-head"><span className="journey-chapter-number">{String(index+1).padStart(2,'0')}</span><div><button className="journey-title-button" onClick={()=>onInspect(c.id)}>{c.title}</button><p>{c.description||'补充这一章的故事推进或结尾钩子。'}</p></div><button onClick={()=>add('scene',c)}><Plus size={14}/>添加场戏</button></div>{children(p,c.id).filter(s=>s.type==='scene').map(s=><div className="journey-scene" id={"journey-entity-"+s.id} key={s.id}><div><button className="journey-title-button" onClick={()=>onInspect(s.id)}>{s.title}<ArrowRight size={13}/></button><small>{children(p,s.id).filter(q=>q.type==='shot').length} 个镜头</small></div><Field label={`${s.title} · 动作与台词`}><textarea value={s.data.script||''} maxLength={120000} rows={3} onChange={e=>store.updateEntity(s.id,{data:{script:e.target.value}},{coalesceKey:`journey:script:${s.id}`})} placeholder="谁做了什么，说了什么？每场只推进一件重要的事。"/></Field><SceneLocationPicker project={p} scene={s}/><button className="journey-minor-button" onClick={()=>{store.setScope(c.id);add('shot',s)}}><Plus size={14}/>为这场添加镜头</button></div>)}</article>)}</div>
        <div className="journey-example"><div><Sparkles size={19}/><div><strong>第一次拆剧本？先看一个示例。</strong><p>追加一章、一场和两个镜头，保留你已有的所有内容。</p></div></div><button data-testid="journey-add-example" onClick={addExample}>追加示例拆解<ArrowRight size={14}/></button></div>
      </div>}

      {stage===3&&<div className="journey-stage-body"><div className="journey-library-grid">{[['character','人物设定',characters,'身份、性格、固定长相；服装变化记在具体场戏。'],['location','场景设定',locations,'地点与空间关系；昼夜、天气和道具变化记在场戏。']].map(([kind,title,list,description])=><section key={kind} className="journey-library"><div className="journey-section-top"><div><h3>{title}</h3><p>{description}</p></div><button onClick={()=>add(kind)} aria-label={`新增${typeLabels[kind]}`}><Plus size={15}/></button></div>{list.length?list.map(e=><button className="journey-library-item" key={e.id} onClick={()=>onInspect(e.id)}><span className={'journey-avatar '+kind}>{entityCover(p,e)?<AssetPreview entity={entityCover(p,e)}/>:kind==='character'?<Users size={19}/>:<Image size={19}/>}</span><span><strong>{e.title}</strong><small>{e.description||'点击补充设定与参考要求'}</small></span><ArrowRight size={14}/></button>):<Empty title={kind==='character'?'故事里有哪些人？':'故事发生在哪里？'} body="可以直接上传参考图，也可先添加名字。" label={`新增${typeLabels[kind]}`} action={()=>add(kind)}/>}</section>)}</div><CharacterStudio project={p} chapterId={chapter?.id} onAdd={onAdd} onInspect={onInspect}/><LocationStudio project={p} onAdd={onAdd} onInspect={onInspect}/><aside className="journey-tip">无人、产品或抽象画面可以跳过人物设定。地点也可以先用文字说明。</aside></div>}

      {stage>=4&&<div className="journey-chapter-filter"><Field label="当前制作章节"><select value={chapter?.id||''} onChange={e=>{store.setScope(e.target.value||null);store.select(null)}}>{!chapters.length&&<option value="">还没有章节</option>}{chapters.map(c=><option key={c.id} value={c.id}>{c.title}</option>)}</select></Field><span>{shots.length} 个镜头 · 已规划 {totalSeconds} 秒</span></div>}

      {stage===4&&<div className="journey-stage-body"><div className="journey-section-top"><div><h3>一个镜头，说清一件事</h3><p>写动作与情绪，比堆叠模型术语更重要。</p></div><button onClick={()=>scenes.length?add('shot',scenes[0]):chapter?add('scene',chapter):go(2)}><Plus size={15}/>{scenes.length?'新增镜头':chapter?'先添加场戏':'先添加章节'}</button></div>{!shots.length?<Empty title="这一章还没有镜头" body="先在章节中添加场戏，再把动作拆成可拍摄的画面。" action={()=>go(2)} label="回到章节剧本"/>:<><div className="shot-visual-nav" role="group" aria-label="选择要设计的镜头">{shots.map((shot,index)=>{const linked=p.links.find(link=>link.target===shot.id&&link.role==='firstFrame')||p.links.find(link=>link.target===shot.id&&p.entities.find(entity=>entity.id===link.source)?.type==='image'),asset=p.entities.find(entity=>entity.id===(shot.data.selectedAssetId||linked?.source)&&['image','video'].includes(entity.type));return <button key={shot.id} type="button" aria-pressed={shot.id===currentShot?.id} onClick={()=>setJourney({reviewShotId:shot.id})}>{asset?<AssetPreview entity={asset} compact/>:<div className="shot-nav-placeholder"><Film size={28}/></div>}<strong>{String(index+1).padStart(2,'0')} · {shot.title}</strong><small>{shot.data.seconds||5} 秒 · {p.links.filter(link=>link.target===shot.id).length} 份参考</small></button>;})}</div><div className="journey-shot-cards">{[currentShot].filter(Boolean).map((s)=><article className="journey-shot-card" id={"journey-entity-"+s.id} key={s.id}><header><span className="journey-shot-number">{String(shots.findIndex(shot=>shot.id===s.id)+1).padStart(2,'0')}</span><h3>{s.title}</h3><small>{p.entities.find(e=>e.id===s.parentId)?.title}</small></header><details className="shot-storyboard-details"><summary>分镜说明与拍法 · 可选</summary><div className="journey-shot-fields"><Field label={`${s.title} · 镜头作用`}><input value={s.data.goal||''} maxLength={1000} onChange={e=>patchShot(s.id,'goal',e.target.value)} placeholder="例如：交代线索 / 人物反应 / 转折"/></Field><Field label={`${s.title} · 时长（秒）`}><NumberInput value={s.data.seconds||6} onValue={value=>patchShot(s.id,'seconds',value)}/></Field></div><Field label={`${s.title} · 画面与动作`}><textarea rows={3} value={s.description} maxLength={24000} onChange={e=>patchShot(s.id,'description',e.target.value)} placeholder="主体做什么、在哪里、什么情绪？有明确运镜想法时再补充。"/></Field><label className="journey-field"><span>{s.title} · 景别</span><select aria-label={`${s.title} · 景别`} value={s.data.shotSize||''} onChange={e=>patchShot(s.id,'shotSize',e.target.value)}><option value="">让故事需求决定</option><option value="特写">特写 · 只看细节或情绪</option><option value="中景">中景 · 看人物动作和关系</option><option value="全景">全景 · 看人物在什么地方</option></select></label><details className="v6-camera-options"><summary>更多拍法（可不填）</summary><div className="v6-requirements"><label>机位怎样动<select aria-label={`${s.title} · 运镜`} value={s.data.cameraMove||''} onChange={e=>patchShot(s.id,'cameraMove',e.target.value)}><option value="">暂不指定</option><option>固定机位 · 让动作自己发生</option><option>缓慢推进 · 靠近细节或情绪</option><option>缓慢拉远 · 揭示周围环境</option><option>跟随人物 · 和主角一起移动</option><option>横向摇摄 · 逐步展示信息</option></select></label><label>从什么高度看<select aria-label={`${s.title} · 机位高度`} value={s.data.cameraHeight||''} onChange={e=>patchShot(s.id,'cameraHeight',e.target.value)}><option value="">暂不指定</option><option>平视 · 像面对面观察</option><option>稍低 · 增强人物气势</option><option>稍高 · 交代人物处境</option><option>俯视 · 看清布局与关系</option></select></label></div></details></details><ShotMediaWorkspace project={p} shot={s} onInspect={onInspect}/><div className="journey-shot-reference"><div><span>参考按需添加</span><small>{p.links.filter(l=>l.target===s.id).length} 项已关联 · 人物 / 首帧 / 动作 / 声音</small></div><button onClick={()=>onInspect(s.id)}>编辑与关联参考<ArrowRight size={14}/></button></div></article>)}</div></>}<div className="journey-tip"><span>没有参考素材，也可以继续。</span>已有角色、地点和素材可跨镜头复用；不会要求每种参考都上传一次。</div><button className="journey-skip" onClick={()=>go(5)}>暂不加参考，继续审核<ArrowRight size={14}/></button></div>}

      {stage===5&&<div className="journey-stage-body"><CandidateReview project={p} shots={shots} currentShot={currentShot} onChooseShot={id=>setJourney({reviewShotId:id})} onInspect={onInspect} onReferences={()=>go(4)}/></div>}

      {stage===6&&<div className="journey-stage-body"><div className="journey-runtime"><div><span>这一章的镜头总时长</span><strong>{totalSeconds}<small>秒</small></strong><p>目标 {brief.seconds??60} 秒{totalSeconds?` · ${totalSeconds>(brief.seconds??60)?'超出':'还差'} ${Number(Math.abs(totalSeconds-(brief.seconds??60)).toFixed(3))} 秒`:''}</p></div><button disabled={!shots.length} onClick={()=>{if(chapter)store.setScope(chapter.id);onPreview()}}><Film size={17}/>章节节奏预演</button></div><p className="journey-small">分镜预演按设定时长显示选定画面；下面可独立安排声音并同步试听。</p><div className="journey-timeline">{shots.map((s,index)=>{const siblings=children(p,s.parentId).filter(e=>e.type==='shot'),at=siblings.findIndex(e=>e.id===s.id);return <div key={s.id}><span>{String(index+1).padStart(2,'0')}</span><button className="journey-timeline-name" onClick={()=>onInspect(s.id)}><strong>{s.title}</strong><small>{p.entities.find(e=>e.id===s.parentId)?.title}</small></button><label><span className="visually-hidden">{s.title} · 预演时长（秒）</span><NumberInput value={s.data.seconds||6} onValue={value=>patchShot(s.id,'seconds',value)}/>秒</label><div><button aria-label={`上移镜头 ${s.title}`} disabled={at===0} onClick={()=>store.reorderEntity(s.id,-1)}><ArrowUp size={14}/></button><button aria-label={`下移镜头 ${s.title}`} disabled={at===siblings.length-1} onClick={()=>store.reorderEntity(s.id,1)}><ArrowDown size={14}/></button></div></div>})}</div>{!shots.length&&<Empty title="还没有可以预演的镜头" body="先添加镜头和时长。声音需求可以先记录下来。" label="返回镜头规划" action={()=>go(4)}/>}<div className="journey-sound"><Field label="声音方案"><select value={sound.mode||''} onChange={e=>setSound('mode',e.target.value)}><option value="">选择一种方案</option><option value="silent">静音先行</option><option value="dialogue">对白主导</option><option value="music">音乐主导</option><option value="mixed">混合声音 · 对白／音乐／生成音</option></select></Field>{sound.mode&&sound.mode!=='silent'&&<div className="journey-field-grid two"><Field label="对白与旁白要求"><textarea rows={3} value={sound.dialogue||''} maxLength={12000} onChange={e=>setSound('dialogue',e.target.value)} placeholder="谁说、什么情绪、语言与语速；不需要对白可留空。"/></Field><Field label="音乐与环境声要求"><textarea rows={3} value={sound.music||''} maxLength={12000} onChange={e=>setSound('music',e.target.value)} placeholder="例如：低调悬疑，广播前停音乐，只留雨声。"/></Field></div>}<Field label="字幕需求"><select value={sound.subtitles||'none'} onChange={e=>setSound('subtitles',e.target.value)}><option value="none">暂不需要字幕</option><option value="dialogue">对白字幕</option><option value="bilingual">双语字幕</option><option value="accessibility">字幕包含重要环境声</option></select></Field>{sound.mode==='silent'&&<p className="journey-tip">已选择静音先行。你可以继续交付检查，后续随时补声音。</p>}</div><SoundStudio project={p} chapterId={chapter?.id} shots={shots}/><SubtitleStudio key={p.id+':'+chapter?.id} project={p} chapterId={chapter?.id}/></div>}

      {stage===7&&<div className="journey-stage-body"><DeliveryStudio project={p} chapterId={chapter?.id} onFix={fixIssue} focusIssue={j.lastDeliveryIssue}/></div>}

      <footer className="journey-panel-footer"><button disabled={stage===1} onClick={()=>go(stage-1)}><ArrowLeft size={15}/>上一步</button><span>不必按顺序填写 · 进度随项目保存</span>{stage<7?<button className="primary" onClick={()=>go(stage+1)}>{stages[stage-1].next}<ArrowRight size={15}/></button>:<button onClick={()=>go(1)}>回看故事方向<ArrowRight size={15}/></button>}</footer>
    </section>
  </div>;
}
