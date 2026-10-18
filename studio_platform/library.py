"""Authorized library projection and original project-CAS metadata actions."""
import copy
import json
from urllib.parse import urlencode
from sqlalchemy import inspect, select
from .repository import documents, jobs, artifacts, NotFound, Conflict
from .content_lifecycle import reference_ids

PROFILES={'character','location','prop'}
FILES={'image','video','audio'}

def profile_file_ids(data):
    """Existing Story inbox membership, projected without changing authoring."""
    result=set()
    def ids(value):
        if isinstance(value,list):result.update(item for item in value if isinstance(item,str))
    for field in ('libraryFileIds','imageIds','referenceAssetIds'):
        ids(data.get(field))
    for field in ('looks','versions'):
        values=data.get(field,[])
        for variant in values if isinstance(values,list) else []:
            if not isinstance(variant,dict):continue
            images=variant.get('imageIds')
            gallery=variant.get('gallery')
            ids(images if isinstance(images,list) else list(gallery.values()) if isinstance(gallery,dict) else [])
    voices=data.get('voices',[])
    for voice in voices if isinstance(voices,list) else []:
        if isinstance(voice,dict) and isinstance(voice.get('assetId'),str):result.add(voice['assetId'])
    ambient=data.get('ambient')
    if isinstance(ambient,dict) and isinstance(ambient.get('assetId'),str):result.add(ambient['assetId'])
    return result

class _LibraryRead:
    """One owner-scoped read snapshot, never a cache between requests.

    Listing used to call the normal single-target authorizer for every item.
    Those write/import fences remain unchanged. This projection loads the same
    owner/scope/trash/content facts once before applying their read predicates.
    """
    def __init__(self,library,principal):
        from .assets import asset_table
        from .universal_trash import where,target_key
        from .content_lifecycle import token
        self.principal=principal;self.rows={};self.assets={};self.artifacts={}
        with library.repo.engine.connect() as conn:
            rows=list(conn.execute(select(documents).where(documents.c.tenant_id==library.tenant,
                documents.c.owner_id==principal.owner,documents.c.project_id=='__projects',
                documents.c.kind=='project')).mappings())
            trash=list(conn.execute(select(documents.c.document_id,documents.c.payload).where(
                *where(library.tenant,principal.owner,'trash_item'))).mappings())
            inactive={row['document_id'] for row in trash if row['payload']['state']!='active'}
            blocked_assets=set();blocked_artifacts=set()
            for row in trash:
                payload=row['payload'];target=payload['target']
                if payload['state'] in {'trashed','purging','purged'}:
                    if target['kind']=='asset':blocked_assets.add(target['id'])
                    elif target['kind']=='artifact':blocked_artifacts.add(target['id'])
            for row in rows:
                pid=row['document_id']
                if (principal.allows(pid,'projects:read') and not row['payload'].get('content_deleted')
                        and target_key({'kind':'project','project_id':pid,'id':pid}) not in inactive):
                    self.rows[pid]=row
            tombstones=set();deleted_keys=set()
            if self.rows and inspect(conn).has_table('platform_quick_chat_objects'):
                from .quick_chat import objects
                for row in conn.execute(select(objects.c.business_key,objects.c.payload).where(
                        objects.c.tenant==library.tenant,objects.c.owner==principal.owner,
                        objects.c.kind=='content_tombstone')).mappings():
                    tombstones.add(row['business_key'])
                    if row['payload'].get('object_key'):deleted_keys.add(row['payload']['object_key'])
            def available(asset_ids=(),artifact_ids=(),keys=()):
                return (not blocked_assets.intersection(asset_ids) and not blocked_artifacts.intersection(artifact_ids)
                    and not deleted_keys.intersection(keys)
                    and not any(token('asset',ident) in tombstones for ident in asset_ids)
                    and not any(token('artifact',ident) in tombstones for ident in artifact_ids))
            asset_projects=[pid for pid in self.rows if principal.allows(pid,'assets:read')]
            if asset_projects:
                for row in conn.execute(select(asset_table).where(asset_table.c.tenant==library.tenant,
                        asset_table.c.owner==principal.owner,asset_table.c.project_id.in_(asset_projects))).mappings():
                    asset=json.loads(row['record']);selection=asset.get('selection') or {}
                    keys=[asset[role]['key'] for role in ('original','model')
                        if isinstance(asset.get(role),dict) and asset[role].get('key')]
                    if available(tuple(value for value in (asset.get('id'),asset.get('parent_id')) if value),
                            (selection['artifact_id'],) if selection.get('artifact_id') else (),keys):
                        self.assets[(row['project_id'],row['id'])]=(asset,row['status'])
            artifact_projects=[pid for pid in self.rows if principal.allows(pid,'jobs:read')]
            if artifact_projects:
                for row in conn.execute(select(artifacts.c.id,artifacts.c.metadata,jobs.c.project_id).join(
                        jobs,jobs.c.id==artifacts.c.job_id).where(jobs.c.tenant_id==library.tenant,
                        jobs.c.owner_id==principal.owner,jobs.c.project_id.in_(artifact_projects),
                        jobs.c.status=='succeeded')).mappings():
                    if not row['metadata'].get('content_deleted') and available(artifact_ids=(row['id'],)):
                        self.artifacts[(row['project_id'],row['id'])]=row

    def project(self,principal,ident,operation='projects:read'):
        if principal is not self.principal or not principal.allows(ident,operation) or ident not in self.rows:
            raise NotFound('project_not_found')
        return self.rows[ident]

    def entity_available(self,pid,entity):
        if entity['type'] not in FILES:return True
        data=entity['data']
        if data.get('cloudAssetId'):return (pid,data['cloudAssetId']) in self.assets
        if data.get('cloudArtifactId'):return (pid,data['cloudArtifactId']) in self.artifacts
        return True


