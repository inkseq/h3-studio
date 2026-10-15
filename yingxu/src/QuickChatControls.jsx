import React,{useEffect,useRef,useState} from 'react';
import {X,Settings2} from 'lucide-react';
import {DeploymentProfileSelect} from './DeploymentProfile.jsx';
import GenerationAvailability from './GenerationAvailability.jsx';
import {modeAvailability,requireModeAvailable} from './generation-availability.js';
import {CONTROL_GROUPS,clone,defaultsFor,effectiveControlSchema,recipeFor,settingsProblems,bindingParticipates,automaticInputMode} from './quick-chat-model.js';
import Control from './GenerationControl.jsx';
export function ChatDialog({title,onClose,children,wide=false,className='',eyebrow=''}){
  const ref=useRef(),opener=useRef(globalThis.document?.activeElement);
  useEffect(()=>{const dialog=ref.current;dialog.showModal();dialog.querySelector('button,input,textarea,select')?.focus();return()=>{dialog.close();if(opener.current?.isConnected)opener.current.focus();};},[]);
  return <dialog className={'qc-dialog '+(wide?'qc-dialog-wide ':'')+className} ref={ref} aria-label={title} onCancel={e=>{e.preventDefault();onClose();}}><div className="qc-dialog-head"><div>{eyebrow&&<span className="qc-eyebrow">{eyebrow}</span>}<h2>{title}</h2></div><button type="button" aria-label="关闭窗口" onClick={onClose}><X size={18}/></button></div>{children}</dialog>;
}
function ProfileInputBoundary({support}){
  const roles={first_frame:'首帧',last_frame:'尾帧',image:'图片参考',video:'视频参考',audio:'音频参考'};
  if(!support?.input_notes&&!support?.joint_cases?.length)return null;
  return <section className="qc-profile-boundary"><strong>此配方的历史测试示例</strong>{support.input_notes&&<p className="qc-muted">{support.input_notes}</p>}{support.joint_cases?.length>0&&<ul className="qc-muted">{support.joint_cases.map((item,index)=><li key={index}>{item.width} × {item.height} · {item.steps} 步 · {item.frames} 帧：{item.input_roles?.length?item.input_roles.map(role=>roles[role]||role).join(' ＋ '):'纯文字'}</li>)}</ul>}<p className="qc-muted">这些示例不限制可选组合。可用输入与参数以模型支持范围、当前执行策略和后台预检为准。</p></section>;
}
export default function QuickChatControls({capabilities,settings,bindings=[],cardPrompt,scope='next',modeIntent,busy=false,onSave,onClose,onEditMaterials,availability,operator=false,onRefreshAvailability,availabilityChecking=false}){
  const [draft,setDraft]=useState(()=>clone(settings)),[prompt,setPrompt]=useState(cardPrompt||''),[error,setError]=useState(''),[pendingMode,setPendingMode]=useState(null);
  const recipe=recipeFor(capabilities,draft),schema=effectiveControlSchema(recipe);
  const [mode,setMode]=useState(modeIntent||recipe?.mode||'auto');
  const set=(field,value)=>setDraft(current=>{const controls={...current.controls};if(value===undefined)delete controls[field];else controls[field]=value;return {...current,controls};});
  const fields=names=>names.filter(name=>schema[name]).map(name=><Control key={name} field={name} schema={schema[name]} value={draft.controls?.[name]} onChange={value=>set(name,value)}/>);
  function effectiveMode(intent){return intent==='auto'?automaticInputMode(bindings,recipe,recipeFor(capabilities,{...draft,recipe_id:capabilities?.recipes?.find(item=>item.mode==='ref')?.id})):intent;}
  function target(intent){const base=capabilities?.recipes?.find(item=>item.mode===effectiveMode(intent));return base?recipeFor(capabilities,{...draft,recipe_id:base.id}):null;}
  function applyMode(intent){
    const next=target(intent);if(!next){setError('服务尚未开放这种生成方式。');return;}
    if(next.mode!==recipe?.mode){try{requireModeAvailable(availability,capabilities,draft,next.mode);}catch(error){setError(error.message);setPendingMode(null);return;}}
    const defaults=defaultsFor(capabilities,next.id,draft.deployment_profile_id),nextSchema=effectiveControlSchema(next),controls={...defaults.controls};
    for(const [field,value]of Object.entries(draft.controls||{}))if(nextSchema[field]&&(!nextSchema[field].enum||nextSchema[field].enum.includes(value)))controls[field]=value;
    setDraft({...draft,recipe_id:next.id,controls});setMode(intent);setPendingMode(null);setError('');
  }
  function changeMode(intent){
    const next=target(intent);if(!next){setError('服务尚未开放这种生成方式。');return;}
    if(next.mode!==recipe?.mode){try{requireModeAvailable(availability,capabilities,draft,next.mode);}catch(error){setError(error.message);return;}}
    const excluded=bindings.filter(b=>bindingParticipates(b,recipe)&&!bindingParticipates(b,next));
    if(excluded.length){setPendingMode({intent,count:excluded.length});return;}applyMode(intent);
  }
  async function save(event){
    event.preventDefault();if(pendingMode){setError('请先确认或取消生成方式切换。');return;}
    if(recipe?.mode!==recipeFor(capabilities,settings)?.mode){try{requireModeAvailable(availability,capabilities,draft);}catch(error){setError(error.message);return;}}
    const problems=settingsProblems(draft,recipe);if(scope==='card'&&!prompt.trim())problems.unshift('请填写完整生成提示词。');
    if(problems.length){setError(problems.join(' '));return;}setError('');
    try{await onSave(draft,prompt,{modeIntent:mode});onClose();}catch(err){setError(err.message||'尚未保存，请核对原操作。');}
  }
  const allGroups=new Set(CONTROL_GROUPS.flatMap(([,names])=>names));
  const duration=schema.duration,resolution=schema.resolution,ratio=schema.aspect_ratio,audio=schema.generate_audio;
  const labels={'16:9':'16:9 横屏','9:16':'9:16 竖屏','1:1':'1:1 方形'};
  const groupLabels={'参考与时间':'参考素材与时间控制','随机性与采样':'随机性与采样 · 偶尔调整','运行与导出':'运行与导出 · 通常无需修改'};
  return <ChatDialog title={scope==='next'?'下一轮 · 高级控制':'把这个镜头调到位'} onClose={onClose} className="qc-controls-dialog" eyebrow="GENERATION SETTINGS">
    <form onSubmit={save}>
      <p className="qc-muted">按常用程度排列 · 可用范围以当前服务与预检为准</p>
      {!recipe?<p role="alert">尚未读取到服务能力，不能猜测支持的参数。</p>:<>
        <DeploymentProfileSelect capabilities={capabilities} value={draft.deployment_profile_id} mode={recipe?.mode} disabled={busy} onChange={value=>setDraft({...draft,deployment_profile_id:value})}/><label className="qc-field"><span>生成方式</span><select aria-label="生成方式" value={pendingMode?.intent||mode} onChange={e=>changeMode(e.target.value)}>
          <option value="auto" disabled={effectiveMode('auto')!==recipe.mode&&!modeAvailability(availability,capabilities,draft,effectiveMode('auto')).available}>自动匹配素材</option>{['fl','ref'].filter(value=>capabilities.recipes.some(item=>item.mode===value)).map(value=>{const status=modeAvailability(availability,capabilities,draft,value);return <option key={value} value={value} disabled={!status.available}>{value==='fl'?'首尾帧':'全能参考'} · {status.label}</option>;})}
        </select></label>
        <GenerationAvailability snapshot={availability} capabilities={capabilities} settings={draft} operator={operator} onRefresh={onRefreshAvailability} checking={availabilityChecking}/>
        <p className="qc-muted">按所选模型核对输入用途。切换后不兼容的素材会保留，但不会加入任务。{mode==='auto'?`当前匹配：${recipe.mode==='fl'?'文生 / 首尾帧':'全能参考'}。`:''}</p>
        {pendingMode&&<section className="qc-warning" role="alert"><p>切换后 {pendingMode.count} 份当前素材不再参与，原素材和历史卡片保留。</p><button type="button" onClick={()=>applyMode(pendingMode.intent)}>切换并保留素材</button><button type="button" onClick={()=>setPendingMode(null)}>保持原方式</button></section>}
        {scope==='card'&&<label className="qc-field"><span>完整视频提示词</span><textarea aria-label="完整视频提示词" rows={4} maxLength={12000} value={prompt} onChange={e=>setPrompt(e.target.value)} required/></label>}
        <div className="qc-form-grid">
          {duration&&(duration.type==='integer'&&Number.isInteger(duration.minimum)&&Number.isInteger(duration.maximum)&&duration.maximum-duration.minimum<60?<label className="qc-field"><span>时长</span><select aria-label="时长" disabled={duration.available===false} value={draft.controls?.duration??duration.default} onChange={e=>set('duration',Number(e.target.value))}>{Array.from({length:Math.max(0,duration.maximum-duration.minimum+1)},(_,i)=>duration.minimum+i).map(value=><option key={value} value={value}>{value} 秒</option>)}</select></label>:fields(['duration']))}
          {resolution&&<label className="qc-field"><span>清晰度</span><select aria-label="清晰度" value={draft.controls?.resolution??resolution.default} disabled={resolution.available===false} onChange={e=>set('resolution',e.target.value)}>{[...(resolution.enum||[])].sort((a,b)=>parseInt(b)-parseInt(a)).map(value=><option key={value} value={value}>{value==='custom'?'自定义宽高':value}</option>)}</select></label>}
          {ratio&&<label className="qc-field"><span>画幅</span><select aria-label="画幅" value={draft.controls?.aspect_ratio??ratio.default} disabled={ratio.available===false} onChange={e=>set('aspect_ratio',e.target.value)}>{['16:9','9:16','1:1','21:9','4:3','3:4',...(ratio.enum||[])].filter((value,index,all)=>all.indexOf(value)===index&&ratio.enum?.includes(value)).map(value=><option key={value} value={value}>{labels[value]||value}</option>)}</select></label>}
          <label className="qc-field"><span>生成份数</span><select aria-label="生成份数" value={draft.copies||1} onChange={e=>setDraft({...draft,copies:Number(e.target.value)})}>{[1,2,3,4].map(n=><option key={n} value={n}>{n}</option>)}</select></label>
        </div>
        {audio&&<label className={'qc-check '+(audio.available===false?'qc-disabled':'')}><input type="checkbox" checked={draft.controls?.generate_audio??audio.default??false} disabled={audio.available===false} onChange={e=>set('generate_audio',e.target.checked)}/>导出生成声音</label>}
        {draft.controls?.resolution==='custom'&&<div className="qc-form-grid">{fields(['width','height'])}<p className="qc-muted">{recipe.custom_canvas_constraints?.description}</p></div>}
        {CONTROL_GROUPS.slice(1).map(([name,names])=><details key={name}><summary>{groupLabels[name]||name}</summary><div className="qc-form-grid">{fields(names)}</div>
          {name==='参考与时间'&&<><ProfileInputBoundary support={recipe.execution_support}/><p className="qc-muted">在每份素材上选择用途、原片选段、视频原声与时间锚点。当前配方单独约束锚点；删除引用不会删除原件。</p>{scope==='card'&&onEditMaterials&&<button type="button" disabled={busy||!!pendingMode} onClick={()=>onEditMaterials(clone(draft),prompt)}>调整这张卡的素材</button>}</>}
          {name==='随机性与采样'&&<p className="qc-muted">denoise不是参考强度。当前Base没有独立CFG或负面提示词入口。</p>}
          {name==='运行与导出'&&<p className="qc-muted">默认沿用云端预设；显式选择保留，范围不匹配时预检解释。音频分块不可用。</p>}
        </details>)}
        {Object.keys(schema).some(name=>!allGroups.has(name)&&!['guides','video_audio','width','height'].includes(name))&&<details><summary>其他服务公开控制</summary><div className="qc-form-grid">{fields(Object.keys(schema).filter(name=>!allGroups.has(name)&&!['guides','video_audio','width','height'].includes(name)))}</div></details>}
      </>}
      {error&&<p className="qc-error" role="alert">{error}</p>}
      <div className="qc-dialog-footer"><span>{scope==='next'?'用于下一张任务卡，不修改历史卡片':'只修改这张卡；已有任务不受影响'}</span><button className="qc-primary" disabled={busy||!recipe}>保存设置</button></div>
    </form>
  </ChatDialog>;
}
