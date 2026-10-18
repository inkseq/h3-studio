import io
import json
import unittest

from studio_platform.targon_runtime_aws import AwsTargonLoader, stdin_targon_loader

ARN='arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:/sixnine/platform/targon-abc123'
VERSION='12345678-1234-1234-1234-123456789012'


def envelope():
    return {'secret_arn':ARN,'version_id':VERSION,'payload':{'schema_version':1,'service':'targon',
        'profile':'targon--rig-root','base_url':'https://api.targon.com',
        'primary_key_variable':'TARGON_API_KEY','api_key':'fake-offline-only'}}


class RuntimeTests(unittest.TestCase):
    def test_pinned_identity_inert_then_memory_only(self):
        calls=[]
        loader=AwsTargonLoader(ARN,VERSION,client_factory=lambda:calls.append('bad'))
        self.assertEqual([],calls)
        loader=stdin_targon_loader(ARN,VERSION,io.BytesIO(json.dumps(envelope()).encode()))
        value=loader('targon',profile='targon--rig-root')
        self.assertEqual(value.base_url,'https://api.targon.com')
        self.assertNotIn('fake-offline-only',repr(value))
        with self.assertRaises(ValueError):loader('lium',profile='lium--rig-root')

    def test_no_identity_or_endpoint_fallback(self):
        for key,value in [('base_url','https://example.org'),('profile','targon--other'),('service','lium')]:
            e=envelope();e['payload'][key]=value
            with self.assertRaises(ValueError):stdin_targon_loader(ARN,VERSION,io.StringIO(json.dumps(e)))
        with self.assertRaises(ValueError):AwsTargonLoader(ARN.replace('/targon-','/lium-'),VERSION)

    def test_duplicate_envelope_keys_rejected(self):
        raw=json.dumps(envelope()).replace('"payload":','"version_id":"other", "payload":')
        with self.assertRaises(ValueError):stdin_targon_loader(ARN,VERSION,io.StringIO(raw))


if __name__=='__main__':unittest.main()
