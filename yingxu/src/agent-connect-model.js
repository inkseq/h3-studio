import {translate} from './i18n/locale.js';
// This handoff deliberately receives identifiers, never the story document or credentials.
export const agentAccessProfiles={
  edit:{label:'编写与调整',labelKey:'account.status1',detail:'编辑故事、上传素材和查看任务；不提交生成。',detailKey:'account.status2',scopes:['projects:read','projects:write','assets:read','assets:write','jobs:read']},
  create:{label:'编写并生成',labelKey:'account.status3',detail:'包含生成任务权限；仍需满足服务能力、预算和预检。',detailKey:'account.status4',scopes:['projects:read','projects:write','assets:read','assets:write','jobs:read','jobs:write']},
  read:{label:'只读与查看',labelKey:'account.status5',detail:'读取故事、素材和任务；不修改作品。',detailKey:'account.status6',scopes:['projects:read','assets:read','jobs:read']},
};

export function agentOrigin(value){
  try{const url=new URL(value),protocol=url.protocol==='https:'||url.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(url.hostname);return protocol&&!url.username&&!url.password?url.origin:'';}catch{return '';}
}
const identifier=value=>typeof value==='string'&&value.length>0&&value.length<=160&&!/[\x00-\x20\x7f\\]/.test(value)?value:null;

export function agentHandoff({origin,account,workspace={},projectId,entityId,profile='edit',view='/',locale='zh'}={}){
  const t=(key,values)=>translate(locale,key,values);
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
    t('account.status7'),
    t('account.handoff1',{url:base+'/for-agents/SKILL.md'}),
    t('account.handoff2',{url:base+'/for-agents/guide.md'}),
    t('account.handoff3',{url:base+'/for-agents/skill.zip'}),
    t('account.handoff4',{url:base+'/for-agents/guide.json'}),
    ...(workUrl?[t('account.handoff5',{url:workUrl}),selectedEntity?t('account.status8'):view==='/freestyle'?t('account.status9'):t('account.status10')]:[t('account.status11')]),
    t('account.handoffAccess',{access:t(access.labelKey)}),
    ...(access===agentAccessProfiles.read?[t('account.status12')]:[]),
    t('account.status13'),
    t('account.status14'),
    ...(access===agentAccessProfiles.read?[t('account.status15')]:[t('account.status16')]),
    t('account.status17'),
  ].join('\n'):'';
  return {base,status,ready,cloudProject,selectedEntity,workUrl,docsUrl,brief,access:{...access,label:t(access.labelKey),detail:t(access.detailKey)}};
}

export function agentKeySuggestion(projectId,profile='edit'){
  const checked=identifier(projectId);if(!checked)return null;
  const selected=Object.hasOwn(agentAccessProfiles,profile)?profile:'edit';
  return {projectId:checked,projectIds:[checked],allProjects:false,profile:selected,expiresInDays:7,scopes:[...agentAccessProfiles[selected].scopes]};
}