def contract():
    return {'storage':'project_scoped','aggregate':'authorized read projection only',
        'list':'GET /v1/library','usage':'GET /v1/library/usage',
        'filters':['project_id','type','work','unused','tag','q','limit','offset'],
        'counts_scope':'authorized_filtered_except_type',
        'inbox':'current file items not dismissed or filed into a profile; read-only authoring projection',
        'actions':['library.tag','library.file','library.rename'],'import':'POST /v1/library/imports',
        'import_status':'GET /v1/library/imports/{operation_id}',
        'deletion':'Use /v1/trash preview/actions; no automatic deletion',
        'usage_scope':'authorized_projects; unused is never permission to purge retained inputs'}

def apply_action(project, action, resolved=None):
    from .guided import entity_by_id
    if action['op']=='library.tag':
        targets=action.get('targets');tags=action.get('tags')
        if not isinstance(targets,list) or not targets or not isinstance(tags,list) or any(not isinstance(t,str) or not t.strip() for t in tags) or len(set(tags))!=len(tags):
            raise ValueError('Choose targets and distinct nonempty tags. / 请选择对象和不重复的非空标签。')
        for target in targets:
            if not isinstance(target,dict) or set(target)!={'kind','id'} or target['kind'] not in {'entity','asset','artifact'} or not isinstance(target['id'],str):
                raise ValueError('Invalid library target. / 素材库对象无效。')
            if target['kind']=='entity':
                entity=entity_by_id(project,target['id'])
                if entity['type'] not in PROFILES|FILES:raise ValueError('Not a library item. / 不是素材库对象。')
                entity['data']['tags']=list(tags);entity['version']+=1
            else:
                project.setdefault('journey',{}).setdefault('libraryTags',{})[target['kind']+':'+target['id']]=list(tags)
    elif action['op']=='library.rename':
        target=action['target'];title=action.get('title')
        if not isinstance(title,str) or not title.strip():raise ValueError('A nonempty title is required. / 需要非空名称。')
        if target['kind']=='entity':
            entity=entity_by_id(project,target['id']);entity['title']=title;entity['version']+=1
        else:project.setdefault('journey',{}).setdefault('libraryTitles',{})[target['kind']+':'+target['id']]=title
    elif action['op']=='library.file':
        profile=entity_by_id(project,action.get('profile_id'))
        files=list(action.get('entity_ids') or [])
        from .guided import new_entity
        for item in resolved or []:
            if item.get('entity'):
                existing=next((e for e in project['entities'] if e['id']==item['id']),None)
                if existing and any(existing['data'].get(k)!=item['entity']['data'].get(k) for k in ('cloudAssetId','cloudArtifactId')):raise Conflict('library_file_identity_conflict')
                if not existing:new_entity(project,item['entity'])
            files.append(item['id'])
        if profile['type'] not in PROFILES or not isinstance(files,list) or not files or len(set(files))!=len(files):raise ValueError('Choose a profile and files. / 请选择档案和文件。')
        for ident in files:
            if entity_by_id(project,ident)['type'] not in FILES:raise ValueError('Choose library media files. / 请选择素材库文件。')
        data=profile['data'];variant=action.get('variant_id')
        if variant and any(entity_by_id(project,ident)['type']!='image' for ident in files):raise ValueError('Look/version image slots require images. / 造型与场景版本图片槽需要图片。')
        data['libraryFileIds']=list(dict.fromkeys(data.get('libraryFileIds',[])+files))
        images=[ident for ident in files if entity_by_id(project,ident)['type']=='image']
        if variant:
            field='looks' if profile['type']=='character' else 'versions' if profile['type']=='location' else None
            value=next((v for v in data.get(field,[]) if v['id']==variant),None) if field else None
            if value is None:raise NotFound('profile_variant_not_found')
            value['imageIds']=list(dict.fromkeys(value.get('imageIds',[])+files))
        elif profile['type']=='prop':data['imageIds']=list(dict.fromkeys(data.get('imageIds',[])+images))
        profile['version']+=1

