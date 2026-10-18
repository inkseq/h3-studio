"""CPU release safety, isolated temporary DB, no GPU or provider request."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from fastapi.testclient import TestClient


class CpuDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='h3-deploy-test-')
        root = Path(self.temp.name)
        self.state = root / 'cloud-state.json'
        self.state.write_text(json.dumps({'phase':'destroyed','status':'DESTROYED'}))
        spec = importlib.util.spec_from_file_location('_h3_deploy_test', (Path(__file__).resolve().parents[1] / 'server.py'))
        self.server = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {
            'H3_STUDIO_DATA': str(root / 'data'),
            'H3_GENERATION_ENABLED':'0', 'H3_PUBLIC_ORIGIN':'https://studio.example.test',
            'H3_CLOUD_STATE':str(self.state), 'H3_RELEASE':'test-release',
        }):
            spec.loader.exec_module(self.server)
        self.network = mock.patch('httpx.HTTPTransport.handle_request', side_effect=AssertionError('No network'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.temp.cleanup()

    def test_cpu_lifespan_health_and_capabilities_do_not_start_worker_or_probe_gpu(self):
        with mock.patch.object(self.server, 'worker') as worker, mock.patch.object(self.server, 'comfy_info') as probe:
            with TestClient(self.server.app, base_url='https://studio.example.test') as client:
                health = client.get('/healthz')
                self.assertEqual(health.json()['status'],'ok')
                self.assertEqual(health.json()['release'],'test-release')
                self.assertFalse(health.json()['generation_enabled'])
                login = client.post('/api/auth/login', json={'username':'superdan'})
                self.assertIn('Secure', login.headers['set-cookie'])
                caps = client.get('/api/capabilities').json()
                self.assertFalse(caps['backends'][0]['available'])
                self.assertIsNone(caps['expert_url'])
                self.assertTrue(caps['lease']['generation_disabled'])
                self.assertEqual(caps['lease']['status'], 'DESTROYED')
                job = client.post('/api/jobs', json={
                    'backend':'comfy-local','model':self.server.MODEL,'mode':'fl',
                    'prompt':'Offline CPU deployment test','duration':5,'resolution':'768P',
                    'aspect_ratio':'16:9','generate_audio':True,'seed':42,'inputs':{},
                })
                self.assertEqual(job.status_code,503)
                self.assertEqual(client.get('/api/jobs').json(), {'jobs':[]})
            worker.assert_not_called()
            probe.assert_not_called()

    def test_health_checks_database_and_does_not_require_account(self):
        client = TestClient(self.server.app)
        self.assertEqual(client.get('/healthz').status_code,200)
        with self.server.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0],0)
        with mock.patch.object(self.server,'db',side_effect=self.server.sqlite3.OperationalError('private diagnostic')):
            response = client.get('/healthz')
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json(), {'status':'unavailable'})
        client.close()

    def test_cpu_start_does_not_recover_or_mutate_existing_gpu_job(self):
        record = {'id':'existing','status':'running','owner':'superdan'}
        with self.server.db() as c:
            c.execute('INSERT INTO jobs(id,status,created,record,owner) VALUES(?,?,?,?,?)',
                ('existing','running',1,json.dumps(record),'superdan'))
        with TestClient(self.server.app):
            self.assertEqual(self.server.get_job('existing')['status'],'running')


if __name__ == '__main__':
    unittest.main()
