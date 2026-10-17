"""Lifecycle regressions on the original project/owner authorities; no live deletion."""
import copy
import unittest
from sqlalchemy import select,func
import test_platform_generation_draft as fixture
from studio_platform.repository import jobs,budget_reservations

class UniversalTrashTests(unittest.TestCase):
    setUp=fixture.GenerationDraftTests.setUp
    login=fixture.GenerationDraftTests.login
    setup_project=fixture.GenerationDraftTests.setup_project
    edit=fixture.GenerationDraftTests.edit
    key=fixture.GenerationDraftTests.key
    def current(self): return self.client.get('/v1/projects/story-one').json()
    def target(self,kind='entity',ident='shot-one'):
        return {'kind':kind,'project_id':'story-one','id':ident,'expected_version':self.current()['version']}
    def apply(self,body,key='trash-one'):
        return self.client.post('/v1/trash/actions',headers={'Idempotency-Key':key},json=body)
    def test_soft_trash_undo_preserves_unrelated_edit_and_original_ids(self):
        self.setup_project();original=self.current()['project']
        body={'action':'trash','items':[self.target()]}
        result=self.apply(body);self.assertEqual(result.status_code,200,result.text)
        self.assertNotIn('shot-one',[e['id'] for e in self.current()['project']['entities']])
        self.assertEqual(self.apply(body).json(),result.json())
        self.edit([{'op':'project.update','patch':{'title':'Unrelated edit'}}],version=self.current()['version']).raise_for_status()
        item=result.json()['items'][0]
        restore={'action':'restore','items':[{**item['target'],'expected_version':item['version']}],
            'expected_project_versions':{'story-one':self.current()['version']}}
        response=self.apply(restore,'undo-one');self.assertEqual(response.status_code,200,response.text)
        project=self.current()['project'];self.assertEqual(project['title'],'Unrelated edit')
        shot=next(e for e in project['entities'] if e['id']=='shot-one')
        self.assertEqual(shot['data'],next(e for e in original['entities'] if e['id']=='shot-one')['data'])
        self.assertEqual(self.client.get('/v1/trash').json()['items'],[])
    def test_last_scene_and_changed_version_are_closed_no_write(self):
        self.setup_project();before=self.current()
        scene=next(e for e in before['project']['entities'] if e['type']=='scene')
        result=self.apply({'action':'trash','items':[self.target(ident=scene['id'])]})
        self.assertEqual(result.status_code,409,result.text)
        self.assertEqual(result.json()['detail']['code'],'trash_last_container_required')
        self.assertTrue(result.json()['detail']['no_mutation_committed'])
        self.assertEqual(self.current(),before)
    def test_owner_cannot_observe_or_restore_foreign_trash(self):
        self.setup_project();result=self.apply({'action':'trash','items':[self.target()]}).json()
        self.login('supervan')
        self.assertEqual(self.client.get('/v1/trash').json()['items'],[])
        self.assertEqual(self.client.get('/v1/trash/actions/'+result['operation_id']).status_code,404)

    def test_project_hide_restore_and_stale_full_save_cannot_resurrect_entity(self):
        self.setup_project();before=self.current()
        moved=self.apply({'action':'trash','items':[self.target()]}).json()
        stale={**before['project'],'title':'Resurrect stale editor'}
        result=self.client.put('/v1/projects/story-one',json={'project':stale,'expected_version':self.current()['version']})
        self.assertEqual(result.status_code,409,result.text)
        body={'action':'trash','items':[self.target('project','story-one')]}
        project_moved=self.apply(body,'project-trash');self.assertEqual(project_moved.status_code,200,project_moved.text)
        self.assertEqual(self.client.get('/v1/projects/story-one').status_code,404)
        self.assertNotIn('story-one',[p['id'] for p in self.client.get('/v1/projects').json()['projects']])
        item=project_moved.json()['items'][0]
        preview=self.client.post('/v1/trash/preview',json={'action':'restore','items':[item['target']]})
        self.assertEqual(preview.status_code,200,preview.text)
        p=preview.json()
        restored=self.apply({'action':'restore','items':[{**v['target'],'expected_version':v['expected_version']} for v in p['items']],
            'expected_project_versions':p['affected_project_versions']},'restore-project')
        self.assertEqual(restored.status_code,200,restored.text)
        self.assertEqual(self.current()['project']['title'],before['project']['title'])
        self.assertNotIn('shot-one',[e['id'] for e in self.current()['project']['entities']])

    def test_two_selected_shots_restore_atomically_in_inverse_order(self):
        self.setup_project()
        project=self.current()['project'];scene=next(e['id'] for e in project['entities'] if e['type']=='scene')
        self.edit([{'op':'entity.create','entity':{'id':'shot-two','type':'shot','title':'Second','parentId':scene}}],version=self.current()['version']).raise_for_status()
        before=copy.deepcopy(self.current()['project'])
        moved=self.apply({'action':'trash','items':[self.target(),self.target(ident='shot-two')]}).json()
        body={'action':'restore','items':[{**i['target'],'expected_version':i['version']} for i in moved['items']],
            'expected_project_versions':{'story-one':self.current()['version']}}
        restored=self.apply(body,'restore-two');self.assertEqual(restored.status_code,200,restored.text)
        byid={e['id']:e for e in self.current()['project']['entities']}
        for old in before['entities']:
            self.assertEqual(byid[old['id']]['data'],old['data']);self.assertEqual(byid[old['id']]['parentId'],old['parentId'])
    def test_variant_removal_does_not_silently_select_another_default_and_undo_restores_bindings(self):
        from studio_platform.universal_trash import remove_authoring,restore_value
        project={'entities':[{'id':'character','type':'character','parentId':None,'version':1,'data':{
            'looks':[{'id':'old'},{'id':'new'}],'defaultLookId':'old'}},
            {'id':'location','type':'location','parentId':None,'version':1,'data':{
                'versions':[{'id':'old'},{'id':'new'}],'defaultVersionId':'old'}},
            {'id':'shot','type':'shot','parentId':None,'version':1,'data':{'storyboard':{
                'cast':[{'characterId':'character','lookId':'old'}],
                'place':{'locationId':'location','versionId':'old'}}}}]}
        for kind,parent in [('look','character'),('location_version','location')]:
            value=copy.deepcopy(project);before=remove_authoring(value,{'kind':kind,'id':'old','parent_id':parent})
            sb=value['entities'][2]['data']['storyboard']
            self.assertEqual(sb['cast'] if kind=='look' else sb['place'],[] if kind=='look' else None)
            restored=restore_value(before,value,value)
            self.assertEqual(restored,project)


