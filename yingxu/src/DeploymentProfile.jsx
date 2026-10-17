import React from 'react';
import {duration} from './operator-model.js';

export function deploymentLabel(capabilities,id){return id?(capabilities?.deployment_profiles||[]).find(p=>p.id===id)?.label||id:'现有服务配置';}
export function DeploymentProfileSelect({capabilities,value,onChange,disabled=false,mode}){
  const profiles=capabilities?.deployment_profiles||[],known=profiles.some(p=>p.id===value),profile=profiles.find(p=>p.id===value),support=profile?.generation_support?.[mode];
  if(!profiles.length&&!value)return null;
  return <label className="qc-field qc-deployment-field"><span>视频部署配方</span><select aria-label="视频部署配方" value={value||''} disabled={disabled} onChange={e=>onChange(e.target.value||null)}><option value="">现有服务配置</option>{value&&!known&&<option value={value}>原配方 · 当前未公开</option>}{profiles.map(p=><option key={p.id} value={p.id}>{p.label}</option>)}</select>{support?.enabled===false&&<small>该配方当前尚不可执行；可以保存草稿，开始生成前会核对容量和参数。</small>}<small>明确选择后按该配方执行，不自动切换模型。未选择时沿用现有服务。</small></label>;
}
export function HistoricalTiming({hint}){
  if(!hint||hint.scope!=='historical_exact_case'||!hint.cases?.length)return null;
  return <details className="qc-timing-hint"><summary>相同规格的历史实测</summary><ul>{hint.cases.flatMap(row=>(row.measurements||[]).map((m,index)=><li key={row.id+'-'+index}>{row.width} × {row.height} · {row.frames} 帧 · {row.steps} 步：<b>{duration(m.total_seconds)}</b><small>{m.measured_at?.slice(0,10)} · {m.sample_count} 个样本 · 缓存条件未受控</small></li>))}</ul><small>历史任务耗时，包含任务内加载；不包含租机、下载和排队。不是本次预计完成时间或质量保证。</small></details>;
}
