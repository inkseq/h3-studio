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