import io
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from sqlalchemy import insert,update
import test_platform_repository as ledger
import test_quick_chat_history as history_fixture
from studio_platform.api import create_app
from studio_platform.auth import Principal
from studio_platform.settings import Settings
from studio_platform.guided import empty_project
from studio_platform.repository import Scope,Conflict,documents,plans,attempts
from studio_platform.quick_chat import objects
from studio_platform.storage import ObjectNotFound

class UniversalPurgeTests(ledger.LedgerCase):
    activate=history_fixture.CreationHistoryTests.activate
    def setUp(self):
        super().setUp()
        self.app=create_app(Settings(Path(self.temp.name)/'universal',tenant_id=self.scope.tenant_id,
            auth_mode='local-test',database_url=self.url),repository=self.repo)
        self.service=self.app.state.quick_chat
        self.trash=self.app.state.universal_trash
        self.principal=Principal(self.scope.owner_id,'browser')
        self.project=empty_project({'id':self.scope.project_id,'title':'Original'})
        self.repo.put_document(Scope(self.scope.tenant_id,self.scope.owner_id,'__projects'),'project',self.scope.project_id,self.project)
    def version(self):return self.repo.get_document(Scope(self.scope.tenant_id,self.scope.owner_id,'__projects'),'project',self.scope.project_id)['version']
    def body(self,kind,ident):return {'action':'trash','items':[{'kind':kind,'id':ident,'project_id':self.scope.project_id,'expected_version':self.version()}]}
    def purge(self,moved,key='purge'):
        body={'action':'purge','items':[{**i['target'],'expected_version':i['version']} for i in moved['items']],
            'expected_project_versions':{self.scope.project_id:self.version()},'confirmed':True}
        return self.trash.apply(self.principal,body,key),body
    def upload(self):
        stream=io.BytesIO();Image.new('RGB',(512,512),'navy').save(stream,'PNG');stream.seek(0)
        return self.service.assets.upload(self.scope.owner_id,self.scope.project_id,stream,'sample.png')
    def test_explicit_asset_purge_unknown_reconciles_original_and_releases_quota_once(self):
        self.activate();asset=self.upload();aid=asset['id']
        original_usage=self.service.assets.usage(self.scope.owner_id)['accounted_bytes']
        moved=self.trash.apply(self.principal,self.body('asset',aid),'trash')
        pending,body=self.purge(moved)
        with patch.object(self.service.storage,'open',side_effect=TimeoutError('opaque')),patch.object(self.service.storage,'delete') as deleted:
            self.service.process_history_cleanup()
        deleted.assert_not_called()
        self.assertEqual(self.trash.get_action(self.principal,pending['operation_id'])['state'],'pending')
        self.assertEqual(self.service.assets.usage(self.scope.owner_id)['accounted_bytes'],original_usage)
        self.assertEqual(self.trash.apply(self.principal,body,'purge')['operation_id'],pending['operation_id'])
        original_delete=self.service.storage.delete
        def lost(key):original_delete(key);raise TimeoutError('lost ack')
        with patch.object(self.service.storage,'delete',side_effect=lost):self.service.process_history_cleanup()
        final=self.trash.get_action(self.principal,pending['operation_id'])
        self.assertEqual(final['state'],'completed');self.assertTrue(final['media_deleted'])
        after=self.service.assets.usage(self.scope.owner_id)['accounted_bytes']
        self.assertLess(after,original_usage)
        self.service.process_history_cleanup();self.assertEqual(self.service.assets.usage(self.scope.owner_id)['accounted_bytes'],after)
        with self.assertRaises(Conflict):self.repo.put_document(Scope(self.scope.tenant_id,self.scope.owner_id,'__projects'),
            'project','resurrection',{'asset_id':aid})
    def test_accepted_input_retained_and_jobs_accounting_unchanged(self):
        self.activate();asset=self.upload();aid=asset['id']
        plan=self.repo.create_plan(self.scope,{'asset_id':aid},{'pool':'test-pool'},expires_at=self.now+1000)
        job=self.repo.create_job(self.scope,plan['id'],'accepted',initial_status='planned')
        with self.repo.transaction() as c:c.execute(update(jobs).where(jobs.c.id==job['id']).values(status='succeeded'))
        with self.repo.engine.connect() as c:
            baseline={t.name:[dict(r) for r in c.execute(select(t)).mappings()] for t in (jobs,plans,attempts,budget_reservations)}
        moved=self.trash.apply(self.principal,self.body('asset',aid),'trash')
        pending,_=self.purge(moved);self.service.process_history_cleanup()
        final=self.trash.get_action(self.principal,pending['operation_id'])
        self.assertEqual(final['state'],'completed');self.assertFalse(final['media_deleted']);self.assertEqual(final['kept']['assets'],1)
        with self.repo.engine.connect() as c:
            for t in (jobs,plans,attempts,budget_reservations):self.assertEqual([dict(r) for r in c.execute(select(t)).mappings()],baseline[t.name])
        from studio_platform.assets import asset_table
        import json
        with self.repo.engine.connect() as c:stored=json.loads(c.execute(select(asset_table.c.record).where(asset_table.c.id==aid)).scalar_one())
        self.service.storage.stat(stored['original']['key'])
    def test_running_job_blocks_trash_without_new_operation(self):
        self.job(status='queued')
        with self.assertRaisesRegex(Conflict,'jobs_active_or_uncertain'):
            self.trash.apply(self.principal,self.body('project',self.scope.project_id),'blocked')
        self.assertEqual(self.trash.list(self.principal)['items'],[])
    def test_same_original_key_concurrent_trash_returns_one_operation(self):
        body=self.body('project',self.scope.project_id)
        results=self.parallel(lambda _:self.trash.apply(self.principal,body,'same'),count=4)
        self.assertEqual(len({r['operation_id'] for r in results}),1)
        self.assertEqual(len(self.trash.list(self.principal)['items']),1)

    def test_original_guided_receipt_is_retained_and_cannot_reattach_purged_asset(self):
        from studio_platform.guided import receipts
        self.activate();asset=self.upload();aid=asset['id']
        body={'expected_version':self.version(),'actions':[{'op':'asset.attach','asset_id':aid}]}
        attached=self.app.state.guided.mutate(self.principal,self.scope.project_id,body,'attach-original')
        with self.repo.engine.connect() as c:before=[dict(r) for r in c.execute(select(receipts)).mappings()]
        moved=self.trash.apply(self.principal,self.body('asset',aid),'trash')
        pending,_=self.purge(moved);self.service.process_history_cleanup()
        final=self.trash.get_action(self.principal,pending['operation_id'])
        self.assertTrue(final['media_deleted'])
        from studio_platform.assets import AssetNotFound
        with self.assertRaises((Conflict,AssetNotFound)):self.app.state.guided.mutate(self.principal,self.scope.project_id,body,'attach-original')
        with self.repo.engine.connect() as c:self.assertEqual([dict(r) for r in c.execute(select(receipts)).mappings()],before)
    def test_other_owner_reference_retains_binary_without_leaking_identity(self):
        self.activate();asset=self.upload();aid=asset['id']
        # Historical inaccessible reference fixture; current API correctly rejects new cross-owner attachments.
        with self.repo.transaction() as c:c.execute(insert(documents).values(tenant_id=self.scope.tenant_id,owner_id='supervan',
            project_id='__projects',kind='project',document_id='foreign',version=1,payload={'asset_id':aid},updated_at=self.now))
        moved=self.trash.apply(self.principal,self.body('asset',aid),'trash')
        pending,_=self.purge(moved);self.service.process_history_cleanup()
        result=self.trash.get_action(self.principal,pending['operation_id'])
        self.assertFalse(result['media_deleted']);self.assertEqual(result['kept']['assets'],1)
        self.assertNotIn('foreign',str(result));self.assertNotIn('supervan',str(result))

    def test_original_output_association_does_not_prevent_explicit_unreferenced_purge(self):
        from studio_platform.artifact_writer import ArtifactWriter
        from studio_platform.repository import artifacts
        import test_creation_history_storage as storage_fixture
        self.activate()
        self.writer=ArtifactWriter(self.repo.engine,self.service.storage,Path(self.temp.name)/'collection',tenant=self.scope.tenant_id)
        queue,job,lease,tag,receipt,specs=storage_fixture.CreationHistoryStorageTests.prepared(self,settled=True)
        with self.repo.engine.connect() as c:
            artifact=dict(c.execute(select(artifacts).where(artifacts.c.job_id==job['id'])).mappings().one())
            before=dict(c.execute(select(jobs).where(jobs.c.id==job['id'])).mappings().one())
            financial=[dict(r) for r in c.execute(select(budget_reservations)).mappings()]
        moved=self.trash.apply(self.principal,self.body('artifact',artifact['id']),'trash-output')
        pending,_=self.purge(moved)
        self.service.process_history_cleanup()
        final=self.trash.get_action(self.principal,pending['operation_id'])
        self.assertEqual(final['state'],'completed');self.assertTrue(final['media_deleted'])
        with self.assertRaises(ObjectNotFound):self.service.storage.stat(specs[0]['object_key'])
        with self.repo.engine.connect() as c:
            self.assertEqual(dict(c.execute(select(jobs).where(jobs.c.id==job['id'])).mappings().one()),before)
            self.assertEqual([dict(r) for r in c.execute(select(budget_reservations)).mappings()],financial)
            self.assertTrue(c.execute(select(artifacts.c.metadata).where(artifacts.c.id==artifact['id'])).scalar_one()['content_deleted'])