class Library:
    def __init__(self,app):
        self.app=app;self.repo=app.state.repository;self.settings=app.state.settings
        self.tenant=self.settings.tenant_id;self.access=app.state.storyboard_versions.access
    def project(self,principal,ident,write=False):
        record=self.access.project(principal,ident,'projects:write' if write else 'projects:read')
        from .universal_trash import active_target
        with self.repo.engine.connect() as conn:
            if active_target(conn,self.tenant,principal.owner,{'kind':'project','project_id':ident,'id':ident}):raise NotFound('project_not_found')
        if record['payload'].get('content_deleted'):raise NotFound('project_not_found')
        return record
    def authorize_target(self,principal,project_id,target):
        if not isinstance(target,dict) or not {'kind','id'}<=target.keys() or target['kind'] not in {'entity','asset','artifact'} or not isinstance(target['id'],str):raise ValueError('Invalid library target. / 素材库对象无效。')
        project=self.project(principal,project_id)['payload']
        kind=target['kind'];ident=target['id']
        if kind=='entity':
            from .guided import entity_by_id
            row=entity_by_id(project,ident)
            if row['type'] not in PROFILES|FILES:raise NotFound('library_item_not_found')
            if row['type'] in FILES:
                data=row['data']
                if data.get('cloudAssetId'):self.authorize_target(principal,project_id,{'kind':'asset','id':data['cloudAssetId']})
                elif data.get('cloudArtifactId'):self.authorize_target(principal,project_id,{'kind':'artifact','id':data['cloudArtifactId']})
            return copy.deepcopy(row)
        if kind=='asset':
            if not principal.allows(project_id,'assets:read'):raise NotFound('library_item_not_found')
            return self.app.state.assets.get(principal.owner,ident,project_id)
        if kind=='artifact':
            row=self.access.artifact(principal,ident);job=self.access.job(principal,row['job_id'])
            if job['project_id']!=project_id or job['status']!='succeeded':raise NotFound('library_item_not_found')
            return dict(row)
        raise ValueError('Invalid library kind. / 素材库类型无效。')
    def resolve_action(self,principal,project_id,action):
        if action['op']=='library.rename':
            self.authorize_target(principal,project_id,action['target'])
        elif action['op']=='library.tag':
            if not isinstance(action.get('targets'),list):raise ValueError('Invalid targets / 对象无效')
            for target in action['targets']:self.authorize_target(principal,project_id,target)
        elif action['op']=='library.file':
            import uuid
            project=self.project(principal,project_id)['payload'];resolved=[]
            for ident in action.get('entity_ids',[]):self.authorize_target(principal,project_id,{'kind':'entity','id':ident})
            for target in action.get('targets',[]):
                self.authorize_target(principal,project_id,target)
                if target['kind']=='entity':resolved.append({'id':target['id']});continue
                field='cloudAssetId' if target['kind']=='asset' else 'cloudArtifactId'
                existing=next((e for e in project['entities'] if e['data'].get(field)==target['id']),None)
                if existing:resolved.append({'id':existing['id']});continue
                op='asset.attach' if target['kind']=='asset' else 'artifact.adopt'
                value=self.app.state.guided.resolve(principal,project_id,{'op':op,'asset_id' if target['kind']=='asset' else 'artifact_id':target['id']})
                ident='library-'+uuid.uuid5(uuid.NAMESPACE_URL,project_id+':'+target['kind']+':'+target['id']).hex
                value.update(id=ident,parentId=None)
                value['title']=project.get('journey',{}).get('libraryTitles',{}).get(target['kind']+':'+target['id'],value['title'])
                resolved.append({'id':ident,'entity':value})
            return resolved
    def entries(self,principal):
        snapshot=_LibraryRead(self,principal)
        # The existing profile projection is pure after its project read. Reuse
        # it against this request's authorized document versions.
        profiles_reader=copy.copy(self.app.state.storyboard_versions);profiles_reader.access=snapshot
        from .generation_draft import read_draft
        from .storyboard_versions import effective_project,preferences
        defaults=None;caps=None
        results=[];global_usage={}
        for row in snapshot.rows.values():
            pid=row['document_id']
            project=row['payload'];lookup={e['id']:e for e in project['entities']}
            usage={e['id']:[] for e in project['entities']};receipt_usage={}
            projection=profiles_reader.profiles(principal,pid)
            if projection['project_version']!=row['version']:raise Conflict('library_projection_changed')
            profiles=projection['profiles']
            for profile in profiles:usage[profile['id']]=list(profile['shot_ids'])
            render={**project,'entities':list(project['entities'])}
            for index,shot in enumerate(project['entities']):
                if shot['type']!='shot':continue
                if defaults is None:defaults=preferences(self.repo,self.tenant,principal.owner)['defaults']
                if 'storyboard' in shot['data'] and caps is None:
                    caps=self.app.state.generation_admission.capabilities_provider()
                # effective_project only mutates the selected shot and reads
                # storyboard defaults. Copy those, not every entity per shot.
                miniature={'entities':[shot],'journey':{'storyboard':{'defaults':
                    project.get('journey',{}).get('storyboard',{}).get('defaults',{})}}}
                effective,_=effective_project(miniature,shot['id'],defaults)
                render['entities'][index]=effective['entities'][0]
                try:draft,_=read_draft(render,shot['id'],capabilities=caps)
                finally:render['entities'][index]=shot
                a_ids,r_ids=reference_ids(draft.get('inputs',{}))
                for ident in a_ids:receipt_usage.setdefault(('asset',ident),[]).append(shot['id'])
                for ident in r_ids:receipt_usage.setdefault(('artifact',ident),[]).append(shot['id'])
                if 'storyboard' not in shot['data']:
                    for link in project['links']:
                        if link['target']==shot['id'] and link['source'] in usage and lookup[link['source']]['type'] in FILES:usage[link['source']].append(shot['id'])
                for ref in draft.get('storyboard',{}).get('references',[]):
                    if ref.get('entity_id') in usage:usage[ref['entity_id']].append(shot['id'])
                selected=shot['data'].get('selectedAssetId')
                if selected in usage:usage[selected].append(shot['id'])
            for token,shot_ids in receipt_usage.items():
                global_usage.setdefault(token,set()).update((pid,sid) for sid in shot_ids)
            for entity in project['entities']:
                for kind,field in (('asset','cloudAssetId'),('artifact','cloudArtifactId')):
                    if entity['data'].get(field):global_usage.setdefault((kind,entity['data'][field]),set()).update((pid,sid) for sid in usage.get(entity['id'],[]))
            attached=set()
            filed={e['id']:profile_file_ids(e['data']) for e in project['entities'] if e['type'] in PROFILES}
            def append(kind,ident,typ,title,data,extra=None):
                tags=data.get('tags',[]) if kind=='entity' else project.get('journey',{}).get('libraryTags',{}).get(kind+':'+ident,[])
                tags=[t for t in tags if isinstance(t,str)] if isinstance(tags,list) else []
                used=set(usage.get(ident,[])) if kind=='entity' else set(receipt_usage.get((kind,ident),[]))
                for k,v in (('asset',data.get('cloudAssetId')),('artifact',data.get('cloudArtifactId'))):
                    if v:used.update(receipt_usage.get((k,v),[]));attached.add((k,v))
                results.append({'target':{'project_id':pid,'kind':kind,'id':ident},'type':typ,'title':project.get('journey',{}).get('libraryTitles',{}).get(kind+':'+ident,title),'tags':tags,
                    'asset_id':data.get('cloudAssetId') or (ident if kind=='asset' else None),'artifact_id':data.get('cloudArtifactId') or (ident if kind=='artifact' else None),'project_version':row['version'],'source_project_id':pid,'source_project_title':project.get('title',''),
                    'provenance':data.get('importSource'),
                    'filed_in':[pid for pid,ids in filed.items() if ident in ids],
                    'inbox':typ in FILES and data.get('storyboardInbox') is not False and not any(ident in ids for ids in filed.values()),
                    'usage':[{'project_id':pid,'shot_id':sid,'web_url':'/?'+urlencode({'project':pid,'entity':sid})} for sid in sorted(used)],
                    'work_ids':[pid] if used else [],'unused':not used,'source':data.get('source','upload' if kind=='asset' else 'generation' if kind=='artifact' else 'authoring'),**(extra or {})})
            for entity in project['entities']:
                if entity['type'] not in PROFILES|FILES:continue
                if not snapshot.entity_available(pid,entity):continue
                append('entity',entity['id'],entity['type'],entity['title'],entity['data'])
            if principal.allows(pid,'assets:read'):
                for (asset_pid,_),(asset,status) in snapshot.assets.items():
                    if asset_pid!=pid or status=='duplicate':continue
                    if ('asset',asset['id']) not in attached:append('asset',asset['id'],asset['kind'],asset.get('file_name',''),asset)
            if principal.allows(pid,'jobs:read'):
                for (artifact_pid,_),output in snapshot.artifacts.items():
                    if artifact_pid!=pid:continue
                    if ('artifact',output['id']) in attached or output['metadata'].get('kind') not in FILES:continue
                    append('artifact',output['id'],output['metadata']['kind'],output['id'],{})
        for entry in results:
            links={(v['project_id'],v['shot_id']) for v in entry['usage']}
            for kind in ('asset','artifact'):
                if entry.get(kind+'_id'):links.update(global_usage.get((kind,entry[kind+'_id']),set()))
            entry['usage']=[{'project_id':pid,'shot_id':sid,'web_url':'/?'+urlencode({'project':pid,'entity':sid})} for pid,sid in sorted(links)]
            entry['work_ids']=sorted({pid for pid,sid in links});entry['unused']=not links
        return results
    def listing(self,principal,*,project_id=None,type=None,work=None,unused=None,tag=None,q=None,limit=100,offset=0):
        if not 1<=limit<=100 or offset<0:raise ValueError('Invalid pagination / 分页无效')
        values=[e for e in self.entries(principal) if (project_id is None or e['target']['project_id']==project_id)
            and (work is None or work in e['work_ids']) and (unused is None or e['unused']==unused)
            and (tag is None or tag in e['tags']) and (not q or q.casefold() in e['title'].casefold() or any(q.casefold() in t.casefold() for t in e['tags']))]
        counts={kind:sum(e['type']==kind for e in values) for kind in sorted(PROFILES|FILES)}
        counts['file']=sum(e['type'] in FILES for e in values)
        counts['inbox']=sum(e['inbox'] for e in values)
        values=[e for e in values if (type is None or e['type']==type
            or type=='profile' and e['type'] in PROFILES or type=='file' and e['type'] in FILES
            or type=='inbox' and e['inbox'])]
        values.sort(key=lambda e:(e['target']['project_id'],e['target']['kind'],e['target']['id']))
        return {'items':values[offset:offset+limit],'next_offset':offset+limit if len(values)>offset+limit else None,
            'counts':counts,'counts_scope':'authorized_filtered_except_type',
            'usage_scope':'authorized_projects','usage_kind':'current_authoring','historical_retention':'unmeasured_not_deletion_permission','storage_scope':'project','aggregate_read_only':True}

