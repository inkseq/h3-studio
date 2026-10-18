"""Trusted-file composition tests; fake providers, no credentials or network."""
from dataclasses import asdict
import base64
import hashlib
import json
import io
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

import httpx

from studio_platform.operator_capacity import OperatorError
from studio_platform.operator_runtime import (create_controller,create_registry,load_runtime_config,
    _assemble,_manifest,_validate_sources,create_controller_from_stdin,InventoryRefresh,LiumMarketRefresh)
from studio_platform.operator_capacity import operator_inventory,operator_heartbeats
from studio_platform.capacity_market import market_inventory
from sqlalchemy import insert,select,update
from studio_platform.runtime_catalog import PROFILE_IDS,engine_manifest,get_profile,model_for
from test_platform_repository import LedgerCase


class DeferredStockExecutor:
    def __init__(self,**kwargs):
        self.work=[]
        self.shutdown_calls=[]

    def submit(self,function):
        future=Future()
        self.work.append((function,future))
        return future

    def complete(self,index=0):
        function,future=self.work[index]
        try: future.set_result(function())
        except Exception as error: future.set_exception(error)

    def shutdown(self,**kwargs): self.shutdown_calls.append(kwargs)


class LiumStockTests(LedgerCase):
    def row(self):
        with self.repo.engine.connect() as connection:
            return connection.execute(select(market_inventory).where(market_inventory.c.provider=="lium")).mappings().first()

    def test_one_inflight_read_interval_and_shutdown_never_wait_or_publish_late_result(self):
        refresh=LiumMarketRefresh(self.repo,Mock())
        executor=DeferredStockExecutor()
        with patch("studio_platform.operator_runtime.ThreadPoolExecutor",return_value=executor), \
             patch.object(refresh,"_probe",return_value={"provider":"lium","status":"ok","observed_at":self.now,"offers":[]}):
            refresh()
            self.now+=300
            refresh()
            self.assertEqual(len(executor.work),1)  # Even a slow request cannot overlap.
            executor.complete()
            refresh()
            self.assertEqual(len(executor.work),2)
            self.assertEqual(self.row()["observed_at"],1000)  # Do not refresh its age on publish.
            executor.complete(1)
            refresh()
            self.now+=29
            refresh()
            self.assertEqual(len(executor.work),2)
            self.now+=1
            refresh()
            self.assertEqual(len(executor.work),3)
            before=dict(self.row())
            refresh(stopping=True)
            self.assertEqual(executor.shutdown_calls,[{"wait":False,"cancel_futures":True}])
            executor.complete(2)
            self.now+=100
            refresh()
            refresh(stopping=True)
            self.assertEqual(len(executor.work),3)
            self.assertEqual(dict(self.row()),before)

    def test_full_stock_scan_is_get_only_uses_existing_identity_closes_client_and_redacts(self):
        from test_capacity_inventory import lium,loader
        from studio_platform import capacity_inventory
        approved_loader=Mock(side_effect=loader)
        refresh=LiumMarketRefresh(self.repo,approved_loader)
        executor=DeferredStockExecutor()
        requests,clients=[],[]
        original_client=httpx.Client
        def response(request):
            requests.append(request)
            return httpx.Response(200,json=[lium()])
        def client(**kwargs):
            self.assertEqual(kwargs["timeout"],capacity_inventory.TIMEOUT_SECONDS)
            self.assertFalse(kwargs["follow_redirects"])
            kwargs["transport"]=httpx.MockTransport(response)
            value=original_client(**kwargs);clients.append(value);return value
        before=self.repo.get_budget("owner-budget")
        with patch("studio_platform.operator_runtime.ThreadPoolExecutor",return_value=executor), \
             patch.object(capacity_inventory.httpx,"Client",side_effect=client), \
             patch("studio_platform.lium_provider._central_loader",side_effect=AssertionError("wrong_identity_source")), \
             patch("studio_platform.lium_provider.LiumProvider._request",side_effect=AssertionError("no_rental_or_dry_run")):
            refresh();executor.complete();refresh();refresh(stopping=True)
        approved_loader.assert_called_once_with("lium",profile="lium--rig-root")
        self.assertEqual([(r.method,str(r.url)) for r in requests],[("GET",capacity_inventory.LIUM_URL)])
        self.assertTrue(clients[0].is_closed)
        row=self.row()
        self.assertEqual(row["payload"]["status"],"ok")
        self.assertEqual(len(row["payload"]["offers"]),1)
        self.assertNotIn("must-not-escape",json.dumps(dict(row)))
        self.assertEqual(before,self.repo.get_budget("owner-budget"))

    def test_transport_timeout_replaces_success_with_unknown_and_closes_owned_client(self):
        from test_capacity_inventory import loader
        from studio_platform import capacity_inventory
        from studio_platform.capacity_market import publish_observation
        publish_observation(self.repo,{"provider":"lium","status":"ok","observed_at":self.now,"offers":[]})
        refresh=LiumMarketRefresh(self.repo,loader)
        executor=DeferredStockExecutor()
        clients=[]
        original_client=httpx.Client
        def timeout(request): raise httpx.ReadTimeout("private-error-must-not-escape")
        def client(**kwargs):
            kwargs["transport"]=httpx.MockTransport(timeout)
            value=original_client(**kwargs);clients.append(value);return value
        with patch("studio_platform.operator_runtime.ThreadPoolExecutor",return_value=executor), \
             patch.object(capacity_inventory.httpx,"Client",side_effect=client):
            refresh();executor.complete();refresh();refresh(stopping=True)
        self.assertTrue(clients[0].is_closed)
        self.assertEqual(self.row()["payload"]["status"],"error")
        self.assertEqual(self.row()["payload"]["reason_code"],"inventory_scan_failed")
        self.assertNotIn("must-not-escape",json.dumps(dict(self.row())))

    def test_unexpected_reader_error_is_recorded_unknown_without_raw_message(self):
        refresh=LiumMarketRefresh(self.repo,Mock())
        executor=DeferredStockExecutor()
        with patch("studio_platform.operator_runtime.ThreadPoolExecutor",return_value=executor), \
             patch.object(refresh,"_probe",side_effect=RuntimeError("private-error-must-not-escape")):
            refresh();executor.complete();refresh();refresh(stopping=True)
        self.assertEqual(self.row()["payload"]["status"],"error")
        self.assertNotIn("must-not-escape",json.dumps(dict(self.row())))


class RuntimeTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root=Path(self.temp.name)
        self.profile=PROFILE_IDS[2]
        self.mode="fl"
        self.expected=engine_manifest(self.profile,self.mode)
        profile=get_profile(self.profile)
        self.provider={"configuration_id":"native-pro6000-fl","model_id":profile["model_id"],"executor_id":"",
            "template_id":"a1111111-1111-4111-8111-111111111111","gpu_count":2,
            "max_price_per_gpu_hour_microusd":1_190_000,"termination_hours":2,
            "user_public_key":"ssh-ed25519 "+base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519"+b"x"*36).decode(),
            "approved_until":10_000_000_000,"allow_preflight_only_price_cap":True,"execution_slots":2,
            "compatible_gpu_names":[profile["gpu_models"][0]],"minimum_vram_mib":92160,
            "server_side_selection":True,"minimum_ram_gib":256,"minimum_disk_gib":350,
            "min_download_mbps":500}
        self.manifest_path=self.root/"provider.json"
        self.write(self.manifest_path,self.provider)
        dirs,hashes=[],[]
        for index in range(2):
            root=self.root/("slot"+str(index));root.mkdir()
            content={"wangp-bootstrap.py":b"# offline fixture only", "wangp-package.tar.gz":b"offline fixture only"}
            content["wangp-manifest.json"]=json.dumps(self.expected.document).encode()
            content["wangp-runtime.json"]=json.dumps({"deployment_profile_id":self.profile,
                "profile_slot_index":index,"expected_host_gpus":2,"port":8199+index,
                "source_bundle_sha256":hashlib.sha256(content["wangp-package.tar.gz"]).hexdigest()}).encode()
            for name,data in content.items():
                (root/name).write_bytes(data)
                (root/name).chmod(0o600)
            dirs.append(str(root))
            hashes.append({k:hashlib.sha256(v).hexdigest() for k,v in content.items()})
        self.binding={"binding_id":"native-pro6000-fl","runtime_profile_id":self.profile,
            "gpu_type":profile["gpu_models"][0],"gpu_count":2,"pool":"native-fl",
            "configuration_id":"native-pro6000-fl","model_id":profile["model_id"],
            "recipe_ids":[model_for(self.profile,self.mode)["generation_recipe_id"]],
            "engine_manifest_digest":self.expected.digest,
            "launch":{"provider":"lium","configuration_id":"native-pro6000-fl","model_id":profile["model_id"],
                "image_id":self.provider["template_id"]},"scope":asdict(self.scope),"budget_account_ids":["owner-budget"],
            "hourly_cost_microusd":2_380_000,"reservation_per_node_microusd":4_760_000,"expires_at":10_000_000_000,
            "max_ttl_seconds":7200,"min_ttl_seconds":3780,"execution_slots":2,"enabled":True,
            "filters":{"min_ram_gib":256,"min_disk_gib":350,"min_download_mbps":500,
                "max_price_per_gpu_hour_microusd":1_190_000,"allowed_countries":[]},
            "boot":{"provider_manifest_file":str(self.manifest_path),"source_dirs":dirs,"source_sha256":hashes}}
        self.registry_path=self.root/"registry.json"
        self.save_binding()
        for name in ("ssh-key","known-hosts"):
            (self.root/name).write_text("not read by factory")
        (self.root/"work").mkdir()
        self.config={"schema_version":1,"registry_file":str(self.registry_path),"work_dir":str(self.root/"work"),
            "ssh_key_file":str(self.root/"ssh-key"),"known_hosts_file":str(self.root/"known-hosts"),"port_start":24000}
        self.path=self.root/"runtime.json"
        self.write(self.path,self.config)

    def write(self,path,value):
        path.write_text(json.dumps(value),encoding="utf-8");path.chmod(0o600)

    def save_binding(self): self.write(self.registry_path,{"schema_version":1,"bindings":[self.binding]})

    def use_targon(self):
        self.profile="h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1"
        profile=get_profile(self.profile)
        self.expected=engine_manifest(self.profile,self.mode)
        self.provider={"configuration_id":"targon-pruned-fl","model_id":profile["model_id"],
            "org_slug":"offline-org","resource_name":"offline-pro6000","image_name":"offline-ubuntu",
            "ssh_key_ids":["offline-key"],"gpu_count":1,"execution_slots":1,"gpu_model":"RTX-PRO-6000B",
            "hourly_cost_cap_microusd":1_690_000,"approved_until":10_000_000_000,
            "max_lifetime_seconds":7200,"minimum_ram_gib":96,"minimum_disk_gib":128,
            "allow_preflight_only_price_cap":True,"allow_controller_lifetime":True}
        self.write(self.manifest_path,self.provider)
        self.binding.update(binding_id="targon-pruned-fl",configuration_id="targon-pruned-fl",
            runtime_profile_id=self.profile,model_id=profile["model_id"],gpu_count=1,execution_slots=1,
            gpu_type=profile["gpu_models"][0],engine_manifest_digest=self.expected.digest,
            launch={"provider":"targon","configuration_id":"targon-pruned-fl","model_id":profile["model_id"],
                "offer_id":"offline-pro6000","image_id":"offline-ubuntu"},
            hourly_cost_microusd=1_690_000,reservation_per_node_microusd=3_380_000,min_ttl_seconds=120,
            filters={"min_ram_gib":96,"min_disk_gib":128,"allowed_countries":[],
                "max_price_per_gpu_hour_microusd":1_690_000})
        boot=self.binding["boot"]
        boot["source_dirs"]=boot["source_dirs"][:1];boot["source_sha256"]=boot["source_sha256"][:1]
        root=Path(boot["source_dirs"][0])
        self.write(root/"wangp-manifest.json",self.expected.document)
        runtime=json.loads((root/"wangp-runtime.json").read_text())
        runtime.update(deployment_profile_id=self.profile,expected_host_gpus=1)
        self.write(root/"wangp-runtime.json",runtime)
        for name in ("wangp-manifest.json","wangp-runtime.json"):
            boot["source_sha256"][0][name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
        self.save_binding()
        (self.root/"guard").mkdir()
        self.config["cleanup_guard_dir"]=str(self.root/"guard")
        self.write(self.path,self.config)
        return {"runtime_profile_id":self.profile,"mode":"fl","gpu_type":self.binding["gpu_type"],
            "gpu_count":1,"node_count":1,"ttl_seconds":120,"filters":{},"provider":"targon"}

    def test_targon_registry_qualifies_only_explicit_manifest_without_provider_or_private_mount(self):
        chosen=self.use_targon()
        self.config.update(ssh_key_file=str(self.root/"unmounted"/"private-key"),
            cleanup_guard_dir=str(self.root/"unmounted"/"guard"))
        self.write(self.path,self.config)
        with patch("studio_platform.operator_runtime._BoundTargonProvider",side_effect=AssertionError("provider")), \
             patch("studio_platform.targon_cleanup.TargonCleanupGuard",side_effect=AssertionError("guard")), \
             patch("studio_platform.targon_provider._central_loader",side_effect=AssertionError("credential")):
            registry=create_registry(self.path)
            self.assertEqual(registry.resolve(chosen).launch.provider,"targon")
            self.assertEqual(registry.offers(chosen,self.now)["reason_code"],"operator_inventory_not_configured")
            with self.assertRaisesRegex(OperatorError,"deployment_not_configured"):
                registry.resolve({**chosen,"provider":"lium"})
        self.assertEqual(list((self.root/"work").iterdir()),[])

    def test_targon_composition_rechecks_manifest_and_source_before_reservation(self):
        self.use_targon()
        with patch("studio_platform.targon_provider._central_loader",side_effect=AssertionError("credential")), \
             patch("studio_platform.targon_provider.TargonProvider._request",side_effect=AssertionError("network")):
            _,registry,providers=_assemble(self.path,clock=lambda:self.now)
            binding=registry.get(self.binding["binding_id"])
            provider=providers[binding.binding_id]
            self.assertEqual(provider.provider_id,"targon")
            self.assertEqual(provider._cleanup_guard.root,self.root/"guard")
            kwargs={"physical_gpus":1,"slots":1,"reserved_cost_microusd":3_380_000,"hard_deadline":self.now+7200}
            provider.validate_launch(binding.launch,**kwargs)
            self.write(self.manifest_path,{**self.provider,"ssh_key_ids":["changed-key"]})
            with self.assertRaisesRegex(OperatorError,"manifest_changed"):
                provider.validate_launch(binding.launch,**kwargs)
            self.write(self.manifest_path,self.provider)
            (Path(binding.boot["source_dirs"][0])/"wangp-bootstrap.py").write_text("changed")
            with self.assertRaisesRegex(OperatorError,"source_changed"):
                provider.validate_launch(binding.launch,**kwargs)

    def test_targon_manifest_cannot_relax_identity_budget_lifetime_or_claim_unknown_filters(self):
        self.use_targon()
        for key,value in (("resource_name","different-resource"),("image_name","different-image"),
            ("gpu_model","RTX-5090"),("hourly_cost_cap_microusd",1_700_000),
            ("max_lifetime_seconds",3600),("max_lifetime_seconds",10800),("minimum_ram_gib",64),
            ("minimum_disk_gib",64),("allow_preflight_only_price_cap",False),("allow_controller_lifetime",False)):
            with self.subTest(key=key,value=value):
                self.write(self.manifest_path,{**self.provider,key:value})
                with self.assertRaises(OperatorError): create_registry(self.path)
        self.write(self.manifest_path,self.provider)
        for key,value in (("min_download_mbps",200),("allowed_countries",["US"])):
            with self.subTest(key=key):
                original=dict(self.binding["filters"])
                self.binding["filters"][key]=value;self.save_binding()
                with self.assertRaisesRegex(OperatorError,"hardware_filters_mismatch"): create_registry(self.path)
                self.binding["filters"]=original
        self.save_binding()
        del self.config["cleanup_guard_dir"];self.write(self.path,self.config)
        with self.assertRaisesRegex(OperatorError,"cleanup_guard_required"): create_registry(self.path)

    def test_targon_inventory_uses_request_start_and_does_not_leak_resource_details(self):
        chosen=self.use_targon()
        _,registry,providers=_assemble(self.path,clock=lambda:self.now)
        provider=providers[self.binding["binding_id"]]
        start=self.now
        def observe(launch):
            self.now+=3
            return {"available_count":1,"hourly_cost_microusd":1_600_000}
        with patch.object(provider,"preflight_availability",side_effect=observe):
            value=registry.offers(chosen,self.now)
        self.assertEqual((value["status"],value["observed_at"]),("available",start))
        self.assertEqual(value["hourly_cost_microusd"],1_690_000)
        self.assertEqual(value["hourly_cost_basis"],"approved_ceiling")
        self.assertNotIn("offline-pro6000",json.dumps(value))
        with patch.object(provider,"clock",side_effect=[start,start-1]), \
             patch.object(provider,"preflight_availability",return_value={}):
            with self.assertRaisesRegex(OperatorError,"inventory_unconfirmed"): provider.inventory_observation()

    def test_factory_is_inert_and_does_not_change_budget_or_policy(self):
        before=self.repo.get_budget("owner-budget")
        with patch("studio_platform.lium_provider._central_loader",side_effect=AssertionError("credential read")), \
             patch("studio_platform.lium_provider.LiumProvider._request",side_effect=AssertionError("network")):
            registry=create_registry(self.path)
            result=create_controller(self.path,repository=self.repo,
                settings=SimpleNamespace(operator_capacity_owners=("superdan",)))
            self.assertEqual(registry.get(self.binding["binding_id"]).gpu_count,2)
            self.assertFalse(result.service.policy()["enabled"])
            self.assertEqual(result.service.policy()["version"],0)
        self.assertEqual(before,self.repo.get_budget("owner-budget"))
        self.assertEqual(list((self.root/"work").iterdir()),[])

    def test_market_refresh_reuses_exact_controller_loader_and_keeps_targon_only_stock(self):
        loader=Mock(side_effect=AssertionError("no_credentials_during_construction"))
        controller=create_controller(self.path,repository=self.repo,settings=SimpleNamespace(),credential_loader=loader)
        self.assertIs(controller.inventory_refresh.market_refresh.loader,loader)
        self.assertIsNone(controller.inventory_refresh.market_refresh.executor)
        loader.assert_not_called()
        self.use_targon()
        controller=create_controller(self.path,repository=self.repo,settings=SimpleNamespace(),credential_loader=loader)
        market = controller.inventory_refresh.market_refresh
        self.assertEqual(set(market.readers), {"lium", "targon"})
        self.assertIsNone(market.loader)
        self.assertIsNone(market.executor)
        loader.assert_not_called()

    def test_config_paths_and_credential_fields_are_explicit(self):
        config=load_runtime_config(self.path)
        self.assertEqual(config["config_path"],str(self.path))
        for field,value in (("registry_file","relative.json"),("api_key","never-a-real-key"),
            ("credential_source","fallback"),("runtime_python","python"),("port_start",65000)):
            with self.subTest(field=field):
                self.write(self.path,{**self.config,field:value})
                with self.assertRaises(OperatorError): load_runtime_config(self.path)

    def test_aws_loader_is_constructed_but_never_called(self):
        self.write(self.path,{**self.config,"credential_source":"aws_runtime",
            "secret_arn":"arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:/sixnine/platform/lium-Ab12Cd",
            "secret_version_id":"a"*32})
        with patch("studio_platform.lium_runtime_aws._client",side_effect=AssertionError("network")):
            create_registry(self.path)

    def test_api_registry_needs_no_private_mount_or_provider(self):
        self.config.update(ssh_key_file=str(self.root/"unmounted"/"private-key"),
            work_dir=str(self.root/"unmounted"/"work"),known_hosts_file=str(self.root/"unmounted"/"known-hosts"))
        self.write(self.path,self.config)
        with patch("studio_platform.operator_runtime._BoundLiumProvider",side_effect=AssertionError("provider")), \
             patch("studio_platform.lium_provider._central_loader",side_effect=AssertionError("credential")):
            registry=create_registry(self.path,repository=self.repo)
            chosen={"runtime_profile_id":self.profile,"mode":"fl","gpu_type":self.binding["gpu_type"],
                "gpu_count":2,"node_count":1,"ttl_seconds":120,"filters":{}}
            self.assertEqual(registry.offers(chosen,self.now)["reason_code"],"operator_inventory_stale")
        with self.assertRaisesRegex(OperatorError,"path_unavailable"):
            create_controller(self.path,repository=self.repo,settings=SimpleNamespace())

    def test_private_stdin_reuses_exact_aws_envelope_without_sdk(self):
        from studio_platform.lium_identity import SERVICE,PROFILE,BASE_URL,KEY_VARIABLE
        arn="arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:/sixnine/platform/lium-Ab12Cd"
        version="a"*32
        self.write(self.path,{**self.config,"credential_source":"aws_runtime","secret_arn":arn,"secret_version_id":version})
        envelope={"secret_arn":arn,"version_id":version,"payload":{"schema_version":1,"service":SERVICE,
            "profile":PROFILE,"base_url":BASE_URL,"primary_key_variable":KEY_VARIABLE,"api_key":"offline-fixture"}}
        with patch("studio_platform.lium_runtime_aws._client",side_effect=AssertionError("AWS network")), \
             patch("studio_platform.lium_provider._central_loader",side_effect=AssertionError("DPAPI")), \
             patch("studio_platform.operator_runtime.create_controller",return_value="created") as create:
            result=create_controller_from_stdin(self.path,stream=io.BytesIO(json.dumps(envelope).encode()))
            self.assertEqual(result,"created")
            loader=create.call_args.kwargs["credential_loader"]
            self.assertEqual(loader(SERVICE,profile=PROFILE).api_key,"offline-fixture")
        for changes in ({"version_id":"b"*32},{"secret_arn":arn+"bad"},{"extra":"denied"}):
            with self.assertRaisesRegex(OperatorError,"credential_envelope_invalid"):
                create_controller_from_stdin(self.path,stream=io.BytesIO(json.dumps({**envelope,**changes}).encode()))

    def test_private_stdin_routes_exact_pinned_provider_envelopes_without_fallback(self):
        self.use_targon()
        references={provider:{"secret_arn":
            "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:/sixnine/platform/"+provider+"-Ab12Cd",
            "secret_version_id":("a" if provider=="lium" else "b")*32} for provider in ("lium","targon")}
        self.config.update(credential_source="aws_runtime",**references["lium"],
            provider_credentials={"targon":references["targon"]})
        self.write(self.path,self.config)
        credentials={provider:{"secret_arn":reference["secret_arn"],"version_id":reference["secret_version_id"],
            "payload":{"schema_version":1,"service":provider,"profile":provider+"--rig-root",
                "base_url":"https://lium.io/api" if provider=="lium" else "https://api.targon.com",
                "primary_key_variable":provider.upper()+"_API_KEY","api_key":"offline-"+provider}}
            for provider,reference in references.items()}
        envelope={"schema_version":2,"credentials":credentials}
        with patch("studio_platform.lium_runtime_aws._client",side_effect=AssertionError("AWS network")), \
             patch("studio_platform.operator_runtime.create_controller",return_value="created") as create:
            create_registry(self.path)
            self.assertEqual(create_controller_from_stdin(self.path,
                stream=io.BytesIO(json.dumps(envelope).encode())),"created")
            loader=create.call_args.kwargs["credential_loader"]
            for provider in ("lium","targon"):
                self.assertEqual(loader(provider,profile=provider+"--rig-root").api_key,"offline-"+provider)
            with self.assertRaises(ValueError): loader("targon",profile="lium--rig-root")
            with self.assertRaisesRegex(OperatorError,"credential_provider_mismatch"):
                loader("vastai",profile="vastai--rig-root")
        invalid=({"schema_version":2,"credentials":{"lium":credentials["lium"]}},
            {"schema_version":2,"credentials":{**credentials,"targon":credentials["lium"]}},credentials["lium"])
        for value in invalid:
            with self.subTest(value=set(value)):
                with self.assertRaisesRegex(OperatorError,"credential_envelope_invalid"):
                    create_controller_from_stdin(self.path,stream=io.BytesIO(json.dumps(value).encode()))
        # A Targon-only host needs no Lium secret reference or loaded credential.
        del self.config["secret_arn"];del self.config["secret_version_id"]
        self.write(self.path,self.config)
        with patch("studio_platform.operator_runtime.create_controller",return_value="created"):
            create_registry(self.path)
            self.assertEqual(create_controller_from_stdin(self.path,stream=io.BytesIO(json.dumps(
                {"schema_version":2,"credentials":{"targon":credentials["targon"]}}).encode())),"created")

    def test_inventory_is_controller_written_hash_bound_fresh_and_redacted(self):
        _,registry,providers=_assemble(self.path,clock=lambda:self.now)
        binding=registry.get(self.binding["binding_id"])
        probe=InventoryRefresh(self.repo,registry,providers)
        with self.repo.transaction() as connection:
            connection.execute(insert(operator_heartbeats).values(id="global",controller_id="ctl",observed_at=self.now,state="running"))
        class Executor:
            def __init__(self,**kwargs): pass
            def submit(self,fn,*args):
                future=Future();future.set_result(fn(*args));return future
            def shutdown(self,**kwargs): pass
        api=create_registry(self.path,repository=self.repo)
        chosen={"runtime_profile_id":self.profile,"mode":"fl","gpu_type":self.binding["gpu_type"],
            "gpu_count":2,"node_count":1,"ttl_seconds":120,"filters":{}}
        with patch("studio_platform.operator_runtime.ThreadPoolExecutor",Executor), \
             patch.object(providers[binding.binding_id],"inventory_observation",return_value=(None,self.now-10)) as call:
            probe("ctl");probe("ctl")
            call.assert_called_once()
        value=api.offers(chosen,self.now)
        self.assertEqual(value["status"],"available")
        self.assertEqual(value["observed_at"],self.now-10)
        self.assertEqual(value["hourly_cost_microusd"],2_380_000)
        self.assertEqual(value["offers"],[])
        self.now+=31
        self.assertTrue(api.offers(chosen,self.now)["stale"])
        with self.repo.transaction() as connection:
            connection.execute(update(operator_heartbeats).values(observed_at=self.now))
            connection.execute(update(operator_inventory).values(binding_hash="f"*64))
        self.assertTrue(api.offers(chosen,self.now)["stale"])
        with self.repo.transaction() as connection:
            connection.execute(update(operator_inventory).values(binding_hash=binding.fingerprint,observed_at=self.now+1))
        self.assertTrue(api.offers(chosen,self.now)["stale"])
        probe("ctl",stopping=True)

    def test_inventory_probe_does_not_block_lifecycle_or_publish_after_shutdown(self):
        _,registry,providers=_assemble(self.path,clock=lambda:self.now)
        probe=InventoryRefresh(self.repo,registry,providers)
        future=Future()
        class Executor:
            def __init__(self,**kwargs): pass
            def submit(self,*args): return future
            def shutdown(self,**kwargs): pass
        with patch("studio_platform.operator_runtime.ThreadPoolExecutor",Executor):
            probe("ctl")
            self.assertIs(probe.pending[1],future)
            probe("ctl")
            future.set_result(("available",None,self.now))
            probe("ctl",stopping=True)
        with self.repo.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(operator_inventory)).first())

    def test_repeated_native_cache_read_preserves_actual_observation_time(self):
        _,registry,providers=_assemble(self.path,clock=lambda:self.now)
        provider=providers[self.binding["binding_id"]]
        provider._availability_cache[provider.bound_manifest]=(self.now-59,None)
        with patch.object(provider,"_select_offer",side_effect=AssertionError("network")):
            result,observed_at=provider.inventory_observation()
        self.assertIsNone(result)
        self.assertEqual(observed_at,self.now-59)

    def test_durable_controller_status_binds_raw_config_and_local_shutdown_only(self):
        from studio_platform.repository import request_hash
        controller=create_controller(self.path,repository=self.repo,settings=SimpleNamespace())
        controller.inventory_refresh=None  # No provider traffic in this file-based test.
        controller.tick()
        path=self.root/"work"/"controller-status.json"
        status=json.loads(path.read_text())
        self.assertEqual(status["runtime_config_sha256"],request_hash(self.config))
        self.assertEqual(status["controller_id"],controller.leader_id)
        self.assertEqual(status["state"],"running")
        self.assertFalse(status["local_connections_released"])
        controller.request_shutdown();controller.tick();controller.shutdown_status()
        status=json.loads(path.read_text())
        self.assertEqual(status["state"],"shutdown_complete")
        self.assertTrue(status["local_connections_released"])
        self.assertFalse(status["cloud_removal_confirmed"])
        self.assertFalse(status["billing_settled"])
        self.assertEqual(list((self.root/"work").glob("*.tmp")),[])

    def test_source_hash_and_identity_checked_for_every_slot(self):
        for index in range(2):
            path=Path(self.binding["boot"]["source_dirs"][index])/"wangp-runtime.json"
            original=path.read_bytes()
            path.write_bytes(original+b" ")
            with self.assertRaisesRegex(OperatorError,"source_changed"): create_registry(self.path)
            path.write_bytes(original)
        self.binding["boot"]["source_dirs"][1]=self.binding["boot"]["source_dirs"][0]
        self.save_binding()
        with self.assertRaisesRegex(OperatorError,"not_unique"): create_registry(self.path)

    def test_hash_agreement_does_not_excuse_wrong_slot_profile(self):
        path=Path(self.binding["boot"]["source_dirs"][1])/"wangp-runtime.json"
        value=json.loads(path.read_text());value["profile_slot_index"]=0
        self.write(path,value)
        self.binding["boot"]["source_sha256"][1][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        self.save_binding()
        with self.assertRaisesRegex(OperatorError,"slot_identity_mismatch"): create_registry(self.path)

    def test_changed_source_is_rechecked_before_reservation(self):
        _,registry,providers=_assemble(self.path,clock=lambda:self.now)
        binding=registry.get(self.binding["binding_id"])
        provider=providers[binding.binding_id]
        kwargs={"physical_gpus":2,"slots":2,"reserved_cost_microusd":4_760_000,"hard_deadline":self.now+7200}
        provider.validate_launch(binding.launch,**kwargs)
        (Path(binding.boot["source_dirs"][0])/"wangp-bootstrap.py").write_text("changed")
        with self.assertRaisesRegex(OperatorError,"source_changed"): provider.validate_launch(binding.launch,**kwargs)

    def test_provider_filters_template_and_money_must_match(self):
        for key,value in (("minimum_ram_gib",128),("gpu_count",1),("execution_slots",1),
            ("max_price_per_gpu_hour_microusd",1_200_000),("server_side_selection",False),
            ("model_id","wrong-model"),("template_id","b1111111-1111-4111-8111-111111111111"),
            ("approved_until",2000)):
            with self.subTest(key=key):
                self.write(self.manifest_path,{**self.provider,key:value})
                with self.assertRaises(OperatorError): create_registry(self.path)
        self.write(self.manifest_path,self.provider)

    def test_unimplemented_cpu_filter_is_explicitly_rejected(self):
        self.binding["filters"]["min_cpu_cores"]=12;self.save_binding()
        with self.assertRaisesRegex(OperatorError,"cpu_filter_unsupported"): create_registry(self.path)

    def test_unsupported_profile_and_inadequate_ttl_rejected(self):
        original=self.binding["runtime_profile_id"]
        self.binding["runtime_profile_id"]="not-supported";self.save_binding()
        with self.assertRaises(ValueError): create_registry(self.path)
        self.binding["runtime_profile_id"]=original
        self.binding["min_ttl_seconds"]=3600;self.save_binding()
        with self.assertRaisesRegex(OperatorError,"provider_ttl_mismatch"): create_registry(self.path)

    def test_offers_redact_provider_details_and_never_claim_live_price(self):
        _,registry,providers=_assemble(self.path,clock=lambda:self.now)
        chosen={"runtime_profile_id":self.profile,"mode":"fl","gpu_type":self.binding["gpu_type"],
            "gpu_count":2,"node_count":1,"ttl_seconds":120,"filters":{}}
        provider=providers[self.binding["binding_id"]]
        with patch.object(provider,"preflight_availability",return_value=None) as call:
            value=registry.offers(chosen,self.now)
            self.assertEqual(value["status"],"available")
            self.assertEqual(value["offers"],[])
            self.assertEqual(value["hourly_cost_basis"],"approved_ceiling")
            call.assert_called_once()
        with patch.object(provider,"preflight_availability",side_effect=ValueError("private response")):
            self.assertNotIn("private response",json.dumps(registry.offers(chosen,self.now)))

    def test_controller_passes_exact_binding_to_existing_boot_factory(self):
        factory=lambda *args:args
        controller=create_controller(self.path,repository=self.repo,settings=SimpleNamespace(),boot_factory=factory)
        binding=controller.service.registry.get(self.binding["binding_id"])
        value=controller.boot_factory(binding,{"id":"test-intent"},{"mode":"fl"})
        self.assertIs(value[0],self.repo)
        self.assertEqual(value[2],binding)
        self.assertEqual(value[5]["config_path"],str(self.path))

    def test_environment_selects_concrete_factory(self):
        from studio_platform.operator_capacity import OperatorRegistry
        with patch.dict("os.environ",{"H3_OPERATOR_RUNTIME_CONFIG":str(self.path)}):
            registry=OperatorRegistry.from_environment()
        self.assertEqual(len(registry.bindings),1)
        with patch.dict("os.environ",{"H3_OPERATOR_RUNTIME_CONFIG":str(self.path),
                "H3_OPERATOR_CAPACITY_REGISTRY":str(self.registry_path)}):
            with self.assertRaisesRegex(OperatorError,"configuration_conflict"):
                OperatorRegistry.from_environment()
