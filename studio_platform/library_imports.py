"""Explicit cross-story copies on the original asset upload/quota journal."""
import copy
import hashlib
import json
import tempfile
import uuid
from typing import Annotated,Literal
from pydantic import BaseModel,ConfigDict,StrictInt,StrictStr,Field
from sqlalchemy import select,insert,update
from .repository import documents,request_hash,Conflict,NotFound
from .content_lifecycle import owner_reference_gate,assert_project_writable

class Source(BaseModel):
    model_config=ConfigDict(extra='forbid')
    project_id:StrictStr
    kind:Literal['entity','asset','artifact']
    id:StrictStr
    expected_version:Annotated[StrictInt,Field(ge=1)]=None
class ImportRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source:Source
    target_project_id:StrictStr
    expected_project_version:Annotated[StrictInt,Field(ge=1)]
    title:StrictStr=None

def file_refs(entity):
    data=entity['data'];ids=set(data.get('imageIds',[])+data.get('referenceAssetIds',[])+data.get('libraryFileIds',[]))
    for variant in data.get('looks',[])+data.get('versions',[]):
        ids.update(variant.get('imageIds',[]));ids.update(v for v in variant.get('gallery',{}).values() if v)
    ids.update(v['assetId'] for v in data.get('voices',[]) if v.get('assetId'))
    if (data.get('ambient') or {}).get('assetId'):ids.add(data['ambient']['assetId'])
    return ids

def remap_profile(entity,mapping):
    value=copy.deepcopy(entity);data=value['data']
    for name in ('imageIds','referenceAssetIds','libraryFileIds'):
        if name in data:data[name]=[mapping[x] for x in data[name]]
    for variant in data.get('looks',[])+data.get('versions',[]):
        if 'imageIds' in variant:variant['imageIds']=[mapping[x] for x in variant['imageIds']]
        if 'gallery' in variant:variant['gallery']={k:mapping[v] if v else v for k,v in variant['gallery'].items()}
    for voice in data.get('voices',[]):
        if voice.get('assetId'):voice['assetId']=mapping[voice['assetId']]
    if (data.get('ambient') or {}).get('assetId'):data['ambient']['assetId']=mapping[data['ambient']['assetId']]
    return value

