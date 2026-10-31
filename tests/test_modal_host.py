"""Modal host lifecycle, exclusively fake clients and the original SQL ledger."""

from dataclasses import replace

from sqlalchemy import select

from studio_platform.modal_host import HostSpec, ModalHosts
from studio_platform.repository import Conflict, budget_accounts, scaler_actions
from test_platform_repository import LedgerCase


class FakeModal:
    def __init__(self):
        self.created = []
        self.stopped = []
        self.hosts = {}
        self.drop_create = False
        self.drop_stop = False

    def create(self, request):
        self.created.append(request)
        identity = 'sb-' + str(len(self.created))
        self.hosts[request['name']] = {'id': identity, 'exit_code': None}
        if self.drop_create:
            raise TimeoutError('PRIVATE_TOKEN_MUST_NOT_ESCAPE')
        return identity

    def lookup(self, name, identity=None):
        return self.hosts.get(name)

    def terminate(self, identity):
        self.stopped.append(identity)
        for host in self.hosts.values():
            if host['id'] == identity:
                host['exit_code'] = 137
        if self.drop_stop:
            raise TimeoutError('PRIVATE_TOKEN_MUST_NOT_ESCAPE')


class ModalHostTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.repo.configure_pool('test-pool', max_instances=10, max_physical_gpus=10)
        self.client = FakeModal()
        self.spec = HostSpec('ap-test', 'im-test', 'vo-test', 4_000_000)
        self.hosts = ModalHosts(
            self.repo, self.client, self.spec, environment='staging', enabled=True
        )

    def intent(self, key='one', cost=1_000_000, duration=600):
        return self.repo.reserve_instance_intent(
            self.scope,
            'test-pool',
            key,
            provider='modal',
            hard_deadline=self.now + duration,
            reserved_cost_microusd=cost,
            budget_account_ids=('owner-budget',),
            dry_run=False,
        )['id']

    def balance(self):
        with self.repo.engine.connect() as c:
            return dict(c.execute(select(budget_accounts)).mappings().one())

    def test_default_and_production_are_inert(self):
        intent = self.intent()
        for service in (
            ModalHosts(self.repo, self.client, self.spec),
            ModalHosts(
                self.repo,
                self.client,
                self.spec,
                environment='production',
                enabled=True,
            ),
        ):
            with self.assertRaisesRegex(Conflict, 'modal_staging_disabled'):
                service.start(intent)
        self.assertEqual(self.client.created, [])

    def test_cpu_memory_sandbox_surcharge_cannot_be_underquoted(self):
        for quote in (1, 3_000_000, 3_503_339, 4_000_001):
            with self.assertRaises(ValueError):
                replace(self.spec, max_hourly_microusd=quote)
        self.assertEqual(
            replace(self.spec, max_hourly_microusd=3_503_340).max_hourly_microusd,
            3_503_340,
        )

    def test_create_uses_existing_funding_and_finite_single_input_host(self):
        intent = self.intent()
        before = self.balance()
        result = self.hosts.start(intent)
        self.assertEqual(result['state'], 'starting')
        request = self.client.created[0]
        self.assertEqual(request['timeout'], 600)
        self.assertEqual(request['idle_timeout'], 60)
        self.assertEqual(request['max_inputs'], 1)
        self.assertEqual(request['max_containers'], 2)
        self.assertEqual(request['memory_mib'], 41 * 1024)
        self.assertEqual(self.balance(), before)

    def test_repeated_start_observes_original_never_creates_again(self):
        intent = self.intent()
        first = self.hosts.start(intent)
        self.assertEqual(self.hosts.start(intent)['instance_id'], first['instance_id'])
        self.assertEqual(len(self.client.created), 1)

    def test_lost_create_response_is_read_only_reconciled(self):
        intent = self.intent()
        self.client.drop_create = True
        result = self.hosts.start(intent)
        self.assertEqual(result['state'], 'creation_unknown')
        self.client.drop_create = False
        result = self.hosts.start(intent)
        self.assertEqual(result['state'], 'starting')
        self.assertEqual(len(self.client.created), 1)

    def test_missing_lookup_is_not_zero_cost_or_permission_to_recreate(self):
        intent = self.intent()
        self.client.drop_create = True
        self.hosts.start(intent)
        self.client.hosts.clear()
        before = self.balance()
        for _ in range(2):
            self.assertEqual(self.hosts.start(intent)['state'], 'creation_unknown')
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(self.balance(), before)

    def test_unknown_host_still_consumes_two_host_ceiling(self):
        self.client.drop_create = True
        self.hosts.start(self.intent('one'))
        self.hosts.start(self.intent('two'))
        with self.assertRaisesRegex(Conflict, 'modal_container_limit'):
            self.hosts.start(self.intent('three'))
        self.assertEqual(len(self.client.created), 2)

    def test_insufficient_reservation_and_expired_deadline_never_create(self):
        with self.assertRaisesRegex(Conflict, 'modal_funding_unconfirmed'):
            self.hosts.start(self.intent(cost=100_000))
        intent = self.intent('expired')
        self.now += 700
        with self.assertRaisesRegex(Conflict, 'modal_window_invalid'):
            self.hosts.start(intent)
        self.assertEqual(self.client.created, [])

    def test_sdk_exception_text_is_not_persisted(self):
        self.client.drop_create = True
        self.hosts.start(self.intent())
        with self.repo.engine.connect() as c:
            data = str(list(c.execute(select(scaler_actions)).mappings()))
        self.assertNotIn('PRIVATE_TOKEN', data)

    def test_stop_ack_alone_does_not_settle_or_replay_stop(self):
        intent = self.intent()
        self.hosts.start(intent)
        before = self.balance()
        self.client.drop_stop = True
        self.assertEqual(self.hosts.stop(intent)['state'], 'destroying')
        self.assertEqual(self.hosts.stop(intent)['state'], 'destroyed')
        self.assertEqual(self.client.stopped, ['sb-1'])
        self.assertEqual(self.balance(), before)

    def test_changed_qualified_image_does_not_adopt_existing_host(self):
        intent = self.intent()
        self.hosts.start(intent)
        changed = ModalHosts(
            self.repo,
            self.client,
            replace(self.spec, image_id='im-other'),
            environment='staging',
            enabled=True,
        )
        with self.assertRaisesRegex(Conflict, 'modal_host_binding_changed'):
            changed.start(intent)
        self.assertEqual(len(self.client.created), 1)
