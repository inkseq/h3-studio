import test from 'node:test';
import assert from 'node:assert/strict';
import {agentHandoff,agentKeySuggestion,agentOrigin} from './agent-connect-model.js';

const ready={origin:'https://inkseq.com/?api_key=do-not-copy#private',account:'owner',workspace:{mode:'cloud',account:'owner',serverVersion:3,dirty:false},projectId:'story-1',entityId:'shot-2'};

test('quick handoff returns to the same saved shot in freestyle with the same scoped key suggestion',()=>{
  const value=agentHandoff({...ready,view:'/freestyle'});
  assert.equal(value.workUrl,'https://inkseq.com/freestyle?project=story-1&entity=shot-2');
  assert.match(value.brief,/\/freestyle\?project=story-1&entity=shot-2/);
  assert.deepEqual(agentKeySuggestion(value.cloudProject).projectIds,['story-1']);
  assert.equal(agentHandoff({...ready,view:'https://other.example/'}).workUrl,null);
  assert.equal(agentHandoff({...ready,view:'/freestyle?token=private'}).workUrl,null);
  assert.equal(agentHandoff({...ready,view:'/freestyle',workspace:{...ready.workspace,dirty:true}}).workUrl,null);
});

test('handoff links point to the saved cloud scope and never carry page query or private document content',()=>{
  const value=agentHandoff({...ready,projectTitle:'Private title',project:{script:'Private script'},api_key:'synthetic-secret'});
  assert.equal(value.workUrl,'https://inkseq.com/?project=story-1&entity=shot-2');
  for(const excluded of ['api_key=','do-not-copy','#private','Private title','Private script','synthetic-secret'])assert.ok(!value.brief.includes(excluded));
  assert.equal(value.ready,true);assert.match(value.brief,/for-agents\/SKILL\.md/);assert.match(value.brief,/不自动替换已采用/);
  assert.ok(value.brief.includes('完整纯文本指南（无需登录）：https://inkseq.com/for-agents/guide.md'));
  assert.ok(value.brief.includes('离线说明与工具包：https://inkseq.com/for-agents/skill.zip'));
});

test('an unsaved draft, another account, or a local workspace cannot leak a handoff to inaccessible or unpublished contents',()=>{
  const variants=[{...ready,account:null},{...ready,workspace:{mode:'local'}},{...ready,workspace:{...ready.workspace,account:'other'}},{...ready,workspace:{...ready.workspace,dirty:true}},{...ready,workspace:{...ready.workspace,serverVersion:null}}];
  for(const input of variants){const result=agentHandoff(input);assert.equal(result.ready,false);assert.equal(result.workUrl,null);assert.equal(result.selectedEntity,null);assert.ok(!result.brief.includes('story-1'));assert.ok(!result.brief.includes('shot-2'));assert.match(result.brief,/暂未指定/);}
});

test('cloud identifiers are URL encoded and do not let a supplied identifier add another parameter',()=>{
  const value=agentHandoff({...ready,projectId:'story&token=fake',entityId:'shot/2?other=no'}),url=new URL(value.workUrl);
  assert.deepEqual([...url.searchParams.keys()],['project','entity']);assert.equal(url.searchParams.get('project'),'story&token=fake');assert.equal(url.searchParams.get('entity'),'shot/2?other=no');
  assert.equal(agentHandoff({...ready,projectId:'id\nprivate'}).ready,false);
  assert.equal(agentHandoff({...ready,projectId:'folder\\id'}).ready,false);
  assert.equal(agentHandoff({...ready,projectId:'a'.repeat(161)}).ready,false);
});

test('credential-bearing and non-web origins fail closed without an alternative service',()=>{
  for(const origin of ['https://name:secret@example.com/','http://public.example','javascript:alert(1)','file:///C:/secret','bad']){assert.equal(agentOrigin(origin),'');assert.equal(agentHandoff({...ready,origin}).brief,'');}
  assert.equal(agentOrigin('http://127.0.0.1:8843/path?token=example'),'http://127.0.0.1:8843');
});

test('key suggestions stay within one existing story, expire in seven days, and default to no generation',()=>{
  const value=agentKeySuggestion('story-1');assert.deepEqual(value.projectIds,['story-1']);assert.equal(value.allProjects,false);assert.equal(value.expiresInDays,7);
  assert.ok(!value.scopes.includes('projects:create'));assert.ok(!value.scopes.includes('jobs:write'));assert.ok(value.scopes.includes('projects:write'));
  assert.ok(agentKeySuggestion('story-1','create').scopes.includes('jobs:write'));assert.ok(!agentKeySuggestion('story-1','read').scopes.includes('projects:write'));
  assert.equal(agentKeySuggestion(null),null);assert.equal(agentKeySuggestion('story-1','unknown').profile,'edit');
  value.scopes.push('fake');assert.ok(!agentKeySuggestion('story-1').scopes.includes('fake'));
  const read=agentHandoff({...ready,profile:'read'});assert.match(read.brief,/不要保存修改/);assert.ok(!read.brief.includes('保存创作变更'));
});