class LibraryImports:
    def __init__(self,library):
        self.library=library;self.app=library.app;self.repo=library.repo;self.tenant=library.tenant
    def where(self,principal,ident):return (documents.c.tenant_id==self.tenant,documents.c.owner_id==principal.owner,
        documents.c.project_id=='__library',documents.c.kind=='library_import',documents.c.document_id==ident)
    def row(self,conn,principal,ident):
        value=conn.execute(select(documents).where(*self.where(principal,ident))).mappings().first()
        if not value:raise NotFound('library_import_not_found')
        return value
    def save(self,conn,principal,ident,payload):
        conn.execute(update(documents).where(*self.where(principal,ident)).values(payload=payload,version=documents.c.version+1,updated_at=self.repo.clock()))
    def access(self,principal,body):
        source=body['source'];self.library.project(principal,source['project_id'])
        target=self.library.project(principal,body['target_project_id'],True)
        from .generation_admission import reject_managed
        reject_managed(target['payload'])
        for scope in ('assets:read','assets:write'):
            if not principal.allows(body['target_project_id'],scope):raise NotFound('project_not_found')
        return target
    def pin(self,principal,pid,kind,ident):
        value=self.library.authorize_target(principal,pid,{'kind':kind,'id':ident})
        if kind=='asset':
            if value['status']!='ready':raise Conflict('source_not_ready')
            original=value['original'];name=value['file_name']
        else:
            original=value['metadata'];name=original.get('filename') or ('output.'+{'image':'png','video':'mp4','audio':'flac'}[original['kind']])
            original={**original,'key':original['object_key']}
        if type(original.get('size_bytes')) is not int or not 0<original['size_bytes']<=self.app.state.assets.max_bytes or not isinstance(original.get('sha256'),str) or len(original['sha256'])!=64:
            raise Conflict('source_integrity_unknown')
        return {'kind':kind,'id':ident,'key':original['key'],'size_bytes':original['size_bytes'],'sha256':original['sha256'],'filename':name}
    def plan(self,principal,body,ident):
        from .library import PROFILES,FILES
        source=body['source'];pid=source['project_id'];files={};profile=None
        original_project=self.library.project(principal,pid)
        if source.get('expected_version',original_project['version'])!=original_project['version']:raise Conflict('library_source_version_conflict')
        value=self.library.authorize_target(principal,pid,source)
        if source['kind']=='entity':
            if value['type'] in PROFILES:
                profile=value
                for eid in file_refs(value):
                    file=self.library.authorize_target(principal,pid,{'kind':'entity','id':eid})
                    if file['type'] not in FILES:raise Conflict('profile_reference_not_file')
                    files[eid]=file
            else:files[value['id']]=value
        else:files['source']={'id':'source','type':'image','title':body.get('title') or source['id'],'data':{'cloudAssetId' if source['kind']=='asset' else 'cloudArtifactId':source['id']}}
        pins={}
        for eid,file in files.items():
            data=file['data'];kind='asset' if data.get('cloudAssetId') else 'artifact' if data.get('cloudArtifactId') else None
            if kind is None:raise Conflict('source_bytes_unavailable')
            sid=data['cloudAssetId' if kind=='asset' else 'cloudArtifactId'];token=kind+':'+sid
            pins[token]=self.pin(principal,pid,kind,sid);file['source_token']=token
        entity_ids={eid:'import-'+uuid.uuid5(uuid.NAMESPACE_URL,ident+':entity:'+eid).hex for eid in files}
        if profile:entity_ids[profile['id']]='import-'+uuid.uuid5(uuid.NAMESPACE_URL,ident+':profile').hex
        payload={'request':copy.deepcopy(body),'request_hash':request_hash(body),'state':'pending','sources':pins,'files':files,
            'source_project_version':original_project['version'],'source_snapshot_sha256':request_hash({'value':value,'files':files}),
            'profile':profile,'entity_ids':entity_ids,'asset_map':{},'created_at':self.repo.clock()}
        if len(json.dumps(payload,ensure_ascii=False).encode())>self.library.settings.max_project_bytes:raise ValueError('Import exceeds configured project byte capacity. / 导入超过项目字节容量。')
        return payload
    def public(self,ident,payload):
        body=payload['request']
        return {'operation_id':ident,'state':payload['state'],'request_hash':payload['request_hash'],
            'source':body['source'],'source_project_version':payload['source_project_version'],'source_snapshot_sha256':payload['source_snapshot_sha256'],'target_project_id':body['target_project_id'],'expected_project_version':body['expected_project_version'],
            'project_version':payload.get('project_version'),'entity_ids':payload['entity_ids'] if payload['state']=='completed' else {},
            'asset_ids':list(payload['asset_map'].values()),'no_authoring_committed':payload['state']=='blocked',
            'reason':payload.get('reason'),'message':'Import retains its original receipt and copies. / 导入保留原回执和已复制文件。'}
    def observe(self,principal,ident):
        with self.repo.engine.connect() as conn:payload=self.row(conn,principal,ident)['payload']
        self.access(principal,payload['request'])
        return self.public(ident,payload)
    def start(self,principal,body,key):
        from .quick_chat import key_check
        key_check(key);target=self.access(principal,body)
        ident=request_hash({'tenant':self.tenant,'owner':principal.owner,'actor':principal.actor_id,'key':key})
        with self.repo.transaction() as conn:
            owner_reference_gate(conn,self.tenant,principal.owner)
            old=conn.execute(select(documents).where(*self.where(principal,ident))).mappings().first()
            if old:
                if old['payload']['request_hash']!=request_hash(body):raise Conflict('library_import_idempotency_conflict')
                payload=old['payload']
            else:
                # Recheck target under the same owner gate before reserving any upload.
                target=self.access(principal,body)
                if target['version']!=body['expected_project_version']:raise Conflict('library_target_version_conflict')
                payload=self.plan(principal,body,ident)
                conn.execute(insert(documents).values(tenant_id=self.tenant,owner_id=principal.owner,project_id='__library',
                    kind='library_import',document_id=ident,version=1,payload=payload,updated_at=self.repo.clock()))
        if payload['state'] in {'completed','blocked'}:return self.public(ident,payload)
        from .asset_operation import asset_operation_lock
        with asset_operation_lock(self.app.state.assets.operation_dir,uuid.uuid5(uuid.NAMESPACE_URL,'library:'+ident).hex) as acquired:
            if acquired:
                try:self.finish(principal,ident)
                except OSError:
                    pass  # Original durable pending intent is the recovery authority; no retry.
        return self.observe(principal,ident)
    def finish(self,principal,ident):
        from .storage_asset_journal import receipts
        with self.repo.engine.connect() as conn:payload=copy.deepcopy(self.row(conn,principal,ident)['payload'])
        if payload['state'] in {'completed','blocked'}:return
        body=payload['request'];self.access(principal,body);pid=body['target_project_id'];assets=self.app.state.assets
        for token,pin in payload['sources'].items():
            self.access(principal,body)
            current=self.pin(principal,body['source']['project_id'],pin['kind'],pin['id'])
            if current!=pin:raise Conflict('source_changed')
            upload_id=uuid.uuid5(uuid.NAMESPACE_URL,'library-upload:'+ident+':'+token).hex
            client_key='library-'+upload_id
            with self.repo.engine.connect() as conn:
                existing=conn.execute(select(receipts.c.id).where(receipts.c.tenant==self.tenant,receipts.c.owner==principal.owner,
                    receipts.c.project_id==pid,receipts.c.id==upload_id)).scalar_one_or_none()
            if existing:
                value=assets.get(principal.owner,existing,pid)
                if value['status']!='ready':assets.resume(principal.owner,existing)
                copied=assets.get(principal.owner,existing,pid)
            else:
                size=pin['size_bytes']
                with self.app.state.storage.open(pin['key']) as stream,tempfile.TemporaryFile() as checked:
                    digest=hashlib.sha256();count=0
                    while chunk:=stream.read(min(1048576,size-count+1)):
                        count+=len(chunk)
                        if count>size:raise Conflict('source_integrity_failed')
                        digest.update(chunk);checked.write(chunk)
                    if count!=size or digest.hexdigest()!=pin['sha256']:raise Conflict('source_integrity_failed')
                    checked.seek(0)
                    # Pin original upload identity before receiving bytes/client-key binding.
                    with assets._operation(upload_id):
                        value=assets._upload(upload_id,principal.owner,pid,checked,pin['filename'],client_asset_id=client_key)
                copied=assets.get(principal.owner,value['asset_id'],pid)
            if copied['status']!='ready' or any(copied['original'][k]!=pin[k] for k in ('size_bytes','sha256')):raise Conflict('imported_asset_not_verified')
            payload['asset_map'][token]=copied['id']
            with self.repo.transaction() as conn:
                owner_reference_gate(conn,self.tenant,principal.owner);self.save(conn,principal,ident,payload)
        actions=[]
        provenance={**body['source'],'project_version':payload['source_project_version'],'snapshot_sha256':payload['source_snapshot_sha256'],'operation_id':ident}
        for eid,file in payload['files'].items():
            actions.append({'op':'asset.attach','asset_id':payload['asset_map'][file['source_token']],
                'entity_id':payload['entity_ids'][eid],'title':body.get('title',file['title']) if not payload['profile'] else file['title']})
            actions.append({'op':'entity.update','entity_id':payload['entity_ids'][eid],'patch':{'data':{'source':'import','importSource':provenance}}})
            if file['data'].get('tags'):
                actions.append({'op':'entity.update','entity_id':payload['entity_ids'][eid],'patch':{'data':{'tags':file['data']['tags']}}})
        if payload['profile']:
            entity=remap_profile(payload['profile'],payload['entity_ids']);entity.update(id=payload['entity_ids'][entity['id']],parentId=None)
            entity={k:v for k,v in entity.items() if k in {'id','type','parentId','title','description','data','status','order'}};entity['title']=body.get('title',entity['title'])
            entity['data'].update(source='import',importSource=provenance)
            actions.append({'op':'entity.create','entity':entity})
        # Original guided CAS/receipt makes a lost completion acknowledgement recoverable.
        request={'expected_version':body['expected_project_version'],'actions':actions}
        try:
            result=self.app.state.guided.mutate(principal,pid,request,'library-import-'+ident)
        except Conflict as exc:
            if str(exc)!='document_version_conflict':raise
            # All upload receipts have already been positively ready/verified.
            payload.update(state='blocked',reason='library_target_version_conflict')
        else:payload.update(state='completed',project_version=result['version'])
        with self.repo.transaction() as conn:
            owner_reference_gate(conn,self.tenant,principal.owner);self.save(conn,principal,ident,payload)
