"""Real first-frame bytes plus original receipt/recovery fences, offline only."""
import hashlib
from contextlib import closing
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from PIL import Image
from sqlalchemy import select, update, delete

from studio_platform import media
from studio_platform.artifact_renditions import ensure
from studio_platform.artifact_writer import ArtifactWriter, ArtifactWritePending
from studio_platform.assets import AssetService
from studio_platform.generation_services import GenerationRead
from studio_platform.media_renditions import POSTER_PREFIX_BYTES, public_artifact_renditions
from studio_platform.queue import TaskQueue
from studio_platform.repository import artifacts
from studio_platform.storage import LocalObjectStore, S3ObjectStore, IntegrityError
from studio_platform.storage_asset_journal import AssetQuotaExceeded
import test_platform_repository as ledger_fixture
import test_platform_api as api_fixture
from test_platform_assets import FaultStore
from test_platform_storage import config
from test_platform_storage_multipart import FakeMultipartS3, ProcessStopped


def video(folder):
    path = Path(folder) / "fixture.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
        "color=c=blue:size=640x360:rate=24", "-t", "2", "-an", "-c:v", "libx264", "-threads", "1",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)],
        check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path.read_bytes()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class PosterWriterTests(ledger_fixture.LedgerCase):
    def setUp(self):
        super().setUp()
        self.work = Path(self.temp.name) / "work"
        self.store = FaultStore(Path(self.temp.name)/"objects")
        self.writer = ArtifactWriter(self.repo.engine,self.store,self.work,tenant=self.scope.tenant_id)
        self.bytes = video(self.temp.name)

    def settled(self):
        self.job(cost=0)
        queue = TaskQueue(self.repo)
        lease = queue.claim("worker", "test-pool").lease
        queue.begin_submission(lease);queue.record_submitted(lease,"task")
        job = queue.begin_collection(lease)
        tag = "attempt-"+uuid.uuid4().hex
        directory = self.work/tag;directory.mkdir(parents=True)
        path = directory/"verified.mp4";path.write_bytes(self.bytes)
        record = self.writer.prepare(job,lease.attempt_id,tag,[("video",path,"video/mp4",{})])
        specs = self.writer.write(record)
        queue.complete(lease,specs,actual_cost_microusd=0,settlement=self.writer.settlement(record))
        return job,lease.attempt_id,tag

    def row(self,job):
        return self.repo.list_artifacts(self.scope,job["id"])[0]

    def test_real_posters_same_artifact_receipt_frozen_roles_quota_and_public_urls(self):
        job,attempt,tag = self.settled();before=self.writer.quota.usage(self.scope.owner_id)["accounted_bytes"]
        original=self.row(job)
        record=ensure(self.writer,job,attempt,tag)
        row=self.row(job);public=GenerationRead.artifact(row)
        self.assertEqual(row['id'],original['id']);self.assertEqual(set(record['roles']),{'video'})
        self.assertEqual(row['metadata']['sha256'],hashlib.sha256(self.bytes).hexdigest())
        self.assertEqual(record['display']['phase'],'complete')
        self.assertNotIn('display_renditions',public['metadata'])
        self.assertNotIn('owners/',json.dumps(public));self.assertTrue(public['rendition_ready'])
        added=0
        for role,edge in (('thumb',320),('preview',1280)):
            spec=record['display']['objects'][role];added+=spec['size_bytes']
            with self.store.open(spec['key']) as stream:
                body=stream.read()
            self.assertEqual(hashlib.sha256(body).hexdigest(),spec['sha256'])
            with Image.open(io.BytesIO(body)) as image:
                self.assertLessEqual(max(image.size),edge);self.assertEqual(image.format,'WEBP')
                self.assertGreater(image.getpixel((0,0))[2],150)
        self.assertEqual(self.writer.quota.usage(self.scope.owner_id)['accounted_bytes'],before+added)
        self.assertFalse((self.work/tag/'display-v1').exists())
        self.assertEqual(ensure(self.writer,job,attempt,tag)['display']['phase'],'complete')
        self.assertEqual(self.writer.quota.usage(self.scope.owner_id)['accounted_bytes'],before+added)

    def test_unknown_put_reuses_same_frozen_objects_and_requires_exact_stopped_writer(self):
        job,attempt,tag=self.settled();self.store.failure='after'
        with self.assertRaises(ArtifactWritePending):ensure(self.writer,job,attempt,tag)
        record=self.writer.get(job,attempt,tag);frozen=dict(record['display']['objects']);key=frozen['thumb']['key']
        with self.assertRaises(ArtifactWritePending):ensure(self.writer,job,attempt,tag)
        with self.assertRaises(ArtifactWritePending):ensure(self.writer,job,attempt,tag,interrupted=True,expected_version=record['version']-1)
        recovered=ensure(self.writer,job,attempt,tag,interrupted=True,expected_version=record['version'])
        self.assertEqual({k:v['sha256'] for k,v in recovered['display']['objects'].items()},
            {k:v['sha256'] for k,v in frozen.items()})
        self.assertEqual(self.store.calls.count(key),1)

    def test_bad_prefix_is_missing_without_failing_original_or_decoding_full_video(self):
        job,attempt,tag=self.settled()
        with mock.patch('studio_platform.artifact_renditions.create_video_renditions',side_effect=media.MediaError('bounded failure')):
            record=ensure(self.writer,job,attempt,tag)
        self.assertEqual(record['display']['phase'],'missing')
        self.assertEqual(self.repo.get_job(self.scope,job['id'])['status'],'succeeded')
        self.assertEqual(self.row(job)['metadata']['rendition_status'],'missing')
        self.assertFalse(public_artifact_renditions(self.row(job))['rendition_ready'])
        self.assertFalse((self.work/tag/'display-v1').exists())
        self.assertEqual(record['display']['reserved_bytes'],0)
        self.assertEqual(ensure(self.writer,job,attempt,tag)['display']['phase'],'complete')

    def test_quota_rejects_before_extra_file_or_write_and_original_is_unchanged(self):
        job,attempt,tag=self.settled();before=list(self.store.calls)
        limited=ArtifactWriter(self.repo.engine,self.store,self.work,tenant=self.scope.tenant_id,
            owner_quota_bytes=1000*1024)
        with self.assertRaises(AssetQuotaExceeded):ensure(limited,job,attempt,tag)
        self.assertEqual(self.store.calls,before)
        self.assertFalse((self.work/tag/'display-v1').exists())
        self.assertEqual(self.repo.get_job(self.scope,job['id'])['status'],'succeeded')

    def test_prefix_capture_hashes_all_bytes_but_retains_only_bound(self):
        data=b'x'*(POSTER_PREFIX_BYTES+12345);key='owners/superdan/assets/test/result.mp4'
        self.store.put(key,io.BytesIO(data),content_type='video/mp4',max_bytes=len(data))
        role=dict(key=key,size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),content_type='video/mp4')
        prefix=io.BytesIO();self.writer._verify_remote(role,lambda:None,prefix=prefix)
        self.assertEqual(len(prefix.getvalue()),POSTER_PREFIX_BYTES)
        with self.assertRaises(IntegrityError):
            self.writer._verify_remote(dict(role,sha256='0'*64),lambda:None,prefix=io.BytesIO())

    def test_r2_original_scratch_removed_and_cached_verification_prefix_avoids_another_original_read(self):
        fake=FakeMultipartS3(config());self.store=S3ObjectStore(config(),client=fake)
        self.writer=ArtifactWriter(self.repo.engine,self.store,self.work,tenant=self.scope.tenant_id)
        job,attempt,tag=self.settled();self.writer.cleanup_settled(job,attempt,tag)
        self.assertFalse((self.work/tag).exists())
        original=self.writer.get(job,attempt,tag)['roles']['video']
        captured=io.BytesIO();self.writer._verify_remote(original,lambda:None,prefix=captured)
        self.writer._poster_prefix={(original['key'],original['sha256']):captured.getvalue()}
        old_open=self.store.open
        def open_without_original(key):
            self.assertNotEqual(key,original['key'],'reuse the already fully verified prefix')
            return old_open(key)
        with mock.patch.object(self.store,'open',side_effect=open_without_original):
            result=ensure(self.writer,job,attempt,tag)
        self.assertEqual(result['display']['phase'],'complete');self.assertFalse((self.work/tag).exists())
        self.assertEqual(set(result['roles']),{'video'})
        self.assertEqual(self.writer.quota.usage(self.scope.owner_id)['accounted_bytes'],
            len(self.bytes)+sum(v['size_bytes'] for v in result['display']['objects'].values()))

    def test_crash_after_display_scratch_unlink_replays_counter_once_without_put(self):
        job,attempt,tag=self.settled()
        from studio_platform import artifact_renditions as module
        original=module._resize_reservation
        def crash(writer,record,size):
            if record['display'].get('phase')=='complete' and record['display'].get('scratch_cleanup',{}).get('phase')=='complete':
                raise ProcessStopped()
            return original(writer,record,size)
        with mock.patch.object(module,'_resize_reservation',side_effect=crash):
            with self.assertRaises(ProcessStopped):ensure(self.writer,job,attempt,tag)
        record=self.writer.get(job,attempt,tag);before=self.writer.quota.usage(self.scope.owner_id)['accounted_bytes']
        self.assertEqual(record['display']['phase'],'complete')
        self.assertEqual(record['display']['scratch_cleanup']['phase'],'pending')
        calls=list(self.store.calls);released=record['display']['scratch_cleanup']['bytes']
        ensure(self.writer,job,attempt,tag)
        self.assertEqual(self.store.calls,calls)
        self.assertEqual(self.writer.quota.usage(self.scope.owner_id)['accounted_bytes'],before-released)
        ensure(self.writer,job,attempt,tag)
        self.assertEqual(self.writer.quota.usage(self.scope.owner_id)['accounted_bytes'],before-released)

    def test_first_frame_processing_refuses_hardlinked_scratch_and_foreign_receipt(self):
        job,attempt,tag=self.settled()
        folder=self.work/tag/'display-v1';folder.mkdir()
        source=folder/'poster-frame.png';source.write_bytes(b'keep')
        import os
        os.link(source,Path(self.temp.name)/'linked.png')
        with self.assertRaises(IntegrityError):ensure(self.writer,job,attempt,tag)
        self.assertEqual(source.read_bytes(),b'keep')
        with self.assertRaises(ArtifactWritePending):ensure(self.writer,dict(job,owner_id='other'),attempt,tag)

    def test_backup_and_explicit_cleanup_include_exact_auxiliary_objects(self):
        job,attempt,tag=self.settled();record=ensure(self.writer,job,attempt,tag)
        import sqlite3
        from studio_platform.backup import _media_references
        with closing(sqlite3.connect(self.repo.engine.url.database)) as connection:
            found,pending=_media_references(connection)
        expected={role['key'] for role in (*record['roles'].values(),*record['display']['objects'].values())}
        self.assertEqual(set(found),expected);self.assertEqual(pending,0)
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        app=create_app(Settings(Path(self.temp.name)/'api',tenant_id=self.scope.tenant_id,auth_mode='local-test',database_url=self.url),repository=self.repo,storage=self.store)
        service=app.state.quick_chat
        from studio_platform.auth import Principal
        principal=Principal(self.scope.owner_id,self.scope.owner_id,all_projects=True)
        from studio_platform.content_references import cleanup_plan
        with self.repo.engine.connect() as conn:
            result=cleanup_plan(conn,service,principal,[],media_targets={'asset_ids':set(),'artifact_ids':{self.row(job)['id']}})
        self.assertEqual({item['object_key'] for item in result['objects']},expected)
        from studio_platform.content_lifecycle import put_tombstone
        before=service.assets.usage(self.scope.owner_id)['accounted_bytes']
        with self.repo.engine.begin() as conn:
            for item in result['objects']:
                put_tombstone(conn,self.scope.tenant_id,self.scope.owner_id,kind='key',value=item['object_key'],
                    action_id='owned-offline-purge',now=self.now,storage_binding=self.writer.binding)
            released=service.assets.finalize_purge(conn,self.scope.owner_id,asset_ids=[],
                artifact_specs=result['objects'],now=self.now)
        self.assertEqual(released['released_bytes'],sum(item['size_bytes'] for item in result['objects']))
        self.assertEqual(service.assets.usage(self.scope.owner_id)['accounted_bytes'],before-released['released_bytes'])

    def test_metadata_only_plan_reports_whole_owner_coverage_and_original_receipt_version(self):
        job,attempt,tag=self.settled()
        from studio_platform.rendition_backfill import plan
        before=plan(self.repo.engine,tenant=self.scope.tenant_id,owner=self.scope.owner_id,limit=1)
        self.assertEqual(before['coverage']['artifacts']['backfill_required'],1)
        ensure(self.writer,job,attempt,tag)
        after=plan(self.repo.engine,tenant=self.scope.tenant_id,owner=self.scope.owner_id,limit=1)
        self.assertEqual(after['coverage']['artifacts']['ready'],1)
        with self.repo.engine.begin() as conn:
            from studio_platform.artifact_writer import write_receipts
            conn.execute(delete(write_receipts))
        with mock.patch.object(self.store,'stat',side_effect=AssertionError('metadata-only census')):
            foreign=plan(self.repo.engine,tenant=self.scope.tenant_id,owner='other')
        self.assertEqual(foreign['coverage']['artifacts']['total'],0)

    def test_asset_video_backfill_and_legacy_coverage_keep_original_receipt_and_bytes(self):
        service=AssetService(self.repo.engine,self.store,Path(self.temp.name)/'assets',tenant=self.scope.tenant_id)
        with mock.patch.object(service,'_prepare_renditions',return_value={}):
            asset=service.upload(self.scope.owner_id,self.scope.project_id,io.BytesIO(self.bytes),'movie.mp4',client_asset_id='video-one')
        original=service.get(self.scope.owner_id,asset['id'])
        from studio_platform.rendition_backfill import plan
        self.assertEqual(plan(self.repo.engine,tenant=self.scope.tenant_id,owner=self.scope.owner_id)['coverage']['assets']['backfill_required'],1)
        receipt=service.journal.get(self.scope.owner_id,asset['id'])
        value=service.ensure_renditions(self.scope.owner_id,asset['id'],expected_version=receipt['version'])
        self.assertTrue(value['rendition_ready'])
        self.assertEqual(service.get(self.scope.owner_id,asset['id'])['original'],original['original'])
        self.assertEqual(service.get(self.scope.owner_id,asset['id'])['model'],original['model'])
        with self.repo.engine.begin() as conn:
            from studio_platform.storage_asset_journal import receipts
            from studio_platform.assets import asset_table
            conn.execute(delete(receipts).where(receipts.c.id==asset['id']))
            raw=dict(original);raw.pop('thumb',None);raw.pop('preview',None);raw.pop('rendition_metadata',None)
            conn.execute(update(asset_table).where(asset_table.c.id==asset['id']).values(record=json.dumps(raw)))
        with mock.patch.object(self.store,'open',side_effect=AssertionError('coverage is metadata-only')):
            report=plan(self.repo.engine,tenant=self.scope.tenant_id,owner=self.scope.owner_id,limit=1)
        self.assertEqual(report['coverage']['assets']['missing_original_receipt'],1)

    def test_original_receipt_cas_prevents_parallel_display_writers_before_io(self):
        job,attempt,tag=self.settled()
        barrier=__import__('threading').Barrier(2)
        original=self.writer.get
        def synchronized(*args,**kwargs):
            value=original(*args,**kwargs);barrier.wait(timeout=3);return value
        from concurrent.futures import ThreadPoolExecutor
        def run():
            try:return ensure(self.writer,job,attempt,tag)['display']['phase']
            except ArtifactWritePending:return 'blocked'
        with mock.patch.object(self.writer,'get',side_effect=synchronized),ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(sorted(outcomes),['blocked','complete'])
        self.assertEqual(len(self.repo.list_artifacts(self.scope,job['id'])),1)

    def test_missing_retry_claim_blocks_second_retry_before_files_and_retains_exact_quota(self):
        job,attempt,tag=self.settled()
        from studio_platform import artifact_renditions as module
        with mock.patch.object(module,'create_video_renditions',side_effect=media.MediaError('bounded first-frame failure')):
            missing=ensure(self.writer,job,attempt,tag)
        self.assertEqual(missing['display']['phase'],'missing')
        self.assertEqual(missing['display']['scratch_cleanup']['phase'],'complete')
        baseline=self.writer.quota.usage(self.scope.owner_id)['accounted_bytes']
        original_save=self.writer._save
        import threading
        claimed,release=threading.Event(),threading.Event()
        def held_claim(record,**kwargs):
            original_save(record,**kwargs)
            if record['display']['phase']=='planned' and not claimed.is_set():
                claimed.set()
                if not release.wait(5):raise AssertionError('test retry was not released')
        from concurrent.futures import ThreadPoolExecutor
        with mock.patch.object(self.writer,'_save',side_effect=held_claim),ThreadPoolExecutor(max_workers=1) as pool:
            first=pool.submit(ensure,self.writer,job,attempt,tag)
            try:
                self.assertTrue(claimed.wait(5))
                held=self.writer.get(job,attempt,tag)
                self.assertEqual(held['display']['phase'],'planned')
                self.assertNotIn('scratch_cleanup',held['display'])
                self.assertNotIn('error_code',held['display'])
                self.assertFalse((self.work/tag/'display-v1').exists())
                with (mock.patch.object(module,'cleanup',side_effect=AssertionError('second retry must not clean')),
                        mock.patch.object(self.store,'open',side_effect=AssertionError('second retry must not read')),
                        mock.patch.object(self.store,'put',side_effect=AssertionError('second retry must not write'))):
                    with self.assertRaisesRegex(ArtifactWritePending,'artifact_poster_writer_unconfirmed'):
                        ensure(self.writer,job,attempt,tag)
                self.assertEqual(self.writer.quota.usage(self.scope.owner_id)['accounted_bytes'],baseline)
                self.assertEqual(self.writer.get(job,attempt,tag)['version'],held['version'])
            finally:
                release.set()
            completed=first.result(timeout=10)
        self.assertEqual(completed['display']['phase'],'complete')
        self.assertFalse((self.work/tag/'display-v1').exists())
        self.assertEqual(self.writer.quota.usage(self.scope.owner_id)['accounted_bytes'],baseline+
            sum(role['size_bytes'] for role in completed['display']['objects'].values()))
        self.assertEqual(self.repo.get_job(self.scope,job['id'])['status'],'succeeded')


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class PosterApiTests(unittest.TestCase):
    setUp=api_fixture.ApiTests.setUp
    login=api_fixture.ApiTests.login
    setup_project=api_fixture.ApiTests.setup_project

    def artifact(self):
        self.setup_project();repo=self.app.state.repository
        from studio_platform.repository import Scope
        scope=Scope(self.settings.tenant_id,'superdan','story-one')
        plan=repo.create_plan(scope,{'prompt':'offline fixture'},{'pool':'test-pool'},
            expires_at=9999999999,estimated_cost_microusd=0)
        job=repo.create_job(scope,plan['id'],'offline-output')
        queue=TaskQueue(repo);lease=queue.claim('worker','test-pool').lease
        queue.begin_submission(lease);queue.record_submitted(lease,'task');job=queue.begin_collection(lease)
        work=Path(self.tmp.name)/'collector';tag='attempt-'+uuid.uuid4().hex
        folder=work/tag;folder.mkdir(parents=True);path=folder/'verified.mp4';path.write_bytes(video(self.tmp.name))
        writer=ArtifactWriter(repo.engine,self.app.state.storage,work,tenant=self.settings.tenant_id)
        record=writer.prepare(job,lease.attempt_id,tag,[('video',path,'video/mp4',{})]);specs=writer.write(record)
        queue.complete(lease,specs,actual_cost_microusd=0,settlement=writer.settlement(record))
        return scope,job,lease,tag,writer

    def test_video_upload_display_fences_hash_bytes_and_no_original_fallback(self):
        self.setup_project();body=video(self.tmp.name)
        response=self.client.post('/v1/assets',data={'client_project_id':'story-one'},
            files={'file':('video.mp4',body,'video/mp4')})
        self.assertEqual(response.status_code,201,response.text)
        value=response.json();self.assertTrue(value['rendition_ready'])
        reply=self.client.get(value['thumbnail_url']);self.assertEqual(reply.status_code,200)
        self.assertEqual(reply.headers['content-type'],'image/webp')
        self.assertEqual(reply.headers['cache-control'],'no-store')
        self.assertEqual(self.client.get(value['content_url']).content,body)
        self.assertEqual(self.client.get(value['thumbnail_url']+'&expected_account=supervan').status_code,409)
        self.login('supervan');self.assertEqual(self.client.get(value['thumbnail_url']).status_code,404)

    def test_cross_story_video_import_creates_posters_on_original_import_upload_receipt(self):
        self.setup_project();body=video(self.tmp.name)
        original=self.client.post('/v1/assets',data={'client_project_id':'story-one'},
            files={'file':('movie.mp4',body,'video/mp4')}).json()
        self.client.post('/v1/projects',json={'id':'target','title':'Target'},headers={'Idempotency-Key':'new-target'}).raise_for_status()
        version=self.client.get('/v1/projects/target').json()['version']
        request={'source':{'project_id':'story-one','kind':'asset','id':original['id']},
            'target_project_id':'target','expected_project_version':version}
        one=self.client.post('/v1/library/imports',json=request,headers={'Idempotency-Key':'same-video-import'})
        self.assertEqual(one.status_code,200,one.text);result=one.json()
        self.assertEqual(result['state'],'completed');self.assertEqual(len(result['asset_ids']),1)
        new_id=result['asset_ids'][0];service=self.app.state.assets
        copied=service.get('superdan',new_id,'target')
        self.assertEqual(copied['kind'],'video');self.assertEqual(copied['original']['sha256'],hashlib.sha256(body).hexdigest())
        self.assertTrue(service.public(copied)['rendition_ready'])
        self.assertEqual(service.journal.get('superdan',new_id)['asset'],copied)
        repeated=self.client.post('/v1/library/imports',json=request,headers={'Idempotency-Key':'same-video-import'})
        self.assertEqual(repeated.json(),result)
        self.assertEqual(len(service.list('superdan','target')),1)

    def test_artifact_posters_original_owner_project_account_and_deleted_object_gate(self):
        scope,job,lease,tag,writer=self.artifact()
        missing=self.app.state.repository.list_artifacts(scope,job['id'])[0]
        with mock.patch('studio_platform.artifact_renditions.ensure',side_effect=AssertionError('GET never creates')):
            response=self.client.get(f'/v1/artifacts/{missing["id"]}/content?rendition=thumb')
        self.assertEqual(response.status_code,404)
        record=ensure(writer,job,lease.attempt_id,tag)
        public=self.client.get(f'/v1/jobs/{job["id"]}').json()['artifacts'][0]
        self.assertTrue(public['rendition_ready']);self.assertNotIn('owners/',json.dumps(public))
        url=public['thumbnail_url'];reply=self.client.get(url)
        self.assertEqual(reply.status_code,200);self.assertEqual(reply.headers['cache-control'],'no-store')
        self.assertEqual(reply.headers['x-authenticated-account'],'superdan')
        self.assertEqual(self.client.head(url).headers['cache-control'],'no-store')
        self.assertEqual(self.client.get(url+'&expected_account=supervan').status_code,409)
        self.assertEqual(self.client.get(url+'&rendition=preview').status_code,400)
        self.assertEqual(self.client.get(url.replace('thumb','original')).status_code,400)
        library=self.client.get('/v1/library?type=video').json()['items']
        self.assertEqual(next(v for v in library if v['artifact_id']==public['id'])['thumbnail_url'],url)
        self.login('supervan');self.assertEqual(self.client.get(url).status_code,404)
        self.login('superdan')
        from studio_platform.content_lifecycle import put_tombstone
        with writer.engine.begin() as conn:
            put_tombstone(conn,scope.tenant_id,scope.owner_id,kind='key',value=record['display']['objects']['thumb']['key'],
                action_id='offline-deletion-proof',now=1,storage_binding=writer.binding)
        self.assertEqual(self.client.get(url).status_code,409)
        self.assertEqual(self.client.get('/v1/library?type=video').json()['items'],[])
        with self.assertRaises(Exception):ensure(writer,job,lease.attempt_id,tag)
