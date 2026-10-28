"""Default-OFF staging Modal process hosts on the original rental journal.

This boundary consumes an already funded instance intent. It neither schedules
jobs nor creates budgets. WorkerRunner, attempt fences and artifact settlement
remain on the CPU host. A missing create response is never retried.
"""

from dataclasses import asdict, dataclass
import math
import re
import uuid

from sqlalchemy import insert, select, update

from .budget_policy import effective_budget_limit
from .repository import (
    Conflict,
    budget_accounts,
    budget_reservations,
    instance_intents,
    scaler_actions,
    scaler_receipts,
)


@dataclass(frozen=True)
class HostSpec:
    app_id: str
    image_id: str
    volume_id: str
    max_hourly_microusd: int
    app_name: str = 'inkseq-staging-worker'
    gpu: str = 'L40S'
    memory_mib: int = 41 * 1024
    cpu_limit: int = 4
    max_containers: int = 2
    max_inputs: int = 1
    idle_timeout: int = 60

    def __post_init__(self):
        for value, prefix in (
            (self.app_id, 'ap-'),
            (self.image_id, 'im-'),
            (self.volume_id, 'vo-'),
        ):
            if not isinstance(value, str) or not re.fullmatch(
                prefix + r'[A-Za-z0-9]+', value
            ):
                raise ValueError('modal_qualified_resource_required')
        if (
            # 2026-10-28 public Sandbox rates: GPU + four physical CPU cores
            # and 41 GiB at non-preemptible memory/CPU rates. A qualified quote
            # must cover this floor; the shared USD50 task ceiling is unchanged.
            type(self.max_hourly_microusd) is not int
            or not 3_503_340 <= self.max_hourly_microusd <= 4_000_000
            or self.gpu != 'L40S'
            or self.memory_mib != 41 * 1024
            or self.cpu_limit != 4
            or self.max_containers != 2
            or self.max_inputs != 1
            or self.idle_timeout != 60
        ):
            raise ValueError('modal_staging_limits_invalid')
        if any(
            type(value) is not int
            for value in (
                self.memory_mib,
                self.cpu_limit,
                self.max_containers,
                self.max_inputs,
                self.idle_timeout,
            )
        ):
            raise ValueError('modal_staging_limits_invalid')
        if self.app_name != 'inkseq-staging-worker':
            raise ValueError('modal_staging_app_required')


