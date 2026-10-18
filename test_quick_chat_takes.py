"""Take groups use original ledgers; supports isolated local PostgreSQL schemas."""
import copy
import io
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from sqlalchemy import select, func
import test_platform_repository as ledger
import test_platform_quick_chat as chat
from studio_platform.api import create_app
from studio_platform.settings import Settings
from studio_platform.auth import Principal
from studio_platform.quick_chat import QuickChatError, objects, EMPTY_INPUTS, DEFAULT_SETTINGS
from studio_platform.repository import Scope, documents, jobs, plans, NotFound, Conflict
from studio_platform.guided import empty_project, new_entity
from studio_platform.generation_draft import read_draft

class TakeTests(ledger.LedgerCase):
    card=chat.QuickChatTests.card
    preflight=chat.QuickChatTests.preflight
    submit=chat.QuickChatTests.submit
    def setUp(self):
        super().setUp()
        self.settings=Settings(Path(self.temp.name)/'app',tenant_id=self.scope.tenant_id,auth_mode='local-test',
            generation_enabled=True,execution_backend='mock',database_url=self.url)
        self.app=create_app(self.settings,repository=self.repo)
        self.service=self.app.state.quick_chat
        self.principal=Principal(self.scope.owner_id,'browser')
        self.session=self.service.create_session(self.principal,{},'session')['session'];self.sid=self.session['id']

    def story(self):
        p=empty_project({'id':'target-story','title':'Story'})
        new_entity(p,{'id':'chapter','type':'chapter','title':'Chapter'})
        new_entity(p,{'id':'scene','type':'scene','title':'Scene','parentId':'chapter'})
        return self.repo.put_document(Scope(self.scope.tenant_id,self.scope.owner_id,'__projects'),'project',p['id'],p,expected_version=0)

    def transfer_body(self):
        return {'target_project_id':'target-story','expected_project_version':1,'parent_id':'scene'}

    def test_fixed_first_distinct_random_again_and_original_immutable(self):
        value=self.card(copies=4,controls={'duration':5,'resolution':'480P','seed':'7'})
        revision=copy.deepcopy(value['revision']);seeds=[i['seed'] for i in revision['items']]
        self.assertEqual(seeds[0],'7');self.assertEqual(len(set(seeds)),4)
        body={'expected_card_version':value['card']['version'],'source_revision_id':revision['id'],'item_id':revision['items'][0]['id'],'copies':2}
        again=self.service.again(self.principal,self.sid,value['card']['id'],body,'again')
        replay=self.service.again(self.principal,self.sid,value['card']['id'],body,'again')
        self.assertEqual(again,replay)
        self.assertEqual(revision,self.service.get_revision(self.principal,self.sid,revision['id']))
        self.assertTrue(set(seeds).isdisjoint({i['seed'] for i in again['revision']['items']}))
        with self.repo.engine.connect() as conn:self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)),0)
        page=self.service.group_takes(self.principal,self.sid,value['card']['id'],limit=2)
        seen=[i['id'] for i in page['takes']]
        while page['before_cursor']:
            page=self.service.group_takes(self.principal,self.sid,value['card']['id'],limit=2,before_cursor=page['before_cursor'])
            seen.extend(i['id'] for i in page['takes'])
        self.assertEqual(len(seen),6);self.assertEqual(len(set(seen)),6)

    def test_preferences_cas_replay_limit_and_scoped_key(self):
        with self.assertRaises(QuickChatError):self.card(copies=5)
        body={'expected_version':0,'copies_max':5,'takes_fold_count':3}
        response=self.service.set_preferences(self.principal,body,'prefs')
        self.assertEqual(response,self.service.set_preferences(self.principal,body,'prefs'))
        self.assertEqual(self.card(copies=5)['revision']['copies'],5)
        with self.assertRaises(QuickChatError):self.service.set_preferences(self.principal,body,'new-key')
        scoped=Principal(self.scope.owner_id,'scoped',machine=True,all_projects=False,project_ids=('target-story',),scopes=('projects:write',))
        with self.assertRaises(NotFound):self.service.set_preferences(scoped,{'expected_version':1,'copies_max':9},'scoped')
        self.assertEqual(self.service.preferences(Principal('other','browser'))['copies_max'],4)

    def test_star_parallel_cas_and_replay_does_not_change_revision(self):
        value=self.card();revision=copy.deepcopy(value['revision']);item=revision['items'][0]['id']
        def apply(index):
            try:return self.service.star_take(self.principal,self.sid,item,{'expected_version':0,'starred':True},'star-'+str(index))
            except QuickChatError as e:return e.code
        result=self.parallel(apply,2)
        self.assertEqual(sum(isinstance(r,dict) for r in result),1)
        self.assertIn('version_conflict',result)
        winner=next(i for i,r in enumerate(result) if isinstance(r,dict))
        self.assertEqual(result[winner],self.service.star_take(self.principal,self.sid,item,{'expected_version':0,'starred':True},'star-'+str(winner)))
        self.assertEqual(revision,self.service.get_revision(self.principal,self.sid,revision['id']))
        with self.assertRaises(NotFound):self.service.star_take(Principal('other','browser'),self.sid,item,{'expected_version':0,'starred':False},'foreign')

    def test_complete_recipe_transfer_asset_replay_and_no_enqueue(self):
        self.story();png=io.BytesIO();Image.new('RGB',(512,512),'blue').save(png,'PNG');png.seek(0)
        uploaded=self.service.upload(self.principal,self.sid,png,'source.png','source')
        body={**DEFAULT_SETTINGS,'prompt':'Unchanged user prompt','inputs':{**copy.deepcopy(EMPTY_INPUTS),'first_frame':{'asset_id':uploaded['asset_id']}},
            'controls':{'duration':5,'resolution':'480P','seed':'18'}}
        value=self.service.save_card(self.principal,self.sid,body,'image-card');item=value['revision']['items'][0]
        receipt=self.service.transfer_take(self.principal,self.sid,item['id'],self.transfer_body(),'transfer')
        self.assertEqual(receipt['status'],'completed',receipt)
        self.assertEqual(receipt,self.service.transfer_take(self.principal,self.sid,item['id'],self.transfer_body(),'transfer'))
        target=self.repo.get_document(Scope(self.scope.tenant_id,self.scope.owner_id,'__projects'),'project','target-story')
        draft,issues=read_draft(target['payload'],receipt['shot_id'])
        self.assertEqual(draft['controls']['seed'],'18');self.assertEqual(draft['prompt'],body['prompt'])
        copied=draft['inputs']['first_frame']['asset_id'];self.assertNotEqual(copied,uploaded['asset_id'])
        original=self.app.state.assets.get(self.principal.owner,uploaded['asset_id'])
        actual=self.app.state.assets.get(self.principal.owner,copied,'target-story')
        self.assertEqual(original['original']['sha256'],actual['original']['sha256'])
        self.assertTrue(next(e for e in target['payload']['entities'] if e['id']==receipt['shot_id'])['data']['seedLocked'])
        with self.repo.engine.connect() as conn:self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)),0)

    def test_transfer_cas_unknown_and_scope_preserve_original_operation(self):
        target=self.story();value=self.card();item=value['revision']['items'][0]['id']
        with patch.object(self.service,'_finish_transfer',side_effect=RuntimeError('injected')):
            uncertain=self.service.transfer_take(self.principal,self.sid,item,self.transfer_body(),'unknown')
        self.assertEqual(uncertain['status'],'unknown')
        self.assertEqual(self.service.get_transfer(self.principal,self.sid,uncertain['id'])['status'],'unknown')
        completed=self.service.transfer_take(self.principal,self.sid,item,self.transfer_body(),'unknown')
        self.assertEqual(completed['id'],uncertain['id']);self.assertEqual(completed['status'],'completed')
        stale=self.service.transfer_take(self.principal,self.sid,item,self.transfer_body(),'stale')
        self.assertEqual(stale['status'],'blocked');self.assertTrue(stale['no_shot_committed'])
        self.assertEqual({k:stale[k] for k in ('source_session_id','item_id','project_id','expected_project_version','parent_id')},
            {'source_session_id':self.sid,'item_id':item,'project_id':'target-story','expected_project_version':1,'parent_id':'scene'})
        with self.assertRaises(NotFound):self.service.transfer_take(Principal('other','browser'),self.sid,item,self.transfer_body(),'foreign')

    def test_queue_matches_original_scheduler_and_no_foreign_identity(self):
        first=self.job('first');other=self.job('other',scope=self.other,cost=0)
        from studio_platform.queue import TaskQueue
        with self.repo.engine.connect() as conn:
            order,_=TaskQueue(self.repo).ordered_candidates(conn,'test-pool',purpose='generate',dispatch_backend='legacy')
        expected=next(i+1 for i,v in enumerate(order) if v['id']==first['id'])
        result=self.service.queue_position(self.principal,first['id'])
        self.assertEqual(result['position'],expected);self.assertIsNone(result['eta_seconds'])
        self.assertNotIn(other['id'],str(result));self.assertNotIn(self.other.owner_id,str(result))
        with self.assertRaises(NotFound):self.service.queue_position(self.principal,other['id'])

    def test_preflight_and_submission_original_admission_and_group_actions(self):
        value=self.card(copies=2);pref=self.preflight(value['revision'])
        self.assertIsNotNone(pref['estimate']['machine_seconds'])
        submitted=self.submit(value['revision'],pref)
        group=self.service.group_takes(self.principal,self.sid,value['card']['id'])
        self.assertEqual({i['submission_id'] for i in group['takes']},{submitted['id']})
        self.assertEqual({i['job_id'] for i in group['takes']},{i['job_id'] for i in submitted['items']})
        again=self.submit(value['revision'],pref)
        self.assertEqual(submitted['id'],again['id'])
        with self.repo.engine.connect() as conn:self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)),2)

    def test_lower_preferences_preserve_historical_revision_and_submission_replay(self):
        value=self.card(copies=4); original=copy.deepcopy(value['revision'])
        pref=self.preflight(original); submitted=self.submit(original,pref)
        self.service.set_preferences(self.principal,{'expected_version':0,'copies_max':1},'lower')
        self.assertEqual(original,self.service.get_revision(self.principal,self.sid,original['id']))
        self.assertEqual(submitted['id'],self.submit(original,pref)['id'])
        group=self.service.group_takes(self.principal,self.sid,value['card']['id'],limit=2)
        self.assertEqual(group['total_count'],4)
        self.assertEqual([t['group_number'] for t in group['takes']],[4,3])
        with self.assertRaises(QuickChatError):
            self.service.again(self.principal,self.sid,value['card']['id'],{'expected_card_version':value['card']['version'],
                'source_revision_id':original['id'],'item_id':original['items'][0]['id'],'copies':2},'over-limit')
        with self.repo.engine.connect() as conn:self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)),4)

    def test_destination_authorization_precedes_upload_journal(self):
        self.story();value=self.card();item=value['revision']['items'][0]['id']
        project=self.service._access(self.principal,self.sid,'projects:read')['payload']['project_id']
        scoped=Principal(self.scope.owner_id,'scoped',machine=True,all_projects=False,project_ids=(project,),
            scopes=('projects:read','projects:write','assets:read','assets:write'))
        with patch.object(self.app.state.assets,'upload',side_effect=AssertionError('must not upload')) as upload:
            with self.assertRaises(NotFound):self.service.transfer_take(scoped,self.sid,item,self.transfer_body(),'denied')
            upload.assert_not_called()
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(objects).where(objects.c.kind=='import')),0)

    def test_transfer_audio_range_preservation(self):
        import wave
        self.story();audio=io.BytesIO()
        with wave.open(audio,'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000);out.writeframes(b'\0\0'*48000)
        audio.seek(0);uploaded=self.service.upload(self.principal,self.sid,audio,'source.wav','audio')
        body={**DEFAULT_SETTINGS,'recipe_id':'h3-base-ref2va-v1','prompt':'Original audio prompt',
            'inputs':{**copy.deepcopy(EMPTY_INPUTS),'audios':[{'asset_id':uploaded['asset_id'],'purpose':'audio','source_range':{'start':0.5,'end':2.5}}]},
            'controls':{'duration':5,'resolution':'480P','seed':'23'}}
        value=self.service.save_card(self.principal,self.sid,body,'audio-card');item=value['revision']['items'][0]
        receipt=self.service.transfer_take(self.principal,self.sid,item['id'],self.transfer_body(),'audio-transfer')
        self.assertEqual(receipt['status'],'completed',receipt)
        target=self.repo.get_document(Scope(self.scope.tenant_id,self.scope.owner_id,'__projects'),'project','target-story')
        draft,issues=read_draft(target['payload'],receipt['shot_id'])
        self.assertEqual(draft['inputs']['audios'][0]['purpose'],'audio')
        self.assertEqual(draft['inputs']['audios'][0]['source_range'],{'start':0.5,'end':2.5})
        self.assertEqual(draft['controls']['seed'],'23')

    def test_extreme_editable_copies_rejected_before_seed_allocation(self):
        self.service.set_preferences(self.principal,{'expected_version':0,'copies_max':2147483647},'large-limit')
        with patch('studio_platform.quick_chat.secrets.randbits',side_effect=AssertionError('no allocation')):
            with self.assertRaises(QuickChatError) as caught:self.card(copies=2147483647)
        self.assertEqual(caught.exception.code,'project_size_exceeded')

    def test_http_preferences_and_group_routes(self):
        from fastapi.testclient import TestClient
        value=self.card()
        with TestClient(self.app) as client:
            client.post('/api/auth/login',json={'username':self.principal.owner}).raise_for_status()
            pref=client.get('/v1/quick-chat/preferences');self.assertEqual(pref.status_code,200,pref.text)
            update=client.patch('/v1/quick-chat/preferences',json={'expected_version':0,'copies_max':6},headers={'Idempotency-Key':'http-pref'})
            self.assertEqual(update.status_code,200,update.text)
            group=client.get('/v1/quick-chat/sessions/'+self.sid+'/cards/'+value['card']['id']+'/takes')
            self.assertEqual(group.status_code,200,group.text);self.assertEqual(group.json()['total_count'],1)
            session=client.get('/v1/quick-chat/sessions/'+self.sid+'/takes')
            self.assertEqual(session.status_code,200,session.text)
            self.assertEqual(session.json()['takes'][0]['card_id'],value['card']['id'])
            self.assertNotIn('recipe',session.json()['takes'][0])

    def test_session_grid_pages_all_66_card_takes_without_writes_or_renumbering(self):
        cards=[self.card(copies=2,key='grid-card-'+str(i)) for i in range(66)]
        starred=cards[0]['revision']['items'][0]['id']
        self.service.star_take(self.principal,self.sid,starred,{'expected_version':0,'starred':True},'grid-star')
        with self.repo.engine.connect() as conn:
            before={name:conn.scalar(select(func.count()).select_from(table)) for name,table in
                [('objects',objects),('jobs',jobs),('plans',plans),('documents',documents)]}
        page=self.service.session_takes(self.principal,self.sid,limit=11)
        self.assertEqual(page['total_count'],132)
        seen=[]; cursors=set()
        while True:
            seen.extend(page['takes'])
            if not page['before_cursor']:break
            self.assertNotIn(page['before_cursor'],cursors);cursors.add(page['before_cursor'])
            page=self.service.session_takes(self.principal,self.sid,limit=11,before_cursor=page['before_cursor'])
        self.assertEqual(len(seen),132);self.assertEqual(len({row['id'] for row in seen}),132)
        self.assertEqual({row['card_id'] for row in seen},{card['card']['id'] for card in cards})
        for card in cards:
            group=[row for row in seen if row['card_id']==card['card']['id']]
            self.assertEqual({row['group_number'] for row in group},{1,2})
            self.assertEqual(self.service.get_revision(self.principal,self.sid,card['revision']['id']),card['revision'])
        self.assertTrue(next(row for row in seen if row['id']==starred)['annotation']['starred'])
        self.assertTrue(all('recipe' not in row and 'job' not in row and row['status']=='draft' for row in seen))
        with self.repo.engine.connect() as conn:
            after={name:conn.scalar(select(func.count()).select_from(table)) for name,table in
                [('objects',objects),('jobs',jobs),('plans',plans),('documents',documents)]}
        self.assertEqual(before,after)

    def test_session_grid_cursor_and_authorization_do_not_cross_group_owner_or_session(self):
        card=self.card(copies=3)
        group=self.service.group_takes(self.principal,self.sid,card['card']['id'],limit=1)
        page=self.service.session_takes(self.principal,self.sid,limit=1)
        with self.assertRaises(QuickChatError):
            self.service.session_takes(self.principal,self.sid,before_cursor=group['before_cursor'])
        with self.assertRaises(QuickChatError):
            self.service.group_takes(self.principal,self.sid,card['card']['id'],before_cursor=page['before_cursor'])
        other=self.service.create_session(self.principal,{},'other-grid-session')['session']['id']
        with self.assertRaises(QuickChatError):
            self.service.session_takes(self.principal,other,before_cursor=page['before_cursor'])
        with self.assertRaises(NotFound):
            self.service.session_takes(Principal('other','browser'),self.sid)
        scoped=Principal(self.principal.owner,'scoped',machine=True,all_projects=False,
            project_ids=('some-other-story',),scopes=('projects:read','jobs:read'))
        with self.assertRaises(NotFound):self.service.session_takes(scoped,self.sid)
        for bad in (0,101,True,1.5):
            with self.assertRaises(QuickChatError):self.service.session_takes(self.principal,self.sid,limit=bad)

    def test_session_grid_reuses_original_job_status_but_only_returns_thumbnail_summary(self):
        card=self.card(copies=2);submitted=self.submit(card['revision'])
        original=self.service.get_submission
        def projected(*args):
            value=original(*args)
            for item in value['items']:
                item['job']['artifacts']=[{'id':'poster-video','kind':'video',
                    'thumbnail_url':'/v1/artifacts/poster-video/content?rendition=thumb',
                    'object_key':'private/internal/key','metadata':{'private':'not a grid field'}}]
            return value
        with patch.object(self.service,'get_submission',side_effect=projected):
            group=self.service.group_takes(self.principal,self.sid,card['card']['id'])
            page=self.service.session_takes(self.principal,self.sid)
        self.assertEqual({row['id']:row['status'] for row in page['takes']},
            {row['id']:row['status'] for row in group['takes']})
        self.assertEqual({row['submission_id'] for row in page['takes']},{submitted['id']})
        self.assertEqual(page['takes'][0]['artifacts'],[{'id':'poster-video','kind':'video',
            'thumbnail_url':'/v1/artifacts/poster-video/content?rendition=thumb'}])

    def test_target_cas_after_upload_preserves_asset_and_original_unknown(self):
        self.story();png=io.BytesIO();Image.new('RGB',(512,512),'red').save(png,'PNG');png.seek(0)
        uploaded=self.service.upload(self.principal,self.sid,png,'source.png','cas-source')
        value=self.service.save_card(self.principal,self.sid,{**DEFAULT_SETTINGS,'prompt':'Keep bytes',
            'inputs':{**copy.deepcopy(EMPTY_INPUTS),'first_frame':{'asset_id':uploaded['asset_id']}}},'cas-card')
        original_upload=self.app.state.assets.upload; copied=[]
        def concurrent_edit(*args,**kwargs):
            result=original_upload(*args,**kwargs);copied.append(result['asset_id'])
            scope=Scope(self.scope.tenant_id,self.scope.owner_id,'__projects')
            target=self.repo.get_document(scope,'project','target-story')
            self.repo.put_document(scope,'project','target-story',target['payload'],expected_version=target['version'])
            return result
        item=value['revision']['items'][0]['id']
        with patch.object(self.app.state.assets,'upload',side_effect=concurrent_edit):
            receipt=self.service.transfer_take(self.principal,self.sid,item,self.transfer_body(),'cas-transfer')
        self.assertEqual(receipt['status'],'blocked');self.assertTrue(receipt['no_shot_committed']);self.assertEqual(len(copied),1)
        self.assertEqual(self.app.state.assets.get(self.principal.owner,copied[0],'target-story')['status'],'ready')
        with patch.object(self.app.state.assets,'upload',side_effect=AssertionError('must not recopy')):
            replay=self.service.transfer_take(self.principal,self.sid,item,self.transfer_body(),'cas-transfer')
        self.assertEqual(receipt,replay)
        target=self.repo.get_document(Scope(self.scope.tenant_id,self.scope.owner_id,'__projects'),'project','target-story')
        self.assertFalse(any(e['id']==receipt['shot_id'] for e in target['payload']['entities']))
        fresh={**self.transfer_body(),'expected_project_version':target['version']}
        selected=self.service.transfer_take(self.principal,self.sid,item,fresh,'explicit-new-selection')
        self.assertEqual(selected['status'],'completed');self.assertNotEqual(selected['id'],receipt['id'])
        self.assertEqual(self.service.get_transfer(self.principal,self.sid,receipt['id'])['status'],'blocked')

    def test_preferences_parallel_cas_and_same_transfer_key_single_shot(self):
        def preferences(index):
            try:return self.service.set_preferences(self.principal,{'expected_version':0,'copies_max':5+index},'concurrent-'+str(index))
            except QuickChatError as error:return error.code
        results=self.parallel(preferences,2)
        self.assertEqual(sum(isinstance(v,dict) for v in results),1);self.assertIn('version_conflict',results)
        self.story();value=self.card();item=value['revision']['items'][0]['id']
        results=self.parallel(lambda index:self.service.transfer_take(self.principal,self.sid,item,self.transfer_body(),'same-transfer'),2)
        self.assertEqual(len({r['id'] for r in results}),1)
        receipt=self.service.get_transfer(self.principal,self.sid,results[0]['id'])
        self.assertEqual(receipt['status'],'completed')
        target=self.repo.get_document(Scope(self.scope.tenant_id,self.scope.owner_id,'__projects'),'project','target-story')
        self.assertEqual(sum(e['type']=='shot' for e in target['payload']['entities']),1)

    def test_openapi_take_dtos_and_safe_strict_write_errors(self):
        from fastapi.testclient import TestClient
        value=self.card();item=value['revision']['items'][0]['id']
        with TestClient(self.app) as client:
            client.post('/api/auth/login',json={'username':self.principal.owner}).raise_for_status()
            schemas=client.get('/openapi.json').json()['components']['schemas']
            self.assertEqual(schemas['TakeAgainRequest']['properties']['copies']['type'],'integer')
            self.assertFalse(schemas['TakeAgainRequest']['additionalProperties'])
            for path,body in [('/v1/quick-chat/preferences',{'expected_version':'private-value','copies_max':6}),
                    ('/v1/quick-chat/sessions/'+self.sid+'/takes/'+item,{'expected_version':0,'starred':'private-value'})]:
                result=client.patch(path,json=body,headers={'Idempotency-Key':'invalid-key'})
                self.assertEqual(result.status_code,422);self.assertNotIn('private-value',result.text)
                self.assertEqual(result.json()['code'],'invalid_request')

    def test_target_change_does_not_close_unresolved_upload_journal(self):
        import json
        from sqlalchemy import update
        from studio_platform.storage_asset_journal import receipts
        self.story();png=io.BytesIO();Image.new('RGB',(512,512),'green').save(png,'PNG');png.seek(0)
        uploaded=self.service.upload(self.principal,self.sid,png,'source.png','unknown-source')
        value=self.service.save_card(self.principal,self.sid,{**DEFAULT_SETTINGS,'prompt':'Preserve unknown',
            'inputs':{**copy.deepcopy(EMPTY_INPUTS),'first_frame':{'asset_id':uploaded['asset_id']}}},'unknown-card')
        original_upload=self.app.state.assets.upload
        def uncertain(*args,**kwargs):
            result=original_upload(*args,**kwargs)
            with self.repo.transaction() as conn:
                row=conn.execute(select(receipts).where(receipts.c.id==result['asset_id'])).mappings().one()
                record=json.loads(row['record']);record['asset']['status']='unknown'
                conn.execute(update(receipts).where(receipts.c.id==result['asset_id']).values(record=json.dumps(record)))
            scope=Scope(self.scope.tenant_id,self.scope.owner_id,'__projects')
            target=self.repo.get_document(scope,'project','target-story')
            self.repo.put_document(scope,'project','target-story',target['payload'],expected_version=target['version'])
            raise RuntimeError('upload acknowledgement lost')
        item=value['revision']['items'][0]['id']
        with patch.object(self.app.state.assets,'upload',side_effect=uncertain):
            receipt=self.service.transfer_take(self.principal,self.sid,item,self.transfer_body(),'unknown-upload')
        replay=self.service.transfer_take(self.principal,self.sid,item,self.transfer_body(),'unknown-upload')
        self.assertEqual(receipt['id'],replay['id']);self.assertEqual(replay['status'],'unknown')
        self.assertFalse(replay['no_shot_committed'])
