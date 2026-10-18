"""Offline pending-deletion handoff; injected Docker/systemd, no provider calls."""
from contextlib import ExitStack, nullcontext
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

from test_platform_release import module, release
from test_operator_host_release import host

with patch.dict(sys.modules, {'release':release, 'operator_capacity':host}):
    handoff = module('operator_handoff_test', 'operator_handoff.py')


def ledger_rows():
    return {
        'counts':{'active_jobs':0, 'unsafe_attempts':0, 'bound_workers':0},
        'intents':[{'id':'intent', 'state':'destroying', 'provider':'targon', 'provider_instance_id':'wkl-test',
                    'hard_deadline':1000, 'created_at':100, 'updated_at':200, 'reserved_cost_microusd':500}],
        'nodes':[{'intent_id':'intent', 'command_id':'start', 'ordinal':0, 'binding_id':'binding',
                  'binding_hash':'frozen', 'desired_state':'stopped', 'runtime_state':'removal_pending',
                  'payload':{'selection':{'node_count':1}, 'hourly_cost_microusd':100}}],
        'actions':[{'intent_id':'intent', 'destroy_started_at':200, 'last_observed_at':210,
                    'last_observation':{'state':'unknown'}, 'create_started_at':100}],
        'commands':[{'id':'start', 'kind':'start', 'state':'waiting', 'updated_at':200,
                     'payload':{'selection':{'node_count':1}, 'hard_deadline':1000}},
                    {'id':'stop', 'kind':'stop', 'state':'waiting', 'payload':{'node_id':'intent'}}],
        'accounts':[{'id':'owner', 'limit_microusd':900, 'reserved_microusd':500, 'spent_microusd':20}],
        'reservations':[{'id':'reservation', 'reference_type':'instance', 'reference_id':'intent',
                         'account_id':'owner', 'amount_microusd':500, 'state':'reserved', 'actual_cost_microusd':None}],
        'policy':[{'enabled':1, 'version':2}], 'gate':[{'max_instances':2}], 'limits':[{'pool':'existing'}],
    }


QUEUE_JOB = '00000000-0000-4000-8000-000000000120'


def queue_rows(*, queue_only=False):
    rows = ledger_rows()
    rows['counts']['active_jobs'] = 1
    rows['unsubmitted_jobs'] = [{'id':QUEUE_JOB, 'status':'queued', 'lease_worker_id':None,
        'lease_expires_at':None, 'current_attempt_id':None, 'attempt_no':0, 'fence':0,
        'attempt_count':0, 'cancel_from_status':None, 'result':None, 'request_hash':'a'*64,
        'tenant_id':'tenant', 'owner_id':'owner', 'project_id':'project', 'actor_id':'actor',
        'idempotency_key':'private-key-sentinel', 'created_at':100, 'updated_at':100,
        'request':{'prompt':'private-prompt-sentinel', 'asset_url':'https://private-media.invalid/'},
        'execution_plan':{'configuration_id':'frozen-h3', 'pool':'existing', 'model_id':'frozen-model'},
        'estimated_cost_microusd':100}]
    if queue_only:
        for section in ('intents','nodes','actions','commands','reservations'):
            rows[section] = []
        rows['accounts'][0]['reserved_microusd'] = 0
    return rows


UNKNOWN_TAG = '00000000-0000-4000-8000-000000000112'
EXECUTOR = '00000000-0000-4000-8000-000000000113'


def unknown_rows():
    rows = ledger_rows()
    rows['intents'][0].update(id=UNKNOWN_TAG, state='creation_unknown', provider='lium', provider_instance_id=None)
    rows['nodes'][0].update(intent_id=UNKNOWN_TAG, runtime_state='creation_unknown')
    rows['actions'][0].update(intent_id=UNKNOWN_TAG, destroy_started_at=None)
    rows['commands'][0].update(state='unknown')
    rows['commands'][0]['payload']['selected_offer'] = {'provider':'lium', 'offer_id':EXECUTOR}
    rows['commands'][1]['payload']['node_id'] = UNKNOWN_TAG
    rows['reservations'][0]['reference_id'] = UNKNOWN_TAG
    return rows


def unknown_marker():
    return {'version':1, 'tag':UNKNOWN_TAG, 'phase':'post_started', 'executor_id':EXECUTOR,
            'absolute_ttl':{'version':1, 'created_at':100, 'hard_deadline':1000, 'requested_hours':1,
                'deadline':1000, 'effective_deadline':1000, 'instance_id':None,
                'provider_created_at':None, 'attempts':[]}}


def unknown_receipt():
    marker = unknown_marker()
    return {UNKNOWN_TAG:{key:marker[key] for key in ('tag', 'phase', 'executor_id', 'absolute_ttl')} |
                       {'journal_sha256':'c'*64}}


