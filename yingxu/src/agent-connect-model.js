// This handoff deliberately receives identifiers, never the story document or credentials.
export const agentAccessProfiles={
  edit:{label:'编写与调整',detail:'编辑故事、上传素材和查看任务；不提交生成。',scopes:['projects:read','projects:write','assets:read','assets:write','jobs:read']},
  create:{label:'编写并生成',detail:'包含生成任务权限；仍需满足服务能力、预算和预检。',scopes:['projects:read','projects:write','assets:read','assets:write','jobs:read','jobs:write']},
  read:{label:'只读与查看',detail:'读取故事、素材和任务；不修改作品。',scopes:['projects:read','assets:read','jobs:read']},
};

export function agentOrigin(value){
  try{const url=new URL(value),protocol=url.protocol==='https:'||url.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(url.hostname);return protocol&&!url.username&&!url.password?url.origin:'';}catch{return '';}
}
const identifier=value=>typeof value==='string'&&value.length>0&&value.length<=160&&!/[\x00-\x20\x7f\\]/.test(value)?value:null;

export function agentHandoff({origin,account,workspace={},projectId,entityId,profile='edit',view='/'}={}){
  const base=agentOrigin(origin),access=agentAccessProfiles[profile]||agentAccessProfiles.edit;
  const accountMatches=!!account&&workspace.account===account;
  const cloudProject=workspace.mode==='cloud'&&accountMatches&&identifier(projectId)?projectId:null;
  const ready=!!cloudProject&&!workspace.dirty&&Number.isInteger(workspace.serverVersion)&&workspace.serverVersion>0;
  const selectedEntity=ready?identifier(entityId):null;
  let status=!account?'signed-out':workspace.mode!=='cloud'?'local':!accountMatches?'account-mismatch':!cloudProject?'missing-project':workspace.dirty?'unsaved':!ready?'unverified':'ready';
  const params=new URLSearchParams();
  if(ready)params.set('project',cloudProject);
  if(selectedEntity)params.set('entity',selectedEntity);
  const workUrl=base&&ready&&['/','/freestyle'].includes(view)?`${base}${view}?${params.toString()}`:null;
  const docsUrl=base?`${base}/for-agents`:null;
  const brief=base?[
    '请使用映序的公开 Skill 与 API，帮助我完成创作。',
    `先阅读：${base}/for-agents/SKILL.md`,
    `完整纯文本指南（无需登录）：${base}/for-agents/guide.md`,
    `离线说明与工具包：${base}/for-agents/skill.zip`,
    `接口与流程：${base}/for-agents/guide.json`,
    ...(workUrl?[`工作位置：${workUrl}`,selectedEntity?'本次先处理链接定位的内容；更改其他部分前与我确认。':view==='/freestyle'?'本次只处理链接中的快速创作，准备这个视频的提示词与参考素材。':'本次只处理链接中的故事。']:['暂未指定可同步的云故事。先介绍可用流程，等我登录并选择、保存云故事后再操作。']),
    `建议授权范围：${access.label}。实际权限以已配置的 API Key 为准。`,
    ...(access===agentAccessProfiles.read?['本次只允许读取与分析。不要保存修改、上传文件或提交生成任务。']:[]),
    '凭据只从我在本地安全配置的 SIXNINE_API_KEY 或已匹配的凭据加载器读取；不要向我索要聊天中的密钥，不要把密钥写进 URL、作品或日志。',
    '先读取当前云版本、实时 capabilities 与 guided schema；只执行我明确要求且权限允许的操作。缺少凭据或生成未启用时说明具体缺口，不提交测试生成。',
    ...(access===agentAccessProfiles.read?['返回相关内容的网页链接和建议，由我决定是否扩大授权。']:['保存创作变更并返回对应的网页链接。提交生成前预检并确认本次任务与费用；超时先核对原任务，不重复提交。']),
    '生成结果先作为候选回到原镜头；未经我要求，不自动替换已采用的版本。完成后告诉我改了什么、哪些步骤未完成，以及从哪里查看或继续调整。',
  ].join('\n'):'';
  return {base,status,ready,cloudProject,selectedEntity,workUrl,docsUrl,brief,access};
}

export function agentKeySuggestion(projectId,profile='edit'){
  const checked=identifier(projectId);if(!checked)return null;
  const selected=Object.hasOwn(agentAccessProfiles,profile)?profile:'edit';
  return {projectId:checked,projectIds:[checked],allProjects:false,profile:selected,expiresInDays:7,scopes:[...agentAccessProfiles[selected].scopes]};
}
