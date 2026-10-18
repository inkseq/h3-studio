"""Offline proof/ledger tests; no provider credentials or real cloud calls."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import unittest

from sqlalchemy import insert, select, update

import test_platform_on_demand_scaler as fixtures
from studio_platform.on_demand_scaler import OnDemandController, cycle_config, json_config
from studio_platform.preparation_recovery import prepare, verify, OPERATION
from studio_platform.repository import (Conflict, attempts, budget_accounts, budget_reservations,
    capacity_approvals, capacity_waiters, instance_intents, jobs, scaler_receipts, plans)
from studio_platform.production_scaler import save


class PreparationFailureBoot:
    def __init__(self, repo, provider, config, intent, port, **kwargs):
        self.repo,self.config,self.intent=repo,config,intent
        self.fleet=None
        self.drained=False
        path=config.work_dir/"boot"/intent["id"]/"bootstrap-state.json"
        path.parent.mkdir(parents=True,exist_ok=True)
        save(path,{"phase":"bootstrap_failed", "identity":{"intent_id":intent["id"],
            "instance_id":intent["provider_instance_id"], "configuration_id":config.configuration_id,
            "sources":config.source_sha256}})
    def tick(self,*args,stopping=False):
        if stopping:self.drained=True
        return {"state":"draining" if stopping else "bootstrap_failed"}
    def request_drain(self):self.drained=True
    def children_done(self):return True
    def close_if_safe(self,**kwargs):return self.drained


class PreparationRecoveryTests(fixtures.OnDemandTests):
    def hold(self,second=False):
        self.controller.current.boot_factory=PreparationFailureBoot
        self.controller.boot_factory=PreparationFailureBoot
        self.scope,self.job=self.submit()
        if second:self.scope2,self.job2=self.submit("supervan","story-two")
        before=self.repo.get_budget("job-budget")
        for _ in range(4):self.tick()
        self.assertTrue(self.controller.current.preparation_hold())
        self.assertFalse(self.controller.stopping())
        self.assertEqual(self.repo.get_budget("job-budget"),before)
        return self.repo.list_instance_intents()[0]

    def failed(self,second=False):
        intent=self.hold(second)
        selected=[self.job]+([self.job2] if second else [])
        with self.repo.transaction() as conn:
            for job in selected:
                self.repo._settle(conn,"job",job["id"],0)
                conn.execute(update(jobs).where(jobs.c.id==job["id"]).values(
                    status="failed",error_code="capacity_approval_expired_or_revoked"))
                conn.execute(update(capacity_waiters).where(capacity_waiters.c.job_id==job["id"]).values(state="failed"))
            conn.execute(update(capacity_approvals).values(enabled=0))
        # Real fake-provider destroy receipt already came from the hold path.
        if intent["state"]!="destroyed":
            for _ in range(3):self.tick()
        self.repo.settle_instance_cost(intent["id"],actual_cost_microusd=120000)
        self.target=self.config
        self.proof={"version":1,"old_config_hash":self.config.fingerprint(),"sequence":1,
            "intent_id":intent["id"],"job_ids":sorted(j["id"] for j in selected),
            "retired":{"controller_exited":True,"no_restart":True,"process_count":0,"boot_children":0,"observed_at":self.now},
            "provider":{"service":"lium","profile":"lium--rig-root","base_url":"https://lium.io/api",
                "http_status":200,"deleted_pod_id":intent["provider_instance_id"],"live_pod_ids":[],
                "pagination_complete":True,"observed_at":self.now},
            "bootstrap_failure":{"intent_id":intent["id"],"instance_id":intent["provider_instance_id"],
                "config_hash":self.controller.current.config.fingerprint(),"sources":self.config.source_sha256,
                "phase":"bootstrap_failed","no_qualification_submission":True,"no_fleet":True,"safe_error":"InsufficientCacheDiskSpace"},
            "repair":{"target_config_hash":self.target.fingerprint(),"target_sources":self.target.source_sha256,
                "previous_runtime_revision":"1"*40,"target_runtime_revision":"2"*40,
                "reason":"bootstrap_cache_storage_fix","acknowledge_legacy_cancel_gap":True}}
        return intent

    def test_preparation_failure_preserves_job_reservation_deletes_gpu_and_stops_auto_rerent(self):
        intent=self.hold()
        for _ in range(15):value=self.tick()
        job=self.repo.get_job(self.scope,self.job["id"])
        self.assertEqual(job["status"],"waiting_capacity")
        self.assertEqual(job["error_code"],"capacity_bootstrap_repair_required")
        self.assertEqual(value["phase"],"awaiting_repair")
        self.assertEqual(self.repo.list_instance_intents()[0]["state"],"destroyed")
        self.assertEqual(len(self.provider.creates),1)
        self.assertEqual(self.provider.destroys,[intent["id"]])
        self.assertEqual(self.controller.sequence,1)
        self.assertFalse(value["admission_ready"])

    def test_held_job_can_cancel_and_original_deadline_is_enforced(self):
        self.hold()
        result=self.repo.request_cancel(self.scope,self.job["id"])
        self.assertEqual(result["status"],"cancelled")
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"],0)
        self.tick()
        self.assertEqual(self.repo.get_job(self.scope,self.job["id"])["status"],"cancelled")

    def test_held_job_expiry_never_renews_confirmation(self):
        self.hold()
        with self.repo.engine.connect() as conn:
            deadline=conn.execute(select(capacity_waiters.c.deadline)).scalar_one()
        self.now=deadline+1
        self.tick(seconds=0)
        result=self.repo.get_job(self.scope,self.job["id"])
        self.assertEqual(result["status"],"failed")
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"],0)
        self.assertEqual(len(self.provider.creates),1)

    def test_qualification_submission_marker_forbids_preparation_hold(self):
        class UnsafeBoot(PreparationFailureBoot):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                path=self.config.work_dir/"boot"/self.intent["id"]/"bootstrap-state.json"
                value=json.loads(path.read_text());value["smoke_submission_started"]=0
                save(path,value)
        self.controller.current.boot_factory=UnsafeBoot
        self.controller.boot_factory=UnsafeBoot
        self.submit()
        for _ in range(3):self.tick()
        self.assertTrue(self.controller.stopping())
        self.assertIsNone(self.controller.current.preparation_hold())
        self.assertEqual(len(self.provider.creates),1)

    def test_restore_original_id_idempotency_reservations_spend_and_next_cycle(self):
        self.failed()
        before=self.repo.get_job(self.scope,self.job["id"])
        gpu=self.repo.get_budget("finite-budget")
        with self.repo.engine.connect() as conn:
            oldres=dict(conn.execute(select(budget_reservations).where(
                budget_reservations.c.reference_id==self.job["id"])).mappings().one())
            original_deadline=conn.execute(select(capacity_waiters.c.deadline)).scalar_one()
        dry=prepare(self.repo,self.config,self.target,self.proof)
        self.assertEqual(dry["phase"],"dry_run")
        self.assertEqual(self.repo.get_job(self.scope,self.job["id"]),before)
        receipt=prepare(self.repo,self.config,self.target,self.proof,apply=True)
        self.assertEqual(receipt["next_sequence"],2)
        self.assertEqual(self.repo.get_budget("finite-budget"),gpu)
        restored=self.repo.get_job(self.scope,self.job["id"])
        for key in ("id","idempotency_key","request_hash","plan_id","request","created_at","estimated_cost_microusd"):
            self.assertEqual(restored[key],before[key])
        self.assertTrue(verify(self.repo,self.target,receipt)["verified"])
        receipt["host_stage_confirmed"]=True
        verify(self.repo,self.target,receipt,release_leader=True)
        state={"version":1,"config_hash":self.target.fingerprint(),"sequence":2,
            "created_at":self.target.created_at,"transfer_from":receipt["previous_approval_id"]}
        save(self.target.work_dir/"service-state.json",state)
        controller=OnDemandController(self.repo,self.settings,self.target,provider=self.provider,
            boot_factory=fixtures.HeartbeatBoot)
        controller.initialize()
        with self.repo.engine.connect() as conn:
            newres=dict(conn.execute(select(budget_reservations).where(
                budget_reservations.c.reference_id==self.job["id"])).mappings().one())
            newdeadline=conn.execute(select(capacity_waiters.c.deadline)).scalar_one()
        for key in ("id","account_id","amount_microusd","created_at"):self.assertEqual(newres[key],oldres[key])
        self.assertEqual(newres["state"],"reserved")
        self.assertLessEqual(newdeadline,original_deadline)
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"],before["estimated_cost_microusd"])
        self.assertEqual(len(self.provider.creates),1)

    def test_expired_new_plan_window_does_not_expire_an_already_accepted_wait(self):
        self.failed()
        with self.repo.transaction() as conn:
            conn.execute(update(plans).values(expires_at=self.now-1))
        self.assertEqual(prepare(self.repo,self.config,self.target,self.proof,apply=True)["phase"],"jobs_restored")

    def test_no_same_runtime_window_extension_or_unretired_host(self):
        self.failed()
        edits=[lambda p:p["repair"].update(target_runtime_revision="1"*40),
            lambda p:p["retired"].update(no_restart=False),
            lambda p:p["bootstrap_failure"].update(no_qualification_submission=False),
            lambda p:p["provider"].update(pagination_complete=False),
            lambda p:p["retired"].update(observed_at=self.now-181)]
        for edit in edits:
            p=copy.deepcopy(self.proof);edit(p)
            with self.subTest(edit=edit),self.assertRaises(Conflict):prepare(self.repo,self.config,self.target,p,apply=True)
        changed=replace(self.config,hard_deadline=self.config.hard_deadline+100,
            scale_policy={**self.config.scale_policy,"hard_deadline":self.config.hard_deadline+100})
        p=copy.deepcopy(self.proof);p["repair"]["target_config_hash"]=changed.fingerprint()
        with self.assertRaisesRegex(Conflict,"original_approval_changed"):
            prepare(self.repo,self.config,changed,p,apply=True)

    def test_cancelled_or_submitted_or_nonzero_charge_job_never_restored(self):
        self.failed()
        self.repo.request_cancel(self.scope,self.job["id"])
        self.assertTrue(self.repo.get_job(self.scope,self.job["id"])["result"]["recovery_cancel_requested"])
        with self.assertRaisesRegex(Conflict,"not_unsubmitted_zero_charge"):
            prepare(self.repo,self.config,self.target,self.proof,apply=True)

    def test_attempt_history_even_without_submission_is_rejected(self):
        self.failed()
        with self.repo.transaction() as conn:
            conn.execute(insert(attempts).values(id="a"*36,job_id=self.job["id"],number=1,status="deferred",
                fence=0,worker_id="fixture",created_at=self.now,updated_at=self.now))
        with self.assertRaisesRegex(Conflict,"not_unsubmitted_zero_charge"):
            prepare(self.repo,self.config,self.target,self.proof,apply=True)

    def test_once_only_and_cancel_after_recovery_invalidates_activation_receipt(self):
        self.failed()
        receipt=prepare(self.repo,self.config,self.target,self.proof,apply=True)
        with self.assertRaisesRegex(Conflict,"already_recovered"):
            prepare(self.repo,self.config,self.target,self.proof,apply=True)
        self.repo.request_cancel(self.scope,self.job["id"])
        with self.assertRaisesRegex(Conflict,"restored_job_changed"):
            verify(self.repo,self.target,receipt)

    def test_recovery_receipt_cannot_skip_cycle_or_drop_an_original_job(self):
        self.failed(second=True)
        receipt=prepare(self.repo,self.config,self.target,self.proof,apply=True)
        changed=copy.deepcopy(receipt);changed["next_sequence"]=3
        with self.assertRaisesRegex(Conflict,"receipt_identity_mismatch"):verify(self.repo,self.target,changed)
        changed=copy.deepcopy(receipt);del changed["restored_job_hashes"][self.job["id"]]
        with self.assertRaisesRegex(Conflict,"receipt_job_set_changed"):verify(self.repo,self.target,changed)

    def test_budget_limit_rolls_back_all_jobs_and_dry_run_accounts_for_total(self):
        self.failed(second=True)
        amount=self.job["estimated_cost_microusd"]
        with self.repo.transaction() as conn:
            conn.execute(update(budget_accounts).where(budget_accounts.c.id=="job-budget").values(limit_microusd=amount))
        for apply in (False,True):
            with self.subTest(apply=apply),self.assertRaisesRegex(Conflict,"budget_unavailable"):
                prepare(self.repo,self.config,self.target,self.proof,apply=apply)
            self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"],0)
            self.assertEqual(self.repo.get_job(self.scope,self.job["id"])["status"],"failed")
            with self.repo.engine.connect() as conn:
                self.assertFalse(conn.execute(select(scaler_receipts.c.id).where(scaler_receipts.c.operation==OPERATION)).first())

    def test_live_or_unsettled_instance_cannot_be_recovered(self):
        intent=self.failed()
        with self.repo.transaction() as conn:
            conn.execute(update(budget_reservations).where(budget_reservations.c.reference_type=="instance",
                budget_reservations.c.reference_id==intent["id"]).values(state="reserved"))
        with self.assertRaisesRegex(Conflict,"not_destroyed_and_settled"):
            prepare(self.repo,self.config,self.target,self.proof,apply=True)

    def test_concurrent_recovery_restores_budget_exactly_once(self):
        self.failed()
        def run():
            try:return prepare(self.repo,self.config,self.target,self.proof,apply=True)["phase"]
            except Conflict as exc:return str(exc)
        with ThreadPoolExecutor(max_workers=2) as executor:
            result=list(executor.map(lambda _:run(),range(2)))
        self.assertEqual(result.count("jobs_restored"),1)
        self.assertEqual(result.count("preparation_recovery_job_already_recovered"),1)
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"],self.job["estimated_cost_microusd"])

    def test_nonzero_job_charge_cancelled_and_original_expiry_rejected(self):
        self.failed()
        with self.repo.transaction() as conn:
            conn.execute(update(budget_reservations).where(budget_reservations.c.reference_id==self.job["id"])
                .values(state="settled",actual_cost_microusd=1))
        with self.assertRaisesRegex(Conflict,"job_reservation_mismatch"):
            prepare(self.repo,self.config,self.target,self.proof,apply=True)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_reservations).where(budget_reservations.c.reference_id==self.job["id"])
                .values(state="released",actual_cost_microusd=0))
            conn.execute(update(capacity_waiters).values(deadline=self.now))
        with self.assertRaisesRegex(Conflict,"wait_deadline_invalid"):
            prepare(self.repo,self.config,self.target,self.proof,apply=True)
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id==self.job["id"]).values(status="cancelled",cancel_from_status="waiting_capacity"))
        with self.assertRaisesRegex(Conflict,"not_unsubmitted_zero_charge"):
            prepare(self.repo,self.config,self.target,self.proof,apply=True)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(PreparationRecoveryTests(name) for name in PreparationRecoveryTests.__dict__ if name.startswith("test_"))


if __name__=="__main__":unittest.main()
