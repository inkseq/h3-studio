"""Offline public-helper integration: OS identity, session upload and recovery."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1] / "skills/inkseq/scripts"
def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

helper = module("qc_public_skill", "sixnine.py")
connect = module("qc_public_connect", "connect.py")


class QuickChatHelperTests(unittest.TestCase):
    origin = "https://studio.example.test"

    def args(self, **overrides):
        return argparse.Namespace(**{**dict(base_url=self.origin, registry_root=None,
            profile=None, connection=None, command="request", method="GET",
            path="/v1/quick-chat/schema", json_file=None, output=None,
            idempotency_key=None), **overrides})

    def run_helper(self, handler, **overrides):
        with patch.dict(os.environ, {"SIXNINE_API_KEY": "synthetic-process-credential"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            helper.run(self.args(**overrides), httpx.MockTransport(handler))
            return json.loads(output.getvalue())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_connection_uses_existing_os_ciphertext_and_does_not_print_key(self):
        token = "synthetic-os-store-credential"
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"LOCALAPPDATA": folder}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            connection_id = "connection-" + "1" * 32
            connect.SecureStore().save(self.origin, connection_id, {
                "protocol": connect.PROTOCOL, "status": "connected", "api_key": token,
                "expected": {"origin": self.origin, "connection_id": connection_id}})
            seen = []
            def request(req):
                seen.append(req.headers["Authorization"])
                return httpx.Response(200, json={"version": "test-only"})
            helper.run(self.args(connection=connection_id), httpx.MockTransport(request))
            self.assertEqual(seen, ["Bearer " + token])
            self.assertNotIn(token, output.getvalue())
            files = list(Path(folder).rglob("*.dpapi"))
            self.assertEqual(len(files), 1)
            self.assertNotIn(token.encode(), files[0].read_bytes())

    def test_explicit_connection_does_not_silently_select_old_env_identity(self):
        with patch.dict(os.environ, {"SIXNINE_API_KEY": "synthetic-process-credential"}):
            with self.assertRaisesRegex(ValueError, "one explicit credential source"):
                helper.credential(self.args(connection="connection-1"))

    def test_native_upload_has_session_route_and_stable_id_no_project_payload(self):
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "fixture.png"
            file.write_bytes(b"synthetic-media-placeholder")
            def request(req):
                seen.append((req.url.path, req.read()))
                return httpx.Response(200, json={"asset_id": "asset-1", "status": "ready"})
            result = self.run_helper(request, command="upload", session="session-1",
                                     project=None, asset_id="stable-client-id", file=str(file))
        self.assertEqual(result["asset_id"], "asset-1")
        self.assertEqual(seen[0][0], "/v1/quick-chat/sessions/session-1/assets")
        self.assertIn(b"stable-client-id", seen[0][1])
        self.assertNotIn(b"client_project_id", seen[0][1])

    def test_native_resume_looks_up_original_client_receipt_and_uses_session_endpoint(self):
        seen = []
        def request(req):
            seen.append((req.method, req.url.path, req.url.params.get("client_asset_id")))
            if req.method == "GET":
                return httpx.Response(200, json={"assets": [{"asset_id": "asset-1",
                    "client_asset_id": "original-client-id", "status": "processing"}]})
            return httpx.Response(200, json={"asset_id": "asset-1", "status": "ready"})
        result = self.run_helper(request, command="resume-upload", session="session-1",
                                 project=None, asset_id="original-client-id")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(seen, [("GET", "/v1/quick-chat/sessions/session-1/assets", "original-client-id"),
            ("POST", "/v1/quick-chat/sessions/session-1/assets/asset-1/resume", None)])

    def test_native_resume_ready_receipt_never_resends_media_or_writes(self):
        seen = []
        def request(req):
            seen.append(req.method)
            return httpx.Response(200, json={"assets": [{"asset_id": "asset-1",
                "client_asset_id": "original-client-id", "status": "ready"}]})
        self.run_helper(request, command="resume-upload", session="session-1",
                        project=None, asset_id="original-client-id")
        self.assertEqual(seen, ["GET"])


    def test_generic_take_requests_preserve_methods_bodies_keys_without_auto_chain(self):
        base='/v1/quick-chat/sessions/session-1'
        cases=[('GET',base+'/cards/card-1/takes?limit=20',None),
            ('PATCH',base+'/takes/item-1',{'expected_version':0,'starred':True}),
            ('POST',base+'/cards/card-1/again',{'expected_card_version':1,'source_revision_id':'revision-1','item_id':'item-1','copies':2}),
            ('POST',base+'/revisions/revision-2/preflights',{'revision_hash':'hash','capabilities_version':'version'}),
            ('POST',base+'/revisions/revision-2/submissions',{'preflight_id':'preflight-1','revision_hash':'hash','confirmed':True}),
            ('GET',base+'/story-transfers/import-1',None),
            ('POST',base+'/takes/item-1/story',{'target_project_id':'project-1','expected_project_version':3,'parent_id':'scene-1'})]
        with tempfile.TemporaryDirectory() as folder:
            for index,(method,path,body) in enumerate(cases):
                seen=[];file=Path(folder)/'request.json'
                if body is not None:file.write_text(json.dumps(body),encoding='utf-8')
                def request(req):
                    seen.append((req.method,str(req.url),json.loads(req.content) if req.content else None,req.headers.get('Idempotency-Key')))
                    return httpx.Response(200,json={'id':'original','status':'unknown'})
                self.run_helper(request,method=method,path=path,json_file=str(file) if body is not None else None,
                    idempotency_key='original-key' if body is not None else None)
                self.assertEqual(seen,[(method,self.origin+path,body,'original-key' if body is not None else None)])
                # Explicit transfer resume uses the identical request, never an automatic retry.
                if index==len(cases)-1:
                    self.run_helper(request,method=method,path=path,json_file=str(file),idempotency_key='original-key')
                    self.assertEqual(seen[0],seen[1]);self.assertEqual(len(seen),2)


if __name__ == "__main__":
    unittest.main()