def register_routes(app):
    from fastapi import Request,Header
    from fastapi.responses import JSONResponse
    from .library_imports import LibraryImports,ImportRequest
    service=Library(app);app.state.library=service;imports=LibraryImports(service)
    @app.get('/v1/library')
    def listing(request:Request,project_id:str|None=None,type:str|None=None,work:str|None=None,unused:bool|None=None,tag:str|None=None,q:str|None=None,limit:int=100,offset:int=0):
        return service.listing(request.state.principal,project_id=project_id,type=type,work=work,unused=unused,tag=tag,q=q,limit=limit,offset=offset)
    @app.get('/v1/library/usage')
    def usage(request:Request,project_id:str,kind:str,id:str):
        service.authorize_target(request.state.principal,project_id,{'kind':kind,'id':id})
        values=[e for e in service.entries(request.state.principal) if e['target']=={'project_id':project_id,'kind':kind,'id':id} or e['target']['project_id']==project_id and e.get(kind+'_id')==id]
        if not values:raise NotFound('library_item_not_found')
        links={ (link['project_id'],link['shot_id']):link for value in values for link in value['usage'] }
        return {**values[0],'usage':list(links.values()),'work_ids':sorted({link['project_id'] for link in links.values()}),'unused':not links,
            'usage_scope':'authorized_projects','usage_kind':'current_authoring','historical_retention':'unmeasured_not_deletion_permission','retention_is_separate':True}
    @app.post('/v1/library/imports')
    def importing(request:Request,body:ImportRequest,key:str=Header(...,alias='Idempotency-Key')):
        value=body.model_dump(exclude_unset=True)
        try:return imports.start(request.state.principal,value,key)
        except Conflict as exc:
            if str(exc) in {'library_target_version_conflict','library_source_version_conflict'}:
                from .repository import request_hash
                return JSONResponse({'code':str(exc),'resource':'library_import','request_hash':request_hash(value),'no_mutation_committed':True,
                    'message':'Source or destination changed before import; reload it. / 导入前来源或目标故事已更改，请刷新。'},status_code=409)
            return JSONResponse({'code':str(exc),'resource':'library_import','request_hash':__import__('studio_platform.repository',fromlist=['request_hash']).request_hash(value),
                'message':'Import was not confirmed; retain the original key and inspect its receipt. / 导入尚未确认，请保留原键并核对回执。'},status_code=409)
    @app.get('/v1/library/imports/{operation_id}')
    def observe(operation_id:str,request:Request):return imports.observe(request.state.principal,operation_id)
