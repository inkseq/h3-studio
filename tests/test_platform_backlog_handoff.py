"""Offline operator reconciliation: real ledger and fake provider, no rentals."""
import copy
from dataclasses import replace
import json
import unittest
import uuid

from sqlalchemy import delete, select, update

import test_platform_on_demand_scaler as fixtures
from studio_platform.backlog_handoff import prepare, verify
from studio_platform.on_demand_scaler import OnDemandController, cycle_config
from studio_platform.repository import (Conflict, budget_reservations, instance_intents,
    jobs, scaler_actions, scaler_receipts)
from studio_platform.scaler import ProviderFact
from studio_platform.production_scaler import FiniteController, ScalerError


class BacklogHandoffTests(fixtures.OnDemandTests):
    def make_unknown(self):
        def no_response(tag, launch, **kwargs):
            self.provider.creates.append((tag,launch))
            return ProviderFact('unknown')
        self.provider.create = no_response
        self.scope,self.job = self.submit()
        for _ in range(7):
            self.tick()
            if self.repo.list_instance_intents():
                break
        with self.repo.engine.connect() as connection:
            intent = dict(connection.execute(select(instance_intents)).mappings().one())
            action = dict(connection.execute(select(scaler_actions)).mappings().one())
            unknown = connection.execute(select(scaler_receipts.c.observed_at).where(
                scaler_receipts.c.operation == 'create')).scalar_one()
        self.now += 90
        key_id = str(uuid.UUID(int=90))
        self.proof = {'version':1,'sequence':1,'intent_id':intent['id'],
            'frozen':{'config_hash':self.config.fingerprint(),'running':True,'paused':True,
                'restart_count':0,'pid':77,'process_count':1,'boot_children':0,
                'started_at':self.config.created_at-10,'frozen_at':self.now},
            'audit':{'service':'lium','profile':'lium--rig-root','base_url':'https://lium.io/api',
                'http_status':200,'items':[],'next_cursor':None,'pod_tag':'sixnine-'+intent['id'],
                'live_tag_matches':0,'billed_tag_matches':0,'since':action['create_started_at']-100,
                'observed_at':self.now,'first_unknown_at':unknown,
                'account_id':str(uuid.UUID(int=91)),'api_key_id':key_id,
                'request_filter':{'method':'POST','route':'/executors/{executor_uuid}/rent',
                    'executor_uuid':action['launch_spec']['offer_id']},
                'positive_controls':[{'pod_id':str(uuid.UUID(int=n)),'action':'pod.create',
                    'method':'POST','status_code':200,'actor_key_id':key_id,
                    'route':'/executors/{executor_uuid}/rent',
                    'executor_uuid':action['launch_spec']['offer_id']} for n in (92,93)]}}
        return intent

    def test_prepare_releases_only_evidenced_instance_preserving_job_and_limits(self):
        intent = self.make_unknown()
        before = self.repo.get_job(self.scope,self.job['id'])
        budget = self.repo.get_budget('finite-budget')
        job_budget = self.repo.get_budget('job-budget')
        dry = prepare(self.repo,self.config,self.proof)
        self.assertEqual(dry['phase'],'dry_run')
        self.assertEqual(self.repo.list_instance_intents()[0]['state'],'creation_unknown')
        receipt = prepare(self.repo,self.config,self.proof,apply=True)
        self.assertEqual(receipt['next_sequence'],2)
        self.assertEqual(receipt['hard_deadline'],self.config.hard_deadline)
        self.assertEqual(self.repo.get_job(self.scope,self.job['id']),before)
        self.assertEqual(self.repo.get_budget('job-budget'),job_budget)
        after = self.repo.get_budget('finite-budget')
        self.assertEqual(after['limit_microusd'],budget['limit_microusd'])
        self.assertEqual(after['spent_microusd'],budget['spent_microusd'])
        self.assertEqual(after['reserved_microusd'],0)
        self.assertEqual(self.repo.list_instance_intents()[0]['id'],intent['id'])
        self.assertEqual(self.repo.list_instance_intents()[0]['state'],'destroyed')
        self.assertTrue(verify(self.repo,self.config,receipt)['verified'])
        self.assertEqual(len(self.provider.creates),1)
        self.assertEqual(self.provider.destroys,[])

    def test_account_audit_must_be_complete_post_freeze_same_account_with_controls(self):
        self.make_unknown()
        edits = [lambda p:p['audit'].update(next_cursor='another'),
            lambda p:p['audit'].update(items=[{'method':'POST'}]),
            lambda p:p['audit'].update(observed_at=self.now-1),
            lambda p:p['audit'].update(request_filter={}),
            lambda p:p['audit']['positive_controls'][0].update(actor_key_id=str(uuid.UUID(int=94))),
            lambda p:p['audit'].update(first_unknown_at=self.now-10)]
        for edit in edits:
            p=copy.deepcopy(self.proof);edit(p)
            with self.subTest(edit=edit),self.assertRaises(Conflict):
                prepare(self.repo,self.config,p,apply=True)
        self.assertEqual(self.repo.list_instance_intents()[0]['state'],'creation_unknown')
        self.assertEqual(self.repo.get_job(self.scope,self.job['id'])['status'],'waiting_capacity')

    def test_controller_freeze_must_cover_original_creation_and_have_no_children(self):
        self.make_unknown()
        for key,value in [('paused',False),('boot_children',1),('process_count',2),
                          ('restart_count',1),('started_at',self.now-1),('frozen_at',self.now-181)]:
            p=copy.deepcopy(self.proof);p['frozen'][key]=value
            with self.subTest(key=key),self.assertRaises(Conflict):
                prepare(self.repo,self.config,p,apply=True)

    def test_already_claimed_or_submitting_job_cannot_be_handed_off(self):
        self.make_unknown()
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == self.job['id']).values(status='submitting'))
        with self.assertRaisesRegex(Conflict,'submission'):
            prepare(self.repo,self.config,self.proof,apply=True)
        self.assertEqual(self.repo.list_instance_intents()[0]['state'],'creation_unknown')

    def test_nonempty_provider_id_is_never_reconciled_from_empty_audit(self):
        self.make_unknown()
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).values(provider_instance_id=str(uuid.UUID(int=95))))
        with self.assertRaisesRegex(Conflict,'reconciliation'):
            prepare(self.repo,self.config,self.proof,apply=True)

    def test_resuming_requires_retired_host_and_unchanged_job_reservation_budget(self):
        self.make_unknown()
        receipt=prepare(self.repo,self.config,self.proof,apply=True)
        with self.assertRaisesRegex(Conflict,'host_retirement'):
            verify(self.repo,self.config,receipt,release_leader=True)
        receipt['host_retirement_confirmed']=True
        self.assertTrue(verify(self.repo,self.config,receipt,release_leader=True)['leader_released'])
        with self.repo.transaction() as conn:
            conn.execute(update(budget_reservations).where(
                budget_reservations.c.reference_id == self.job['id']).values(amount_microusd=1))
        with self.assertRaisesRegex(Conflict,'backlog_changed'):
            verify(self.repo,self.config,receipt,release_leader=True)

    def test_budget_window_never_extended_and_remaining_cycle_required(self):
        self.make_unknown()
        p=copy.deepcopy(self.proof);p['sequence']=self.config.max_cycles
        with self.assertRaisesRegex(Conflict,'cycle_limit'):
            prepare(self.repo,self.config,p,apply=True)
        self.now=self.config.stop_claiming_at-299
        self.proof['frozen']['frozen_at']=self.now
        self.proof['audit']['observed_at']=self.now
        with self.assertRaisesRegex(Conflict,'window_expired'):
            prepare(self.repo,self.config,self.proof,apply=True)

    def test_evidenced_retirement_allows_real_controller_transfer_without_rebilling(self):
        self.make_unknown()
        receipt=prepare(self.repo,self.config,self.proof,apply=True)
        receipt['host_retirement_confirmed']=True
        verify(self.repo,self.config,receipt,release_leader=True)
        before=self.repo.get_budget('job-budget')
        manifest={**self.config.manifests[0],'executor_id':'','compatible_gpu_names':['NVIDIA H100 80GB HBM3'],
            'minimum_vram_mib':70000,'minimum_ram_gib':64,'minimum_disk_gib':100,
            'require_docker_in_docker':True,'server_side_selection':True}
        launch={**self.config.launches[0],'offer_id':''}
        config=replace(self.config,launches=[launch],manifests=[manifest])
        state={'version':1,'config_hash':config.fingerprint(),'sequence':2,
            'created_at':config.created_at,'transfer_from':cycle_config(self.config,1).capacity_approval_id}
        (config.work_dir/'service-state.json').write_text(json.dumps(state))
        controller=OnDemandController(self.repo,self.settings,config,provider=self.provider,
            boot_factory=fixtures.HeartbeatBoot)
        controller.initialize()
        self.assertNotIn(self.config.launches[0],config.launches)
        self.assertEqual(FiniteController._managed(controller.current)[0][0]['state'],'destroyed')
        after=self.repo.get_job(self.scope,self.job['id'])
        self.assertEqual(after['id'],self.job['id'])
        self.assertEqual(after['status'],'waiting_capacity')
        self.assertEqual(after['execution_plan']['capacity_approval_id'],cycle_config(config,2).capacity_approval_id)
        self.assertEqual(after['estimated_cost_microusd'],self.job['estimated_cost_microusd'])
        self.assertEqual(after['request'],self.job['request'])
        self.assertEqual(self.repo.get_budget('job-budget'),before)
        self.assertEqual(len(self.provider.creates),1)

    def test_changed_selector_never_accepts_old_intent_without_exact_audited_retirement(self):
        self.make_unknown()
        receipt=prepare(self.repo,self.config,self.proof,apply=True)
        manifest={**self.config.manifests[0],'executor_id':'','compatible_gpu_names':['NVIDIA H100 80GB HBM3'],
            'minimum_vram_mib':70000,'server_side_selection':True}
        config=replace(self.config,launches=[{**self.config.launches[0],'offer_id':''}],manifests=[manifest])
        controller=OnDemandController(self.repo,self.settings,config,provider=self.provider,
            boot_factory=fixtures.HeartbeatBoot)
        # Build the exact cycle without initialize/transfer modifying the job.
        from studio_platform.on_demand_scaler import ServiceCycle
        current=ServiceCycle(self.repo,self.settings,cycle_config(config,2),provider=self.provider)
        with self.repo.engine.connect() as conn:
            audited=dict(conn.execute(select(scaler_receipts).where(
                scaler_receipts.c.operation == 'operator-audit')).mappings().one())
        for mutation in ['wrong_hash','wrong_model','allocated','absent_receipt']:
            with self.repo.transaction() as conn:
                fact=copy.deepcopy(audited['facts'])
                if mutation == 'wrong_hash':fact['launch_spec_sha256']='0'*64
                if mutation == 'wrong_model':fact['model_id']='wrong-model'
                conn.execute(update(scaler_receipts).where(scaler_receipts.c.id == audited['id']).values(facts=fact))
                conn.execute(update(instance_intents).where(instance_intents.c.id == receipt['intent_id'])
                    .values(provider_instance_id=str(uuid.UUID(int=99)) if mutation == 'allocated' else None))
                if mutation == 'absent_receipt':conn.execute(delete(scaler_receipts).where(scaler_receipts.c.id == audited['id']))
            with self.subTest(mutation=mutation),self.assertRaises(ScalerError):
                FiniteController._managed(current)


def load_tests(loader, tests, pattern):
    # Reuse fixture setup, without duplicating its inherited test suite.
    return unittest.TestSuite(BacklogHandoffTests(name) for name in BacklogHandoffTests.__dict__
        if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()