class ModalHosts:
    """Injected client: create(request), lookup(name, id), terminate(id).

    Client lookup returns only {id, exit_code}, or None on unconfirmed absence.
    None is never evidence that a paid create did not happen. Process exit is
    removal evidence only: supplier cost stays unknown until invoice adoption.
    """

    def __init__(self, repo, client, spec, *, environment=None, enabled=False):
        self.repo, self.client, self.spec = repo, client, spec
        self.enabled = enabled is True and environment == 'staging'

    def _intent(self, c, identity):
        row = self.repo._locked(
            c, select(instance_intents).where(instance_intents.c.id == identity)
        )
        if row is None or row['provider'] != 'modal':
            raise Conflict('modal_original_intent_required')
        return row

    def _action(self, c, identity):
        row = self.repo._locked(
            c, select(scaler_actions).where(scaler_actions.c.intent_id == identity)
        )
        if row is not None and row['launch_spec'] != asdict(self.spec):
            raise Conflict('modal_host_binding_changed')
        return row

    def _receipt(self, c, identity, operation, facts):
        c.execute(
            insert(scaler_receipts).values(
                id=str(uuid.uuid4()),
                intent_id=identity,
                operation=operation,
                observed_at=self.repo.clock(),
                facts=facts,
            )
        )
        c.execute(
            update(scaler_actions)
            .where(scaler_actions.c.intent_id == identity)
            .values(
                last_observation=facts,
                last_observed_at=self.repo.clock(),
            )
        )

    @staticmethod
    def _summary(row):
        return {
            'intent_id': row['id'],
            'instance_id': row.get('provider_instance_id'),
            'state': row['state'],
        }

    def _funding(self, c, intent, timeout):
        amount = intent['reserved_cost_microusd']
        # Round up; use the qualified all-resource ceiling, never GPU-only price.
        required = math.ceil(self.spec.max_hourly_microusd * (timeout + 300) / 3600)
        rows = list(
            c.execute(
                select(budget_reservations).where(
                    budget_reservations.c.reference_type == 'instance',
                    budget_reservations.c.reference_id == intent['id'],
                )
            ).mappings()
        )
        if (
            amount < required
            or not rows
            or any(
                r['state'] != 'reserved' or r['amount_microusd'] != amount for r in rows
            )
        ):
            raise Conflict('modal_funding_unconfirmed')
        from .dstack_test_budget import ACCOUNT

        if any(row['account_id'] == ACCOUNT for row in rows):
            from .modal_budget import enforce

            enforce(c, intent['id'], asdict(self.spec), self.repo.clock())
        for row in rows:
            account = self.repo._locked(
                c,
                select(budget_accounts).where(
                    budget_accounts.c.id == row['account_id']
                ),
            )
            if (
                account is None
                or account['reserved_microusd'] < amount
                or account['spent_microusd'] + account['reserved_microusd']
                > effective_budget_limit(account)
            ):
                raise Conflict('modal_funding_unconfirmed')

    def start(self, identity):
        if not self.enabled:
            raise Conflict('modal_staging_disabled')
        with self.repo.transaction() as c:
            self.repo._lock_capacity(c)
            intent = self._intent(c, identity)
            action = self._action(c, identity)
            existing = action is not None
            if not existing:
                if intent['state'] != 'reserved':
                    raise Conflict('modal_original_intent_required')
                deadline = intent['hard_deadline']
                remaining = (
                    deadline - self.repo.clock()
                    if type(deadline) in (int, float)
                    else 0
                )
                if not math.isfinite(remaining) or not 0 < remaining <= 7200:
                    raise Conflict('modal_window_invalid')
                timeout = math.floor(remaining)
                if timeout < 1:
                    raise Conflict('modal_window_invalid')
                active = list(
                    c.execute(
                        select(instance_intents.c.id)
                        .join(
                            scaler_actions,
                            scaler_actions.c.intent_id == instance_intents.c.id,
                        )
                        .where(
                            instance_intents.c.provider == 'modal',
                            instance_intents.c.state != 'destroyed',
                        )
                    ).scalars()
                )
                if len(active) >= self.spec.max_containers:
                    raise Conflict('modal_container_limit')
                self._funding(c, intent, timeout)
                c.execute(
                    insert(scaler_actions).values(
                        intent_id=identity,
                        pool=intent['pool'],
                        launch_spec=asdict(self.spec),
                        create_started_at=self.repo.clock(),
                    )
                )
                self.repo.update_instance(identity, 'creating', connection=c)
                # Commit before the sole external create. Crash here is unknown,
                # never permission for another create after controller recovery.
        if existing:
            return self.observe(identity)
        request = {
            **asdict(self.spec),
            'name': 'inkseq-' + identity,
            'timeout': timeout,
        }
        try:
            instance_id = self.client.create(request)
            if not isinstance(instance_id, str) or not re.fullmatch(
                r'sb-[A-Za-z0-9]+', instance_id
            ):
                raise ValueError
        except Exception:
            with self.repo.transaction() as c:
                row = self._intent(c, identity)
                if row['state'] == 'creating':
                    row = self.repo.update_instance(
                        identity, 'creation_unknown', connection=c
                    )
                self._receipt(c, identity, 'create_unknown', {'state': 'unknown'})
                return self._summary(row)
        with self.repo.transaction() as c:
            row = self._intent(c, identity)
            if row['state'] in {'creating', 'creation_unknown'}:
                row = self.repo.update_instance(
                    identity, 'starting', provider_instance_id=instance_id, connection=c
                )
            elif row['provider_instance_id'] != instance_id:
                raise Conflict('modal_create_result_needs_reconciliation')
            self._receipt(c, identity, 'created', {'instance_id': instance_id})
            return self._summary(row)

    def observe(self, identity):
        with self.repo.transaction() as c:
            row = self._intent(c, identity)
            if self._action(c, identity) is None:
                return self._summary(row)
        try:
            fact = self.client.lookup('inkseq-' + identity, row['provider_instance_id'])
            if fact is None:
                return self._summary(row)
            if (
                set(fact) != {'id', 'exit_code'}
                or not isinstance(fact['id'], str)
                or not re.fullmatch(r'sb-[A-Za-z0-9]+', fact['id'])
                or fact['exit_code'] is not None
                and type(fact['exit_code']) is not int
                or row['provider_instance_id'] not in (None, fact['id'])
            ):
                raise ValueError
        except Exception:
            return self._summary(row)
        with self.repo.transaction() as c:
            row = self._intent(c, identity)
            if row['state'] in {'creating', 'creation_unknown'}:
                row = self.repo.update_instance(
                    identity, 'starting', provider_instance_id=fact['id'], connection=c
                )
            if fact['exit_code'] is not None and row['state'] != 'destroyed':
                if row['state'] in {'ready', 'busy'}:
                    row = self.repo.update_instance(identity, 'draining', connection=c)
                if row['state'] != 'destroying':
                    row = self.repo.update_instance(
                        identity, 'destroying', connection=c
                    )
                row = self.repo.update_instance(
                    identity, 'destroyed', destruction_confirmed=True, connection=c
                )
            self._receipt(c, identity, 'observed', fact)
            return self._summary(row)

    def stop(self, identity):
        # Recover/stop existing paid hosts even when new admission is OFF.
        with self.repo.transaction() as c:
            row = self._intent(c, identity)
            action = self._action(c, identity)
            if action is None or row['provider_instance_id'] is None:
                return self._summary(row)
            prior = (
                action['destroy_started_at'] is not None or row['state'] == 'destroyed'
            )
            if not prior:
                if row['state'] in {'ready', 'busy'}:
                    row = self.repo.update_instance(identity, 'draining', connection=c)
                row = self.repo.update_instance(identity, 'destroying', connection=c)
                c.execute(
                    update(scaler_actions)
                    .where(scaler_actions.c.intent_id == identity)
                    .values(destroy_started_at=self.repo.clock())
                )
        if prior:
            return self.observe(identity)
        try:
            self.client.terminate(row['provider_instance_id'])
        except Exception:
            pass  # Unknown response: original identity remains for observation.
        return self._summary(row)  # Acknowledgment never proves physical removal.
