"""Library projection and journaled imports; isolated local databases/storage only."""
import copy
import io
import unittest
from unittest.mock import patch
from sqlalchemy import select,func
from PIL import Image
import test_platform_generation_draft as fixture
from studio_platform.repository import documents,request_hash,jobs,budget_reservations
from studio_platform.storage_asset_journal import receipts

class LibraryTests(unittest.TestCase):
    setUp=fixture.GenerationDraftTests.setUp
    login=fixture.GenerationDraftTests.login
    setup_project=fixture.GenerationDraftTests.setup_project
    key=fixture.GenerationDraftTests.key
    edit=fixture.GenerationDraftTests.edit
    def current(self,pid='story-one'):return self.client.get('/v1/projects/'+pid).json()
    def prepare(self):
        self.setup_project()
        stream=io.BytesIO();Image.new('RGB',(512,512),'blue').save(stream,'PNG')
        response=self.client.post('/v1/assets',data={'client_project_id':'story-one','client_asset_id':'source-file'},files={'file':('source.png',stream.getvalue(),'image/png')})
        self.assertEqual(response.status_code,201,response.text);self.asset=response.json()['asset_id']
        self.edit([{'op':'asset.attach','asset_id':self.asset,'entity_id':'file-one','title':'Portrait'},
            {'op':'entity.create','entity':{'id':'hero','type':'character','title':'Hero','data':{'looks':[{'id':'look','name':'Day','version':1,'gallery':{},'imageIds':['file-one']}],'defaultLookId':'look'}}}],version=self.current()['version']).raise_for_status()
        response=self.client.post('/v1/projects',json={'id':'target','title':'Target'},headers={'Idempotency-Key':'target'})
        self.assertEqual(response.status_code,201,response.text)
    def action(self,actions,key='metadata'):
        return self.client.post('/v1/projects/story-one/actions',json={'expected_version':self.current()['version'],'actions':actions},headers={'Idempotency-Key':key})
    def import_body(self):return {'source':{'project_id':'story-one','kind':'entity','id':'hero'},'target_project_id':'target','expected_project_version':self.current('target')['version']}
    def importing(self,body,key='import'):
        return self.client.post('/v1/library/imports',json=body,headers={'Idempotency-Key':key})
    def test_display_renditions_follow_original_authorized_asset_and_import_receipt(self):
        self.prepare()
        before=self.app.state.assets.usage('superdan')
        values=self.client.get('/v1/library?type=image').json()['items']
        self.assertEqual(len(values),1)
        entry=values[0];asset=self.app.state.assets.get('superdan',self.asset)
        self.assertTrue(entry['rendition_ready'])
        self.assertEqual(entry['thumbnail_url'],f'/v1/assets/{self.asset}/content?rendition=thumb')
        self.assertEqual(entry['renditions']['thumb']['sha256'],asset['thumb']['sha256'])
        self.assertEqual(self.app.state.assets.usage('superdan'),before)  # GET never generates/writes.
        imported=self.importing(self.import_body()).json()
        self.assertEqual(imported['state'],'completed')
        target=self.current('target')['project'];file=next(e for e in target['entities'] if e['type']=='image')
        copied=self.app.state.assets.public(self.app.state.assets.get('superdan',file['data']['cloudAssetId']))
        self.assertTrue(copied['rendition_ready'])
        self.assertNotEqual(copied['thumbnail_url'],entry['thumbnail_url'])
        self.login('supervan');self.assertEqual(self.client.get('/v1/library?type=image').json()['items'],[])
    def test_tags_usage_scope_filters_and_atomic_file_into_profile(self):
        self.prepare()
        self.action([{'op':'library.tag','targets':[{'kind':'entity','id':'hero'},{'kind':'entity','id':'file-one'}],'tags':['blue','hero']},
            {'op':'library.file','profile_id':'hero','entity_ids':['file-one'],'variant_id':'look'}]).raise_for_status()
        response=self.client.get('/v1/library?tag=blue&type=profile&unused=true')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([x['target']['id'] for x in response.json()['items']],['hero'])
        self.assertEqual(response.json()['usage_scope'],'authorized_projects')
        self.action([{'op':'entity.update','entity_id':'shot-one','patch':{'data':{'cast':['hero'],'storyboard':{'version':1,'mode':'fl','quality':'draft'}}}}],'use').raise_for_status()
        result=self.client.get('/v1/library?work=story-one&type=profile').json()
        self.assertEqual(result['items'][0]['usage'][0]['shot_id'],'shot-one')
        self.login('supervan');self.assertEqual(self.client.get('/v1/library').json()['items'],[])
    def test_profile_import_remaps_files_exact_bytes_and_replays_no_quota_duplicate(self):
        self.prepare();body=self.import_body()
        before=copy.deepcopy(self.current()['project'])
        response=self.importing(body);self.assertEqual(response.status_code,200,response.text);receipt=response.json()
        self.assertEqual(receipt['state'],'completed');self.assertEqual(receipt['request_hash'],request_hash(body))
        target=self.current('target')['project'];lookup={x['id']:x for x in target['entities']}
        profile=lookup[receipt['entity_ids']['hero']];file=lookup[receipt['entity_ids']['file-one']]
        self.assertEqual(profile['data']['looks'][0]['imageIds'],[file['id']]);self.assertNotEqual(file['data']['cloudAssetId'],self.asset)
        source=self.app.state.assets.get('superdan',self.asset);copied=self.app.state.assets.get('superdan',file['data']['cloudAssetId'])
        self.assertEqual(source['original']['sha256'],copied['original']['sha256'])
        usage=self.app.state.assets.usage('superdan')
        self.assertEqual(self.importing(body).json(),receipt);self.assertEqual(self.app.state.assets.usage('superdan'),usage)
        self.assertEqual(self.current()['project'],before)
        with self.app.state.repository.engine.connect() as c:
            self.assertEqual(c.execute(select(func.count()).select_from(jobs)).scalar_one(),0)
            self.assertEqual(c.execute(select(func.count()).select_from(budget_reservations)).scalar_one(),0)
    def test_lost_upload_ack_reuses_original_receipt_and_stale_target_is_closed(self):
        self.prepare();body=self.import_body();assets=self.app.state.assets;original=assets._upload
        def lost(*args,**kwargs):original(*args,**kwargs);raise TimeoutError('lost')
        with patch.object(assets,'_upload',side_effect=lost):
            self.assertEqual(self.importing(body).json()['state'],'pending')
        with self.app.state.repository.engine.connect() as c:
            row=c.execute(select(documents).where(documents.c.kind=='library_import')).mappings().one();ident=row['document_id']
            count=c.execute(select(func.count()).select_from(receipts)).scalar_one()
        observed=self.client.get('/v1/library/imports/'+ident).json();self.assertEqual(observed['state'],'pending')
        # Explicit unrelated target edit; original import must not overwrite it.
        self.client.post('/v1/projects/target/actions',json={'expected_version':self.current('target')['version'],
            'actions':[{'op':'project.update','patch':{'title':'Changed'}}]},headers={'Idempotency-Key':'edit-target'}).raise_for_status()
        response=self.importing(body);self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'blocked');self.assertTrue(response.json()['no_authoring_committed'])
        with self.app.state.repository.engine.connect() as c:self.assertEqual(c.execute(select(func.count()).select_from(receipts)).scalar_one(),count)
        self.assertEqual(self.current('target')['project']['title'],'Changed')
        self.assertEqual(self.current('target')['project']['entities'],[])
    def test_denied_target_has_no_import_or_upload_intent(self):
        self.prepare();body=self.import_body();self.login('supervan')
        response=self.importing(body);self.assertEqual(response.status_code,404,response.text)
        with self.app.state.repository.engine.connect() as c:self.assertEqual(c.execute(select(func.count()).select_from(documents).where(documents.c.kind=='library_import')).scalar_one(),0)

    def test_lost_authoring_ack_replays_original_guided_receipt(self):
        self.prepare();body=self.import_body();guided=self.app.state.guided;original=guided.mutate
        def lost(*args,**kwargs):original(*args,**kwargs);raise TimeoutError('lost authoring ack')
        with patch.object(guided,'mutate',side_effect=lost):self.assertEqual(self.importing(body).json()['state'],'pending')
        committed=copy.deepcopy(self.current('target'))
        final=self.importing(body).json();self.assertEqual(final['state'],'completed');self.assertEqual(self.current('target'),committed)
    def test_initial_stale_cas_closed_before_copy_and_changed_key_body_rejected(self):
        self.prepare();body=self.import_body();body['expected_project_version']+=1
        response=self.importing(body);self.assertEqual(response.status_code,409,response.text)
        self.assertTrue(response.json()['no_mutation_committed']);self.assertEqual(response.json()['request_hash'],request_hash(body))
        with self.app.state.repository.engine.connect() as c:self.assertEqual(c.execute(select(func.count()).select_from(documents).where(documents.c.kind=='library_import')).scalar_one(),0)
        body=self.import_body();self.importing(body).raise_for_status()
        changed={**body,'title':'Different'};self.assertEqual(self.importing(changed).status_code,409)
    def test_raw_receipt_metadata_and_profile_file_trash_restore_keep_original_names(self):
        self.prepare();body={'expected_version':self.current()['version'],'actions':[
            {'op':'library.rename','target':{'kind':'asset','id':self.asset},'title':'Custom name'},
            {'op':'library.file','profile_id':'hero','entity_ids':['file-one']}]}
        response=self.client.post('/v1/projects/story-one/actions',json=body,headers={'Idempotency-Key':'rename-file'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.app.state.assets.get('superdan',self.asset)['file_name'],'source.png')
        self.assertEqual(self.client.post('/v1/projects/story-one/actions',json=body,headers={'Idempotency-Key':'rename-file'}).json(),response.json())
        stale=self.client.post('/v1/projects/story-one/actions',json=body,headers={'Idempotency-Key':'stale'})
        self.assertTrue(stale.json()['detail']['no_mutation_committed'])
        target={'project_id':'story-one','kind':'entity','id':'file-one','expected_version':self.current()['version']}
        moved=self.client.post('/v1/trash/actions',json={'action':'trash','items':[target]},headers={'Idempotency-Key':'trash-file'})
        self.assertEqual(moved.status_code,200,moved.text)
        hero=next(e for e in self.current()['project']['entities'] if e['id']=='hero');self.assertEqual(hero['data']['libraryFileIds'],[])
        item=moved.json()['items'][0]
        restored=self.client.post('/v1/trash/actions',json={'action':'restore','items':[{**item['target'],'expected_version':item['version']}],
            'expected_project_versions':{'story-one':self.current()['version']}},headers={'Idempotency-Key':'restore-file'})
        self.assertEqual(restored.status_code,200,restored.text)
        hero=next(e for e in self.current()['project']['entities'] if e['id']=='hero');self.assertEqual(hero['data']['libraryFileIds'],['file-one'])

    def test_interruption_before_upload_client_key_binding_keeps_exact_original_journal(self):
        self.prepare();body=self.import_body();journal=self.app.state.assets.journal
        with patch.object(journal,'bind',side_effect=TimeoutError('before bind')):
            self.assertEqual(self.importing(body).json()['state'],'pending')
        with self.app.state.repository.engine.connect() as c:
            original=[dict(r) for r in c.execute(select(receipts).where(receipts.c.project_id=='target')).mappings()]
        self.assertEqual(len(original),1);self.assertIsNone(original[0]['client_key'])
        # Recovery may remain refused until the original uploader's prerequisites hold;
        # either outcome must retain the same upload identity, not allocate another.
        self.importing(body)
        with self.app.state.repository.engine.connect() as c:
            after=[dict(r) for r in c.execute(select(receipts).where(receipts.c.project_id=='target')).mappings()]
        self.assertEqual([r['id'] for r in after],[original[0]['id']])

    def test_selected_default_look_usage_excludes_alternative_and_explicit_suppression(self):
        self.prepare()
        # Two distinct receipt-backed file entities; only selected default is used.
        stream=io.BytesIO();Image.new('RGB',(512,512),'red').save(stream,'PNG')
        other=self.client.post('/v1/assets',data={'client_project_id':'story-one','client_asset_id':'alternate-file'},files={'file':('alternate.png',stream.getvalue(),'image/png')}).json()['asset_id']
        scene=next(e['id'] for e in self.current()['project']['entities'] if e['type']=='scene')
        self.action([{'op':'asset.attach','asset_id':other,'entity_id':'alternate'},
            {'op':'entity.update','entity_id':'hero','patch':{'data':{'looks':[
                {'id':'look','name':'Day','version':1,'gallery':{},'imageIds':['file-one']},
                {'id':'other','name':'Night','version':1,'gallery':{},'imageIds':['alternate']}],'defaultLookId':'look'}}},
            {'op':'entity.update','entity_id':scene,'patch':{'data':{'cast':['hero']}}},
            {'op':'entity.update','entity_id':'shot-one','patch':{'data':{'storyboard':{'version':1,'mode':'ref','quality':'draft'}}}}]).raise_for_status()
        rows={r['target']['id']:r for r in self.client.get('/v1/library?type=file').json()['items']}
        self.assertFalse(rows['file-one']['unused']);self.assertTrue(rows['alternate']['unused'])
        self.action([{'op':'entity.update','entity_id':'shot-one','patch':{'data':{'cast':[],'storyboard':{'version':1,'mode':'ref','quality':'draft','place':None}}}}],'suppress').raise_for_status()
        rows={r['target']['id']:r for r in self.client.get('/v1/library?type=file').json()['items']}
        self.assertTrue(rows['file-one']['unused']);self.assertTrue(rows['alternate']['unused'])
    def test_raw_file_filing_is_same_cas_and_keeps_quota(self):
        self.prepare();quota=self.app.state.assets.usage('superdan')
        response=self.action([{'op':'library.file','profile_id':'hero','targets':[{'kind':'asset','id':self.asset}]}])
        self.assertEqual(response.status_code,200,response.text)
        hero=next(e for e in self.current()['project']['entities'] if e['id']=='hero')
        self.assertEqual(hero['data']['libraryFileIds'],['file-one']);self.assertEqual(self.app.state.assets.usage('superdan'),quota)
    def test_source_version_binding_and_frozen_import_provenance(self):
        self.prepare();body=self.import_body();body['source']['expected_version']=self.current()['version']
        response=self.importing(body);self.assertEqual(response.status_code,200,response.text)
        result=response.json();self.assertEqual(result['source_project_version'],body['source']['expected_version'])
        self.action([{'op':'entity.update','entity_id':'hero','patch':{'title':'Later source'}}]).raise_for_status()
        self.assertEqual(self.importing(body).json(),result)
        target=self.current('target')['project'];profile=next(e for e in target['entities'] if e['id']==result['entity_ids']['hero'])
        self.assertEqual(profile['title'],'Hero');self.assertEqual(profile['data']['importSource']['snapshot_sha256'],result['source_snapshot_sha256'])

    def test_scoped_import_authorizes_both_stories_before_receiving(self):
        from studio_platform.auth import Principal
        from studio_platform.library_imports import LibraryImports
        from studio_platform.repository import NotFound
        self.prepare();body=self.import_body();service=LibraryImports(self.app.state.library)
        for projects in [('story-one',),('target',)]:
            principal=Principal('superdan','scoped',machine=True,project_ids=projects,scopes=('projects:read','projects:write','assets:read','assets:write'))
            with self.assertRaises(NotFound):service.start(principal,body,'scoped-import')
            visible=self.app.state.library.listing(principal)
            self.assertTrue(all(r['target']['project_id'] in projects for r in visible['items']))
        with self.app.state.repository.engine.connect() as c:
            self.assertEqual(c.execute(select(func.count()).select_from(documents).where(documents.c.kind=='library_import')).scalar_one(),0)
            self.assertEqual(c.execute(select(func.count()).select_from(receipts).where(receipts.c.project_id=='target')).scalar_one(),0)
    def test_source_integrity_failure_creates_no_upload_and_does_not_replace_intent(self):
        self.prepare();body=self.import_body()
        with patch.object(self.app.state.storage,'open',return_value=io.BytesIO(b'wrong')):
            response=self.importing(body)
        self.assertEqual(response.status_code,409,response.text)
        with self.app.state.repository.engine.connect() as c:
            ident=c.execute(select(documents.c.document_id).where(documents.c.kind=='library_import')).scalar_one()
            self.assertEqual(c.execute(select(func.count()).select_from(receipts).where(receipts.c.project_id=='target')).scalar_one(),0)
        self.assertEqual(self.client.get('/v1/library/imports/'+ident).json()['state'],'pending')
    def test_postgres_concurrent_same_import_one_original_operation(self):
        if self.app.state.repository.engine.dialect.name!='postgresql':self.skipTest('isolated PostgreSQL concurrency')
        from concurrent.futures import ThreadPoolExecutor
        from studio_platform.auth import Principal
        from studio_platform.library_imports import LibraryImports
        self.prepare();body=self.import_body();service=LibraryImports(self.app.state.library);principal=Principal('superdan','parallel')
        with ThreadPoolExecutor(max_workers=3) as pool:
            values=list(pool.map(lambda _:service.start(principal,body,'parallel-import'),range(3)))
        self.assertEqual(len({v['operation_id'] for v in values}),1)
        final=service.start(principal,body,'parallel-import');self.assertEqual(final['state'],'completed')
        with self.app.state.repository.engine.connect() as c:
            self.assertEqual(c.execute(select(func.count()).select_from(receipts).where(receipts.c.project_id=='target')).scalar_one(),1)
            self.assertEqual(c.execute(select(func.count()).select_from(documents).where(documents.c.kind=='library_import')).scalar_one(),1)

    def test_generated_artifact_import_and_authoring_usage_keep_original_receipt(self):
        import uuid
        from sqlalchemy import insert,update
        from studio_platform.repository import Scope,artifacts,attempts
        self.prepare();repo=self.app.state.repository;scope=Scope(self.settings.tenant_id,'superdan','story-one')
        plan=repo.create_plan(scope,{}, {'pool':'test-pool'},expires_at=repo.clock()+1000)
        job=repo.create_job(scope,plan['id'],'synthetic-output',initial_status='planned')
        original=self.app.state.assets.get('superdan',self.asset)['original'];aid=str(uuid.uuid4())
        metadata={'validated':True,'kind':'image','object_key':original['key'],'size_bytes':original['size_bytes'],'sha256':original['sha256'],'mime':'image/png'}
        with repo.transaction() as c:
            c.execute(update(jobs).where(jobs.c.id==job['id']).values(status='succeeded'))
            attempt=str(uuid.uuid4())
            c.execute(insert(attempts).values(id=attempt,job_id=job['id'],number=1,status='succeeded',fence=1,worker_id='synthetic',created_at=1,updated_at=1))
            c.execute(insert(artifacts).values(id=aid,job_id=job['id'],attempt_id=attempt,metadata=metadata,created_at=repo.clock()))
        adopted=self.action([{'op':'artifact.adopt','artifact_id':aid,'entity_id':'generated'},
            {'op':'link.create','link':{'source':'generated','target':'shot-one','role':'firstFrame'}}])
        self.assertEqual(adopted.status_code,200,adopted.text)
        rows={r['target']['id']:r for r in self.client.get('/v1/library?type=file').json()['items']}
        self.assertFalse(rows['generated']['unused'])
        body={'source':{'project_id':'story-one','kind':'artifact','id':aid},'target_project_id':'target','expected_project_version':self.current('target')['version']}
        result=self.importing(body);self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['state'],'completed')
        with repo.engine.connect() as c:self.assertEqual(c.execute(select(artifacts.c.metadata).where(artifacts.c.id==aid)).scalar_one(),metadata)

    def test_stale_source_proof_is_preinsert_and_existing_replay_keeps_snapshot(self):
        self.prepare();body=self.import_body();body['source']['expected_version']=self.current()['version']+1
        result=self.importing(body);self.assertEqual(result.status_code,409,result.text)
        self.assertEqual(result.json()['code'],'library_source_version_conflict');self.assertTrue(result.json()['no_mutation_committed'])
        self.assertEqual(result.json()['request_hash'],request_hash(body))
        with self.app.state.repository.engine.connect() as c:self.assertEqual(c.execute(select(func.count()).select_from(documents).where(documents.c.kind=='library_import')).scalar_one(),0)

    def test_pending_import_blocks_source_and_destination_lifecycle_then_recovers(self):
        from studio_platform.library_imports import LibraryImports
        self.prepare();body=self.import_body()
        with patch.object(LibraryImports,'finish',side_effect=TimeoutError('before upload')):
            pending=self.importing(body).json()
        self.assertEqual(pending['state'],'pending')
        with self.app.state.repository.engine.connect() as c:
            self.assertEqual(c.execute(select(func.count()).select_from(receipts)).scalar_one(),1)
        for pid,kind,ident in [('story-one','entity','file-one'),('story-one','project','story-one'),('target','project','target')]:
            response=self.client.post('/v1/trash/actions',json={'action':'trash','items':[{'project_id':pid,'kind':kind,'id':ident,'expected_version':self.current(pid)['version']}]},headers={'Idempotency-Key':'pending-'+pid+kind})
            self.assertEqual(response.status_code,409,response.text)
            self.assertEqual(response.json()['detail']['code'],'authoring_operation_unresolved')
            self.assertTrue(response.json()['detail']['no_mutation_committed'])
        done=self.importing(body).json();self.assertEqual(done['operation_id'],pending['operation_id']);self.assertEqual(done['state'],'completed')

    def test_completed_import_audit_excluded_only_from_explicit_owned_purge(self):
        from studio_platform.content_references import cleanup_plan
        from types import SimpleNamespace
        from sqlalchemy import update
        self.prepare();self.importing(self.import_body()).raise_for_status()
        repo=self.app.state.repository
        principal=SimpleNamespace(owner='superdan',allows=lambda *args:True)
        service=SimpleNamespace(tenant=self.app.state.settings.tenant_id,storage=self.app.state.assets.store,assets=self.app.state.assets)
        with repo.transaction() as c:
            row=c.execute(select(documents).where(documents.c.kind=='library_import')).mappings().one()
            original=copy.deepcopy(row['payload'])
            # Remove only isolated fixture's current source authoring reference;
            # compare the original audit independently of other live references.
            c.execute(update(documents).where(documents.c.project_id=='__projects',documents.c.document_id=='story-one').values(payload={'entities':[]}))
            target={'asset_ids':{self.asset},'artifact_ids':set()}
            completed=cleanup_plan(c,service,principal,[],media_targets=target)
            self.assertFalse(any(w['reason']=='story_or_profile_reference' for w in completed['warnings']))
            c.execute(update(documents).where(documents.c.kind=='library_import').values(payload={**original,'state':'pending'}))
            pending=cleanup_plan(c,service,principal,[],media_targets=target)
            self.assertTrue(any(w['reason']=='story_or_profile_reference' for w in pending['warnings']))
            c.execute(update(documents).where(documents.c.kind=='library_import').values(payload=original))
            self.assertEqual(c.execute(select(documents.c.payload).where(documents.c.kind=='library_import')).scalar_one(),original)

    def test_terminal_import_receipt_survives_source_trash_without_recreation(self):
        self.prepare();body=self.import_body();done=self.importing(body).json()
        response=self.client.post('/v1/trash/actions',json={'action':'trash','items':[{'project_id':'story-one','kind':'project','id':'story-one','expected_version':self.current()['version']}]},headers={'Idempotency-Key':'trash-source'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/v1/library/imports/'+done['operation_id']).json(),done)
        self.assertEqual(self.importing(body).json(),done)
        self.login('supervan');self.assertEqual(self.client.get('/v1/library/imports/'+done['operation_id']).status_code,404)

    def test_original_guided_validation_is_closed_without_authoring_or_reupload(self):
        self.prepare();body=self.import_body()
        from studio_platform.library_imports import LibraryImports
        original=LibraryImports.plan
        def large(service,*args):
            payload=original(service,*args);file=next(iter(payload['files'].values()))
            payload['files']={str(i):copy.deepcopy(file) for i in range(101)}
            payload['entity_ids'].update({str(i):'copied-'+str(i) for i in range(101)})
            return payload
        before=copy.deepcopy(self.current('target'))
        with patch.object(LibraryImports,'plan',large):response=self.importing(body)
        self.assertEqual(response.status_code,200,response.text)
        result=response.json();self.assertEqual(result['state'],'blocked');self.assertTrue(result['no_authoring_committed'])
        self.assertEqual(result['reason'],'library_authoring_validation_failed')
        self.assertEqual(self.current('target'),before)
        usage=self.app.state.assets.usage('superdan')
        self.assertEqual(self.importing(body).json(),result);self.assertEqual(self.app.state.assets.usage('superdan'),usage)

    def test_same_receipt_usage_unions_authorized_stories_not_copied_assets(self):
        self.prepare()
        project=copy.deepcopy(self.current()['project']);project['id']='target'
        shot=next(e for e in project['entities'] if e['id']=='shot-one')
        shot['data'].pop('storyboard',None)
        project['links'].append({'id':'use-file','source':'file-one','target':'shot-one','role':'reference'})
        response=self.client.put('/v1/projects/target',json={'project':project,'expected_version':self.current('target')['version']},headers={'Idempotency-Key':'cross-story-ref'})
        self.assertEqual(response.status_code,200,response.text)
        rows=self.client.get('/v1/library?project_id=story-one&work=target&type=file').json()['items']
        self.assertTrue(any(row['asset_id']==self.asset and 'target' in row['work_ids'] for row in rows))

    def test_large_owner_projection_has_bounded_reads_and_single_shot_render_copies(self):
        from sqlalchemy import event,insert,update
        from studio_platform.auth import Principal
        from studio_platform.storyboard_versions import effective_project
        self.prepare();repo=self.app.state.repository;tenant=self.settings.tenant_id
        principal=Principal('superdan','library-read')
        base=copy.deepcopy(self.current()['project'])
        next(e for e in base['entities'] if e['id']=='shot-one')['data']['storyboard']={'version':1,'mode':'fl','quality':'draft'}
        with repo.transaction() as conn:
            conn.execute(update(documents).where(documents.c.tenant_id==tenant,documents.c.owner_id=='superdan',
                documents.c.project_id=='__projects',documents.c.document_id=='story-one').values(payload=base))
        def measure():
            statements=[];copies=[]
            def sql(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
            def render(project,*args,**kwargs):
                copies.append(len(project['entities']));return effective_project(project,*args,**kwargs)
            event.listen(repo.engine,'before_cursor_execute',sql)
            try:
                with patch('studio_platform.storyboard_versions.effective_project',side_effect=render),patch.object(
                        self.app.state.generation_admission,'capabilities_provider',wraps=self.app.state.generation_admission.capabilities_provider) as caps:
                    rows=self.app.state.library.listing(principal,type='image',limit=100)
                return rows,len(statements),copies,caps.call_count
            finally:event.remove(repo.engine,'before_cursor_execute',sql)
        small,small_reads,_,_=measure()
        projects=[]
        for number in range(6):
            project=copy.deepcopy(base);project['id']='large-'+str(number)
            # Local authoring media remain valid library rows without claiming
            # another project's uploaded receipt. They never touch storage.
            project['entities']=[e for e in project['entities'] if e['id'] not in {'file-one','hero'}]
            prototype=next(e for e in project['entities'] if e['type']=='shot')
            for n in range(10):
                shot=copy.deepcopy(prototype);shot['id']='shot-extra-'+str(n);project['entities'].append(shot)
            for n in range(180):
                project['entities'].append({'id':'local-image-'+str(n),'type':'image','title':'Image '+str(n),
                    'parentId':None,'description':'','version':1,'order':n,'status':'draft',
                    'data':{'fileId':'local-file-'+str(n)}})
            projects.append(project)
        with repo.transaction() as conn:
            conn.execute(insert(documents),[{'tenant_id':tenant,'owner_id':'superdan','project_id':'__projects',
                'kind':'project','document_id':p['id'],'payload':p,'version':1,'updated_at':1} for p in projects])
        before=[copy.deepcopy(p) for p in projects]
        large,large_reads,copies,cap_reads=measure()
        self.assertEqual(len(large['items']),100);self.assertEqual(large['next_offset'],100)
        self.assertEqual(large_reads,small_reads);self.assertLessEqual(large_reads,8)
        self.assertEqual(len(copies),67);self.assertEqual(set(copies),{1});self.assertEqual(cap_reads,1)
        with repo.engine.connect() as conn:
            after=list(conn.execute(select(documents.c.payload).where(documents.c.tenant_id==tenant,
                documents.c.owner_id=='superdan',documents.c.document_id.in_([p['id'] for p in projects])).order_by(documents.c.document_id)).scalars())
        self.assertEqual(after,before)
        print(f'Library scale: {small_reads} -> {large_reads} SQL statements; 1080 extra files, 66 extra shots; capabilities={cap_reads}; copied shot entities={len(copies)}')

    def test_read_snapshot_matches_canonical_asset_lifecycle_fences(self):
        import json,uuid
        from sqlalchemy import insert,update
        from studio_platform.assets import asset_table
        from studio_platform.auth import Principal
        from studio_platform.content_lifecycle import token
        from studio_platform.library import _LibraryRead
        from studio_platform.quick_chat import objects
        from studio_platform.repository import Conflict,NotFound
        from studio_platform.universal_trash import target_key
        self.prepare();repo=self.app.state.repository;tenant=self.settings.tenant_id
        assets=[]
        for n in range(9):
            ident=f'{n+1:032x}';asset=fixture.GenerationDraftTests.source(self,ident,'image')
            asset['original']['key']='original/'+ident;asset['model']['key']='model/'+ident
            if n==2:asset['parent_id']='deleted-parent'
            if n==3:asset['selection']={'artifact_id':'deleted-selection'}
            assets.append(asset)
        with repo.transaction() as conn:
            for asset in assets:conn.execute(update(asset_table).where(asset_table.c.id==asset['id']).values(record=json.dumps(asset)))
            def tombstone(kind,ident,owner='superdan',other_tenant=tenant,payload=None):
                conn.execute(insert(objects).values(id='delete-'+uuid.uuid4().hex,tenant=other_tenant,owner=owner,
                    session_id='__content',kind='content_tombstone',business_key=token(kind,ident),version=1,
                    payload=payload or {},created_at=1,updated_at=1))
            tombstone('asset',assets[1]['id']);tombstone('asset','deleted-parent');tombstone('artifact','deleted-selection')
            tombstone('key','bound-key',payload={'object_key':assets[4]['original']['key']})
            tombstone('asset',assets[0]['id'],owner='supervan');tombstone('asset',assets[0]['id'],other_tenant='other-tenant')
            for n,state in ((5,'trashed'),(6,'purging'),(7,'purged'),(8,'active')):
                target={'kind':'asset','project_id':'story-one','id':assets[n]['id']}
                conn.execute(insert(documents).values(tenant_id=tenant,owner_id='superdan',project_id='__trash',
                    kind='trash_item',document_id=target_key(target),payload={'state':state,'target':target},version=1,updated_at=1))
        principal=Principal('superdan','snapshot');snapshot=_LibraryRead(self.app.state.library,principal)
        expected=[]
        for asset in assets:
            try:self.app.state.library.authorize_target(principal,'story-one',{'kind':'asset','id':asset['id']});visible=True
            except (LookupError,Conflict,NotFound):visible=False
            self.assertEqual(('story-one',asset['id']) in snapshot.assets,visible,asset['id'])
            if visible:expected.append(asset['id'])
        self.assertEqual(expected,[assets[0]['id'],assets[8]['id']])
        restricted=Principal('superdan','projects-only',machine=True,project_ids=('story-one',),scopes=('projects:read',))
        self.assertEqual(_LibraryRead(self.app.state.library,restricted).assets,{})
        self.assertEqual(_LibraryRead(self.app.state.library,Principal('supervan','other-owner')).assets,{})

    def test_read_snapshot_matches_canonical_artifact_owner_scope_and_lifecycle(self):
        import uuid
        from sqlalchemy import insert,update
        from studio_platform.auth import Principal
        from studio_platform.content_lifecycle import token
        from studio_platform.library import _LibraryRead
        from studio_platform.quick_chat import objects
        from studio_platform.repository import Scope,artifacts,attempts,Conflict,NotFound
        from studio_platform.universal_trash import target_key
        self.prepare();repo=self.app.state.repository;tenant=self.settings.tenant_id
        scope=Scope(tenant,'superdan','story-one')
        plan=repo.create_plan(scope,{}, {'pool':'test-pool'},expires_at=repo.clock()+1000)
        job=repo.create_job(scope,plan['id'],'synthetic-availability',initial_status='planned')
        ids=[str(uuid.uuid4()) for _ in range(8)];attempt=str(uuid.uuid4())
        with repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id==job['id']).values(status='succeeded'))
            conn.execute(insert(attempts).values(id=attempt,job_id=job['id'],number=1,status='succeeded',
                fence=1,worker_id='synthetic',created_at=1,updated_at=1))
            for n,ident in enumerate(ids):
                metadata={'kind':'video','object_key':'synthetic/'+ident,'size_bytes':1,'sha256':'0'*64}
                if n==1:metadata['content_deleted']=True
                conn.execute(insert(artifacts).values(id=ident,job_id=job['id'],attempt_id=attempt,
                    metadata=metadata,created_at=1))
                if n==2:conn.execute(insert(objects).values(id='delete-'+uuid.uuid4().hex,tenant=tenant,
                    owner='superdan',session_id='__content',kind='content_tombstone',business_key=token('artifact',ident),
                    version=1,payload={},created_at=1,updated_at=1))
                if 3<=n<=6:
                    target={'kind':'artifact','project_id':'story-one','id':ident}
                    conn.execute(insert(documents).values(tenant_id=tenant,owner_id='superdan',project_id='__trash',
                        kind='trash_item',document_id=target_key(target),payload={'state':['trashed','purging','purged','active'][n-3],
                        'target':target},version=1,updated_at=1))
                if n==7:conn.execute(insert(objects).values(id='delete-key-'+uuid.uuid4().hex,tenant=tenant,
                    owner='superdan',session_id='__content',kind='content_tombstone',business_key=token('key',metadata['object_key']),
                    version=1,payload={'object_key':metadata['object_key']},created_at=1,updated_at=1))
        principal=Principal('superdan','snapshot');snapshot=_LibraryRead(self.app.state.library,principal)
        expected=[]
        for ident in ids:
            try:self.app.state.library.authorize_target(principal,'story-one',{'kind':'artifact','id':ident});visible=True
            except (LookupError,Conflict,NotFound):visible=False
            self.assertEqual(('story-one',ident) in snapshot.artifacts,visible)
            if visible:expected.append(ident)
        # GenerationAccess.artifact fences artifact identity; its canonical
        # read does not additionally gate an artifact by metadata.object_key.
        self.assertEqual(expected,[ids[0],ids[6],ids[7]])
        restricted=Principal('superdan','assets-only',machine=True,project_ids=('story-one',),scopes=('projects:read','assets:read'))
        self.assertEqual(_LibraryRead(self.app.state.library,restricted).artifacts,{})
        self.assertEqual(_LibraryRead(self.app.state.library,Principal('supervan','foreign')).artifacts,{})

    def test_projection_uses_identical_effective_drafts_and_no_capabilities_for_empty_stories(self):
        from sqlalchemy import update
        from studio_platform.auth import Principal
        from studio_platform.generation_draft import read_draft
        from studio_platform.repository import Scope
        from studio_platform.storyboard_versions import effective_project
        self.prepare();repo=self.app.state.repository;tenant=self.settings.tenant_id
        project=copy.deepcopy(self.current()['project'])
        project.setdefault('journey',{})['storyboard']={'defaults':{'recipe_id':'h3-base-fl2va-v1','controls':{'steps':20}}}
        first=next(e for e in project['entities'] if e['id']=='shot-one')
        first['data'].update(storyboard={'version':1,'mode':'ref','quality':'draft'},cast=['hero'])
        for n in range(3):
            shot=copy.deepcopy(first);shot['id']='variant-'+str(n)
            if n!=0:shot['data'].pop('storyboard')
            shot['data']['h3']={'recipeId':'h3-base-ref2va-v1','explicitReferences':n==2,'controls':{'steps':22+n}}
            project['entities'].append(shot)
        defaults={'recipe_id':'h3-base-ref2va-v1','controls':{'resolution':'720P'}}
        repo.put_document(Scope(tenant,'superdan','__preferences'),'storyboard_preferences','defaults',defaults)
        with repo.transaction() as conn:
            conn.execute(update(documents).where(documents.c.tenant_id==tenant,documents.c.owner_id=='superdan',
                documents.c.project_id=='__projects',documents.c.document_id=='story-one').values(payload=project))
        seen=[]
        def compare(render,shot_id,**kwargs):
            expected,_=effective_project(project,shot_id,defaults)
            actual=read_draft(render,shot_id,**kwargs)
            self.assertEqual(actual,read_draft(expected,shot_id,**kwargs));seen.append(shot_id)
            return actual
        before=copy.deepcopy(project)
        with patch('studio_platform.generation_draft.read_draft',side_effect=compare):
            self.app.state.library.listing(Principal('superdan','snapshot'))
        self.assertEqual(len(seen),4);self.assertEqual(self.current()['project'],before)
        empty=Principal('superdan','empty',machine=True,project_ids=('target',),scopes=('projects:read',))
        with patch.object(self.app.state.generation_admission,'capabilities_provider',side_effect=AssertionError('not needed')):
            self.assertEqual(self.app.state.library.listing(empty)['items'],[])

    def test_duplicate_assets_and_legacy_or_deleted_project_shapes_keep_existing_read_semantics(self):
        from sqlalchemy import insert,update
        from studio_platform.assets import asset_table
        from studio_platform.auth import Principal
        self.prepare();repo=self.app.state.repository;tenant=self.settings.tenant_id
        duplicate=fixture.GenerationDraftTests.source(self,'d'*32,'image')
        legacy={'id':'old-shape','schemaVersion':1,'title':None,'entities':[{'id':'old-image','type':'image',
            'title':'Legacy image','data':{'fileId':'old-local-file'}}],'links':[]}
        with repo.transaction() as conn:
            conn.execute(update(asset_table).where(asset_table.c.id.in_((self.asset,duplicate['id']))).values(status='duplicate'))
            for ident,payload in (('old-shape',legacy),('deleted-shape',{'content_deleted':True})):
                conn.execute(insert(documents).values(tenant_id=tenant,owner_id='superdan',project_id='__projects',
                    kind='project',document_id=ident,payload=payload,version=1,updated_at=1))
        principal=Principal('superdan','snapshot')
        rows=self.app.state.library.listing(principal)['items'];ids={r['target']['id'] for r in rows}
        self.assertIn('file-one',ids)  # Attached duplicate is still authorized by AssetService.get.
        self.assertNotIn(self.asset,ids);self.assertNotIn(duplicate['id'],ids)  # Raw duplicate enumeration stays excluded.
        self.assertIn('old-image',ids);self.assertFalse(any(r['target']['project_id']=='deleted-shape' for r in rows))

    def test_read_snapshot_rechecks_project_trash_and_has_no_cross_request_cache(self):
        from sqlalchemy import insert
        from studio_platform.auth import Principal
        from studio_platform.library import _LibraryRead
        from studio_platform.universal_trash import target_key
        self.prepare();repo=self.app.state.repository;principal=Principal('superdan','snapshot')
        self.assertIn('story-one',_LibraryRead(self.app.state.library,principal).rows)
        target={'kind':'project','project_id':'story-one','id':'story-one'}
        with repo.transaction() as conn:
            conn.execute(insert(documents).values(tenant_id=self.settings.tenant_id,owner_id='superdan',project_id='__trash',
                kind='trash_item',document_id=target_key(target),payload={'state':'trashed','target':target},version=1,updated_at=1))
        self.assertNotIn('story-one',_LibraryRead(self.app.state.library,principal).rows)
        self.assertEqual(self.app.state.library.listing(principal)['items'],[])
