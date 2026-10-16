"""Offline durable transport, provenance, byte-integrity and permission boundaries."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from tools import native_source_retention as retention, restore_native_sources as restore
from tools.ci_changes import dispatch_action

ROOT = Path(__file__).resolve().parent


class DurableTransport(unittest.TestCase):
    def setUp(self):
        self.value = json.loads((ROOT / 'deploy/dstack/native-retention.json').read_text())
        self.inputs = dict(zip(retention.INPUTS, (self.value['run'], self.value['commit'], self.value['index_sha256'])))
        self.run = dict(id=int(self.value['run']), path=retention.WORKFLOW, event='workflow_dispatch',
            head_sha=self.value['commit'], head_branch='main', status='completed', conclusion='success',
            repository={'full_name':retention.REPOSITORY}, head_repository={'full_name':retention.REPOSITORY})
        self.jobs = {'total_count':1, 'jobs':[dict(name='publish-aws',run_id=self.run['id'],
            head_sha=self.run['head_sha'],status='completed',conclusion='success')]}
        self.raw = {'source-set.tar.gz':b'opaque public native bytes'}
        source_hash = hashlib.sha256(self.raw['source-set.tar.gz']).hexdigest()
        self.raw['migration-manifest.json'] = json.dumps({'version':1,'commit':self.value['commit'],
            'source_index_sha256':self.value['index_sha256'], 'files':{'source-set.tar.gz':source_hash}}).encode()
        for name, raw in self.raw.items():
            self.value['objects'][name].update(size=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name)/'restored'
        self.s3, self.sts = Mock(), Mock()
        self.sts.get_caller_identity.return_value = {'Account':restore.ACCOUNT}
        self.s3.get_object.side_effect = self.response

    def response(self, **request):
        name = request['Key'].rsplit('/',1)[1];item=self.value['objects'][name]
        self.assertEqual(request,{'Bucket':restore.BUCKET,'Key':restore.key(self.value,name),'VersionId':item['version_id']})
        return dict(Body=io.BytesIO(self.raw[name]),ContentLength=item['size'],VersionId=item['version_id'],ServerSideEncryption='AES256')

    def run_restore(self):
        return restore.restore(self.value,self.inputs,self.run,self.jobs,self.destination,s3=self.s3,sts=self.sts)

    def test_exact_versioned_bytes_restore_without_github_archive(self):
        self.run_restore()
        self.assertEqual({p.name:p.read_bytes() for p in self.destination.iterdir()},self.raw)
        self.assertEqual(self.s3.get_object.call_count,2)
        artifact={'total_count':1,'artifacts':[{'name':'sixnine-migration-'+self.value['commit'],
            'id':1,'expired':True,'workflow_run':{'id':self.run['id'],'head_sha':self.run['head_sha'],'head_branch':'main'}}]}
        with self.assertRaises(ValueError):retention.verify_run(self.run,artifact,self.inputs)

    def test_missing_failed_or_other_publication_rejected_before_s3(self):
        for field,new in [('run_id',1),('head_sha','a'*40),('status','in_progress'),('conclusion','failure')]:
            jobs=copy.deepcopy(self.jobs);jobs['jobs'][0][field]=new
            with self.subTest(field=field),self.assertRaises(ValueError):restore.verify_publication(self.run,jobs,self.inputs)
        for jobs in ({'total_count':2,'jobs':self.jobs['jobs']},{'total_count':0,'jobs':[]},
                {'total_count':2,'jobs':self.jobs['jobs']*2}):
            with self.assertRaises(ValueError):restore.verify_publication(self.run,jobs,self.inputs)
        for field,new in [('event','pull_request'),('head_branch','topic'),('conclusion','failure'),('head_sha','a'*40)]:
            with self.assertRaises(ValueError):restore.verify_publication(dict(self.run,**{field:new}),self.jobs,self.inputs)
        self.s3.get_object.assert_not_called()

    def test_actual_hash_overrun_truncation_and_metadata_fail_closed(self):
        for mode in ['hash','overrun','truncated','version','size','encryption']:
            self.destination=Path(self.temp.name)/mode
            def response(mode=mode,**request):
                r=self.response(**request)
                if mode in ['hash','overrun','truncated']:
                    raw=self.raw[request['Key'].rsplit('/',1)[1]]
                    r['Body']=io.BytesIO((b'x'+raw[1:]) if mode=='hash' else raw+b'x' if mode=='overrun' else raw[:-1])
                else:r[{'version':'VersionId','size':'ContentLength','encryption':'ServerSideEncryption'}[mode]]='wrong'
                return r
            self.s3.get_object.side_effect=response
            with self.subTest(mode=mode),self.assertRaises(ValueError):self.run_restore()

    def test_account_and_existing_destination_do_not_read_s3(self):
        self.sts.get_caller_identity.return_value={'Account':'000000000000'}
        with self.assertRaises(ValueError):self.run_restore()
        self.sts.get_caller_identity.return_value={'Account':restore.ACCOUNT}
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):self.run_restore()
        self.s3.get_object.assert_not_called()

    def test_binding_is_closed_and_policy_only_grants_exact_versions(self):
        original=json.loads((ROOT/'deploy/dstack/native-retention.json').read_text())
        self.assertEqual(restore.validate_binding(original,self.inputs),original)
        for change in ({'bucket':'elsewhere'},{'commit':'a'*40},{'run':'1'},{'extra':True}):
            with self.assertRaises(ValueError):restore.validate_binding(dict(original,**change),self.inputs)
        for invalid in ('null','../path',''):
            value=copy.deepcopy(original);value['objects']['source-set.tar.gz']['version_id']=invalid
            with self.assertRaises(ValueError):restore.validate_binding(value,self.inputs)
        policy=restore.session_policy(original)
        self.assertEqual(len(policy['Statement']),2)
        for statement in policy['Statement']:
            self.assertEqual(statement['Action'],'s3:GetObjectVersion')
            self.assertNotIn('*',statement['Resource'])
            self.assertEqual(set(statement['Condition']['StringEquals']),{'s3:VersionId'})

    def test_manifest_must_link_the_original_source_archive(self):
        manifest=json.loads(self.raw['migration-manifest.json']);manifest['source_index_sha256']='a'*64
        self.raw['migration-manifest.json']=json.dumps(manifest).encode()
        raw=self.raw['migration-manifest.json']
        self.value['objects']['migration-manifest.json'].update(size=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaises(ValueError):self.run_restore()

    def test_s3_dispatch_is_explicit_and_main_only(self):
        inputs=dict(self.inputs,deploy=True,release_kind='platform',retain_native_sources_transport='s3')
        self.assertEqual(dispatch_action(inputs,'refs/heads/main'),'prepare-platform')
        for change in ({'deploy':False},{'release_kind':'frontend'},{'retain_native_sources_run':''},
                {'retain_native_sources_transport':'latest'}):
            with self.assertRaises(ValueError):dispatch_action(dict(inputs,**change),'refs/heads/main')
        with self.assertRaises(ValueError):dispatch_action(inputs,'refs/heads/topic')

    def test_workflow_does_not_give_container_or_pr_jobs_aws_credentials(self):
        workflow=(ROOT/'.github/workflows/ci.yml').read_text()
        job=workflow.split('\n  restore-native:\n')[1].split('\n  containers:\n')[0]
        self.assertIn("github.ref == 'refs/heads/main'",job)
        self.assertIn("github.event_name == 'push' || github.event_name == 'workflow_dispatch'",job)
        self.assertIn('inline-session-policy: ${{ steps.binding.outputs.policy }}',job)
        self.assertNotIn('pull_request',job)
        containers=workflow.split('\n  containers:\n')[1].split('\n  test:\n')[0]
        self.assertNotIn('id-token:',containers)
        self.assertNotIn('configure-aws-credentials',containers)
        self.assertIn('needs: [changes, restore-native]',containers)
        self.assertIn("needs.restore-native.result == 'success' || needs.restore-native.result == 'skipped'",containers)


if __name__=='__main__':unittest.main()
