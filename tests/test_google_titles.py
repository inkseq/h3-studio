"""Synthetic profile/file/HTTP checks; never use the live Registry."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from studio_platform.google_chat import ChatError, GoogleChatClient, ORIGIN, PROFILE
from studio_platform.google_titles import GoogleTitleGenerator, TITLE_MODEL, validate_config, runtime_config, configured_generator
from studio_platform.google_title_config import GoogleTitleConfigError
from studio_platform.settings import Settings

FAKE = {"enabled": True, "service": "gemini", "profile": PROFILE, "base_url": ORIGIN,
        "api_key": "synthetic-private-google-key-only"}


class GoogleTitleTests(unittest.TestCase):
    def test_exact_model_short_request_no_media_and_secret_header_only(self):
        requests = []
        def handle(request):
            requests.append(request)
            self.assertEqual(request.headers['x-goog-api-key'], FAKE['api_key'])
            self.assertNotIn(FAKE['api_key'], str(request.url))
            body = json.loads(request.content)
            self.assertEqual(body['generationConfig']['thinkingConfig'], {'thinkingLevel': 'minimal'})
            self.assertEqual(body['generationConfig']['maxOutputTokens'], 64)
            self.assertEqual(body['contents'], [{'role': 'user', 'parts': [{'text': 'x'*2000}]}])
            self.assertNotIn(FAKE['api_key'], request.content.decode())
            return httpx.Response(200, json={'candidates':[{'finishReason':'STOP','content':{'parts':[
                {'text':'private thought', 'thought':True}, {'text':'霓虹追龙'}]}}]})
        config = validate_config(FAKE)
        client = GoogleChatClient(loader=lambda: config, transport=httpx.MockTransport(handle), timeout=httpx.Timeout(20, connect=5))
        self.assertEqual(GoogleTitleGenerator(client).generate('x'*3000), '霓虹追龙')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, '/v1beta/models/'+TITLE_MODEL+':generateContent')
        self.assertNotIn(FAKE['api_key'], repr(config))

    def test_rejected_empty_truncated_and_http_error_no_retry_or_private_detail(self):
        for code, body in [(429, {'error':{'message':FAKE['api_key']}}),
                           (200, {'candidates':[{'finishReason':'MAX_TOKENS','content':{'parts':[{'text':'partial'}]}}]}),
                           (200, {'candidates':[]})]:
            calls=[]
            def handle(request):
                calls.append(request)
                return httpx.Response(code, json=body)
            client=GoogleChatClient(loader=lambda:validate_config(FAKE),transport=httpx.MockTransport(handle))
            with self.assertRaises(ChatError) as error:
                GoogleTitleGenerator(client).generate('synthetic prompt')
            self.assertEqual(len(calls),1)
            self.assertNotIn(FAKE['api_key'],str(error.exception))

    def test_exact_profile_and_disabled_source(self):
        self.assertIsNone(validate_config({'enabled':False}))
        with self.assertRaises(GoogleTitleConfigError): validate_config({'enabled':0})
        for key, bad in [('service','other'),('profile','default'),('base_url','https://example.test'),
                         ('enabled',1),('api_key','bad\nsecret')]:
            with self.subTest(key=key), self.assertRaises(GoogleTitleConfigError) as error:
                validate_config({**FAKE,key:bad})
            self.assertNotIn(FAKE['api_key'],str(error.exception))

    def test_runtime_file_bounds_duplicate_fields_and_optional_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'google_titles'
            settings=Settings(data_dir=Path(tmp), title_config_file=path)
            original=os.fstat
            def root_metadata(fd):
                values=list(original(fd)); values[4]=0
                return os.stat_result(values)
            with patch('studio_platform.google_title_config.os.fstat', side_effect=root_metadata):
                path.write_text(json.dumps(FAKE), encoding='utf-8')
                path.chmod(0o640)
                self.assertEqual(runtime_config(path).base_url,ORIGIN)
                for raw in ('{"enabled":false,"enabled":false}', 'x'*4097, '{}'):
                    path.write_text(raw,encoding='utf-8')
                    with self.assertRaises(GoogleTitleConfigError): runtime_config(path)
                    with self.assertRaises(ChatError): configured_generator(settings).generate('ignored')
                path.write_text('{"enabled":false}',encoding='utf-8')
                self.assertIsNone(configured_generator(settings))

    def test_feature_off_no_loading_and_conflicting_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings=Settings(data_dir=Path(tmp))
            self.assertIsNone(configured_generator(settings))
            with self.assertRaises(ValueError): replace(settings,title_config_file=Path('relative'))
            with self.assertRaises(ValueError): replace(settings,title_config_file=Path(tmp)/'key',title_use_central=True)
            with self.assertRaises(ValueError): replace(settings,public_origin='https://www.sixnine.art',title_use_central=True)


if __name__ == '__main__':
    unittest.main()
