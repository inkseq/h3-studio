"""Explicit REF policy -> original job -> synthetic native artifacts, no GPU."""
import copy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from studio_platform.api import create_app
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies, validate_policy
from studio_platform.inference.outputs import NATIVE_DELIVERY, delivery_spec
from studio_platform.inference.wangp import WanGPBackend
from studio_platform.inference.wangp_contract import RuntimeObservation, RuntimeOutput
from studio_platform.inference.wangp_ref_compiler import H3Ref2VACompiler, RECIPE_ID
from studio_platform.lium_bootstrap import BootConfig, BootError
from studio_platform.on_demand_scaler import json_config
from studio_platform.production_scaler import verify_policy, verify_sources
from studio_platform.qualification_profiles import MULTIMODAL_INPUT_LIMITS, QUEUED_TASK_PROFILE
from studio_platform.repository import Scope, request_hash, attempts, instance_intents
from studio_platform.runtime_hosts.wangp import WanGPHost
from studio_platform.runtime_hosts.wangp_http import StagedInputs
from studio_platform.runtime_hosts.wangp_launcher import resolve_inputs
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal
from studio_platform.settings import Settings
from studio_platform.worker import WorkerRunner
from test_platform_api import generation_request, png, project
from test_platform_execution_policy import policy
from test_platform_production_scaler import configuration
from test_platform_repository import LedgerCase
from test_platform_wangp_host import FakeSession
import test_platform_wangp_ref as ref_fixture


