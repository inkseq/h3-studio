import React from 'react';
import {failureDetails} from './cloud-model.js';

export default function GenerationFailure({job}){
  const details=failureDetails(job);if(!details)return null;
  return <details className="qc-generation-failure"><summary>失败详情 · {details.stage} · {details.reason}</summary><dl>{details.rows.map(([label,value])=><React.Fragment key={label}><dt>{label}</dt><dd>{value}</dd></React.Fragment>)}</dl><p>原任务和参数保留，不会自动重新生成。可用现有重试入口重新预检并确认，或“以此继续修改”简化素材、时长或清晰度。</p></details>;
}