import test_platform_quick_chat as chat_fixture
class UniversalTakeTests(ledger.LedgerCase):
    card=chat_fixture.QuickChatTests.card
    def setUp(self):
        super().setUp()
        self.settings=Settings(Path(self.temp.name)/'take',tenant_id=self.scope.tenant_id,
            auth_mode='local-test',database_url=self.url,generation_enabled=True,execution_backend='mock')
        self.app=create_app(self.settings,repository=self.repo);self.service=self.app.state.quick_chat
        self.principal=Principal(self.scope.owner_id,'browser')
        self.session=self.service.create_session(self.principal,{},'session')['session'];self.sid=self.session['id']
    def test_take_trashed_no_new_preflight_again_original_revision_unchanged_then_restore(self):
        from studio_platform.quick_chat import VERSION
        value=self.card(copies=2);revision=copy.deepcopy(value['revision']);item=revision['items'][0]
        trash=self.app.state.universal_trash
        with self.repo.engine.connect() as c:session=self.service._get(c,self.principal,self.sid,kind='session')
        project=session['payload']['project_id']
        target={'kind':'take','project_id':project,'parent_id':self.sid,'id':item['id']}
        preview=trash.apply(self.principal,{'action':'trash','items':[target]},'preview',preview=True)
        moved=trash.apply(self.principal,{'action':'trash','items':[{**target,'expected_version':preview['items'][0]['expected_version']}]},'trash')
        self.assertEqual(revision,self.service.get_revision(self.principal,self.sid,revision['id']))
        with self.assertRaisesRegex(Conflict,'take_in_trash'):
            self.service.preflight(self.principal,self.sid,revision['id'],{'capabilities_version':VERSION,
                'revision_hash':revision['input_hash'],'item_ids':[item['id']]},'new-preflight')
        with self.assertRaisesRegex(Conflict,'take_in_trash'):
            self.service.again(self.principal,self.sid,value['card']['id'],{'expected_card_version':value['card']['version'],
                'source_revision_id':revision['id'],'item_id':item['id'],'copies':1},'again')
        entries=self.service.group_takes(self.principal,self.sid,value['card']['id'])['takes']
        self.assertEqual(next(i for i in entries if i['id']==item['id'])['trash_state'],'trashed')
        preview=trash.apply(self.principal,{'action':'restore','items':[target]},'preview',preview=True)
        trash.apply(self.principal,{'action':'restore','items':[{**target,'expected_version':preview['items'][0]['expected_version']}],
            'expected_project_versions':preview['affected_project_versions']},'restore')
        self.assertEqual(revision,self.service.get_revision(self.principal,self.sid,revision['id']))
        with self.repo.engine.connect() as c:self.assertEqual(c.scalar(select(func.count()).select_from(jobs)),0)

import test_backup_expected_deletion as deletion_fixture
import test_platform_backup as backup_fixture
import test_backup_incomplete_coverage as incomplete_fixture
class UniversalDeletionProofTests(unittest.TestCase):
    setUp=backup_fixture.BackupTests.setUp
    target=incomplete_fixture.IncompleteCoverageTests.target
    source=deletion_fixture.ExpectedDeletionTests.source
    def test_universal_closed_delete_retains_job_input_and_classifies_only_own_output(self):
        import json
        from studio_platform import backup
        def mutate(db,payload,spec,action,ident):
            payload['action']='purge'
            db.execute("UPDATE platform_quick_chat_objects SET kind='universal_purge',payload=? WHERE id=?",(json.dumps(payload),action))
            db.execute('UPDATE platform_jobs SET request=?,result=? WHERE id=?',(json.dumps(self.job['request']),
                json.dumps({'artifact_id':ident}),self.job['id']))
        directory=self.source(mutation=mutate)
        result=backup.verify_local(directory,allow_incomplete=True)
        self.assertEqual(result['coverage']['expected_missing_objects'],1)
        self.assertEqual(result['coverage']['unexpected_missing_objects'],0)
