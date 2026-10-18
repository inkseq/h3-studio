"""One real ledger handoff with fake local evidence; no cloud/SSH/SDK calls."""
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import insert, select, update

from studio_platform.autoscale import ScalePolicy
from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.live_runtime_handoff import (SAFE_PROOF_FIELDS, lookup, prepare, verify)
from studio_platform.on_demand_scaler import OnDemandConfig, cycle_config
from studio_platform.production_scaler import FiniteController, MODEL, verify_policy
from studio_platform.qualification_profiles import (MULTIMODAL_INPUT_LIMITS,
    MULTIMODAL_PROFILE, QUEUED_TASK_PROFILE)
from studio_platform.repository import (Conflict, Scope, attempts, budget_accounts,
    budget_reservations, capacity_approvals, capacity_cycles, capacity_waiters,
    instance_intents, jobs, plans, request_hash, scaler_actions, scaler_leaders,
    scaler_receipts, registered_workers)
from studio_platform.scaler import LaunchSpec, ProviderFact
from studio_platform.settings import Settings
from studio_platform.source_snapshot import source_snapshot
import test_platform_api as api_fixtures
import test_platform_execution_policy as policy_fixtures
import test_platform_production_scaler as scaler_fixtures
import test_platform_repository as repository_fixtures


class LiveRuntimeHandoffTests(repository_fixtures.LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        base = scaler_fixtures.configuration(self.root,self.now)
        values = {**asdict(base),"allowed_owners":["superdan","supervan"],
            "work_dir":self.root/"old-service","max_cycles":2,
            "qualification_profile":MULTIMODAL_PROFILE,
            "launches":base.launches[:1],"manifests":base.manifests[:1],
            "scale_policy":{**base.scale_policy,"max_instances":1,"max_physical_gpus":1,"idle_before_drain_s":600}}
        self.old_service = OnDemandConfig(**values)
        self.old_policy = policy_fixtures.policy(self.now)
        self.old_policy.update(pool=self.old_service.pool,configuration_id=self.old_service.configuration_id,
            recipe_ids=list(self.old_service.recipe_ids),budget_accounts=["job-budget"])
        self.old_policy["qualification"].update(status="runtime_required",profile=MULTIMODAL_PROFILE,
            evidence_id=self.old_service.qualification_evidence_id,expires_at=self.now+7000)
        self.old_policy["reservation"].update(expected_runtime_s=1800,cost_microusd=800000,expires_at=self.now+7000)
        self.old_policy["envelope"].update(max_duration_seconds=6,max_reference_files=3,max_guides=1,
            allow_first_last=True,input_limits=copy.deepcopy(MULTIMODAL_INPUT_LIMITS))
        self.old_policy["envelope"]["controls"].update(video_decode=["tiled"],encoder_device=["cpu"],ref_image_size=["max"])
        self.old_service = replace(self.old_service,execution_policy_sha256=request_hash(self.old_policy))
        self.new_policy = copy.deepcopy(self.old_policy)
        self.new_policy.update(pool="queued-pool",configuration_id="queued-config",revision="queued-transition")
        self.new_policy["qualification"].update(profile=QUEUED_TASK_PROFILE,evidence_id="queued-real-task-required")
        self.new_policy["reservation"]["duration_reference_seconds"] = 124/24
        self.new_policy["envelope"]["max_duration_seconds"] = 362/24
        self.new_service = replace(self.old_service,pool=self.new_policy["pool"],
            configuration_id=self.new_policy["configuration_id"],capacity_approval_id="queued-approval",
            cycle_id="queued-service",work_dir=self.root/"new-service",qualification_profile=QUEUED_TASK_PROFILE,
            qualification_evidence_id=self.new_policy["qualification"]["evidence_id"],
            execution_policy_sha256=request_hash(self.new_policy),
            launches=[{**self.old_service.launches[0],"configuration_id":self.new_policy["configuration_id"]}],
            manifests=[{**self.old_service.manifests[0],"configuration_id":self.new_policy["configuration_id"]}])
        self.old,self.new = cycle_config(self.old_service,1),cycle_config(self.new_service,1)
        self.repo.configure_capacity(max_instances=1,max_physical_gpus=1)
        for config in (self.old,self.new):
            self.repo.configure_pool(config.pool,max_instances=1,max_physical_gpus=1)
        self.repo.configure_budget("finite-budget",tenant_id="sixnine",limit_microusd=6_000_000)
        self.repo.configure_budget("job-budget",tenant_id="sixnine",limit_microusd=30_000_000)
        for config,value,enabled in ((self.old,self.old_policy,True),(self.new,self.new_policy,False)):
            self.repo.approve_capacity(config.capacity_approval_id,tenant_id=config.tenant,pool=config.pool,
                model_id=MODEL,configuration_id=config.configuration_id,recipe_ids=list(config.recipe_ids),
                policy_hash=config.execution_policy_sha256,qualification_evidence_id=config.qualification_evidence_id,
                qualification_expires_at=value["qualification"]["expires_at"],quote_expires_at=value["reservation"]["expires_at"],
                expires_at=min(config.stop_claiming_at,value["qualification"]["expires_at"],value["reservation"]["expires_at"]),
                launch=LaunchSpec(**config.launches[0]),scale_policy=ScalePolicy(**config.scale_policy),
                budget_scope=config.scope,budget_account_ids=config.budget_account_ids,enabled=enabled)
        self.path = self.root/"policy.json"
        self.write_policy(self.old_policy)
        self.settings = Settings(self.old.data_dir,database_url=self.url,auth_mode="password",
            public_origin="https://www.sixnine.art",generation_enabled=True,execution_backend="comfy-worker",
            execution_policy_file=self.path)
        request = api_fixtures.generation_request()
        request["controls"].update(duration=5,steps=50,resolution="768P",video_decode="tiled",encoder_device="cpu")
        compiled,fingerprint = compile_request(request,lambda _:None)
        self.project = api_fixtures.project()
        compiled["server_source_hash"] = source_snapshot(self.project,"shot-one")
        self.scope = Scope("sixnine","superdan","story-one")
        self.repo.put_document(Scope("sixnine","superdan","__projects"),"project","story-one",self.project)
        admission = ExecutionPolicies(self.settings,self.repo).evaluate(compiled,self.scope,fingerprint)
        self.assertTrue(admission.execution["enabled"],admission.execution)
        self.plan = self.repo.create_plan(self.scope,compiled,admission.execution,
            expires_at=admission.expires_at,estimated_cost_microusd=admission.cost)
        self.job = self.repo.create_job(self.scope,self.plan["id"],"one-original-job",
            initial_status="waiting_capacity",budget_account_ids=admission.execution["budget_account_ids"])
        self.intent = self.repo.reserve_instance_intent(self.old.scope,self.old.pool,"one-original-rental",
            physical_gpus=1,slots=1,reserved_cost_microusd=2_000_000,hard_deadline=self.old.hard_deadline,
            budget_account_ids=self.old.budget_account_ids,provider="lium",dry_run=False)
        self.pod = str(uuid.UUID(int=44))
        self.repo.update_instance(self.intent["id"],"creating")
        self.repo.update_instance(self.intent["id"],"starting",provider_instance_id=self.pod)
        with self.repo.transaction() as conn:
            conn.execute(insert(scaler_actions).values(intent_id=self.intent["id"],pool=self.old.pool,
                launch_spec=self.old.launches[0],create_started_at=self.now,
                last_observation=asdict(ProviderFact("running",self.pod)),last_observed_at=self.now))
            conn.execute(insert(scaler_leaders).values(pool=self.old.pool,leader_id="old-running-controller",
                fence=7,expires_at=self.now+180,consecutive_breaches=0,sequence=4))
            conn.execute(insert(capacity_cycles).values(approval_id=self.old.capacity_approval_id,
                intent_id=self.intent["id"],created_at=self.now))
            conn.execute(update(capacity_waiters).where(capacity_waiters.c.job_id == self.job["id"])
                .values(intent_id=self.intent["id"]))
        self.now += 30
        self.proof = {"version":1,"operation_id":str(uuid.uuid4()),"sequence":1,
            "job_id":self.job["id"],"intent_id":self.intent["id"],"provider_instance_id":self.pod,
            "old_config_hash":self.old.fingerprint(),"new_config_hash":self.new.fingerprint(),
            "leader":{"leader_id":"old-running-controller","fence":7},
            "frozen":{"config_hash":self.old.fingerprint(),"running":True,"paused":True,
                "continuous_paused":True,"children_paused":True,"restart_count":0,"pid":77,
                "started_at":self.now-40,"frozen_at":self.now-10,"observed_at":self.now},
            "runtime":{"provider":"lium","provider_running":True,"instance_id":self.pod,
                "configuration_id":self.old.configuration_id,"source_sha256":self.old.source_sha256,
                "safe_deadline":self.old.hard_deadline,"queue_running":0,"queue_pending":0,
                "known_smokes_terminal":True,"unknown_submission":False,
                "smoke_inventory_sha256":"1"*64,"terminal_smokes_sha256":"2"*64,"observed_at":self.now}}

    def write_policy(self,value):
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def prepare(self,*,apply=False,proof=None):
        return prepare(self.repo,self.old,self.new,proof or self.proof,
            old_policy=self.old_policy,new_policy=self.new_policy,apply=apply)

    def db_hash(self):
        with self.repo.engine.connect() as conn:
            return request_hash({t.name:[dict(r) for r in conn.execute(select(t)).mappings()]
                for t in (jobs,plans,attempts,budget_accounts,budget_reservations,instance_intents,
                    scaler_actions,scaler_leaders,scaler_receipts,capacity_cycles,capacity_approvals,capacity_waiters)})

    def adoption(self,receipt):
        return {**receipt,"host_retirement_confirmed":True,"runtime_adoption":{
            "prepared":True,"config_hash":self.new.fingerprint(),"intent_id":self.intent["id"],
            "instance_id":self.pod,"source_sha256":self.new.source_sha256,"profile":QUEUED_TASK_PROFILE,
            "generation_verified":False,"synthetic_receipts_archived":True,"no_bootstrap_restart":True,
            "safe_deadline":receipt["physical_deadline"],"queue_running":0,"queue_pending":0,
            "proof_sha256":"4"*64,"ledger_handoff_sha256":request_hash(receipt),
            "observed_at":self.now}}

    def test_dry_run_and_apply_preserve_rental_billing_request_plan_budget_and_ttl(self):
        before = self.db_hash()
        dry = self.prepare()
        self.assertEqual(dry["phase"],"dry_run")
        self.assertEqual(self.db_hash(),before)
        job_before = self.repo.get_job(self.scope,self.job["id"])
        budget_before = [self.repo.get_budget(b) for b in ("finite-budget","job-budget")]
        receipt = self.prepare(apply=True)
        self.assertEqual(set(receipt),set(SAFE_PROOF_FIELDS))
        self.assertEqual(lookup(self.repo,receipt["operation_id"]),receipt)
        self.assertTrue(verify(self.repo,self.old,self.new,receipt)["verified"])
        after = self.repo.get_job(self.scope,self.job["id"])
        for k in ("id","request","request_hash","plan_id","expected_runtime_s","estimated_cost_microusd", "created_at","attempt_no","not_before"):
            self.assertEqual(after[k],job_before[k])
        self.assertEqual([self.repo.get_budget(b) for b in ("finite-budget","job-budget")],budget_before)
        physical = self.repo.list_instance_intents(pool=self.new.pool)[0]
        self.assertEqual(physical["id"],self.intent["id"])
        self.assertEqual(physical["provider_instance_id"],self.pod)
        self.assertEqual(physical["hard_deadline"],self.old.hard_deadline)
        self.assertEqual(physical["billing_status"],"pending")
        self.assertEqual(physical["reserved_cost_microusd"],2_000_000)
        self.assertNotIn("prompt",json.dumps(receipt))

    def test_original_web_source_activates_and_worker_claims_once_after_handoff(self):
        receipt = self.prepare(apply=True)
        self.assertTrue(verify(self.repo,self.old,self.new,self.adoption(receipt),release_leader=True)["activated"])
        self.write_policy(self.new_policy)
        verify_policy(self.new,self.settings)
        provider = scaler_fixtures.FakeProvider(lambda:self.now)
        controller = FiniteController(self.repo,self.settings,self.new,provider=provider)
        control = WorkerControl(self.repo)
        control.register(WorkerSpec("new-worker",self.new.pool,"lium",self.pod,("GPU-same-physical",),
            tuple(self.new.recipe_ids),MODEL,self.new.configuration_id))
        control.mark_ready("new-worker",upstream_idle_confirmed=True)
        result = controller.cold.advance_once(self.new.capacity_approval_id)
        self.assertEqual((result["activated"],result["failed"]),(1,0))
        claim = control.claim("new-worker",self.new.pool,job_filter=controller.scope_filter(),job_allowed=controller.job_allowed)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.job["id"],self.job["id"])
        self.assertEqual(claim.job["request"],self.job["request"])
        self.assertEqual(claim.job["attempt_no"],1)
        self.assertIsNone(control.claim("new-worker",self.new.pool))
        self.assertEqual(provider.creates,[])
        self.assertEqual(provider.destroys,[])

    def test_unknown_smoke_or_nonidle_or_stale_proof_never_mutates(self):
        before = self.db_hash()
        for field,value in (("unknown_submission",True),("known_smokes_terminal",False),
                ("queue_running",1),("queue_pending",1),("provider_running",False),
                ("source_sha256",{}),("safe_deadline",self.old.hard_deadline+1),("observed_at",self.now-121)):
            proof = copy.deepcopy(self.proof)
            proof["runtime"][field] = value
            with self.subTest(field=field),self.assertRaises(Conflict):
                self.prepare(apply=True,proof=proof)
            self.assertEqual(self.db_hash(),before)

    def test_other_global_unknown_rental_blocks_transfer_without_settling_it(self):
        with self.repo.transaction() as conn:
            conn.execute(insert(instance_intents).values(id=str(uuid.uuid4()),intent_key="other-unknown",
                pool="another-pool",request_hash="3"*64,state="creation_unknown",physical_gpus=1,
                slots=1,reserved_cost_microusd=1_000_000,hard_deadline=self.old.hard_deadline,
                provider="lium",created_at=self.now,updated_at=self.now))
        before = self.db_hash()
        with self.assertRaisesRegex(Conflict,"other_global_rental"):
            self.prepare(apply=True)
        self.assertEqual(self.db_hash(),before)

    def test_historical_retired_worker_does_not_require_erasing_history(self):
        spec = asdict(WorkerSpec("historical-worker",self.old.pool,"lium",str(uuid.UUID(int=43)),
            ("GPU-old-cycle",),tuple(self.old.recipe_ids),MODEL,self.old.configuration_id))
        with self.repo.transaction() as conn:
            conn.execute(insert(registered_workers).values(id="historical-worker",pool=self.old.pool,
                provider="lium",instance_id=spec["instance_id"],spec=spec,spec_hash=request_hash(spec),
                state="retired",current_job_id=None,drain_requested=1,fence=3,
                expires_at=self.now-10,updated_at=self.now-10))
        receipt = self.prepare(apply=True)
        self.assertTrue(verify(self.repo,self.old,self.new,receipt)["verified"])
        with self.repo.engine.connect() as conn:
            row = conn.execute(select(registered_workers).where(registered_workers.c.id == "historical-worker")).mappings().one()
        self.assertEqual(row["state"],"retired")
        self.assertEqual(row["pool"],self.old.pool)

    def test_current_pod_worker_is_not_silently_rebound_or_retired(self):
        control = WorkerControl(self.repo)
        control.register(WorkerSpec("old-current-worker",self.old.pool,"lium",self.pod,
            ("GPU-live",),tuple(self.old.recipe_ids),MODEL,self.old.configuration_id))
        before = self.db_hash()
        with self.assertRaisesRegex(Conflict,"registered_worker"):
            self.prepare(apply=True)
        self.assertEqual(self.db_hash(),before)
        self.assertEqual(control.get("old-current-worker")["state"],"registered")

    def test_started_attempt_even_waiting_label_is_never_transferred(self):
        with self.repo.transaction() as conn:
            conn.execute(insert(attempts).values(id=str(uuid.uuid4()),job_id=self.job["id"],number=1,
                status="submission_unknown",fence=1,worker_id="old-worker",created_at=self.now,
                updated_at=self.now,submission_started_at=self.now))
        before = self.db_hash()
        with self.assertRaisesRegex(Conflict,"submission"):
            self.prepare(apply=True)
        self.assertEqual(self.db_hash(),before)

    def test_source_changed_refuses_without_changing_payload_or_reservation(self):
        project = copy.deepcopy(self.project)
        project["entities"][-1]["description"] = "User changed original creative direction"
        self.repo.put_document(Scope("sixnine","superdan","__projects"),"project","story-one",project,expected_version=1)
        before = self.db_hash()
        with self.assertRaisesRegex(Conflict,"original_source_changed"):
            self.prepare(apply=True)
        self.assertEqual(self.db_hash(),before)

    def test_failure_during_atomic_mutation_rolls_back_every_binding(self):
        before = self.db_hash()
        with patch.object(self.repo,"_emit",side_effect=RuntimeError("offline injected transaction failure")):
            with self.assertRaises(RuntimeError):
                self.prepare(apply=True)
        self.assertEqual(self.db_hash(),before)

    def test_activation_requires_retired_host_and_runtime_adoption_before_enable(self):
        receipt = self.prepare(apply=True)
        before = self.db_hash()
        with self.assertRaisesRegex(Conflict,"host_retirement"):
            verify(self.repo,self.old,self.new,receipt,release_leader=True)
        with self.assertRaisesRegex(Conflict,"prepared_runtime"):
            verify(self.repo,self.old,self.new,{**receipt,"host_retirement_confirmed":True},release_leader=True)
        self.assertEqual(self.db_hash(),before)
        self.assertTrue(verify(self.repo,self.old,self.new,receipt)["verified"])

    def test_adoption_does_not_reenable_second_time_or_hide_changed_budget(self):
        receipt = self.prepare(apply=True)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_accounts).where(budget_accounts.c.id == "finite-budget").values(spent_microusd=1))
        with self.assertRaisesRegex(Conflict,"budget_changed"):
            verify(self.repo,self.old,self.new,self.adoption(receipt),release_leader=True)

    def test_lost_activation_response_can_be_verified_without_second_activation(self):
        receipt = self.prepare(apply=True)
        verify(self.repo,self.old,self.new,self.adoption(receipt),release_leader=True)
        result = verify(self.repo,self.old,self.new,receipt)
        self.assertTrue(result["already_activated"])
        self.assertTrue(result["activated"])
        before = self.db_hash()
        self.assertTrue(verify(self.repo,self.old,self.new,receipt,release_leader=True)["already_activated"])
        self.assertEqual(self.db_hash(),before)

    def test_changed_immutable_grant_is_refused_even_when_approval_hash_recomputed(self):
        receipt = self.prepare(apply=True)
        with self.repo.transaction() as conn:
            row = conn.execute(select(capacity_approvals).where(capacity_approvals.c.id == self.new.capacity_approval_id)).mappings().one()
            payload = {**row["payload"],"quote_expires_at":row["payload"]["quote_expires_at"]+1}
            conn.execute(update(capacity_approvals).where(capacity_approvals.c.id == row["id"])
                .values(payload=payload,approval_hash=request_hash(payload)))
        with self.assertRaisesRegex(Conflict,"grant_changed"):
            verify(self.repo,self.old,self.new,self.adoption(receipt),release_leader=True)

    def test_no_zero_billing_and_physical_deadline_is_not_extended_by_new_service_window(self):
        old_hard = self.old.hard_deadline
        self.new = replace(self.new,hard_deadline=old_hard+18000,authorization_extension_s=18000,
            scale_policy={**self.new.scale_policy,"hard_deadline":old_hard+18000})
        self.proof["new_config_hash"] = self.new.fingerprint()
        # Approval is immutable; the operator creates the correctly authorized
        # new identity before applying the handoff. No rental occurs here.
        with self.repo.transaction() as conn:
            row = conn.execute(select(capacity_approvals).where(capacity_approvals.c.id == self.new.capacity_approval_id)).mappings().one()
            payload = {**row["payload"],"scale_policy":self.new.scale_policy}
            conn.execute(update(capacity_approvals).where(capacity_approvals.c.id == self.new.capacity_approval_id)
                .values(payload=payload,approval_hash=request_hash(payload)))
        receipt = self.prepare(apply=True)
        self.assertEqual(receipt["physical_deadline"],old_hard)
        self.assertLessEqual(receipt["waiter_deadline"],self.job["execution_plan"]["qualification_expires_at"])
        self.assertEqual(self.repo.list_instance_intents(pool=self.new.pool)[0]["billing_status"],"pending")


if __name__ == "__main__":
    unittest.main()
