"""Additive take metadata and explicit recipe transfer on original authorities."""
from __future__ import annotations
import base64
import copy
import hashlib
import json
import math
import tempfile
import uuid
from sqlalchemy import select, insert, update, and_, or_, func
from .repository import documents, jobs, Scope, NotFound, Conflict, request_hash
from .content_lifecycle import owner_reference_gate, assert_project_writable, assert_reference_payload

INTEGER_MAX = 2147483647
PREFERENCE_DEFAULTS = {"copies_max": 4, "takes_fold_count": 8}

class TransferTargetChanged(Exception):
    """The project transaction committed no shot; upload truth is checked separately."""


class QuickChatTakesMixin:
    def preferences(self, principal, *, conn=None):
        if conn is None:
            with self.repo.engine.connect() as connection:
                return self.preferences(principal, conn=connection)
        row = conn.execute(select(documents).where(self.repo._scope(documents, Scope(self.tenant, principal.owner, "__preferences")),
            documents.c.kind == "quick_chat_preferences", documents.c.document_id == "takes")).mappings().first()
        return {**PREFERENCE_DEFAULTS, **(row["payload"] if row else {}), "version": row["version"] if row else 0,
            "bounds": {name: {"minimum": 1, "maximum": INTEGER_MAX, "maximum_kind": "integer_representation"} for name in PREFERENCE_DEFAULTS}}

    def set_preferences(self, principal, body, key):
        from .quick_chat import fields, QuickChatError
        fields(body, {"expected_version", *PREFERENCE_DEFAULTS}, {"expected_version"})
        if not principal.allows("__preferences", "projects:write"):
            raise NotFound("preferences_not_found")
        if type(body["expected_version"]) is not int or body["expected_version"] < 0 or any(
            type(value) is not int or not 1 <= value <= INTEGER_MAX for name, value in body.items() if name != "expected_version"):
            raise QuickChatError("invalid_take_preferences", "Take preferences must be positive integers. / 候选设置必须为正整数。", 422)
        scope = Scope(self.tenant, principal.owner, "__preferences")
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.tenant, principal.owner)
            # Reuse original owner documents for preferences and replay receipts.
            from .quick_chat import key_check
            key_check(key)
            receipt_id = request_hash({"key": key})
            receipt_where = (self.repo._scope(documents, scope), documents.c.kind == "quick_chat_preferences_receipt", documents.c.document_id == receipt_id)
            old = conn.execute(select(documents).where(*receipt_where)).mappings().first()
            if old:
                if old["payload"]["fingerprint"] != request_hash(body):
                    raise QuickChatError("idempotency_conflict", "Operation key has different input. / 同一操作Key不能使用不同内容。")
                return old["payload"]["response"]
            current = self.preferences(principal, conn=conn)
            if current["version"] != body["expected_version"]:
                raise QuickChatError("version_conflict", "Settings changed; reload them. / 设置已更改，请重新读取。")
            payload = {name: body.get(name, current[name]) for name in PREFERENCE_DEFAULTS}
            where = (self.repo._scope(documents, scope), documents.c.kind == "quick_chat_preferences", documents.c.document_id == "takes")
            version = current["version"] + 1
            values = dict(payload=payload, version=version, updated_at=self.repo.clock())
            if current["version"]:
                conn.execute(update(documents).where(*where).values(**values))
            else:
                conn.execute(insert(documents).values(tenant_id=self.tenant, owner_id=principal.owner, project_id=scope.project_id,
                    kind="quick_chat_preferences", document_id="takes", **values))
            response = {**payload, "version": version, "bounds": current["bounds"]}
            conn.execute(insert(documents).values(tenant_id=self.tenant, owner_id=principal.owner, project_id=scope.project_id,
                kind="quick_chat_preferences_receipt", document_id=receipt_id, version=1,
                payload={"fingerprint": request_hash(body), "response": response}, updated_at=self.repo.clock()))
            return response

    def _copies_allowed(self, principal, copies, conn):
        from .quick_chat import QuickChatError
        if copies > self.preferences(principal, conn=conn)["copies_max"]:
            raise QuickChatError("copies_limit_exceeded", "Copies exceed your editable setting. / 份数超过您在设置中选择的上限。", 422)

    def _take(self, conn, principal, session_id, item_id):
        from .quick_chat import objects
        if not isinstance(item_id, str):
            raise NotFound("take_not_found")
        for row in conn.execute(select(objects).where(objects.c.tenant == self.tenant, objects.c.owner == principal.owner,
                objects.c.session_id == session_id, objects.c.kind == "revision")).mappings():
            item = next((i for i in row["payload"]["items"] if i["id"] == item_id), None)
            if item:
                return dict(row), item
        raise NotFound("take_not_found")

    @staticmethod
    def _take_recipe(revision, item):
        return {**{name: copy.deepcopy(revision[name]) for name in ("title", "prompt", "recipe_id", "inputs")},
            "deployment_profile_id": revision.get("deployment_profile_id"),
            "controls": {**copy.deepcopy(revision["controls"]), "seed": item["seed"]}, "copies": 1}

    def group_takes(self, principal, session_id, card_id, *, limit=20, before_cursor=None):
        return self._takes_page(principal, session_id, card_id, limit=limit, before_cursor=before_cursor)

    def session_takes(self, principal, session_id, *, limit=40, before_cursor=None):
        """Bounded read projection of original takes, never a new take catalog."""
        page = self._takes_page(principal, session_id, None, limit=limit, before_cursor=before_cursor)
        values = []
        for take in page['takes']:
            value = {key: copy.deepcopy(take[key]) for key in (
                'id', 'card_id', 'revision_id', 'submission_id', 'job_id', 'status',
                'index', 'group_number', 'created_at', 'annotation', 'trash_state')}
            value['title'] = take['annotation'].get('title') or take['recipe'].get('title', '')
            value['artifacts'] = [{key: copy.deepcopy(artifact[key]) for key in (
                'id', 'kind', 'thumbnail_url', 'preview_url', 'rendition_status') if key in artifact}
                for artifact in (take.get('job') or {}).get('artifacts', [])]
            values.append(value)
        return {'session_id': session_id, 'takes': values, 'total_count': page['total_count'],
            'before_cursor': page['before_cursor'], 'projection': 'original_takes_v1',
            'includes_trashed': True}

    def _takes_page(self, principal, session_id, card_id, *, limit, before_cursor):
        from .quick_chat import objects, QuickChatError
        self._access(principal, session_id, "projects:read", "jobs:read")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise QuickChatError("invalid_page", "Invalid page size. / 分页大小无效。", 422)
        cursor = None
        if before_cursor:
            try:
                if not isinstance(before_cursor,str) or len(before_cursor)>512: raise ValueError()
                cursor = json.loads(base64.urlsafe_b64decode(before_cursor.encode()))
                if len(before_cursor)>512 or not isinstance(cursor,list) or len(cursor)!=4 or cursor[0]!=(card_id or 'session:'+session_id) or type(cursor[1]) not in (int,float) or not math.isfinite(cursor[1]) or not isinstance(cursor[2],str) or type(cursor[3]) is not int:
                    raise ValueError()
            except Exception:
                raise QuickChatError("invalid_cursor", "Invalid group cursor. / 候选组游标无效。", 422) from None
        with self.repo.engine.connect() as conn:
            if card_id is not None:
                self._get(conn, principal, card_id, session_id, "card")
            query = select(objects).where(objects.c.tenant==self.tenant, objects.c.owner==principal.owner,
                objects.c.session_id==session_id, objects.c.kind=="revision")
            if card_id is not None:
                query = query.where(objects.c.parent_id==card_id)
            # Original immutable revision versions define creation order, independent of
            # clocks and pagination; appending a revision cannot renumber old takes.
            metadata=conn.execute(query.with_only_columns(objects.c.id, objects.c.parent_id,
                objects.c.payload["version"].as_integer().label("revision_version"),
                func.json_array_length(objects.c.payload["items"]).label("item_count"))).mappings()
            group_offsets={}; group_counts={}; total_count=0
            for original in sorted(metadata,key=lambda r:(r['parent_id'],r["revision_version"])):
                group_offsets[original["id"]]=group_counts.get(original['parent_id'],0)
                group_counts[original['parent_id']]=group_offsets[original['id']]+original['item_count']
                total_count+=original["item_count"]
            if cursor:
                query=query.where(or_(objects.c.created_at<cursor[1], and_(objects.c.created_at==cursor[1],objects.c.id<=cursor[2])))
            revisions=list(conn.execute(query.order_by(objects.c.created_at.desc(),objects.c.id.desc()).limit(limit+1)).mappings())
            entries=[]
            for r in revisions:
                for item in sorted(r["payload"]["items"],key=lambda i:i["index"],reverse=True):
                    identity=(r["created_at"],r["id"],item["index"])
                    if cursor and identity>=tuple(cursor[1:]):continue
                    entries.append((r,item))
            page=entries[:limit]; values=[]; submissions={}
            from .agent_management import creator
            session=self._get(conn,principal,session_id,session_id,"session")
            from .universal_trash import active_target
            project=self.repo.get_document(Scope(self.tenant,principal.owner,"__projects"),"project",session["payload"]["project_id"])
            for r,item in page:
                trash=active_target(conn,self.tenant,principal.owner,{"kind":"take","project_id":session["payload"]["project_id"],"parent_id":session_id,"id":item["id"]})
                submission=conn.execute(select(objects.c.id).where(objects.c.tenant==self.tenant,objects.c.owner==principal.owner,
                    objects.c.kind=="submission",objects.c.business_key==r["id"])).scalar_one_or_none()
                if submission and submission not in submissions:
                    submissions[submission]=self.get_submission(principal,session_id,submission)
                live=next((i for i in submissions.get(submission,{}).get("items",[]) if i["id"]==item["id"]),{})
                annotation=conn.execute(select(objects).where(objects.c.tenant==self.tenant,objects.c.owner==principal.owner,
                    objects.c.session_id==session_id,objects.c.kind=="take_annotation",objects.c.business_key==item["id"])).mappings().first()
                values.append({"status":"draft","job":None,"job_id":None,"retryable":False,"resume_admission_available":False,
                    **copy.deepcopy(live),"id":item["id"],"card_id":r['parent_id'],"index":item["index"],"seed":item["seed"],"revision_id":r["id"],
                    "created_by":creator(conn,self.tenant,principal.owner,"take",item["id"]),"group_number":group_offsets[r["id"]]+item["index"]+1,"submission_id":submission,"created_at":r["created_at"],"recipe":self._take_recipe(r["payload"],item),
                    "trash_state":trash["payload"]["state"] if trash else "active","trash_version":trash["version"] if trash else project["version"],
                    "project_id":session["payload"]["project_id"],
                    "annotation":{"version":annotation["version"] if annotation else 0,"starred":annotation["payload"].get("starred",False) if annotation else False,"title":annotation["payload"].get("title","") if annotation else ""}})
            after=None
            if len(entries)>limit:
                r,item=page[-1];after=base64.urlsafe_b64encode(json.dumps([card_id or 'session:'+session_id,r["created_at"],r["id"],item["index"]]).encode()).decode()
            return {"card_id":card_id,"takes":values,"total_count":total_count,"before_cursor":after}

    def star_take(self, principal, session_id, item_id, body, key):
        from .quick_chat import objects, fields, QuickChatError
        fields(body,{"expected_version","starred","title"},{"expected_version"})
        if type(body["expected_version"]) is not int or body["expected_version"]<0 or not ({"starred","title"}&body.keys()) or ("starred" in body and type(body["starred"]) is not bool) or ("title" in body and not isinstance(body["title"],str)):
            raise QuickChatError("invalid_annotation","Invalid annotation. / 收藏参数无效。",422)
        if len(json.dumps(body,ensure_ascii=False).encode())>self.settings.max_project_bytes:
            raise QuickChatError("invalid_annotation","Annotation exceeds configured project byte capacity. / 标注超过项目字节容量。",422)
        with self.repo.transaction() as conn:
            self._access(principal,session_id,"projects:write",conn=conn,lock=True)
            self._take(conn,principal,session_id,item_id)
            namespace="take-star:"+item_id
            prior=self._replay(conn,principal,namespace,key,body)
            if prior:return self._get(conn,principal,prior,session_id,"take_annotation_receipt")["payload"]
            row=conn.execute(select(objects).where(objects.c.tenant==self.tenant,objects.c.owner==principal.owner,
                objects.c.session_id==session_id,objects.c.kind=="take_annotation",objects.c.business_key==item_id)).mappings().first()
            if body["expected_version"]!=(row["version"] if row else 0):
                raise QuickChatError("version_conflict","Annotation changed; reload it. / 收藏已更改，请重新读取。")
            value={"starred":False,"title":"",**(row["payload"] if row else {}),**{k:body[k] for k in ("starred","title") if k in body}}
            if row:row=self._put(conn,dict(row),value,bump=True)
            else:row=self._new(conn,principal,session_id,"take_annotation",value,business=item_id)
            response={"take_id":item_id,"version":row["version"],**value}
            receipt=self._new(conn,principal,session_id,"take_annotation_receipt",response)
            self._remember(conn,principal,namespace,key,body,receipt["id"])
            return response

    def again(self, principal, session_id, card_id, body, key):
        from .quick_chat import fields, QuickChatError
        fields(body,{"expected_card_version","source_revision_id","item_id","copies"},{"expected_card_version","source_revision_id","item_id","copies"})
        with self.repo.transaction() as conn:
            session=self._access(principal,session_id,"projects:write","assets:read",conn=conn,lock=True)
            prior=self._replay(conn,principal,"again:"+card_id,key,body)
            if prior:revision=self._get(conn,principal,prior,session_id,"revision")
            else:
                card=self._get(conn,principal,card_id,session_id,"card",lock=True)
                if type(body['expected_card_version']) is not int or body['expected_card_version']!=card['version']:
                    raise QuickChatError('version_conflict','Card changed; reload it. / 卡片已更改，请重新读取。')
                from .universal_trash import assert_take_active
                assert_take_active(conn,self.tenant,principal.owner,session_id,body['item_id'])
                source,item=self._take(conn,principal,session_id,body['item_id'])
                if source['id']!=body['source_revision_id'] or source['parent_id']!=card_id:raise NotFound('take_not_found')
                authored=self._take_recipe(source['payload'],item);authored['copies']=body['copies'];authored['controls'].pop('seed',None)
                snapshot,sources=self._card_snapshot(principal,session,authored)
                if snapshot['provenance']!=source['payload']['provenance']:
                    raise QuickChatError('source_changed','Original source receipt changed. / 原素材收据已更改。')
                revision=self._create_revision(conn,principal,session,card,snapshot,sources,source['id'])
                self._remember(conn,principal,'again:'+card_id,key,body,revision['id'])
        return {'card':self.get_card(principal,session_id,card_id),'revision':self.get_revision(principal,session_id,revision['id'])}

    def queue_position(self, principal, job_id):
        from .queue import TaskQueue
        with self.repo.engine.connect() as conn:
            job=conn.execute(select(jobs).where(jobs.c.id==job_id,jobs.c.tenant_id==self.tenant,jobs.c.owner_id==principal.owner)).mappings().first()
            if job is None or not principal.allows(job['project_id'],'jobs:read'):raise NotFound('job_not_found')
            backend=job['execution_plan'].get('dispatch_backend','legacy')
            ordered,_=TaskQueue(self.repo).ordered_candidates(conn,job['pool'],purpose='generate',dispatch_backend=backend)
            rank=next((i+1 for i,v in enumerate(ordered) if v['id']==job_id),None)
            return {'job_id':job_id,'position':rank,'advisory':True,'eta_seconds':None,'scope':'original_pool_fairness',
                'observed_at':self.repo.clock(),'not_a_start_guarantee':True}

    def _story_target(self, conn, principal, project_id, *, lock=False):
        if not isinstance(project_id,str) or not all(principal.allows(project_id,s) for s in ('projects:read','projects:write','assets:read','assets:write')):
            raise NotFound('project_not_found')
        query=select(documents).where(self.repo._scope(documents,Scope(self.tenant,principal.owner,'__projects')),
            documents.c.kind=='project',documents.c.document_id==project_id)
        row=self.repo._locked(conn,query) if lock else conn.execute(query).mappings().first()
        if row is None or row['payload'].get('integration_kind')=='quick_chat':raise NotFound('project_not_found')
        assert_project_writable(conn,self.tenant,principal.owner,project_id)
        return dict(row)

    def transfer_take(self, principal, session_id, item_id, body, key):
        from .quick_chat import fields, QuickChatError
        from .generation_draft import input_entries
        fields(body,{'target_project_id','expected_project_version','parent_id','title'}, {'target_project_id','expected_project_version','parent_id'})
        if type(body['expected_project_version']) is not int or body['expected_project_version']<1 or not isinstance(body['parent_id'],str) or 'title' in body and (not isinstance(body['title'],str) or not body['title'].strip() or len(body['title'])>160):
            raise QuickChatError('invalid_transfer','Invalid story transfer. / 转入故事参数无效。',422)
        with self.repo.transaction() as conn:
            session=self._access(principal,session_id,'projects:write','assets:read',conn=conn,lock=True)
            target=self._story_target(conn,principal,body['target_project_id'],lock=True)
            prior=self._replay(conn,principal,'story-transfer:'+item_id,key,body)
            if prior:record=self._get(conn,principal,prior,session_id,'import')
            else:
                if not any(e['id']==body['parent_id'] and e['type']=='scene' for e in target['payload']['entities']):raise NotFound('scene_not_found')
                from .universal_trash import assert_take_active
                assert_take_active(conn,self.tenant,principal.owner,session_id,item_id)
                revision,item=self._take(conn,principal,session_id,item_id)
                recipe=self._take_recipe(revision['payload'],item)
                sources={ident:self._asset(principal,session,ident) for _,_,ident in input_entries(recipe['inputs'])}
                for source in revision['payload']['provenance']:
                    if sources[source['asset_id']]['original']['sha256']!=source['sha256']:raise Conflict('source_changed')
                record=self._new(conn,principal,session_id,'import',{'purpose':'take_to_story','status':'pending',
                    'source_revision_id':revision['id'],'item_id':item_id,'recipe':recipe,'request':copy.deepcopy(body),
                    'sources':{ident:{'sha256':value['original']['sha256'],'size_bytes':value['original']['size_bytes']} for ident,value in sources.items()},
                    'asset_map':{},'shot_id':'shot-'+uuid.uuid4().hex,'error_code':None})
                if target['version']!=body['expected_project_version']:
                    record=self._put(conn,record,{**record['payload'],'status':'blocked','no_shot_committed':True,'error_code':'target_version_conflict'})
                self._remember(conn,principal,'story-transfer:'+item_id,key,body,record['id'])
        if record['payload']['status'] in {'completed','blocked'}:return self.get_transfer(principal,session_id,record['id'])
        from .asset_operation import asset_operation_lock
        lock_id=uuid.uuid5(uuid.NAMESPACE_URL,record['id']).hex
        with asset_operation_lock(self.assets.operation_dir,lock_id) as acquired:
            if not acquired:return self.get_transfer(principal,session_id,record['id'])
            try:self._finish_transfer(principal,session_id,record['id'])
            except Exception as error:
                # Never discard uploaded originals or silently replace an uncertain receipt.
                with self.repo.transaction() as conn:
                    current=self._get(conn,principal,record['id'],session_id,'import',lock=True)
                    if current['payload']['status'] not in {'completed','blocked'}:
                        closed=isinstance(error,TransferTargetChanged) and self._transfer_uploads_settled(conn,principal,current)
                        self._put(conn,current,{**current['payload'],'status':'blocked' if closed else 'unknown',
                            'no_shot_committed':closed,'error_code':'target_version_conflict' if closed else
                            error.code if isinstance(error,QuickChatError) else 'story_transfer_requires_reconciliation'})
        return self.get_transfer(principal,session_id,record['id'])

    def _transfer_uploads_settled(self, conn, principal, record):
        from .storage_asset_journal import receipts
        p=record['payload']
        for asset_id in p['sources']:
            client_id='take-'+uuid.uuid5(uuid.NAMESPACE_URL,record['id']+':'+asset_id).hex
            raw=conn.execute(select(receipts.c.record).where(receipts.c.tenant==self.tenant,
                receipts.c.owner==principal.owner,receipts.c.project_id==p['request']['target_project_id'],
                receipts.c.client_key==client_id)).scalar_one_or_none()
            # Original uploader journals before any storage write. Absent means
            # never started; anything other than ready must retain reconciliation.
            if raw is not None and json.loads(raw).get('asset',{}).get('status')!='ready':return False
        return True

    def _finish_transfer(self, principal, session_id, record_id):
        from .generation_draft import input_entries, configure
        from .guided import new_entity, now_iso
        from .project_validation import validate_project
        from .project_activity import append_activity
        with self.repo.engine.connect() as conn:
            session=self._access(principal,session_id,'projects:write','assets:read',conn=conn)
            row=self._get(conn,principal,record_id,session_id,'import');payload=copy.deepcopy(row['payload'])
            if payload['status'] in {'completed','blocked'}:return
            target=self._story_target(conn,principal,payload['request']['target_project_id'])
            if target['version']!=payload['request']['expected_project_version']:raise TransferTargetChanged()
        project_id=payload['request']['target_project_id']
        for original_id,pin in payload['sources'].items():
            source=self._asset(principal,session,original_id)
            if any(source['original'].get(k)!=v for k,v in pin.items()):raise Conflict('source_changed')
            client_id='take-'+uuid.uuid5(uuid.NAMESPACE_URL,record_id+':'+original_id).hex
            from .storage_asset_journal import receipts
            with self.repo.engine.connect() as conn:
                existing_id=conn.execute(select(receipts.c.id).where(receipts.c.tenant==self.tenant,receipts.c.owner==principal.owner,
                    receipts.c.project_id==project_id,receipts.c.client_key==client_id)).scalar_one_or_none()
            existing=self.assets.get(principal.owner,existing_id,project_id) if existing_id else None
            if existing:
                copied=self.assets.public(self.assets.get(principal.owner,existing['asset_id'],project_id)) if existing['status']=='ready' else self.assets.resume(principal.owner,existing['asset_id'])
            else:
                size=pin['size_bytes']
                if type(size) is not int or not 0<size<=self.assets.max_bytes:raise Conflict('source_size_invalid')
                with self.storage.open(source['original']['key']) as stream, tempfile.TemporaryFile() as checked:
                    digest=hashlib.sha256();count=0
                    while chunk:=stream.read(min(1048576,size-count+1)):
                        count+=len(chunk)
                        if count>size:raise Conflict('source_integrity_failed')
                        digest.update(chunk);checked.write(chunk)
                    if count!=size or digest.hexdigest()!=pin['sha256']:raise Conflict('source_integrity_failed')
                    checked.seek(0)
                    copied=self.assets.upload(principal.owner,project_id,checked,source['file_name'],client_asset_id=client_id)
            if copied['status']!='ready':raise Conflict('transferred_asset_not_ready')
            received=self.assets.get(principal.owner,copied['asset_id'],project_id)
            if any(received['original'].get(k)!=v for k,v in pin.items()):raise Conflict('transferred_asset_integrity_failed')
            with self.repo.transaction() as conn:
                current=self._get(conn,principal,record_id,session_id,'import',lock=True)
                mapping={**current['payload']['asset_map'],original_id:copied['asset_id']}
                self._put(conn,current,{**current['payload'],'asset_map':mapping})
        with self.repo.transaction() as conn:
            self._access(principal,session_id,'projects:write','assets:read',conn=conn,lock=True)
            current=self._get(conn,principal,record_id,session_id,'import',lock=True)
            if current['payload']['status'] in {'completed','blocked'}:return
            p=current['payload'];target=self._story_target(conn,principal,project_id,lock=True)
            if target['version']!=p['request']['expected_project_version']:raise TransferTargetChanged()
            recipe=copy.deepcopy(p['recipe']);sources={}
            for slot,entry,ident in input_entries(recipe['inputs']):
                copied_id=p['asset_map'][ident];entry['media_id' if slot=='guides' else 'asset_id']=copied_id
                sources[copied_id]=self._source(self.assets.get(principal.owner,copied_id,project_id))
            project=copy.deepcopy(target['payload'])
            if not any(e['id']==p['request']['parent_id'] and e['type']=='scene' for e in project['entities']):raise NotFound('scene_not_found')
            new_entity(project,{'id':p['shot_id'],'type':'shot','parentId':p['request']['parent_id'],
                'title':p['request'].get('title',recipe['title']),'data':{'seconds':recipe['controls'].get('duration',5),
                'prompt':recipe['prompt'],'quickChatTakeOrigin':{'session_id':session_id,'revision_id':p['source_revision_id'],'item_id':p['item_id'],'transfer_id':record_id},'seedLocked':True}})
            configure(project,{'shot_id':p['shot_id'],**{k:v for k,v in recipe.items() if k in {'recipe_id','deployment_profile_id','controls','inputs','prompt'}}},sources)
            assert_reference_payload(conn,self.tenant,principal.owner,project)
            project['updatedAt']=now_iso();validate_project(project,self.settings.max_project_bytes)
            version=target['version']+1
            conn.execute(update(documents).where(self.repo._scope(documents,Scope(self.tenant,principal.owner,'__projects')),
                documents.c.kind=='project',documents.c.document_id==project_id,documents.c.version==target['version']).values(
                payload=project,version=version,updated_at=self.repo.clock()))
            append_activity(conn,tenant_id=self.tenant,principal=principal,project_id=project_id,version=version,
                occurred_at=self.repo.clock(),before=target['payload'],after=project,
                actions=[{'op':'entity.create','entity':{'id':p['shot_id']}}],event_type='project.edited')
            self._put(conn,current,{**p,'status':'completed','project_version':version,'error_code':None})

    def get_transfer(self, principal, session_id, record_id):
        self._access(principal,session_id,'projects:read','assets:read')
        with self.repo.engine.connect() as conn:
            row=self._get(conn,principal,record_id,session_id,'import');p=row['payload']
            if p.get('purpose')!='take_to_story':raise NotFound('transfer_not_found')
            self._story_target(conn,principal,p['request']['target_project_id'])
        from urllib.parse import urlencode
        return {'id':record_id,'status':p['status'],'source_session_id':session_id,'item_id':p['item_id'],
            'expected_project_version':p['request']['expected_project_version'],'parent_id':p['request']['parent_id'],
            'project_id':p['request']['target_project_id'],
            'project_version':p.get('project_version'),'shot_id':p['shot_id'],'seed':p['recipe']['controls']['seed'],
            'seed_locked':True,'no_shot_committed':p.get('no_shot_committed',False),'error_code':p.get('error_code'),'web_url':'/?'+urlencode({'project':p['request']['target_project_id'],'entity':p['shot_id']}) if p['status']=='completed' else None}