class LedgerTests(unittest.TestCase):
    def summary(self, rows):
        return handoff.ledger_summary(rows, {'binding':'frozen'})

    def test_pending_stop_adopted_without_changing_original_records(self):
        rows = ledger_rows(); original = copy.deepcopy(rows)
        value = self.summary(rows)
        self.assertEqual(value['pending_ids'], ['intent'])
        self.assertEqual(rows, original)

    def test_jobs_attempts_or_workers_block_handoff(self):
        for field in ('active_jobs', 'unsafe_attempts', 'bound_workers'):
            with self.subTest(field=field):
                rows = ledger_rows(); rows['counts'][field] = 1
                with self.assertRaisesRegex(ValueError, 'ledger_unsafe'): self.summary(rows)

    def test_unknown_creation_running_nodes_and_missing_delete_proof_are_rejected(self):
        for state in ('creating', 'creation_unknown', 'running', 'draining'):
            rows = ledger_rows(); rows['intents'][0]['state'] = state
            with self.subTest(state=state), self.assertRaises(ValueError): self.summary(rows)
        for field,value in (('provider_instance_id', None),):
            rows = ledger_rows(); rows['intents'][0][field] = value
            with self.assertRaises(ValueError): self.summary(rows)
        for value in (None, True, float('nan')):
            rows = ledger_rows(); rows['actions'][0]['destroy_started_at'] = value
            with self.assertRaises(ValueError): self.summary(rows)

    def test_incomplete_start_must_not_rent_missing_ordinal_after_handoff(self):
        rows = ledger_rows(); rows['commands'][0]['payload']['selection']['node_count'] = 2
        with self.assertRaises(ValueError): self.summary(rows)
        rows = ledger_rows(); rows['nodes'][0]['ordinal'] = 1
        with self.assertRaises(ValueError): self.summary(rows)

    def test_missing_reservation_changed_binding_or_stop_intent_rejected(self):
        for section,field,value in (('nodes','binding_hash','changed'), ('nodes','desired_state','running'),
                                    ('reservations','state','settled')):
            rows = ledger_rows(); rows[section][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.summary(rows)
        rows = ledger_rows(); rows['reservations'] = []
        with self.assertRaises(ValueError): self.summary(rows)

    def test_deadline_budget_request_and_selection_changes_break_fingerprint(self):
        original = self.summary(ledger_rows())['immutable_hash']
        mutations = [('intents','hard_deadline',1001), ('intents','provider_instance_id','wkl-other'),
                     ('accounts','limit_microusd',901), ('reservations','amount_microusd',501)]
        for section,field,value in mutations:
            rows = ledger_rows(); rows[section][0][field] = value
            self.assertNotEqual(self.summary(rows)['immutable_hash'], original)
        rows = ledger_rows(); rows['nodes'][0]['payload']['selection']['gpu_count'] = 8
        self.assertNotEqual(self.summary(rows)['immutable_hash'], original)

    def test_real_terminal_progress_and_settlement_do_not_change_identity(self):
        rows = ledger_rows(); original = self.summary(rows)
        rows['intents'][0].update(state='destroyed', updated_at=300)
        rows['actions'][0].update(last_observed_at=300, last_observation={'state':'destroyed'})
        rows['commands'][0]['state'] = 'blocked'; rows['commands'][1]['state'] = 'completed'
        rows['reservations'][0].update(state='settled', actual_cost_microusd=50)
        rows['accounts'][0].update(reserved_microusd=0, spent_microusd=70)
        current = self.summary(rows)
        self.assertEqual(current['immutable_hash'], original['immutable_hash'])
        self.assertNotEqual(current['accounting_hash'], original['accounting_hash'])
        self.assertEqual(current['pending_ids'], [])


class UnsubmittedQueueLedgerTests(unittest.TestCase):
    def summary(self, rows, *, preserve=True):
        return handoff.ledger_summary(rows, {'binding':'frozen'}, preserve_unsubmitted_queue=preserve)

    def test_opt_in_snapshots_all_job_identity_request_and_configuration_without_mutation(self):
        rows = queue_rows(); original = copy.deepcopy(rows)
        value = self.summary(rows)
        snapshot = value['preserved_unsubmitted_queue'][0]
        self.assertEqual(set(snapshot), {'job_id','request_hash','request_snapshot_hash',
                                        'configuration_snapshot_hash','record_hash'})
        self.assertEqual(snapshot['job_id'], QUEUE_JOB)
        self.assertEqual(snapshot['request_hash'], 'a'*64)
        for key in ('request_snapshot_hash','configuration_snapshot_hash','record_hash'):
            self.assertRegex(snapshot[key], r'^[a-f0-9]{64}$')
        self.assertEqual(rows, original)
        serialized = json.dumps(value)
        for private in ('private-prompt-sentinel','private-key-sentinel','private-media.invalid',
                        'frozen-h3','frozen-model','actor','project'):
            self.assertNotIn(private, serialized)

    def test_default_still_rejects_even_completely_unsubmitted_queued_work(self):
        with self.assertRaisesRegex(ValueError,'ledger_unsafe'):
            self.summary(queue_rows(), preserve=False)
        value = self.summary(ledger_rows(), preserve=False)
        self.assertNotIn('preserved_unsubmitted_queue',value)
        self.assertEqual(value['immutable_hash'], 'c93fe6b4151d079457c2113d55908be901893052ef9c524dab2943a81606af43')

    def test_any_prior_claim_attempt_assignment_or_nonqueued_job_blocks(self):
        for field,value in (('status','planned'), ('status','running'), ('status','waiting_capacity'),
                           ('lease_worker_id','worker'), ('lease_expires_at',1), ('current_attempt_id','attempt'),
                           ('attempt_no',1), ('fence',1), ('attempt_count',1), ('cancel_from_status','queued'),
                           ('result',{}), ('request_hash','invalid'), ('request',None), ('execution_plan',None),
                           ('id','private-prompt-sentinel'), ('attempt_no',False), ('attempt_count',False)):
            rows = queue_rows(); rows['unsubmitted_jobs'][0][field] = value
            with self.subTest(field=field,value=value), self.assertRaisesRegex(ValueError,'ledger_unsafe'):
                self.summary(rows)
        for count in ('unsafe_attempts','bound_workers'):
            rows = queue_rows(); rows['counts'][count] = 1
            with self.subTest(count=count), self.assertRaisesRegex(ValueError,'ledger_unsafe'):
                self.summary(rows)

    def test_complete_count_and_unique_queue_are_required(self):
        for queue in (None, [], 'not-a-list'):
            rows = queue_rows(); rows['unsubmitted_jobs'] = queue
            with self.subTest(queue=queue), self.assertRaisesRegex(ValueError,'ledger_unsafe'):
                self.summary(rows)
        rows = queue_rows(); rows['unsubmitted_jobs'] *= 2; rows['counts']['active_jobs'] = 2
        with self.assertRaisesRegex(ValueError,'ledger_unsafe'): self.summary(rows)
        rows = queue_rows(); rows['counts']['active_jobs'] = True
        with self.assertRaisesRegex(ValueError,'ledger_unsafe'): self.summary(rows)

    def test_ownership_request_config_cost_and_timestamp_changes_are_fenced(self):
        original = self.summary(queue_rows())
        for field,value in (('tenant_id','another-tenant'), ('owner_id','another-owner'), ('project_id','another-project'),
                           ('actor_id','another-actor'), ('idempotency_key','another-key'), ('request_hash','b'*64),
                           ('request',{'prompt':'different'}), ('execution_plan',{'configuration_id':'different'}),
                           ('estimated_cost_microusd',200), ('updated_at',101)):
            rows = queue_rows(); rows['unsubmitted_jobs'][0][field] = value
            changed = self.summary(rows)
            with self.subTest(field=field):
                self.assertNotEqual(changed['immutable_hash'], original['immutable_hash'])
                self.assertNotEqual(changed['preserved_unsubmitted_queue'], original['preserved_unsubmitted_queue'])

    def test_snapshot_order_does_not_depend_on_input_order(self):
        rows = queue_rows()
        second = {**rows['unsubmitted_jobs'][0],'id':'00000000-0000-4000-8000-000000000119'}
        rows['unsubmitted_jobs'].append(second); rows['counts']['active_jobs'] = 2
        value = self.summary(rows)
        rows['unsubmitted_jobs'].reverse()
        self.assertEqual(self.summary(rows),value)
        self.assertEqual([row['job_id'] for row in value['preserved_unsubmitted_queue']], [second['id'],QUEUE_JOB])

    def test_queue_only_keeps_original_accounting_and_no_rental_obligation(self):
        rows = queue_rows(queue_only=True)
        value = self.summary(rows)
        self.assertEqual(value['pending_ids'], [])
        self.assertEqual(value['unknown_rent_journals'], {})
        self.assertEqual(value['accounting_hash'], handoff.ledger_summary(
            {**rows,'counts':{**rows['counts'],'active_jobs':0}}, {'binding':'frozen'})['accounting_hash'])

    def test_unknown_rental_evidence_is_preserved_under_queue_opt_in(self):
        rows = unknown_rows(); queued = queue_rows()
        rows['counts'] = queued['counts']; rows['unsubmitted_jobs'] = queued['unsubmitted_jobs']
        value = handoff.ledger_summary(rows, {'binding':'frozen'}, unknown_receipt(), preserve_unsubmitted_queue=True)
        self.assertEqual(value['unknown_rent_journals'],unknown_receipt())
        with self.assertRaisesRegex(ValueError,'ledger_unsafe'):
            handoff.ledger_summary(rows, {'binding':'frozen'}, preserve_unsubmitted_queue=True)


class UnknownLedgerTests(unittest.TestCase):
    def summary(self, rows, receipt=None):
        return handoff.ledger_summary(rows, {'binding':'frozen'}, unknown_receipt() if receipt is None else receipt)

    def test_original_unknown_stop_and_accounting_are_preserved_as_unknown(self):
        rows = unknown_rows(); original = copy.deepcopy(rows)
        result = self.summary(rows)
        self.assertEqual(result['pending_ids'], [UNKNOWN_TAG])
        self.assertEqual(result['unknown_rent_journals'], unknown_receipt())
        self.assertEqual(rows, original)
        rows['actions'][0].update(last_observed_at=400, last_observation={'state':'unknown'})
        rows['nodes'][0]['runtime_state'] = 'observation_failed'
        rows['commands'][0]['updated_at'] = 400
        self.assertEqual(self.summary(rows), result)

    def test_unknown_requires_exact_durable_journal_and_original_stop(self):
        for change in ({}, {UNKNOWN_TAG:{**unknown_receipt()[UNKNOWN_TAG], 'phase':'checking'}},
                       {UNKNOWN_TAG:{**unknown_receipt()[UNKNOWN_TAG], 'tag':EXECUTOR}},
                       {UNKNOWN_TAG:{**unknown_receipt()[UNKNOWN_TAG], 'journal_sha256':'not-a-hash'}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.summary(unknown_rows(), change)
        for section, field, value in (('nodes','desired_state','running'), ('intents','state','creating'),
            ('intents','provider','targon'), ('intents','provider_instance_id','invented-id'),
            ('actions','destroy_started_at',200), ('actions','create_started_at',None),
            ('reservations','state','settled'), ('reservations','actual_cost_microusd',0),
            ('reservations','amount_microusd',1), ('commands','state','completed')):
            rows = unknown_rows()
            rows[section][1 if section == 'commands' else 0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): self.summary(rows)
        rows = unknown_rows(); rows['commands'][1]['kind'] = 'drain'
        with self.assertRaises(ValueError): self.summary(rows)
        rows = unknown_rows(); rows['commands'][0]['payload']['selected_offer']['offer_id'] = UNKNOWN_TAG
        with self.assertRaises(ValueError): self.summary(rows)

    def test_unknown_deadline_model_budget_journal_or_missing_ordinal_fail_closed(self):
        original = self.summary(unknown_rows())
        changed = unknown_receipt(); changed[UNKNOWN_TAG]['journal_sha256'] = 'd'*64
        self.assertNotEqual(self.summary(unknown_rows(), changed)['immutable_hash'], original['immutable_hash'])
        for field in ('created_at', 'hard_deadline', 'deadline', 'effective_deadline'):
            changed = unknown_receipt(); changed[UNKNOWN_TAG]['absolute_ttl'][field] += 1
            with self.subTest(field=field), self.assertRaises(ValueError): self.summary(unknown_rows(), changed)
        rows = unknown_rows(); rows['nodes'][0]['payload']['selection']['model'] = 'different-model'
        self.assertNotEqual(self.summary(rows)['immutable_hash'], original['immutable_hash'])
        rows = unknown_rows(); rows['accounts'][0]['limit_microusd'] += 1
        self.assertNotEqual(self.summary(rows)['immutable_hash'], original['immutable_hash'])
        rows = unknown_rows(); rows['commands'][0]['payload']['selection']['node_count'] = 2
        with self.assertRaises(ValueError): self.summary(rows)
        rows['commands'][0]['state'] = 'blocked'
        with self.assertRaises(ValueError): self.summary(rows)
        for field in ('active_jobs','unsafe_attempts','bound_workers'):
            rows = unknown_rows(); rows['counts'][field] = 1
            with self.subTest(field=field), self.assertRaises(ValueError): self.summary(rows)

    def test_legacy_deletion_only_hash_matches_original_algorithm(self):
        # Recorded by the pre-change 98bf26f helper against this existing fixture.
        result = handoff.ledger_summary(ledger_rows(), {'binding':'frozen'})
        self.assertEqual(result['immutable_hash'], 'c93fe6b4151d079457c2113d55908be901893052ef9c524dab2943a81606af43')
        self.assertEqual(result['accounting_hash'], '43a6652b13c01fd1a75d35723b61168f67c18a3ebba71075995e32237fd6d45f')
        self.assertEqual(result['unknown_rent_journals'], {})


class UnknownJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root/'rent-journal'; self.directory.mkdir(mode=0o700)
        self.path = self.directory/(UNKNOWN_TAG+'.json')
        self.write(unknown_marker())

    def write(self, marker):
        self.path.write_text(json.dumps(marker)); self.path.chmod(0o600)

    def test_reads_original_raw_hash_without_creating_lock_or_rewriting_any_file(self):
        import hashlib
        before = [(p.name,p.read_bytes(),p.stat().st_mtime_ns) for p in self.directory.iterdir()]
        result = handoff.unknown_rent_journals(unknown_rows(), self.root)
        self.assertEqual(result[UNKNOWN_TAG]['journal_sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result[UNKNOWN_TAG]['absolute_ttl'], unknown_marker()['absolute_ttl'])
        self.assertEqual(before, [(p.name,p.read_bytes(),p.stat().st_mtime_ns) for p in self.directory.iterdir()])
        self.assertEqual(handoff.unknown_rent_journals(ledger_rows(), self.root/'missing'), {})

    def test_missing_marker_or_directory_never_initializes_or_repairs_it(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError): handoff.unknown_rent_journals(unknown_rows(), self.root)
        self.assertEqual(list(self.directory.iterdir()), [])
        with self.assertRaisesRegex(ValueError,'journal_unavailable'):
            handoff.unknown_rent_journals(unknown_rows(), self.root/'missing')
        self.assertFalse((self.root/'missing').exists())

    def test_original_identity_phase_deadline_and_ttl_are_required(self):
        for field,value in (('phase','checking'), ('tag',EXECUTOR), ('absolute_ttl',None)):
            marker = unknown_marker(); marker[field] = value; self.write(marker)
            with self.subTest(field=field), self.assertRaises((ValueError,TypeError)):
                handoff.unknown_rent_journals(unknown_rows(), self.root)
        marker = unknown_marker(); marker['absolute_ttl']['hard_deadline'] = 999
        marker['absolute_ttl'].update(deadline=999,effective_deadline=999); self.write(marker)
        with self.assertRaisesRegex(ValueError,'journal_mismatch'):
            handoff.unknown_rent_journals(unknown_rows(), self.root)
        marker = unknown_marker(); marker['absolute_ttl'].update(instance_id=EXECUTOR,provider_created_at=100)
        self.write(marker)
        with self.assertRaisesRegex(ValueError,'journal_mismatch'):
            handoff.unknown_rent_journals(unknown_rows(), self.root)

    def test_linked_oversized_duplicate_or_public_journal_is_rejected(self):
        linked = self.directory/'another'; os.link(self.path, linked)
        with self.assertRaisesRegex(ValueError,'not_private'):
            handoff.unknown_rent_journals(unknown_rows(), self.root)
        linked.unlink()
        self.path.write_bytes(b' '*4097)
        with self.assertRaisesRegex(ValueError,'journal_invalid'):
            handoff.unknown_rent_journals(unknown_rows(), self.root)
        raw = json.dumps(unknown_marker()).replace('"version": 1', '"version": 1, "version": 1', 1)
        self.path.write_text(raw)
        with self.assertRaisesRegex(ValueError,'journal_invalid'):
            handoff.unknown_rent_journals(unknown_rows(), self.root)
        self.write(unknown_marker())
        if os.name != 'nt':
            self.path.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'not_private'):
                handoff.unknown_rent_journals(unknown_rows(), self.root)


class HostHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(host, 'ROOT', self.root))
        self.stack.enter_context(patch.object(handoff, 'locked', side_effect=lambda:nullcontext()))
        self.stack.enter_context(patch.object(release, '_protected_json', side_effect=lambda path, **kw:json.loads(Path(path).read_text())))
        self.writes = []
        def write(path,value):
            self.writes.append((Path(path).name, copy.deepcopy(value)))
            Path(path).write_text(json.dumps(value))
        self.stack.enter_context(patch.object(host, 'atomic', side_effect=write))
        self.old = {'schema_version':1, 'commit':'a'*40, 'image_id':'old-image', 'runtime_config_sha256':'d'*64, 'files':{'runtime':'e'*64}}
        self.pin = {**host.pin_for(self.old), 'state':'running', 'admission':'closed', 'controller_id':'old-controller'}
        self.runtime = {'existing':'unchanged'}
        self.environment = {'SIXNINE_IMAGE':'sixnine-platform:'+'a'*40}
        self.next_environment = {'SIXNINE_IMAGE':'sixnine-platform:'+'b'*40}
        self.overlay = {'services':{'app':{'environment':{'SIXNINE_OPERATOR_CAPACITY_OWNERS':'superdan'}}}}
        self.unit = {'MainPID':'0','ExecMainPID':'321','ActiveState':'failed','ExecStart':'original',
                     'Restart':'no','KillMode':'process','User':'root','unit_files':{'original.service':'frozen'}}
        self.ledger = handoff.ledger_summary(ledger_rows(), {'binding':'frozen'})
        self.record = {'schema_version':1, 'phase':'drain_requested', 'target_commit':'b'*40, 'target_image_id':'new-image',
            'old_prepared':self.old, 'old_pin':{**self.pin,'admission':'open'}, 'old_overlay':self.overlay,
            'supervisor_unit':'sixnine-old.service','supervisor':{**self.unit,'MainPID':'321','ActiveState':'active'}, 'ledger':self.ledger}
        write(handoff.record_path(), self.record); write(self.root/'overlay.json', self.overlay); write(self.root/'active.json', self.pin)
        self.prepared = self.mock(host, 'prepared', return_value=(self.runtime,self.old,self.root,self.environment))
        self.mock(host, 'checked_pin', side_effect=lambda value:json.loads((self.root/'active.json').read_text()))
        self.supervisor = self.mock(handoff, 'supervisor', return_value=self.unit)
        self.inspect = self.mock(host, 'inspect_controller', return_value={'Running':False,'Restarting':False,'Paused':False,
                                        'OOMKilled':False,'Status':'exited','ExitCode':0})
        self.receipt = self.mock(host, 'receipt', return_value={'state':'shutdown_complete','local_connections_released':True})
        self.mock(host, 'no_competing_controller')
        self.probe = self.mock(handoff, 'ledger_probe', return_value=self.ledger)
        self.mock(handoff, 'current_target', return_value=('b'*40,self.root,self.next_environment,'new-image'))
        self.mock(host, 'default_profile', return_value='original-profile')
        self.mock(host, 'overlay', return_value=self.overlay)
        self.mock(host, 'compose', return_value=b'{}')
        self.mock(release, 'command', return_value=b'2.38.2')
        self.validate = self.mock(host, 'validate_rendered')
        self.launch = self.mock(host, 'launch')
        self.drain = self.mock(host, 'request_drain')

    def mock(self, owner, name, **kwargs):
        return self.stack.enter_context(patch.object(owner, name, **kwargs))

    def test_successor_keeps_original_config_and_never_clears_barrier(self):
        runtime, prepared, _, _, pin = handoff.successor()
        self.assertEqual(runtime, self.runtime)
        self.assertEqual(prepared, {**self.old,'commit':'b'*40,'image_id':'new-image'})
        self.assertTrue(pin['active']); self.assertEqual(pin['admission'], 'closed')
        record = json.loads(handoff.record_path().read_text())
        self.assertEqual(record['old_prepared'], self.old)
        self.assertEqual(record['old_pin'], self.record['old_pin'])
        self.assertEqual(record['phase'], 'launch_intent')
        self.launch.assert_not_called()
        self.validate.assert_called_once()
        self.assertEqual(self.validate.call_args.kwargs, {'owners':'superdan'})

    def test_old_supervisor_still_alive_blocks_even_after_clean_container_exit(self):
        self.supervisor.return_value = {**self.unit, 'MainPID':'321', 'ActiveState':'active'}
        with self.assertRaisesRegex(release.ReleaseError, 'old_supervisor_not_retired'): handoff.successor()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'], 'drain_requested')
        self.launch.assert_not_called()

    def test_uncertain_exit_or_local_children_block_handoff(self):
        for change in ({'ExitCode':137}, {'OOMKilled':True}, {'Running':True}, {'Paused':True}):
            original = self.inspect.return_value
            self.inspect.return_value = {**original, **change}
            with self.subTest(change=change), self.assertRaisesRegex(release.ReleaseError, 'old_exit_unconfirmed'):
                handoff.successor()
            self.inspect.return_value = original
        self.receipt.return_value = {'state':'shutdown_waiting','local_connections_released':False}
        with self.assertRaisesRegex(release.ReleaseError, 'local_ownership_unconfirmed'): handoff.successor()
        self.launch.assert_not_called()

    def test_configuration_or_ledger_change_blocks_before_pin_replacement(self):
        self.probe.return_value = {**self.ledger, 'immutable_hash':'f'*64}
        with self.assertRaisesRegex(release.ReleaseError, 'ledger_changed'): handoff.successor()
        self.assertEqual(json.loads((self.root/'active.json').read_text()), self.pin)
        self.probe.return_value = self.ledger
        self.prepared.return_value = (self.runtime,{**self.old,'files':{}},self.root,self.environment)
        with self.assertRaisesRegex(release.ReleaseError, 'configuration_changed'): handoff.successor()

    def test_post_staging_change_keeps_consumed_intent_and_blocks_replay(self):
        self.probe.side_effect = [self.ledger, {**self.ledger,'accounting_hash':'f'*64}]
        with self.assertRaisesRegex(release.ReleaseError, 'ledger_changed_before_launch'): handoff.successor()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'], 'launch_intent')
        with self.assertRaisesRegex(release.ReleaseError, 'already_launched'): handoff.successor()
        self.launch.assert_not_called()

    def test_unknown_credential_delivery_is_never_replayed(self):
        self.launch.side_effect = TimeoutError('synthetic delivery uncertainty')
        with self.assertRaises(TimeoutError): handoff.start()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'], 'launch_intent')
        with self.assertRaisesRegex(release.ReleaseError, 'already_launched'): handoff.start()
        self.launch.assert_called_once()
        self.drain.assert_called_once()

    def test_successful_start_launches_once_then_reopens_only_app(self):
        old_state = self.inspect.return_value
        self.inspect.side_effect = [old_state, {'Running':True,'Restarting':False,'OOMKilled':False}]
        self.receipt.side_effect = [self.receipt.return_value, {'state':'running','controller_id':'new-controller'}]
        process = Mock(returncode=0)
        process.poll.side_effect = [None, 0]
        def launch(*args):
            self.assertEqual(json.loads(handoff.record_path().read_text())['phase'], 'launch_intent')
            return process
        self.launch.side_effect = launch
        app = {'services':{'app':{}}}
        self.mock(host,'app_overlay',return_value=app)
        self.mock(host,'validate_app_rendered')
        self.mock(release,'wait_ready')
        restore = self.mock(host,'restore',return_value={'state':'cpu_restored'})
        with patch.object(release,'command',side_effect=[b'2.38.2',b'2.38.2',json.dumps(app).encode(),b'']) as command:
            self.assertEqual(handoff.start(),{'state':'cpu_restored'})
        self.launch.assert_called_once()
        self.assertEqual(command.call_args_list[-1].args[0][-4:], ['up','-d','--no-deps','app'])
        restore.assert_called_once()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'],'running')
        pin = json.loads((self.root/'active.json').read_text())
        self.assertEqual((pin['controller_id'],pin['admission']),('new-controller','open'))

    def configure_start_inspection(self, observations, *, original_retirement=True):
        self.inspect.side_effect = ([self.inspect.return_value] if original_retirement else []) + observations
        self.receipt.side_effect = ([self.receipt.return_value] if original_retirement else []) + [
            {'state':'running','controller_id':'new-controller'}]
        process = Mock(returncode=0)
        process.poll.side_effect = [None]*len(observations) + [0]
        self.launch.return_value = process
        app = {'services':{'app':{}}}
        self.mock(host,'app_overlay',return_value=app)
        self.mock(host,'validate_app_rendered')
        self.mock(release,'wait_ready')
        restore = self.mock(host,'restore',return_value={'state':'cpu_restored'})
        def command(arguments, **kwargs):
            if arguments[-2:] == ['version','--short']: return b'2.38.2'
            if arguments[-3:] == ['config','--format','json']: return json.dumps(app).encode()
            return b''
        self.mock(release,'command',side_effect=command)
        return process, restore

    def test_delayed_container_inspection_retries_same_launch_before_admission(self):
        unavailable = release.ReleaseError('container_operation_failed_no_details_logged')
        ready = {'Running':True,'Restarting':False,'OOMKilled':False}
        process, restore = self.configure_start_inspection([unavailable, unavailable, ready])
        def sleep(seconds):
            self.assertEqual(seconds,2)
            self.assertEqual(json.loads((self.root/'active.json').read_text())['admission'],'closed')
            self.launch.assert_called_once()
        wait = Mock(side_effect=sleep)
        self.assertEqual(handoff.start(clock=lambda:0, sleep=wait),{'state':'cpu_restored'})
        self.assertEqual(wait.call_count,2)
        self.launch.assert_called_once()
        restore.assert_called_once()
        self.drain.assert_not_called()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'],'running')

    def test_container_inspection_retry_stops_at_original_120_second_deadline(self):
        unavailable = release.ReleaseError('container_operation_failed_no_details_logged')
        self.configure_start_inspection([unavailable, unavailable])
        wait = Mock()
        with self.assertRaisesRegex(release.ReleaseError,'startup_timeout'):
            handoff.start(clock=Mock(side_effect=[0,119,120]),sleep=wait)
        wait.assert_called_once_with(2)
        self.launch.assert_called_once()
        self.drain.assert_called_once()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'],'launch_intent')
        self.assertEqual(json.loads((self.root/'active.json').read_text())['admission'],'closed')

    def test_container_identity_failure_is_fatal_without_retry_or_relaunch(self):
        self.configure_start_inspection([release.ReleaseError('operator_container_identity_changed')])
        wait = Mock()
        with self.assertRaisesRegex(release.ReleaseError,'container_identity_changed'):
            handoff.start(sleep=wait)
        wait.assert_not_called()
        self.launch.assert_called_once()
        self.drain.assert_called_once()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'],'launch_intent')

    def test_malformed_container_inspection_is_fatal_without_retry(self):
        self.configure_start_inspection([ValueError('malformed container inspection')])
        wait = Mock()
        with self.assertRaisesRegex(ValueError,'malformed container inspection'):
            handoff.start(sleep=wait)
        wait.assert_not_called()
        self.launch.assert_called_once()
        self.drain.assert_called_once()

    def test_client_exit_during_inspection_retry_is_not_relaunched(self):
        process, _ = self.configure_start_inspection([
            release.ReleaseError('container_operation_failed_no_details_logged')])
        process.poll.side_effect = [None,0]
        wait = Mock()
        with self.assertRaisesRegex(release.ReleaseError,'startup_unconfirmed'):
            handoff.start(clock=lambda:0,sleep=wait)
        wait.assert_called_once_with(2)
        self.launch.assert_called_once()
        self.drain.assert_called_once()
        self.assertEqual(json.loads(handoff.record_path().read_text())['phase'],'launch_intent')

    def test_explicit_successor_factory_reuses_existing_supervision(self):
        self.configure_start_inspection([{'Running':True,'Restarting':False,'OOMKilled':False}],
                                       original_retirement=False)
        prepared = {**self.old,'commit':'b'*40,'image_id':'new-image'}
        pin = host.pin_for(prepared)
        def reviewed_factory():
            host.atomic(handoff.record_path(),{**self.record,'phase':'launch_intent'})
            host.atomic(self.root/'active.json',pin)
            return self.runtime,prepared,self.root,self.next_environment,pin
        factory = Mock(side_effect=reviewed_factory)
        with patch.object(handoff,'successor') as original:
            self.assertEqual(handoff.start(successor_factory=factory),{'state':'cpu_restored'})
        original.assert_not_called()
        factory.assert_called_once()
        self.launch.assert_called_once_with(self.root,self.next_environment,self.runtime,pin)
        self.assertEqual(json.loads((self.root/'active.json').read_text())['admission'],'open')

    def test_prepare_records_original_identity_before_term(self):
        handoff.record_path().unlink()
        self.supervisor.return_value = {**self.unit,'MainPID':'321','ActiveState':'active'}
        self.receipt.return_value = {'state':'running'}
        self.mock(handoff, 'only_controller')
        self.mock(handoff, 'supervisor_client', return_value={'supervisor_pid':321,'docker_client_pids':[322]})
        self.drain.side_effect = lambda *args:self.assertEqual(json.loads(handoff.record_path().read_text())['phase'],'drain_requested')
        result = handoff.prepare('b'*40, 'sixnine-old.service')
        self.assertEqual(result['pending_deletions'], 1)
        self.drain.assert_called_once()
        with self.assertRaisesRegex(release.ReleaseError, 'already_recorded'):
            handoff.prepare('b'*40, 'sixnine-old.service')
        self.drain.assert_called_once()

    def test_new_journal_prepare_preserves_consumed_historical_record(self):
        original = handoff.record_path().read_bytes()
        journal = handoff.record_path('inkseq-20261010')
        self.supervisor.return_value = {**self.unit,'MainPID':'321','ActiveState':'active'}
        self.receipt.return_value = {'state':'running'}
        self.mock(handoff, 'only_controller')
        self.mock(handoff, 'supervisor_client', return_value={'supervisor_pid':321,'docker_client_pids':[322]})
        def before_term(*args):
            value = json.loads(journal.read_text())
            self.assertEqual(value['journal_id'], 'inkseq-20261010')
            self.assertEqual(value['ledger'], self.ledger)
            self.assertEqual(handoff.record_path().read_bytes(), original)
        self.drain.side_effect = before_term
        result = handoff.prepare('b'*40, 'sixnine-old.service', journal_id='inkseq-20261010')
        self.assertEqual(result['pending_deletions'], 1)
        with self.assertRaisesRegex(release.ReleaseError, 'already_recorded'):
            handoff.prepare('b'*40, 'sixnine-old.service', journal_id='inkseq-20261010')
        self.drain.assert_called_once()
        self.assertEqual(handoff.record_path().read_bytes(), original)

    def new_journal(self, token='inkseq-20261010'):
        path = handoff.record_path(token)
        path.write_text(json.dumps({**self.record,'journal_id':token}))
        return path

    def use_queue_ledger(self, *, queue_only=False):
        self.ledger = handoff.ledger_summary(queue_rows(queue_only=queue_only), {'binding':'frozen'},
                                            preserve_unsubmitted_queue=True)
        self.record.update(ledger=self.ledger, preserve_unsubmitted_queue=True)
        self.probe.return_value = self.ledger
        return self.new_journal('queue-120')

    def test_queue_only_prepare_is_explicit_named_and_durable_before_drain(self):
        self.use_queue_ledger(queue_only=True).unlink()
        original = handoff.record_path().read_bytes()
        self.supervisor.return_value = {**self.unit,'MainPID':'321','ActiveState':'active'}
        self.receipt.return_value = {'state':'running'}
        self.mock(handoff,'only_controller')
        self.mock(handoff,'supervisor_client',return_value={'supervisor_pid':321,'docker_client_pids':[322]})
        def before_term(*args):
            value = json.loads(handoff.record_path('queue-120').read_text())
            self.assertTrue(value['preserve_unsubmitted_queue'])
            self.assertEqual(value['ledger'],self.ledger)
            self.assertEqual(value['old_prepared'],self.old)
        self.drain.side_effect = before_term
        result = handoff.prepare('b'*40,'sixnine-old.service',journal_id='queue-120',preserve_unsubmitted_queue=True)
        self.assertEqual(result['preserved_unsubmitted_jobs'],1)
        self.assertEqual(result['pending_deletions'],0)
        self.probe.assert_called_once_with(self.root,self.environment,preserve_unsubmitted_queue=True)
        self.drain.assert_called_once()
        self.assertEqual(handoff.record_path().read_bytes(),original)

    def test_queue_flag_without_named_journal_never_closes_admission(self):
        with self.assertRaisesRegex(release.ReleaseError,'queue_requires_named_journal'):
            handoff.prepare('b'*40,'sixnine-old.service',preserve_unsubmitted_queue=True)
        self.probe.assert_not_called(); self.drain.assert_not_called()

    def test_default_queue_only_prepare_remains_strict_and_creates_no_journal(self):
        self.ledger = handoff.ledger_summary(ledger_rows(), {'binding':'frozen'})
        self.probe.return_value = {**self.ledger,'pending_ids':[]}
        self.supervisor.return_value = {**self.unit,'MainPID':'321','ActiveState':'active'}
        self.receipt.return_value = {'state':'running'}
        self.mock(handoff,'only_controller')
        self.mock(handoff,'supervisor_client',return_value={'supervisor_pid':321,'docker_client_pids':[322]})
        with self.assertRaisesRegex(release.ReleaseError,'pending_deletion_required'):
            handoff.prepare('b'*40,'sixnine-old.service',journal_id='queue-120')
        self.assertFalse(handoff.record_path('queue-120').exists())
        self.probe.assert_called_once_with(self.root,self.environment)
        self.drain.assert_not_called()

    def test_successor_preserves_queue_snapshot_through_new_image_without_launch(self):
        path = self.use_queue_ledger(queue_only=True)
        original = handoff.record_path().read_bytes()
        runtime, prepared, _, _, _ = handoff.successor(journal_id='queue-120')
        self.assertEqual(runtime,self.runtime)
        self.assertEqual(prepared,{**self.old,'commit':'b'*40,'image_id':'new-image'})
        value = json.loads(path.read_text())
        self.assertEqual(value['ledger'],self.ledger)
        self.assertEqual(value['retired_ledger'],self.ledger)
        self.assertEqual(value['phase'],'launch_intent')
        self.assertEqual(self.probe.call_args_list[0].kwargs, {'preserve_unsubmitted_queue':True})
        self.assertEqual(self.probe.call_args_list[1].kwargs,
                         {'require_removal_cadence':True,'preserve_unsubmitted_queue':True})
        self.assertEqual(handoff.record_path().read_bytes(),original)
        self.launch.assert_not_called()

    def test_queue_change_before_successor_never_replaces_pin_or_launches(self):
        path = self.use_queue_ledger()
        changed = copy.deepcopy(self.ledger)
        changed['preserved_unsubmitted_queue'][0]['record_hash'] = 'f'*64
        self.probe.return_value = changed
        with self.assertRaisesRegex(release.ReleaseError,'queue_changed'):
            handoff.successor(journal_id='queue-120')
        self.assertEqual(json.loads(path.read_text())['phase'],'drain_requested')
        self.assertEqual(json.loads((self.root/'active.json').read_text()),self.pin)
        self.launch.assert_not_called()

    def test_queue_change_through_new_image_consumes_intent_and_never_replays(self):
        path = self.use_queue_ledger()
        changed = copy.deepcopy(self.ledger)
        changed['preserved_unsubmitted_queue'][0]['configuration_snapshot_hash'] = 'f'*64
        self.probe.side_effect = [self.ledger,changed]
        with self.assertRaisesRegex(release.ReleaseError,'ledger_changed_before_launch'):
            handoff.successor(journal_id='queue-120')
        self.assertEqual(json.loads(path.read_text())['phase'],'launch_intent')
        with self.assertRaisesRegex(release.ReleaseError,'already_launched'):
            handoff.successor(journal_id='queue-120')
        self.launch.assert_not_called()

    def test_queue_handoff_cannot_change_budget_or_configuration(self):
        path = self.use_queue_ledger()
        self.probe.return_value = {**self.ledger,'accounting_hash':'f'*64}
        with self.assertRaisesRegex(release.ReleaseError,'accounting_changed'):
            handoff.successor(journal_id='queue-120')
        self.probe.return_value = self.ledger
        self.prepared.return_value = (self.runtime,{**self.old,'runtime_config_sha256':'f'*64},self.root,self.environment)
        with self.assertRaisesRegex(release.ReleaseError,'configuration_changed'):
            handoff.successor(journal_id='queue-120')
        self.assertEqual(json.loads(path.read_text())['phase'],'drain_requested')
        self.launch.assert_not_called()

    def test_invalid_or_missing_queue_snapshot_policy_rejected_before_pin_writes(self):
        path = self.use_queue_ledger()
        original = json.loads(path.read_text())
        for change in ({'preserve_unsubmitted_queue':1}, {'ledger':{k:v for k,v in self.ledger.items()
                if k != 'preserved_unsubmitted_queue'}}):
            path.write_text(json.dumps({**original,**change}))
            with self.subTest(change=change), self.assertRaisesRegex(release.ReleaseError,'queue_'):
                handoff.successor(journal_id='queue-120')
        self.assertEqual(json.loads((self.root/'active.json').read_text()),self.pin)
        self.launch.assert_not_called()

    def use_same_target_rollover(self):
        self.record.update(target_commit=self.old['commit'],target_image_id=self.old['image_id'],same_target_rollover=True)
        path = self.use_queue_ledger(queue_only=True)
        self.mock(handoff,'current_target',return_value=(self.old['commit'],self.root,self.environment,self.old['image_id']))
        self.container = {'Id':'c'*64, 'Name':'/'+self.pin['container_name'], 'Image':self.pin['image_id'],
            'Config':{'Labels':{'com.docker.compose.project':'sixnine-platform',
                'com.docker.compose.service':host.SERVICE,host.LABEL:self.pin['prepared_hash']},
                'Env':['private-container-env-sentinel']},
            'HostConfig':{'RestartPolicy':{'Name':'no','MaximumRetryCount':0}},
            'State':{'Running':False,'Restarting':False,'Paused':False,'OOMKilled':False,'Dead':False,
                'Status':'exited','Pid':0,'ExitCode':0}}
        self.docker_calls = []
        def command(arguments, **kwargs):
            self.docker_calls.append(arguments)
            if arguments == ['compose','version','--short']: return b'2.38.2'
            if arguments[0] == 'inspect': return json.dumps([self.container]).encode()
            if arguments[:2] == ['container','ls']: return b''
            if arguments[0] == 'rename':
                recorded = json.loads(path.read_text())
                self.assertEqual(recorded['phase'],'launch_intent')
                self.assertEqual(recorded['container_archive']['phase'],'rename_intent')
                self.assertEqual(recorded['retired_proof'],self.receipt.return_value)
                self.assertEqual(json.loads((self.root/'active.json').read_text()),self.pin)
                self.assertEqual(arguments[1],self.container['Id'])
                self.container['Name'] = '/'+arguments[2]
                return b''
            raise AssertionError(arguments)
        self.archive_command = self.mock(release,'command',side_effect=command)
        return path

    def test_same_target_prepare_requires_named_queue_opt_in_and_exact_image(self):
        self.use_queue_ledger(queue_only=True).unlink()
        self.mock(handoff,'current_target',return_value=(self.old['commit'],self.root,self.environment,self.old['image_id']))
        self.supervisor.return_value = {**self.unit,'MainPID':'321','ActiveState':'active'}
        self.receipt.return_value = {'state':'running'}
        self.mock(handoff,'only_controller')
        self.mock(handoff,'supervisor_client',return_value={'supervisor_pid':321,'docker_client_pids':[322]})
        with self.assertRaisesRegex(release.ReleaseError,'target_unchanged'):
            handoff.prepare(self.old['commit'],'sixnine-old.service',journal_id='ordinary')
        self.drain.assert_not_called()
        handoff.prepare(self.old['commit'],'sixnine-old.service',journal_id='queue-120',preserve_unsubmitted_queue=True)
        self.assertTrue(json.loads(handoff.record_path('queue-120').read_text())['same_target_rollover'])
        self.drain.assert_called_once()

    def test_same_target_rollover_archives_exact_exited_id_and_retains_receipt(self):
        path = self.use_same_target_rollover()
        original = handoff.record_path().read_bytes()
        _, prepared, _, _, pin = handoff.successor(journal_id='queue-120')
        self.assertEqual(prepared,self.old)
        self.assertEqual(pin['container_name'],self.pin['container_name'])
        value = json.loads(path.read_text())
        archive = value['container_archive']
        self.assertEqual(archive['phase'],'confirmed')
        self.assertEqual(archive['original']['container_id'],'c'*64)
        self.assertEqual(archive['archived']['container_id'],'c'*64)
        self.assertEqual(archive['archived']['container_name'],self.pin['container_name']+'-retired-queue-120')
        self.assertEqual(archive['original']['prepared_hash'],self.pin['prepared_hash'])
        self.assertEqual(value['retired_proof'],self.receipt.return_value)
        self.assertEqual(value['ledger'],self.ledger)
        self.assertEqual(value['retired_ledger'],self.ledger)
        self.assertEqual(handoff.record_path().read_bytes(),original)
        self.assertNotIn('private-container-env-sentinel',path.read_text())
        self.assertEqual([call for call in self.docker_calls if call[0] == 'rename'],
                         [['rename','c'*64,archive['archived_name']]])
        self.assertIn(['inspect','c'*64],self.docker_calls)
        self.assertFalse(any(call[0] in ('rm','kill','start') for call in self.docker_calls))
        with self.assertRaisesRegex(release.ReleaseError,'already_launched'):
            handoff.successor(journal_id='queue-120')
        self.launch.assert_not_called()

    def test_same_target_image_configuration_queue_or_exit_change_never_renames(self):
        path = self.use_same_target_rollover()
        initial = copy.deepcopy(self.container)
        for section,field,value in (('State','ExitCode',137), ('State','Running',True),
                                   ('State','Pid',1), ('State','OOMKilled',True)):
            self.container = copy.deepcopy(initial); self.container[section][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(release.ReleaseError,'archive_exit_unconfirmed'):
                handoff.successor(journal_id='queue-120')
            self.assertEqual(json.loads(path.read_text())['phase'],'drain_requested')
        self.container = initial
        with patch.object(host,'overlay',return_value={'changed':'configuration'}):
            with self.assertRaisesRegex(release.ReleaseError,'rollover_configuration_changed'):
                handoff.successor(journal_id='queue-120')
        changed = copy.deepcopy(self.ledger); changed['preserved_unsubmitted_queue'] = []
        self.probe.side_effect = [self.ledger,changed]
        with self.assertRaisesRegex(release.ReleaseError,'ledger_changed_before_launch'):
            handoff.successor(journal_id='queue-120')
        self.assertFalse(any(call[0] == 'rename' for call in self.docker_calls))
        self.assertEqual(json.loads(path.read_text())['phase'],'drain_requested')
        self.launch.assert_not_called()

    def test_archive_unknown_rename_is_consumed_and_never_replayed(self):
        path = self.use_same_target_rollover()
        original_command = self.archive_command.side_effect
        def command(arguments, **kwargs):
            result = original_command(arguments,**kwargs)
            if arguments[0] == 'rename':
                raise release.ReleaseError('container_operation_failed_no_details_logged')
            return result
        self.archive_command.side_effect = command
        with self.assertRaisesRegex(release.ReleaseError,'container_operation_failed'):
            handoff.successor(journal_id='queue-120')
        record = json.loads(path.read_text())
        self.assertEqual(record['phase'],'launch_intent')
        self.assertEqual(record['container_archive']['phase'],'rename_intent')
        self.assertEqual(json.loads((self.root/'active.json').read_text()),self.pin)
        with self.assertRaisesRegex(release.ReleaseError,'already_launched'):
            handoff.successor(journal_id='queue-120')
        self.assertEqual(len([call for call in self.docker_calls if call[0] == 'rename']),1)
        self.launch.assert_not_called()

    def test_archive_collision_or_replacement_identity_cannot_launch(self):
        path = self.use_same_target_rollover()
        original_command = self.archive_command.side_effect
        def occupied(arguments, **kwargs):
            if arguments[:2] == ['container','ls']: return b'occupied-container\n'
            return original_command(arguments,**kwargs)
        self.archive_command.side_effect = occupied
        with self.assertRaisesRegex(release.ReleaseError,'archive_name_in_use'):
            handoff.successor(journal_id='queue-120')
        self.assertEqual(json.loads(path.read_text())['phase'],'drain_requested')
        self.assertFalse(any(call[0] == 'rename' for call in self.docker_calls))
        def replaced(arguments, **kwargs):
            result = original_command(arguments,**kwargs)
            if arguments == ['inspect','c'*64]:
                replacement = {**self.container,'Id':'d'*64}
                return json.dumps([replacement]).encode()
            return result
        self.archive_command.side_effect = replaced
        with self.assertRaisesRegex(release.ReleaseError,'archive_identity_changed'):
            handoff.successor(journal_id='queue-120')
        self.assertEqual(json.loads(path.read_text())['phase'],'launch_intent')
        self.assertEqual(json.loads((self.root/'active.json').read_text()),self.pin)
        self.launch.assert_not_called()

    def test_same_target_image_or_archive_identity_mismatch_is_rejected_before_rename(self):
        path = self.use_same_target_rollover()
        original = copy.deepcopy(self.container)
        mutations = [lambda row:row.update(Id='invalid'), lambda row:row.update(Name='/different'),
            lambda row:row.update(Image='different-image'),
            lambda row:row['Config']['Labels'].update({'com.docker.compose.project':'other-project'}),
            lambda row:row['Config']['Labels'].update({'com.docker.compose.service':'app'}),
            lambda row:row['Config']['Labels'].update({host.LABEL:'different-pin'}),
            lambda row:row['HostConfig']['RestartPolicy'].update(Name='always')]
        for mutate in mutations:
            self.container = copy.deepcopy(original); mutate(self.container)
            with self.subTest(mutate=mutate), self.assertRaisesRegex(release.ReleaseError,'archive_identity_changed'):
                handoff.successor(journal_id='queue-120')
            self.assertEqual(json.loads(path.read_text())['phase'],'drain_requested')
        self.container = original
        with patch.object(handoff,'current_target',return_value=(self.old['commit'],self.root,self.environment,'different-image')):
            with self.assertRaisesRegex(release.ReleaseError,'target_image_changed'):
                handoff.successor(journal_id='queue-120')
        self.assertFalse(any(call[0] == 'rename' for call in self.docker_calls))
        self.launch.assert_not_called()

    def test_original_launch_name_must_be_free_after_archive_before_pin_replacement(self):
        path = self.use_same_target_rollover()
        original_command = self.archive_command.side_effect
        def occupied_original(arguments, **kwargs):
            result = original_command(arguments,**kwargs)
            if arguments[:2] == ['container','ls'] and arguments[4] == 'name=^/'+self.pin['container_name']+'$':
                return b'another-container\n'
            return result
        self.archive_command.side_effect = occupied_original
        with self.assertRaisesRegex(release.ReleaseError,'archive_name_in_use'):
            handoff.successor(journal_id='queue-120')
        self.assertEqual(json.loads(path.read_text())['phase'],'launch_intent')
        self.assertEqual(json.loads((self.root/'active.json').read_text()),self.pin)
        self.assertEqual(len([call for call in self.docker_calls if call[0] == 'rename']),1)
        self.launch.assert_not_called()

    def test_new_journal_rejects_copied_or_changed_record_identity(self):
        path = self.new_journal()
        original = handoff.record_path().read_bytes()
        for token in (None, 'another-rollover'):
            path.write_text(json.dumps({**self.record,'journal_id':token}))
            with self.subTest(token=token), self.assertRaisesRegex(release.ReleaseError,'journal_identity_changed'):
                handoff.successor(journal_id='inkseq-20261010')
        self.assertEqual(handoff.record_path().read_bytes(), original)
        self.launch.assert_not_called()

    def test_new_journal_preserves_exact_accounting_before_launch(self):
        path = self.new_journal()
        original = handoff.record_path().read_bytes()
        self.probe.return_value = {**self.ledger,'accounting_hash':'f'*64}
        with self.assertRaisesRegex(release.ReleaseError,'accounting_changed'):
            handoff.successor(journal_id='inkseq-20261010')
        self.assertEqual(json.loads(path.read_text())['phase'],'drain_requested')
        self.assertEqual(handoff.record_path().read_bytes(), original)
        self.assertEqual(json.loads((self.root/'active.json').read_text()), self.pin)
        self.launch.assert_not_called()

    def use_unknown_ledger(self):
        self.ledger = handoff.ledger_summary(unknown_rows(), {'binding':'frozen'}, unknown_receipt())
        self.record['ledger'] = self.ledger
        self.probe.return_value = self.ledger
        return self.new_journal('unknown-112')

    def test_stopped_unknown_named_handoff_keeps_both_original_and_retired_proofs(self):
        path = self.use_unknown_ledger()
        original = handoff.record_path().read_bytes()
        self.configure_start_inspection([{'Running':True,'Restarting':False,'OOMKilled':False}])
        self.mock(handoff, 'successor_supervisor', return_value={'MainPID':'999'})
        self.assertEqual(handoff.start(journal_id='unknown-112', successor_unit='sixnine-unknown.service'),
                         {'state':'cpu_restored'})
        record = json.loads(path.read_text())
        self.assertEqual(record['ledger'], self.ledger)
        self.assertEqual(record['retired_ledger'], self.ledger)
        self.assertEqual(record['phase'], 'running')
        self.assertEqual(handoff.record_path().read_bytes(), original)
        self.assertEqual(self.probe.call_args_list[-1].kwargs, {'require_removal_cadence':True})
        self.launch.assert_called_once()

    def test_unknown_journal_change_before_or_after_staging_cannot_launch(self):
        path = self.use_unknown_ledger()
        changed = copy.deepcopy(self.ledger)
        changed['unknown_rent_journals'][UNKNOWN_TAG]['journal_sha256'] = 'f'*64
        self.probe.return_value = changed
        with self.assertRaisesRegex(release.ReleaseError,'unknown_rent_journal_changed'):
            handoff.successor(journal_id='unknown-112')
        self.assertEqual(json.loads(path.read_text())['phase'], 'drain_requested')
        self.probe.side_effect = [self.ledger,changed]
        with self.assertRaisesRegex(release.ReleaseError,'ledger_changed_before_launch'):
            handoff.successor(journal_id='unknown-112')
        self.assertEqual(json.loads(path.read_text())['phase'], 'launch_intent')
        self.launch.assert_not_called()

    def test_unknown_handoff_requires_named_supervision_before_drain(self):
        self.use_unknown_ledger()
        handoff.record_path().unlink()
        self.supervisor.return_value = {**self.unit,'MainPID':'321','ActiveState':'active'}
        self.receipt.return_value = {'state':'running'}
        self.mock(handoff, 'only_controller')
        self.mock(handoff, 'supervisor_client', return_value={'supervisor_pid':321,'docker_client_pids':[322]})
        with self.assertRaisesRegex(release.ReleaseError,'unknown_rent_requires_named_journal'):
            handoff.prepare('b'*40,'sixnine-old.service')
        self.drain.assert_not_called()
        self.assertFalse(handoff.record_path().exists())

    def test_new_journal_start_binds_supervisor_and_preserves_original_through_success(self):
        path = self.new_journal()
        original = handoff.record_path().read_bytes()
        self.configure_start_inspection([{'Running':True,'Restarting':False,'OOMKilled':False}])
        identity = {'MainPID':'999', 'unit_files':{'new.service':'exact-source'}}
        own = self.mock(handoff, 'successor_supervisor', return_value=identity)
        self.assertEqual(handoff.start(journal_id='inkseq-20261010',
            successor_unit='sixnine-inkseq-rollover.service'),{'state':'cpu_restored'})
        own.assert_called_once_with('sixnine-inkseq-rollover.service','inkseq-20261010')
        value = json.loads(path.read_text())
        self.assertEqual(value['phase'],'running')
        self.assertEqual(value['journal_id'],'inkseq-20261010')
        self.assertEqual(value['successor_supervisor'], identity)
        self.assertEqual(value['ledger'], self.ledger)
        self.assertEqual(value['retired_ledger'], self.ledger)
        self.assertEqual(handoff.record_path().read_bytes(), original)
        self.launch.assert_called_once()

    def test_unknown_new_journal_launch_cannot_replay_or_overwrite_original(self):
        path = self.new_journal()
        original = handoff.record_path().read_bytes()
        self.mock(handoff, 'successor_supervisor', return_value={'exact':'unit'})
        self.launch.side_effect = TimeoutError('synthetic delivery uncertainty')
        for error in (TimeoutError, release.ReleaseError):
            with self.assertRaises(error):
                handoff.start(journal_id='inkseq-20261010',successor_unit='sixnine-next.service')
        self.assertEqual(json.loads(path.read_text())['phase'],'launch_intent')
        self.assertEqual(handoff.record_path().read_bytes(), original)
        self.launch.assert_called_once()

    def test_wrong_successor_supervisor_rejects_before_journal_or_pin_writes(self):
        path = self.new_journal()
        original = path.read_bytes()
        self.mock(handoff,'successor_supervisor',side_effect=release.ReleaseError('unit_mismatch'))
        with self.assertRaisesRegex(release.ReleaseError,'unit_mismatch'):
            handoff.start(journal_id='inkseq-20261010',successor_unit='sixnine-wrong.service')
        self.assertEqual(path.read_bytes(), original)
        self.launch.assert_not_called()
        self.drain.assert_not_called()

    def test_explicit_journal_cannot_use_custom_factory_or_unbound_unit(self):
        factory = Mock()
        with self.assertRaisesRegex(release.ReleaseError,'custom_factory_forbidden'):
            handoff.start(journal_id='inkseq-20261010',successor_factory=factory)
        with self.assertRaisesRegex(release.ReleaseError,'successor_unit_without_journal'):
            handoff.start(successor_unit='sixnine-next.service')
        factory.assert_not_called()
        self.launch.assert_not_called()
        self.drain.assert_not_called()

    def test_supervisor_process_must_own_exact_controller_client(self):
        proc = self.root/'proc'
        def process(pid, argv, children):
            folder = proc/str(pid); (folder/'task'/str(pid)).mkdir(parents=True)
            (folder/'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in argv)+b'\0')
            (folder/'task'/str(pid)/'children').write_text(' '.join(str(child) for child in children))
        unit = {**self.unit,'MainPID':'321', 'ExecStart':{
            'path':'/usr/bin/python3', 'argv':'/usr/bin/python3 /opt/sixnine-release/operator_capacity.py start'}}
        process(321, ['/usr/bin/python3','/opt/sixnine-release/operator_capacity.py','start'], [322])
        process(322, ['/usr/bin/docker','compose','-f',str(self.root/'compose.yaml'),'run',
                      '--name',self.pin['container_name'],'--label',host.LABEL+'='+self.pin['prepared_hash'],host.SERVICE], [])
        proof = handoff.supervisor_client(unit,self.pin,self.root,proc_root=proc)
        self.assertEqual(proof['docker_client_pids'], [322])
        (proc/'322'/'cmdline').write_bytes(b'/usr/bin/docker\0compose\0run\0--name\0unrelated\0')
        with self.assertRaisesRegex(release.ReleaseError, 'does_not_own_controller'):
            handoff.supervisor_client(unit,self.pin,self.root,proc_root=proc)
        (proc/'321'/'cmdline').write_bytes(b'/usr/bin/python3\0/unrelated.py\0start\0')
        with self.assertRaisesRegex(release.ReleaseError, 'process_changed'):
            handoff.supervisor_client(unit,self.pin,self.root,proc_root=proc)


class ControllerProcessTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(host, 'inspect_controller', return_value={
            'Running':True, 'Paused':False, 'Restarting':False, 'OOMKilled':False}))
        self.command = self.stack.enter_context(patch.object(release, 'command'))
        self.pin = {'container_name':'exact-original-controller'}

    def test_native_pid_comm_output_accepts_only_controller_and_optional_init(self):
        for output in (b'PID COMMAND\n101 python\n',
                       b'PID COMMAND\n100 docker-init\n101 python\n',
                       b'PID COMMAND\n100 tini\n101 python3\n'):
            with self.subTest(output=output):
                self.command.return_value = output
                handoff.only_controller({}, self.pin)
        self.assertEqual(self.command.call_args.args[0],
            ['top','exact-original-controller','-eo','pid,comm'])

    def test_extra_processes_and_shell_wrappers_block(self):
        for output in (b'PID COMMAND\n100 python\n101 python\n',
                       b'PID COMMAND\n100 python\n101 ssh\n',
                       b'PID COMMAND\n100 sh\n',
                       b'PID COMMAND\n100 tini\n101 docker-init\n102 python\n'):
            with self.subTest(output=output):
                self.command.return_value = output
                with self.assertRaisesRegex(release.ReleaseError,'owned_children_present'):
                    handoff.only_controller({}, self.pin)

    def test_empty_malformed_or_unexpected_process_format_blocks(self):
        for output in (b'', b'PID COMMAND\n', b'COMMAND\npython\n',
                       b'PID CMD\n100 python\n', b'PID COMMAND\nx python\n',
                       b'PID COMMAND\n0 python\n', b'PID COMMAND\n100 python extra\n',
                       b'PID COMMAND\n100 tini\n100 python\n'):
            with self.subTest(output=output):
                self.command.return_value = output
                with self.assertRaisesRegex(release.ReleaseError,'process_inspection_invalid'):
                    handoff.only_controller({}, self.pin)

    def test_failed_inspection_blocks_without_fallback(self):
        self.command.side_effect = release.ReleaseError('container_operation_failed_no_details_logged')
        with self.assertRaisesRegex(release.ReleaseError,'container_operation_failed'):
            handoff.only_controller({}, self.pin)
        self.command.assert_called_once()


class SystemdIdentityTests(unittest.TestCase):
    def output(self, command, *, unit='sixnine-next.service', pid='321'):
        return (f'MainPID={pid}\nExecMainPID={pid}\nRestart=no\nKillMode=process\nActiveState=active\nUser=root\n'
            'TimeoutStopUSec=infinity\nSendSIGKILL=no\n'
            f'FragmentPath=/etc/systemd/system/{unit}\nDropInPaths=\n'
            f'ExecStart={{ path=/usr/bin/python3 ; argv[]={command} ; ignore_errors=no ; pid={pid} ; }}\n').encode()

    def test_only_exact_helper_commands_and_matching_journal_unit_are_allowed(self):
        commands = [
            '/usr/bin/python3 /opt/sixnine-release/operator_capacity.py start',
            '/usr/bin/python3 /opt/sixnine-release/operator_handoff.py start',
            '/usr/bin/python3 /opt/sixnine-release/operator_handoff_continuation.py resume',
            '/usr/bin/python3 /opt/sixnine-release/operator_handoff.py start --journal-id inkseq-20261010 --successor-unit sixnine-next.service',
        ]
        for command in commands:
            with self.subTest(command=command), patch.object(handoff.subprocess,'run',return_value=Mock(stdout=self.output(command))), \
                    patch.object(host,'protected_file',side_effect=lambda path:path), patch.object(release,'checksum',return_value='hash'):
                self.assertEqual(handoff.supervisor('sixnine-next.service')['ExecStart']['argv'], command)
        for command in (
            commands[2]+' --journal-id extra', commands[3].replace('sixnine-next.service','sixnine-wrong.service'),
            commands[3].replace('inkseq-20261010','../escape'), commands[0]+' --arbitrary',
            '/usr/bin/python3 /tmp/operator_handoff.py start', commands[3]+' --unit another',
        ):
            with self.subTest(command=command), patch.object(handoff.subprocess,'run',return_value=Mock(stdout=self.output(command))), \
                    self.assertRaisesRegex(release.ReleaseError,'command_changed'):
                handoff.supervisor('sixnine-next.service')

    def test_finite_timeout_or_sigkill_cannot_supervise_rollover(self):
        command = '/usr/bin/python3 /opt/sixnine-release/operator_handoff_continuation.py resume'
        for old,new in ((b'TimeoutStopUSec=infinity',b'TimeoutStopUSec=90s'), (b'SendSIGKILL=no',b'SendSIGKILL=yes')):
            with self.subTest(new=new), patch.object(handoff.subprocess,'run',return_value=Mock(stdout=self.output(command).replace(old,new))), \
                    self.assertRaisesRegex(release.ReleaseError,'supervisor_unsafe'):
                handoff.supervisor('sixnine-next.service')

    def test_journal_path_rejects_traversal_whitespace_and_long_tokens(self):
        for token in ('', '../other', '/tmp/journal', 'a/b', 'a.b', 'UPPER', 'a\nb', 'a'*65, True, 1):
            with self.subTest(token=token), self.assertRaisesRegex(release.ReleaseError,'journal_id_invalid'):
                handoff.record_path(token)
        self.assertEqual(handoff.record_path('inkseq-20261010').name,'handoff-inkseq-20261010.json')
        self.assertEqual(handoff.record_path().name,'handoff.json')

    def test_successor_unit_and_own_proc_must_bind_same_journal(self):
        unit = 'sixnine-next.service'
        arguments = ['/usr/bin/python3','/opt/sixnine-release/operator_handoff.py','start',
                     '--journal-id','inkseq-20261010','--successor-unit',unit]
        value = {'MainPID':'321','ActiveState':'active','ExecStart':{
            'path':'/usr/bin/python3','argv':' '.join(arguments)}}
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory); (proc/'321').mkdir()
            file = proc/'321'/'cmdline'; file.write_bytes(b'\0'.join(arg.encode() for arg in arguments)+b'\0')
            with patch.object(handoff,'supervisor',return_value=value):
                self.assertEqual(handoff.successor_supervisor(unit,'inkseq-20261010',proc_root=proc,pid=321), value)
                with self.assertRaisesRegex(release.ReleaseError,'supervisor_mismatch'):
                    handoff.successor_supervisor(unit,'different',proc_root=proc,pid=321)
                file.write_bytes(file.read_bytes().replace(b'inkseq-20261010',b'different'))
                with self.assertRaisesRegex(release.ReleaseError,'process_mismatch'):
                    handoff.successor_supervisor(unit,'inkseq-20261010',proc_root=proc,pid=321)
    def test_exec_metadata_changes_do_not_change_supervisor_identity(self):
        command = '/usr/bin/python3 /opt/sixnine-release/operator_capacity.py start'
        def output(metadata):
            return ('MainPID=321\nExecMainPID=321\nRestart=no\nKillMode=process\nActiveState=active\nUser=root\n'
                'TimeoutStopUSec=infinity\nSendSIGKILL=no\n'
                'FragmentPath=/etc/systemd/system/sixnine-original.service\nDropInPaths=\n'
                'ExecStart={ path=/usr/bin/python3 ; argv[]='+command+' ; ignore_errors=no ; '+metadata+' }\n').encode()
        with patch.object(handoff.subprocess,'run',side_effect=[Mock(stdout=output('pid=321 ; code=(null) ;')),
                        Mock(stdout=output('pid=321 ; code=exited ; status=1 ;'))]), \
                patch.object(host,'protected_file',side_effect=lambda path:path), \
                patch.object(release,'checksum',return_value='unit-hash'):
            before = handoff.supervisor('sixnine-original.service')
            after = handoff.supervisor('sixnine-original.service')
        self.assertEqual(before, after)
        with patch.object(handoff.subprocess,'run',return_value=Mock(stdout=output('pid=321 ;').replace(
                command.encode(),b'/usr/bin/python3 /opt/sixnine-release/operator_capacity.py start --wrong'))):
            with self.assertRaisesRegex(release.ReleaseError,'command_changed'):
                handoff.supervisor('sixnine-original.service')


class RealSchemaProbeTests(unittest.TestCase):
    def test_queue_opt_in_reads_complete_real_rows_and_never_changes_jobs_or_money(self):
        from test_operator_capacity import OperatorTests
        from sqlalchemy import insert, select, update
        from studio_platform.repository import jobs, attempts, budget_accounts, budget_reservations
        from types import SimpleNamespace
        import sqlalchemy
        import studio_platform.operator_runtime as runtime
        from studio_platform.settings import Settings
        case = OperatorTests(); case.setUp()
        try:
            plan = case.repo.create_plan(case.scope, {'recipe_id':'h3-base-fl2va-v1',
                'request':{'model':'test-h3','prompt':'private-real-probe-prompt'}},
                {'pool':case.binding.pool,'backend':'disabled','configuration_id':case.binding.configuration_id,
                 'engine_manifest_digest':case.binding.engine_manifest_digest,'enabled':False}, expires_at=9000)
            job = case.repo.create_job(case.scope,plan['id'],'private-real-probe-key',budget_account_ids=('owner-budget',))
            second = case.repo.create_job(case.scope,plan['id'],'private-real-probe-second',budget_account_ids=('owner-budget',))
            tables = (jobs,attempts,budget_accounts,budget_reservations)
            def snapshot():
                with case.repo.engine.connect() as conn:
                    return [[dict(row) for row in conn.execute(select(table).order_by(table.c[0])).mappings()]
                            for table in tables]
            before = snapshot()
            real_engine = case.repo.engine
            scripts = []
            class Connection:
                def __enter__(self): self.conn = real_engine.connect(); return self
                def __exit__(self,*args): self.conn.close()
                def execute(self,statement,*args,**kwargs):
                    if str(statement).startswith('SET TRANSACTION'): return None
                    return self.conn.execute(statement,*args,**kwargs)
            engine = SimpleNamespace(connect=lambda:Connection(),dispose=lambda:None)
            def compose(*args,**kwargs):
                script = args[-1]; scripts.append(script); output = io.StringIO()
                with patch.object(sqlalchemy,'create_engine',return_value=engine), \
                        patch.object(runtime,'create_registry',return_value=case.registry), \
                        patch.object(runtime,'load_runtime_config',return_value={'work_dir':'/unused-no-rental'}), \
                        patch.object(Settings,'from_environment',return_value=SimpleNamespace(database_url='fixture')), \
                        redirect_stdout(output):
                    exec(compile(script,'<queue-read-only-probe>','exec'),{})
                return output.getvalue().encode()
            with patch.object(host,'compose',side_effect=compose):
                with self.assertRaisesRegex(ValueError,'ledger_unsafe'):
                    handoff.ledger_probe(Path('/same-image'),{})
                original = handoff.ledger_probe(Path('/same-image'),{},preserve_unsubmitted_queue=True)
                self.assertEqual([row['job_id'] for row in original['preserved_unsubmitted_queue']],
                                 sorted([job['id'],second['id']]))
                self.assertEqual(snapshot(),before)
                self.assertNotIn('private-real-probe',json.dumps(original))
                self.assertEqual(handoff.ledger_probe(Path('/same-image'),{},preserve_unsubmitted_queue=True,
                    require_removal_cadence=True),original)
                # Even a terminal/deferred historical attempt forbids treating
                # this queued row as never claimed or submitting it again.
                with case.repo.transaction() as conn:
                    conn.execute(insert(attempts).values(id='00000000-0000-4000-8000-000000000122',
                        job_id=job['id'],number=1,status='deferred',fence=1,worker_id='historical',
                        created_at=case.now,updated_at=case.now,submission_started_at=None,upstream_stopped=1))
                with self.assertRaisesRegex(ValueError,'ledger_unsafe'):
                    handoff.ledger_probe(Path('/same-image'),{},preserve_unsubmitted_queue=True)
                with case.repo.transaction() as conn:
                    conn.execute(attempts.delete())
                    conn.execute(update(jobs).where(jobs.c.id == second['id']).values(status='planned'))
                with self.assertRaisesRegex(ValueError,'ledger_unsafe'):
                    handoff.ledger_probe(Path('/same-image'),{},preserve_unsubmitted_queue=True)
            self.assertTrue(all('REPEATABLE READ READ ONLY' in script for script in scripts))
            self.assertTrue(all('LIMIT' not in script for script in scripts))
            self.assertEqual(case.provider.creates,[])
            self.assertEqual(case.provider.destroys,[])
        finally:
            case.tearDown(); case.doCleanups()

    def test_probe_uses_real_schema_and_preserves_expired_unbound_worker(self):
        # Reuse isolated fixture setup and only its injected fake provider.
        from test_operator_capacity import OperatorTests
        from sqlalchemy import select, update
        from studio_platform.repository import instance_intents, scaler_actions, registered_workers
        from studio_platform.operator_capacity import operator_commands, operator_nodes
        from studio_platform.control import WorkerControl, WorkerSpec
        from types import SimpleNamespace
        import sqlalchemy
        import studio_platform.operator_runtime as runtime
        from studio_platform.settings import Settings
        case = OperatorTests(); case.setUp()
        try:
            case.create()
            with case.repo.engine.connect() as conn:
                command = dict(conn.execute(select(operator_commands)).mappings().one())
            case.controller._start(command)
            intent = case.repo.list_instance_intents()[0]
            control = WorkerControl(case.repo)
            control.register(WorkerSpec('historical-worker',case.binding.pool,'lium',intent['provider_instance_id'],
                ('GPU-fixture',),case.binding.recipe_ids,case.binding.model_id,case.binding.configuration_id,
                backend='wangp-worker',engine_manifest_digest=case.binding.engine_manifest_digest))
            with case.repo.transaction() as conn:
                conn.execute(update(instance_intents).values(state='destroying'))
                conn.execute(update(operator_nodes).values(desired_state='stopped'))
                conn.execute(update(scaler_actions).values(destroy_started_at=case.now))
                conn.execute(update(registered_workers).values(expires_at=1))
            scripts = []
            real_engine = case.repo.engine
            class Connection:
                def __enter__(self):
                    self.conn = real_engine.connect(); return self
                def __exit__(self,*args): self.conn.close()
                def execute(self, statement, *args, **kwargs):
                    if str(statement).startswith('SET TRANSACTION'):
                        return None  # SQLite fixture; production keeps PostgreSQL READ ONLY.
                    return self.conn.execute(statement,*args,**kwargs)
            engine = SimpleNamespace(connect=lambda:Connection(),dispose=lambda:None)
            def compose(*args, **kwargs):
                script = args[-1]; scripts.append(script)
                output = io.StringIO()
                with patch.object(sqlalchemy,'create_engine',return_value=engine), \
                        patch.object(runtime,'create_registry',return_value=case.registry), \
                        patch.object(runtime,'load_runtime_config',return_value={'work_dir':'/unused-for-deletions'}), \
                        patch.object(Settings,'from_environment',return_value=SimpleNamespace(database_url='fixture')), \
                        redirect_stdout(output):
                    exec(compile(script,'<read-only-probe>','exec'),{})
                return output.getvalue().encode()
            with patch.object(host,'compose',side_effect=compose):
                result = handoff.ledger_probe(Path('/fake-release'),{},require_removal_cadence=True)
                self.assertEqual(result['pending_ids'],[intent['id']])
                with case.repo.transaction() as conn:
                    conn.execute(update(registered_workers).values(expires_at=10**12))
                with self.assertRaisesRegex(ValueError,'ledger_unsafe'):
                    handoff.ledger_probe(Path('/fake-release'),{})
            self.assertIn('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY',scripts[0])
            self.assertIn('REMOVAL_CHECK_INTERVAL_SECONDS == 60',scripts[0])
            with real_engine.connect() as conn:
                worker = conn.execute(select(registered_workers)).mappings().one()
                self.assertEqual(worker['id'],'historical-worker')
                self.assertNotEqual(worker['state'],'retired')
        finally:
            case.tearDown(); case.doCleanups()

    def test_stopped_unknown_survives_shutdown_successor_and_repeated_ticks_without_rent(self):
        from test_operator_capacity import OperatorTests, OperatorProvider, FakeBoot
        from studio_platform.operator_controller import OperatorController
        from studio_platform.rent_journal import RentJournal
        from studio_platform.settings import Settings
        from types import SimpleNamespace
        import sqlalchemy
        import studio_platform.operator_runtime as runtime
        case = OperatorTests(); case.setUp()
        try:
            with tempfile.TemporaryDirectory() as directory:
                work_dir = Path(directory).resolve()
                journal = RentJournal(work_dir/'rent-journal')
                class UnknownProvider(OperatorProvider):
                    def create(self, tag, launch, *, hard_deadline):
                        self.creates.append((tag,launch,hard_deadline))
                        journal.save(tag,'checking')
                        deadline = min(case.now+3600,hard_deadline)
                        ttl = {'version':1,'created_at':case.now,'hard_deadline':hard_deadline,'requested_hours':1,
                               'deadline':deadline,'effective_deadline':deadline,'instance_id':None,
                               'provider_created_at':None,'attempts':[]}
                        journal.save(tag,'post_started',absolute_ttl=ttl)
                        raise TimeoutError('synthetic original response lost')
                case.provider = UnknownProvider()
                case.create(); case.controller.tick()
                node = case.service.state(case.actor)['nodes'][0]
                stop = case.service.node_command(case.actor,node['id'],{'expected_version':node['version']},'stop-unknown','stop')
                case.controller.tick()
                intent = case.repo.list_instance_intents()[0]
                marker_path = work_dir/'rent-journal'/(intent['id']+'.json')
                original_marker = marker_path.read_bytes()
                budget = case.repo.get_budget('owner-budget')
                scripts = []
                real_engine = case.repo.engine
                class Connection:
                    def __enter__(self):
                        self.conn = real_engine.connect(); return self
                    def __exit__(self,*args): self.conn.close()
                    def execute(self,statement,*args,**kwargs):
                        if str(statement).startswith('SET TRANSACTION'): return None
                        return self.conn.execute(statement,*args,**kwargs)
                engine = SimpleNamespace(connect=lambda:Connection(),dispose=lambda:None)
                def compose(*args,**kwargs):
                    script = args[-1]; scripts.append(script); output = io.StringIO()
                    with patch.object(sqlalchemy,'create_engine',return_value=engine), \
                            patch.object(runtime,'create_registry',return_value=case.registry), \
                            patch.object(runtime,'load_runtime_config',return_value={'work_dir':str(work_dir)}), \
                            patch.object(Settings,'from_environment',return_value=SimpleNamespace(database_url='fixture')), \
                            redirect_stdout(output):
                        exec(compile(script,'<read-only-probe>','exec'),{})
                    return output.getvalue().encode()
                with patch.object(host,'compose',side_effect=compose):
                    original = handoff.ledger_probe(Path('/old-image'),{})
                    case.controller.request_shutdown(); case.controller.tick()
                    status = case.controller.shutdown_status()
                    self.assertEqual(status['state'],'shutdown_complete')
                    self.assertFalse(status['cloud_removal_confirmed'])
                    self.assertFalse(status['billing_settled'])
                    retired = handoff.ledger_probe(Path('/old-image'),{})
                    successor = handoff.ledger_probe(Path('/new-image'),{},require_removal_cadence=True)
                    self.assertEqual(original,retired)
                    self.assertEqual(retired,successor)
                case.now += 61  # Original exclusive leader lease expires before its replacement.
                replacement = OperatorController(case.service,provider_factory=lambda _:case.provider,
                    boot_factory=lambda *args:FakeBoot(case,*args),enabled=True,leader_id='successor')
                for _ in range(3): replacement.tick()
                after = case.repo.list_instance_intents()[0]
                self.assertEqual((after['id'],after['state'],after['provider_instance_id'],after['hard_deadline']),
                                 (intent['id'],'creation_unknown',None,intent['hard_deadline']))
                self.assertEqual(marker_path.read_bytes(),original_marker)
                self.assertEqual(case.repo.get_budget('owner-budget'),budget)
                self.assertEqual(len(case.provider.creates),1)
                self.assertEqual(case.provider.destroys,[])
                self.assertEqual(replacement.boots,{})
                operation = next(op for op in case.service.state(case.actor)['operations'] if op['id']==stop['operation']['id'])
                self.assertEqual(operation['state'],'waiting')
                self.assertTrue(all('READ ONLY' in script for script in scripts))
        finally:
            case.tearDown(); case.doCleanups()


if __name__ == '__main__':
    unittest.main()
