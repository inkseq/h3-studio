"""Pinned broker schemas and bounded fake CPU reads; no provider/GPU calls."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from studio_platform.control import WorkerSpec
from studio_platform.fleet import SlotConfig
from studio_platform.hatchet_dispatch import BrokerConfig
from studio_platform.hatchet_readiness import BrokerReadiness, _expectation, _workers_client
from studio_platform.runtime_catalog import engine_manifest, PROFILE_IDS


class BrokerReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.now = 1_800_000_000.0
        self.config = BrokerConfig(self.directory/"token", "http://127.0.0.1:18888",
            "127.0.0.1:17077", tls=False)
        self.manifest = engine_manifest(PROFILE_IDS[0], "fl")
        self.spec = WorkerSpec("dstack-test", "dstack-test-pool", "vast", "gpu-original",
            ("GPU-physical",), (self.manifest.document["generation_recipe_id"],),
            self.manifest.document["model_id"], "dstack-test-config", "wangp-worker", self.manifest.digest,
            output_delivery="native-frames-v1", dispatch_backend="hatchet-v1")
        manifest_file = self.directory/"manifest.json"
        manifest_file.write_text(json.dumps(self.manifest.document), encoding="utf-8")
        manifest_file.chmod(0o600)
        self.client_file = self.directory/"client.json"
        self.client_file.write_text(json.dumps({"version":1,"enabled":True,
            "configuration_id":self.spec.configuration_id,"manifest_file":str(manifest_file)}), encoding="utf-8")
        self.client_file.chmod(0o600)
        self.slot = SlotConfig(self.spec, True, "http://127.0.0.1:8199", ("http://127.0.0.1:8199",),
            runtime_config_file=str(self.client_file))
        # Pinned server list omits both fields; only detail includes workflows.
        self.row = self.worker(registered_workflows=None)
        self.client = Mock()
        self.client.list.return_value = SimpleNamespace(rows=[self.row], pagination=None)
        self.client.get.return_value = self.worker()
        self.factory = Mock(return_value=self.client)
        self.readiness = BrokerReadiness(self.config, lambda:self.now, self.factory)

    def worker(self, **changes):
        name, expected, workflow = _expectation(self.config, self.slot)
        value = {"metadata":SimpleNamespace(id="22222222-2222-4222-8222-222222222222"),
            "name":name,"type":"SELFHOSTED","status":"ACTIVE",
            "last_heartbeat_at":datetime.fromtimestamp(self.now-2,timezone.utc),
            "last_listener_established":None,
            "dispatcher_id":"11111111-1111-4111-8111-111111111111",
            "labels":[SimpleNamespace(key=k,value=v) for k,v in expected.items()],
            "registered_workflows":[SimpleNamespace(name=workflow)],
            "webhook_url":"private signed URL must not surface"}
        value.update(changes)
        return SimpleNamespace(**value)

    def test_constructor_no_io_and_exact_active_observation_safe(self):
        self.factory.assert_not_called()
        value = self.readiness.projection(self.slot)
        self.assertTrue(value["ready"])
        self.assertEqual(set(value),{"ready","worker_id","observed_at","heartbeat_at","reason_code"})
        self.assertEqual(value["worker_id"],self.spec.worker_id)
        self.assertNotIn("private",json.dumps(value))
        self.assertEqual(self.client.list.call_count,1)
        self.client.get.assert_called_once_with(self.row.metadata.id)

    def test_inactive_history_does_not_hide_single_live_consumer(self):
        self.client.list.return_value.rows += [self.worker(status="INACTIVE"),self.worker(status="PAUSED")]
        self.assertTrue(self.readiness.probe(self.slot))

    def test_missing_paused_inactive_wrong_name_type_and_duplicate_fail_closed(self):
        cases=[[],[self.worker(status="PAUSED")],[self.worker(status="INACTIVE")],
            [self.worker(name="another-worker")],[self.worker(type="WEBHOOK")],
            [self.worker(),self.worker()]]
        for rows in cases:
            with self.subTest(rows=len(rows)):
                probe=BrokerReadiness(self.config,lambda:self.now,self.factory)
                self.client.list.return_value.rows=rows
                self.assertFalse(probe.probe(self.slot))

    def test_required_labels_missing_drifted_or_duplicate_are_rejected(self):
        original=self.worker().labels
        changed=[SimpleNamespace(key=label.key,value="wrong" if label.key=="sixnine_mode" else label.value) for label in original]
        for entries in (None,original[:-1],changed,original+[original[0]]):
            with self.subTest(entries=entries is None):
                probe=BrokerReadiness(self.config,lambda:self.now,self.factory)
                self.client.list.return_value.rows=[self.worker(labels=entries)]
                self.assertFalse(probe.probe(self.slot))

    def test_exact_physical_worker_label_required_in_addition_to_worker_name(self):
        _, expected, _ = _expectation(self.config, self.slot)
        self.assertEqual(expected["sixnine_worker"], self.spec.worker_id)
        entries = [SimpleNamespace(key=label.key,
            value="another-physical-slot" if label.key == "sixnine_worker" else label.value)
            for label in self.worker().labels]
        self.client.list.return_value.rows = [self.worker(labels=entries)]
        self.assertEqual(self.readiness.projection(self.slot)["reason_code"], "hatchet_consumer_labels_mismatch")

    def test_stale_future_naive_and_missing_heartbeats_are_rejected(self):
        stamps=[None,datetime.fromtimestamp(self.now-61,timezone.utc),
            datetime.fromtimestamp(self.now+1,timezone.utc),datetime.fromtimestamp(self.now)]
        for stamp in stamps:
            with self.subTest(stamp=stamp):
                probe=BrokerReadiness(self.config,lambda:self.now,self.factory)
                self.client.list.return_value.rows=[self.worker(last_heartbeat_at=stamp)]
                self.assertFalse(probe.probe(self.slot))

    def test_exact_detail_without_dispatcher_or_workflow_is_not_consumer_proof(self):
        for changes in ({"dispatcher_id":None},{"dispatcher_id":"not-a-uuid"},
                {"registered_workflows":[]},{"registered_workflows":[SimpleNamespace(name="other-workflow")]}):
            probe=BrokerReadiness(self.config,lambda:self.now,self.factory)
            self.client.get.return_value=self.worker(**changes)
            self.assertFalse(probe.probe(self.slot))

    def test_missing_invalid_list_identity_cannot_issue_detail_read(self):
        for identifier in (None,"not-a-uuid","00000000-0000-0000-0000-000000000000"):
            probe=BrokerReadiness(self.config,lambda:self.now,self.factory)
            self.client.list.return_value.rows=[self.worker(metadata=SimpleNamespace(id=identifier))]
            self.assertEqual(probe.projection(self.slot)["reason_code"],"hatchet_consumer_identity_unconfirmed")
        self.client.get.assert_not_called()

    def test_detail_identity_eligibility_and_fresh_heartbeat_rechecked(self):
        entries=self.worker().labels
        cases=({"metadata":SimpleNamespace(id="33333333-3333-4333-8333-333333333333")},
            {"name":"another-worker"},{"status":"PAUSED"},{"status":"INACTIVE"},{"type":"WEBHOOK"},
            {"labels":entries[:-1]},
            {"last_heartbeat_at":datetime.fromtimestamp(self.now-61,timezone.utc)})
        for changes in cases:
            with self.subTest(changes=list(changes)):
                probe=BrokerReadiness(self.config,lambda:self.now,self.factory)
                self.client.get.return_value=self.worker(**changes)
                self.assertFalse(probe.probe(self.slot))

    def test_detail_failure_invalidates_ready_and_does_not_repeat_read(self):
        self.assertTrue(self.readiness.probe(self.slot))
        self.now+=15
        self.client.get.side_effect=TimeoutError("Bearer secret / private signed URL")
        value=self.readiness.projection(self.slot)
        self.assertFalse(value["ready"])
        self.assertEqual(value["reason_code"],"hatchet_consumer_observation_failed")
        self.assertNotIn("secret",json.dumps(value))
        self.readiness.probe(self.slot)
        self.assertEqual(self.client.list.call_count,2)
        self.assertEqual(self.client.get.call_count,2)

    def test_refresh_and_concurrent_calls_share_15_second_budget(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            self.assertTrue(all(executor.map(lambda _:self.readiness.probe(self.slot),range(12))))
        self.assertEqual(self.client.list.call_count,1)
        self.assertEqual(self.client.get.call_count,1)
        self.now+=14.9
        self.assertTrue(self.readiness.probe(self.slot))
        self.assertEqual(self.client.list.call_count,1)
        self.now+=0.1
        self.assertTrue(self.readiness.probe(self.slot))
        self.assertEqual(self.client.list.call_count,2)
        self.assertEqual(self.client.get.call_count,2)

    def test_outage_invalidates_observation_and_has_no_immediate_retry(self):
        self.assertTrue(self.readiness.probe(self.slot))
        self.now+=15
        self.client.list.side_effect=TimeoutError("Bearer secret / private signed URL")
        value=self.readiness.projection(self.slot)
        self.assertFalse(value["ready"])
        self.assertEqual(value["reason_code"],"hatchet_consumer_observation_failed")
        self.assertNotIn("secret",json.dumps(value))
        self.readiness.probe(self.slot)
        self.assertEqual(self.client.list.call_count,2)

    def test_partial_list_and_missing_rows_are_ambiguous(self):
        for response in (SimpleNamespace(rows=None,pagination=None),
                SimpleNamespace(rows=[self.row],pagination=SimpleNamespace(current_page=1,next_page=2,num_pages=2)),
                SimpleNamespace(rows=[self.row],pagination=SimpleNamespace(current_page=2,next_page=2,num_pages=2)),
                SimpleNamespace(rows=[self.row],pagination=SimpleNamespace(current_page=1,next_page=2,num_pages=1)),
                SimpleNamespace(rows=[self.row],pagination=SimpleNamespace(current_page=1,next_page=2,num_pages=0)),
                SimpleNamespace(rows=[self.row],pagination=SimpleNamespace(current_page=1,next_page=None,num_pages=None))):
            probe=BrokerReadiness(self.config,lambda:self.now,self.factory)
            self.client.list.return_value=response
            self.assertFalse(probe.probe(self.slot))

    def test_clock_reversal_does_not_emit_positive_or_repeat_read(self):
        self.assertTrue(self.readiness.probe(self.slot))
        self.now-=1
        self.assertFalse(self.readiness.probe(self.slot))
        self.assertEqual(self.client.list.call_count,1)

    def test_changed_manifest_or_route_cannot_reuse_cached_ready(self):
        self.assertTrue(self.readiness.probe(self.slot))
        altered=replace(self.slot,spec=replace(self.spec,dispatch_backend="legacy"))
        self.assertFalse(self.readiness.probe(altered))
        altered=replace(self.slot,spec=replace(self.spec,engine_manifest_digest="0"*64))
        self.assertFalse(self.readiness.probe(altered))
        self.assertEqual(self.client.list.call_count,1)

    @unittest.skipUnless(importlib.util.find_spec("hatchet_sdk"),"optional pinned SDK unavailable")
    def test_actual_sdk_worker_schema_and_public_list_bounded_transport(self):
        from hatchet_sdk import ClientConfig
        from hatchet_sdk.clients.rest.models.worker import Worker
        from hatchet_sdk.clients.rest.models.worker_list import WorkerList
        from urllib3 import HTTPResponse
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        tenant="11111111-1111-4111-8111-111111111111"
        token=encode({"alg":"HS256"})+"."+encode({"sub":tenant,"server_url":self.config.server_url,
            "grpc_broadcast_address":self.config.host_port})+"."+encode("synthetic-signature")
        config=ClientConfig(_env_file=None,token=token,server_url=self.config.server_url,
            host_port=self.config.host_port,namespace=self.config.namespace,tenant_id=tenant)
        stamp=datetime.fromtimestamp(self.now-2,timezone.utc).isoformat()
        meta={"id":tenant,"createdAt":stamp,"updatedAt":stamp}
        name,expected,workflow=_expectation(self.config,self.slot)
        raw={"metadata":meta,"name":name,"type":"SELFHOSTED","status":"ACTIVE",
            "lastHeartbeatAt":stamp,"dispatcherId":tenant,
            "labels":[{"metadata":meta,"key":k,"value":v} for k,v in expected.items()]}
        detail={**raw,"registeredWorkflows":[{"id":tenant,"name":workflow}]}
        self.client.list.return_value=WorkerList(rows=[Worker.from_dict(raw)])
        self.client.get.return_value=Worker.from_dict(detail)
        self.assertTrue(self.readiness.probe(self.slot))
        with patch("studio_platform.hatchet_readiness.create_client",return_value=SimpleNamespace(config=config)):
            client=_workers_client(self.config)
        self.assertEqual(client.client_config.tenacity.max_attempts,0)
        captured=[]
        def request(*args,**kwargs):
            captured.append((args,kwargs))
            value=detail if args[1].endswith("/api/v1/workers/"+tenant) else {"rows":[raw], "pagination":{
                "current_page":1,"num_pages":1,"next_page":1}}
            return HTTPResponse(body=io.BytesIO(json.dumps(value).encode()),status=200,
                headers={"Content-Type":"application/json"},preload_content=False)
        with patch("urllib3.PoolManager.request",side_effect=request):
            response=client.list()
        self.assertEqual(len(response.rows),1)
        self.assertEqual(len(captured),1)
        self.assertFalse(captured[0][1]["redirect"])
        self.assertFalse(captured[0][1]["retries"])
        self.assertEqual(captured[0][1]["timeout"].total,3)
        self.assertEqual(captured[0][0][:2],("GET",self.config.server_url+"/api/v1/tenants/"+tenant+"/worker"))
        with patch("urllib3.PoolManager.request",side_effect=request):
            found=client.get(tenant)
        self.assertEqual(found.metadata.id,tenant)
        self.assertEqual(found.registered_workflows[0].name,workflow)
        self.assertIsNone(found.last_listener_established)
        self.assertEqual(captured[1][0][:2],("GET",self.config.server_url+"/api/v1/workers/"+tenant))
        with patch("urllib3.PoolManager.request") as forbidden:
            with self.assertRaises(ValueError):
                client.get("not-a-uuid")
            with client.client() as transport:
                for method,url in (("POST",self.config.server_url+"/api/v1/tenants/"+tenant+"/worker"),
                        ("GET",self.config.server_url+"/api/v1/workers/"+tenant),
                        ("GET","https://another-origin.invalid/api/v1/tenants/"+tenant+"/worker")):
                    with self.assertRaises(ValueError):
                        transport.call_api(method,url)
            forbidden.assert_not_called()
        with patch("urllib3.PoolManager.request",side_effect=request):
            probe=BrokerReadiness(self.config,lambda:self.now,lambda _:client)
            self.assertTrue(probe.probe(self.slot))
        for rows, pages, reason in (
                ([], {"current_page":1,"num_pages":0,"next_page":2}, "hatchet_consumer_unconfirmed"),
                ([raw], {"current_page":1,"num_pages":2,"next_page":2}, "hatchet_consumer_observation_failed")):
            with self.subTest(pages=pages):
                def page(*args,**kwargs):
                    return HTTPResponse(body=io.BytesIO(json.dumps({"rows":rows,"pagination":pages}).encode()),
                        status=200, headers={"Content-Type":"application/json"},preload_content=False)
                probe=BrokerReadiness(self.config,lambda:self.now,lambda _:client)
                with patch("urllib3.PoolManager.request",side_effect=page) as get:
                    self.assertEqual(probe.projection(self.slot)["reason_code"],reason)
                    self.assertEqual(get.call_count,1)
        for status,payload,reason in (
                (307,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_redirect_rejected"),
                (401,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_unauthorized"),
                (403,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_forbidden"),
                (404,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_not_found"),
                (429,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_rate_limited"),
                (503,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_unavailable"),
                (400,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_rejected"),
                (200,b"x"*(1024*1024+1),"hatchet_consumer_response_too_large"),
                (200,b"PRIVATE-BODY-SECRET","hatchet_consumer_observation_failed")):
            with self.subTest(status=status):
                def rejected(*args,**kwargs):
                    return HTTPResponse(body=io.BytesIO(payload),status=status,
                        headers={"Location":"https://private-redirect.invalid", "Content-Type":"application/json"},
                        preload_content=False)
                probe=BrokerReadiness(self.config,lambda:self.now,lambda _:client)
                with patch("urllib3.PoolManager.request",side_effect=rejected) as get:
                    projection=probe.projection(self.slot)
                    self.assertFalse(projection["ready"])
                    self.assertEqual(projection["reason_code"],reason)
                    self.assertIsNone(projection["heartbeat_at"])
                    self.assertNotIn("PRIVATE-BODY-SECRET",json.dumps(projection))
                    self.assertNotIn("private-redirect",json.dumps(projection))
                    self.assertEqual(probe.projection(self.slot),projection)
                    self.assertEqual(get.call_count,1)

        for status,payload,reason in (
                (307,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_redirect_rejected"),
                (401,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_unauthorized"),
                (403,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_forbidden"),
                (404,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_not_found"),
                (429,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_rate_limited"),
                (503,b"PRIVATE-BODY-SECRET","hatchet_consumer_http_unavailable"),
                (200,b"x"*(1024*1024+1),"hatchet_consumer_response_too_large")):
            with self.subTest(detail_status=status):
                def rejected_detail(*args,**kwargs):
                    if args[1].endswith("/worker"):
                        return request(*args,**kwargs)
                    return HTTPResponse(body=io.BytesIO(payload),status=status,
                        headers={"Content-Type":"application/json"},preload_content=False)
                probe=BrokerReadiness(self.config,lambda:self.now,lambda _:client)
                with patch("urllib3.PoolManager.request",side_effect=rejected_detail) as get:
                    projection=probe.projection(self.slot)
                    self.assertFalse(projection["ready"])
                    self.assertEqual(projection["reason_code"],reason)
                    self.assertNotIn("PRIVATE-BODY-SECRET",json.dumps(projection))
                    self.assertEqual(probe.projection(self.slot),projection)
                    self.assertEqual(get.call_count,2)

    def test_returned_projection_mutation_cannot_change_cached_proof(self):
        value=self.readiness.projection(self.slot)
        value["ready"]=False
        value["worker_id"]="another-worker"
        cached=self.readiness.projection(self.slot)
        self.assertTrue(cached["ready"])
        self.assertEqual(cached["worker_id"],self.spec.worker_id)
        self.assertEqual(self.client.list.call_count,1)


if __name__=="__main__":
    unittest.main()