class RefAPITests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.root = Path(self.temp.name)
        self.engine = ref_fixture.manifest()
        self.value = policy(self.now)
        self.value.update(backend="wangp-worker",recipe_ids=[RECIPE_ID],engine_manifest_digest=self.engine.digest,
            output_delivery=NATIVE_DELIVERY)
        self.value["qualification"].update(status="runtime_required",profile=QUEUED_TASK_PROFILE)
        self.value["envelope"].update(max_pixels=832*480,max_duration_seconds=124/24,max_steps=50,
            max_reference_files=3,max_guides=0,allow_first_last=False,allow_audio=True,
            controls={"sampler_name":["euler"],"scheduler":["auto"],"video_decode":["tiled"],
                      "audio_decode":["normal"],"encoder_device":["default"]},
            input_limits={**MULTIMODAL_INPUT_LIMITS,"max_image_pixels":832*480,"max_video_pixels":832*480,
                "max_video_duration_seconds":73/24,"max_audio_duration_seconds":3,
                "guide_kinds":[],"guide_recipe_ids":[],"allow_video_audio":False})
        self.path = self.root/"policy.json"
        self.write()
        self.settings = Settings(self.root/"data",auth_mode="local-test",database_url=self.url,
            generation_enabled=True,execution_backend="wangp-worker",execution_policy_file=self.path)
        for name, owner in (("test-tenant:sixnine",None),("test-owner:sixnine:superdan","superdan")):
            self.repo.configure_budget(name,tenant_id="sixnine",owner_id=owner,limit_microusd=10_000_000)
        self.control = WorkerControl(self.repo)
        self.spec = WorkerSpec("ref-slot","synthetic-pool","test-only","ref-instance",("ref-gpu",),
            (RECIPE_ID,),"MiniMax-H3-Base-BF16","synthetic-config","wangp-worker",self.engine.digest,
            output_delivery=NATIVE_DELIVERY)
        self.control.register(self.spec)
        self.control.mark_ready(self.spec.worker_id,upstream_idle_confirmed=True)
        self.app = create_app(self.settings,repository=self.repo)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.client.post("/api/auth/login",json={"username":"superdan"}).raise_for_status()
        self.client.post("/v1/projects",json={"project":project()}).raise_for_status()
        self.scope = Scope("sixnine","superdan","story-one")

    def tearDown(self):
        if getattr(self,"host",None) is not None:
            self.host.close()
        super().tearDown()

    def write(self):
        self.path.write_text(json.dumps(self.value),encoding="utf-8")
        self.path.chmod(0o600)

    def upload(self,name,body):
        response = self.client.post("/v1/assets",data={"client_project_id":"story-one","client_asset_id":name},
            files={"file":(name,body)})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()["id"]

    def body(self,all_kinds=False):
        image = self.upload("ref.png",png())
        inputs = {"images":[image]}
        if all_kinds:
            video = ref_fixture.RefOwnedMediaTests.media(self,"ref.mp4")
            audio = ref_fixture.RefOwnedMediaTests.media(self,"ref.wav")
            inputs.update(videos=[{"asset_id":self.upload("ref.mp4",video.read_bytes()),"include_audio":False}],
                          audios=[self.upload("ref.wav",audio.read_bytes())])
        return generation_request(recipe_id=RECIPE_ID,inputs=inputs,
            controls={"duration":5,"resolution":"480P","seed":"4294967295"})

    def test_explicit_ref_api_job_keeps_native_artifacts_and_retries_never_regenerate(self):
        body = self.body(all_kinds=True)
        plan = self.client.post("/v1/generation-plans",json=body)
        self.assertEqual(plan.status_code,201,plan.text)
        self.assertEqual(plan.json()["status"],"ready",plan.text)
        self.assertEqual(self.repo.list_jobs(self.scope),[])
        with self.repo.engine.connect() as conn:
            self.assertEqual(list(conn.execute(select(attempts))),[])
            self.assertEqual(list(conn.execute(select(instance_intents))),[])
        headers = {"Idempotency-Key":"ref-proof-once"}
        accepted = self.client.post("/v1/jobs",json={"plan_id":plan.json()["plan_id"]},headers=headers)
        self.assertEqual(accepted.status_code,202,accepted.text)
        job_id = accepted.json()["id"]
        again = self.client.post("/v1/jobs",json={"plan_id":plan.json()["plan_id"]},headers=headers)
        self.assertEqual(again.json()["id"],job_id)
        job = self.repo.get_job(self.scope,job_id)
        self.assertEqual(job["execution_plan"]["engine_manifest_digest"],self.engine.digest)
        self.assertEqual(delivery_spec(job)["frame_count"],124)
        inputs = StagedInputs(self.root/"staged")
        def stage(item,source,*,heartbeat):
            inputs.save(item,source)
            return item
        outputs = self.root/"outputs"
        outputs.mkdir()
        video,audio = outputs/"native.mp4",outputs/"native.wav"
        subprocess.run(["ffmpeg","-v","error","-nostdin","-f","lavfi","-i","color=red:size=832x480:rate=24",
            "-f","lavfi","-i","sine=frequency=440:sample_rate=32000","-t",str(124/24),"-c:v","libx264",
            "-threads","1","-pix_fmt","yuv420p","-c:a","aac","-ac","2",str(video)],check=True,capture_output=True,timeout=30)
        subprocess.run(["ffmpeg","-v","error","-nostdin","-f","lavfi","-i","sine=frequency=440:sample_rate=32000",
            "-t",str(124/24),"-ac","2",str(audio)],check=True,capture_output=True,timeout=30)
        received = []
        class Session(FakeSession):
            def submit_task(self,settings):
                received.append(settings)
                return super().submit_task(settings)
        session = Session()
        journal = ReceiptJournal(self.root/"receipts.sqlite",slot_key="ref-test",manifest_digest=self.engine.digest,create=True)
        host = WanGPHost(session=session,journal=journal,manifest=self.engine,output_root=outputs,
            sealed_root=self.root/"sealed",settings_resolver=lambda p:resolve_inputs(p,inputs,self.engine))
        self.host = host
        backend = WanGPBackend(enabled=True,slot_key="ref-test",manifest=self.engine,transport=host,
            compiler=H3Ref2VACompiler(self.engine,stage))
        runner = WorkerRunner(self.repo,self.app.state.storage,self.root/"worker",backend=backend,control=self.control,
            submission_guard=ExecutionPolicies(self.settings,self.repo).submission_allowed,retry_after_s=0)
        runner.run_once(self.spec.worker_id,self.spec.pool)
        self.assertEqual(session.calls,1)
        self.assertEqual(received[0]["model_type"],"minimax_h3_ref2va")
        self.assertEqual((received[0]["video_prompt_type"],received[0]["audio_prompt_type"]),("IV-U","A"))
        session.handle.observation = RuntimeObservation("succeeded",stopped=True,outputs={
            "video":RuntimeOutput(video,"video/mp4"),"audio":RuntimeOutput(audio,"audio/wav")})
        for _ in range(4):
            runner.run_once(self.spec.worker_id,self.spec.pool)
            if self.repo.get_job(self.scope,job_id)["status"] == "succeeded":
                break
        self.assertEqual(self.repo.get_job(self.scope,job_id)["status"],"succeeded")
        self.assertEqual(session.calls,1)
        artifacts = self.client.get("/v1/jobs/"+job_id+"/artifacts").json()["artifacts"]
        self.assertGreaterEqual(len(artifacts),2)
        for artifact in artifacts:
            response = self.client.get("/v1/artifacts/"+artifact["id"]+"/content")
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.content)
        self.client.post("/api/auth/login",json={"username":"supervan"}).raise_for_status()
        self.assertEqual(self.client.get("/v1/jobs/"+job_id).status_code,404)
        self.assertEqual(self.client.get("/v1/artifacts/"+artifacts[0]["id"]+"/content").status_code,404)

    def test_fl_policy_and_changed_manifest_never_admit_original_ref_plan(self):
        body = self.body()
        first = self.client.post("/v1/generation-plans",json=body)
        self.assertEqual(first.json()["status"],"ready")
        self.value.update(recipe_ids=["h3-base-fl2va-v1"],engine_manifest_digest="b"*64)
        self.write()
        blocked = self.client.post("/v1/generation-plans",json=body)
        self.assertEqual(blocked.status_code,201,blocked.text)
        self.assertEqual(blocked.json()["status"],"blocked")
        confirm = self.client.post("/v1/jobs",json={"plan_id":first.json()["plan_id"]},headers={"Idempotency-Key":"stale-ref"})
        self.assertIn(confirm.status_code,(409,422))
        caps = self.client.get("/v1/capabilities").json()
        ref = next(r for r in caps["recipes"] if r["id"] == RECIPE_ID)
        self.assertFalse(ref["enabled"])
        self.assertEqual(ref["execution_support"]["status"],"not_qualified")
        self.assertEqual(self.repo.list_jobs(self.scope),[])

    def test_explicit_cold_approval_preserves_original_reference_job_without_provider_call(self):
        from studio_platform.autoscale import ScalePolicy
        from studio_platform.scaler import LaunchSpec
        self.value.update(pool="ref-cold",configuration_id="ref-cold-config")
        self.write()
        self.repo.configure_pool("ref-cold",max_instances=1,max_physical_gpus=1)
        scale = ScalePolicy(dry_run=False,max_instances=1,max_physical_gpus=1,new_instance_physical_gpus=1,
            new_instance_slots=1,cold_start_s=30,queue_target_s=60,min_improvement_s=1,cooldown_s=0,
            idle_before_drain_s=600,approved_remaining_microusd=10_000_000,
            instance_reservation_microusd=1_000_000,hard_deadline=self.now+3400)
        self.repo.approve_capacity("ref-explicit-cold",tenant_id="sixnine",pool="ref-cold",
            model_id=self.spec.model_id,configuration_id="ref-cold-config",recipe_ids=[RECIPE_ID],
            policy_hash=request_hash(self.value),qualification_evidence_id=self.value["qualification"]["evidence_id"],
            qualification_expires_at=self.value["qualification"]["expires_at"],
            quote_expires_at=self.value["reservation"]["expires_at"],expires_at=self.now+1000,
            launch=LaunchSpec("test-only","ref-cold-config",self.spec.model_id),scale_policy=scale,
            budget_scope=Scope("sixnine","operator","capacity"),budget_account_ids=["test-tenant:sixnine"],
            backend="wangp-worker",engine_manifest_digest=self.engine.digest,output_delivery=NATIVE_DELIVERY,enabled=True)
        result = self.client.post("/v1/generation-plans",json=self.body())
        self.assertEqual(result.status_code,201,result.text)
        self.assertEqual(result.json()["status"],"ready",result.text)
        self.assertEqual(result.json()["execution"]["admission_state"],"waiting_capacity")
        submit = self.client.post("/v1/jobs",json={"plan_id":result.json()["plan_id"]},
            headers={"Idempotency-Key":"cold-ref-original"})
        self.assertEqual(submit.status_code,202,submit.text)
        job = self.repo.get_job(self.scope,submit.json()["id"])
        self.assertEqual(job["status"],"waiting_capacity")
        self.assertEqual(job["execution_plan"]["capacity_approval_id"],"ref-explicit-cold")
        self.assertEqual(job["request"]["recipe_id"],RECIPE_ID)
        self.assertEqual(delivery_spec(job)["frame_count"],124)
        with self.repo.engine.connect() as conn:
            self.assertEqual(list(conn.execute(select(attempts))),[])
            self.assertEqual(list(conn.execute(select(instance_intents))),[])

    def test_rejects_broadened_operator_scope_and_foreign_inputs(self):
        changes = [lambda p:p.pop("output_delivery"),lambda p:p["recipe_ids"].append("h3-base-fl2va-v1"),
            lambda p:p["envelope"].update(allow_first_last=True),lambda p:p["envelope"].update(max_reference_files=4),
            lambda p:p["envelope"]["input_limits"].update(max_videos=2),
            lambda p:p["envelope"]["input_limits"].update(max_video_duration_seconds=4),
            lambda p:p["envelope"]["input_limits"].update(allow_video_audio=True)]
        for change in changes:
            value = copy.deepcopy(self.value)
            change(value)
            with self.assertRaises(ValueError):
                validate_policy(value)
        body = self.body()
        self.client.post("/api/auth/login",json={"username":"supervan"}).raise_for_status()
        self.client.post("/v1/projects",json={"project":project()}).raise_for_status()
        self.assertEqual(self.client.post("/v1/generation-plans",json=body).status_code,404)

    def test_legacy_multi_recipe_policy_and_fl_worker_do_not_enable_ref(self):
        body = self.body()
        self.control.drain(self.spec.worker_id)
        old = replace(self.spec,worker_id="legacy-fl",instance_id="legacy-fl-instance",physical_gpu_ids=("legacy-fl-gpu",),
            recipe_ids=("h3-base-fl2va-v1",))
        self.control.register(old)
        self.control.mark_ready(old.worker_id,upstream_idle_confirmed=True)
        response = self.client.post("/v1/generation-plans",json=body)
        self.assertEqual(response.json()["status"],"blocked",response.text)
        self.value.update(recipe_ids=["h3-base-fl2va-v1",RECIPE_ID],configuration_id="legacy-multi-config")
        self.value.pop("output_delivery")
        before = copy.deepcopy(self.value)
        self.assertEqual(validate_policy(self.value),before)
        self.write()
        response = self.client.post("/v1/generation-plans",json=body)
        self.assertEqual(response.json()["status"],"blocked",response.text)
        self.assertTrue(any("Ref2VA" in b for b in response.json()["blockers"]))
        ref = next(r for r in self.client.get("/v1/capabilities").json()["recipes"] if r["id"] == RECIPE_ID)
        self.assertFalse(ref["enabled"])
        self.assertEqual(ref["execution_support"]["available_recipe_ids"],["h3-base-fl2va-v1"])
        self.assertEqual(self.repo.list_jobs(self.scope),[])

    def test_explicit_controller_recipe_hash_boot_manifest_and_policy_bind_together(self):
        base = configuration(self.root,self.now)
        legacy = json_config(base)
        self.assertNotIn("execution_recipe_id",legacy)
        self.assertEqual(request_hash(legacy),base.fingerprint())
        documents = {"wangp-manifest.json":self.engine.document_json.encode(),"wangp-bootstrap.py":b"# test",
            "wangp-runtime.json":b"{}","wangp-package.tar.gz":b"test-only"}
        for name, content in documents.items():
            (base.source_dir/name).write_bytes(content)
        config = replace(base,execution_backend="wangp-worker",engine_manifest_digest=self.engine.digest,
            execution_recipe_id=RECIPE_ID,output_delivery=NATIVE_DELIVERY,qualification_profile=QUEUED_TASK_PROFILE,
            source_sha256={k:hashlib.sha256(v).hexdigest() for k,v in documents.items()})
        self.value.update(pool=config.pool,configuration_id=config.configuration_id)
        self.value["qualification"]["evidence_id"] = config.qualification_evidence_id
        self.write()
        config = replace(config,execution_policy_sha256=request_hash(self.value))
        self.assertEqual(config.recipe_ids,(RECIPE_ID,))
        self.assertEqual(json_config(config)["execution_recipe_id"],RECIPE_ID)
        self.assertNotEqual(replace(config,execution_recipe_id="").fingerprint(),config.fingerprint())
        verify_policy(config,self.settings)
        verify_sources(config)
        BootConfig(config.work_dir,config.source_dir,config.ssh_key_file,config.known_hosts_file,18901,
            config.configuration_id,qualification_profile=QUEUED_TASK_PROFILE,execution_backend="wangp-worker",
            engine_manifest_digest=self.engine.digest,recipe_ids=(RECIPE_ID,),output_delivery=NATIVE_DELIVERY)
        with self.assertRaisesRegex(BootError,"recipe_manifest"):
            verify_sources(replace(config,execution_recipe_id=""))
        with self.assertRaises(ValueError):
            replace(config,output_delivery="")
