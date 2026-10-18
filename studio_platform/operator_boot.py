"""Prepared WanGP slots for an existing operator allocation, never a renter.

One CPU controller owns private SSH tunnels and worker children. Every physical
GPU has its own journal/runtime/configuration; shared weight bytes are locked.
Restart never replays a setup command or resurrects an unobserved worker child.
"""

from dataclasses import asdict
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import or_, select

from .control import TERMINAL, WorkerControl, worker_spec_payload
from .fleet import FleetSupervisor, read_config as read_fleet, run_slot
from .inference.wangp import WanGPBackend
from .lium_bootstrap import BootConfig, BootController, BootError, idle_proof_type
from .lium_provider import _uuid
from .operator_capacity import operator_nodes, public_bootstrap
from .qualification_profiles import QUEUED_TASK_PROFILE
from .repository import (
    Repository,
    attempts,
    instance_intents,
    jobs,
    registered_devices,
    registered_workers,
    request_hash,
)
from .runtime_catalog import engine_manifest, get_profile
from .wangp_bootstrap import SOURCE_NAMES, WanGPSSHHost
from .worker import _slot_lock


def source_hashes(directory):
    result = {}
    directory = Path(directory)
    for name in SOURCE_NAMES:
        file = directory / name
        if (
            file.is_symlink()
            or not file.is_file()
            or file.stat().st_size > 16 * 1024**2
        ):
            raise BootError('operator_boot_source_invalid')
        result[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def ports_for(work_dir, intent_id, count, start):
    """Stable local tunnel assignments. Never recycle a past intent's ports."""
    _uuid(intent_id)
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    if type(start) is not int or not 1024 <= start <= 64512 or not 1 <= count <= 8:
        raise BootError('operator_port_range_invalid')
    with _slot_lock(root, 'operator-port-allocation') as locked:
        if not locked:
            raise BootError('operator_port_allocation_busy')
        path = root / 'ports.json'
        if path.is_symlink():
            raise BootError('operator_port_receipt_invalid')
        value = (
            json.loads(path.read_text())
            if path.exists()
            else {'start': start, 'ports': {}}
        )
        flat = [v for values in value.get('ports', {}).values() for v in values]
        if (
            value.get('start') != start
            or len(flat) != len(set(flat))
            or any(type(v) is not int or not start <= v < start + 1024 for v in flat)
        ):
            raise BootError('operator_port_receipt_invalid')
        if intent_id in value['ports']:
            if len(value['ports'][intent_id]) != count:
                raise BootError('operator_port_identity_changed')
            return tuple(value['ports'][intent_id])
        available = [v for v in range(start, start + 1024) if v not in flat][:count]
        if len(available) != count:
            raise BootError('operator_port_range_exhausted')
        value['ports'][intent_id] = available
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as target:
            json.dump(value, target, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
        return tuple(available)


class OperatorBoot:
    def __init__(
        self,
        repo,
        provider,
        binding,
        intent,
        selection,
        runtime_config,
        *,
        boot_class=BootController,
        popen=None,
    ):
        self.repo, self.provider, self.binding = repo, provider, binding
        self.intent_id, self.instance_id = intent['id'], intent['provider_instance_id']
        self.provider_id = binding.launch.provider
        if intent['provider'] != self.provider_id:
            raise BootError('operator_boot_provider_mismatch')
        self.runtime_config = runtime_config
        self._popen_impl = popen or subprocess.Popen
        self.stopping = False
        self._guard = lambda: False
        self.slots = []
        root = Path(runtime_config['work_dir'])
        ports = ports_for(
            root, self.intent_id, binding.execution_slots, runtime_config['port_start']
        )
        profile = get_profile(binding.runtime_profile_id)
        sources, hashes = binding.boot['source_dirs'], binding.boot['source_sha256']
        if (
            len(sources) != binding.execution_slots
            or len(hashes) != binding.execution_slots
        ):
            raise BootError('operator_slot_source_count_mismatch')
        for index in range(binding.execution_slots):
            if source_hashes(sources[index]) != hashes[index]:
                raise BootError('operator_slot_source_changed')
            config = BootConfig(
                root / 'boot' / self.intent_id / str(index),
                Path(sources[index]),
                Path(runtime_config['ssh_key_file']),
                Path(runtime_config['known_hosts_file']),
                ports[index],
                binding.configuration_id,
                model_id=binding.model_id,
                min_gpu_bytes=profile.get('hardware_admission', {}).get(
                    'minimum_total_vram_bytes',
                    (30 if 'Pruned' in profile['model_id'] else 90) * 1024**3,
                ),
                enabled=True,
                trust_first_host_key=runtime_config.get('trust_first_host_key', False),
                smoke_enabled=False,
                fleet_enabled=True,
                recipe_ids=binding.recipe_ids,
                minimum_remaining_s=300,
                qualification_profile=QUEUED_TASK_PROFILE,
                execution_backend='wangp-worker',
                engine_manifest_digest=binding.engine_manifest_digest,
                output_delivery='native-frames-v1',
                deployment_profile_id=binding.runtime_profile_id,
                runtime_python='/venv/main/bin/python',
                profile_slot_index=index,
                expected_host_gpus=binding.gpu_count,
                provider=self.provider_id,
            )
            boot = boot_class(
                repo,
                provider,
                config,
                ssh_factory=WanGPSSHHost,
                fleet_factory=self._fleet,
            )
            boot.start_guard = self._start_allowed
            boot.enable_pollable_upload()
            self.slots.append(boot)

    def _fleet(self, config, repo, path):
        return FleetSupervisor(config, repo, path, popen=self._popen)

    def _popen(self, argv, **kwargs):
        if (
            len(argv) != 9
            or argv[:3] != [sys.executable, '-m', 'studio_platform.fleet']
            or argv[3] != '--config'
            or argv[5] != '--slot'
            or argv[7] != '--config-hash'
        ):
            raise BootError('operator_worker_command_invalid')
        kwargs['stdin'] = subprocess.DEVNULL
        return self._popen_impl(
            [
                sys.executable,
                '-m',
                'studio_platform.operator_boot',
                '--worker',
                '--runtime-config',
                self.runtime_config['config_path'],
                '--intent',
                self.intent_id,
                *argv[3:],
            ],
            **kwargs,
        )

    def set_start_guard(self, guard):
        self._guard = guard

    def _start_allowed(self):
        if self.stopping or self._guard() is not True:
            return False
        with self.repo.engine.connect() as conn:
            node = (
                conn.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == self.intent_id
                    )
                )
                .mappings()
                .one()
            )
        return (
            node['desired_state'] == 'running'
            and node['binding_hash'] == self.binding.fingerprint
        )

    def request_drain(self):
        self.stopping = True
        for boot in self.slots:
            boot.cancel_preparation()
            if boot.fleet:
                boot.fleet.drain()

    def cancel_preparation(self):
        for boot in self.slots:
            boot.cancel_preparation()

    def tick(self, intent_id, *, stopping=False):
        if intent_id != self.intent_id:
            raise BootError('operator_boot_identity_mismatch')
        if stopping:
            self.request_drain()
        reports = []
        for boot in self.slots:
            if self.stopping:
                # Existing children keep reconciling/collecting. Do not close
                # the tunnel until the original rental is confirmed destroyed.
                if boot.fleet:
                    boot.fleet.tick()
                reports.append({'state': 'draining'})
            else:
                reports.append(boot.tick(intent_id))
        if self.stopping:
            self._retire_stopped_workers()
        states = {r['state'] for r in reports}
        if states == {'fleet_running'}:
            state = 'ready'
        elif states <= {'draining'}:
            state = 'draining'
        elif states & {'bootstrap_start_unknown', 'bootstrap_reconciliation_required'}:
            state = 'blocked'
        elif states & {
            'bootstrap_failed',
            'staging_failed',
            'fleet_attention_required',
        }:
            state = 'failed'
        elif states & {'fleet_recovery_required', 'staging_recovery_required'}:
            state = 'blocked'
        else:
            state = 'preparing'
        reason = (
            'bootstrap_reconciliation_required'
            if states & {'bootstrap_start_unknown', 'bootstrap_reconciliation_required'}
            else 'operator_bootstrap_failed'
            if state in {'failed', 'blocked'}
            else None
        )
        return public_bootstrap(
            {'state': state, 'reason_code': reason, 'slots': reports}
        )

    def _retire_stopped_workers(self):
        """Release only this parent's exited children, never an unowned crash.

        All configured slots must agree. The original provider stop still needs
        its own fresh idle observation and removal receipt on the next tick.
        """
        if not self.stopping or len(self.slots) != self.binding.execution_slots:
            return False
        # Fake/absent or restarted fleets cannot establish process ownership.
        if any(
            boot.fleet is None or getattr(boot.fleet, 'config', None) is None
            for boot in self.slots
        ):
            return False
        control = WorkerControl(self.repo)
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == self.intent_id),
            )
            node = self.repo._locked(
                connection,
                select(operator_nodes).where(
                    operator_nodes.c.intent_id == self.intent_id
                ),
            )
            if (
                intent is None
                or node is None
                or intent['state'] != 'draining'
                or node['desired_state'] not in {'drained', 'stopped'}
                or node['binding_hash'] != self.binding.fingerprint
                or node['binding_id'] != self.binding.binding_id
                or intent['provider'] != self.provider_id
                or intent['provider_instance_id'] != self.instance_id
                or intent['pool'] != self.binding.pool
                or intent['physical_gpus'] != self.binding.gpu_count
                or intent['slots'] != self.binding.execution_slots
            ):
                return False
            configured, physical = {}, set()
            for index, boot in enumerate(self.slots):
                pending = getattr(boot, 'preparation_pending', None)
                fleet = boot.fleet
                active = [slot for slot in fleet.config.slots if slot.enabled]
                worker_id = (
                    self.provider_id
                    + '-'
                    + self.intent_id.replace('-', '')
                    + '-gpu'
                    + str(index)
                )
                if (
                    not callable(pending)
                    or pending()
                    or len(active) != 1
                    or set(fleet.children) != {worker_id}
                    or active[0].spec.worker_id != worker_id
                    or fleet.children[worker_id].poll() is None
                    or getattr(boot, 'bound_intent', None) != self.intent_id
                    or getattr(boot, 'bound_instance', None) != self.instance_id
                ):
                    return False
                spec, backend = active[0].spec, getattr(boot, 'backend', None)
                if (
                    spec.provider != self.provider_id
                    or spec.instance_id != self.instance_id
                    or spec.pool != self.binding.pool
                    or spec.backend != 'wangp-worker'
                    or spec.model_id != self.binding.model_id
                    or spec.configuration_id != self.binding.configuration_id
                    or spec.recipe_ids != self.binding.recipe_ids
                    or spec.engine_manifest_digest
                    != self.binding.engine_manifest_digest
                    or spec.output_delivery != boot.config.output_delivery
                    or physical.intersection(spec.physical_gpu_ids)
                    or not isinstance(backend, WanGPBackend)
                    or backend.enabled is not True
                    or backend.manifest.digest != self.binding.engine_manifest_digest
                    or backend.slot_key != self.intent_id
                    or not backend.expected_incarnation
                ):
                    return False
                configured[worker_id] = (spec, backend)
                physical.update(spec.physical_gpu_ids)
            if len(physical) != self.binding.gpu_count:
                return False
            workers = list(
                connection.execute(
                    select(registered_workers)
                    .where(
                        registered_workers.c.provider == self.provider_id,
                        registered_workers.c.instance_id == self.instance_id,
                    )
                    .order_by(registered_workers.c.id)
                    .with_for_update()
                ).mappings()
            )
            by_id = {worker['id']: worker for worker in workers}
            if not set(configured) <= set(by_id) or any(
                worker['state'] != 'retired' and worker['id'] not in configured
                for worker in workers
            ):
                return False
            for worker_id, (spec, _) in configured.items():
                worker = by_id[worker_id]
                if (
                    worker['current_job_id'] is not None
                    or worker['state'] not in {'draining', 'unknown', 'retired'}
                    or worker['state'] != 'retired'
                    and worker['drain_requested'] != 1
                    or worker['pool'] != spec.pool
                    or worker['spec_hash'] != request_hash(worker_spec_payload(spec))
                    or worker['spec'] != worker_spec_payload(spec)
                ):
                    return False
            worker_ids = list(by_id)
            devices = list(
                connection.execute(
                    select(registered_devices)
                    .where(
                        or_(
                            (registered_devices.c.provider == self.provider_id)
                            & (registered_devices.c.instance_id == self.instance_id),
                            registered_devices.c.worker_id.in_(worker_ids),
                        )
                    )
                    .order_by(
                        registered_devices.c.provider,
                        registered_devices.c.instance_id,
                        registered_devices.c.gpu_id,
                    )
                    .with_for_update()
                ).mappings()
            )
            for worker_id, (spec, _) in configured.items():
                held = [
                    device
                    for device in devices
                    if device['worker_id'] == worker_id
                    and device['state'] != 'released'
                ]
                if by_id[worker_id]['state'] == 'retired':
                    if held:
                        return False
                elif {device['gpu_id'] for device in held} != set(
                    spec.physical_gpu_ids
                ) or any(
                    device['provider'] != self.provider_id
                    or device['instance_id'] != self.instance_id
                    or device['state'] != 'owned'
                    for device in held
                ):
                    return False
            if any(
                device['state'] != 'released' and device['worker_id'] not in configured
                for device in devices
            ):
                return False
            # Lock workers -> jobs -> attempts, matching claim/observe ordering.
            history_query = (
                select(attempts)
                .where(attempts.c.worker_id.in_(worker_ids))
                .order_by(attempts.c.id)
            )
            history = list(connection.execute(history_query).mappings())
            job_rows = {
                job['id']: job
                for job in connection.execute(
                    select(
                        jobs.c.id,
                        jobs.c.status,
                        jobs.c.lease_worker_id,
                        jobs.c.lease_expires_at,
                    )
                    .where(
                        or_(
                            jobs.c.id.in_([attempt['job_id'] for attempt in history]),
                            jobs.c.lease_worker_id.in_(worker_ids),
                        )
                    )
                    .order_by(jobs.c.id)
                    .with_for_update()
                ).mappings()
            }
            history = list(
                connection.execute(history_query.with_for_update()).mappings()
            )
            if any(
                job['status'] not in TERMINAL
                or job['lease_worker_id'] is not None
                or job['lease_expires_at'] is not None
                for job in job_rows.values()
            ) or any(
                attempt['job_id'] not in job_rows
                or attempt['status'] not in TERMINAL
                or (
                    attempt['submission_started_at'] is not None
                    or attempt['upstream_task_id'] is not None
                )
                and attempt['upstream_stopped'] != 1
                for attempt in history
            ):
                return False
            from .artifact_writer import write_receipts

            receipts = connection.execute(
                select(write_receipts.c.record)
                .where(write_receipts.c.job_id.in_(job_rows))
                .with_for_update()
            ).scalars()
            try:
                if any(
                    json.loads(record).get('phase') != 'settled' for record in receipts
                ):
                    return False
            except (TypeError, ValueError, AttributeError):
                return False
            # The pinned adapter authenticates manifest, slot and incarnation.
            # Probe while the ledger locks exclude new work; do not reuse a prior
            # provider fact or infer remote idle from the exited local process.
            proofs = []
            for _, backend in configured.values():
                observed_at = self.repo.clock()
                if backend.is_idle() is not True:
                    return False
                proofs.append(observed_at)
            if any(not 0 <= self.repo.clock() - at <= 30 for at in proofs):
                return False
            for worker_id, (_, backend) in configured.items():
                worker = by_id[worker_id]
                if worker['state'] == 'retired':
                    continue
                retired = control._retire_locked(connection, worker)
                self.repo._emit(
                    connection,
                    'worker.owned_drain_retired',
                    worker_id,
                    {
                        'intent_id': self.intent_id,
                        'worker_id': worker_id,
                        'reason_code': 'owned_child_exited_runtime_idle',
                        'spec_hash': worker['spec_hash'],
                        'previous_fence': worker['fence'],
                        'next_fence': retired['fence'],
                        'manifest_digest': self.binding.engine_manifest_digest,
                        'slot_key': backend.slot_key,
                        'runtime_incarnation': backend.expected_incarnation,
                        'observed_at': min(proofs),
                    },
                )
            return True

    def idle_probe(self, tag, instance_id):
        if tag != self.intent_id or instance_id != self.instance_id:
            raise BootError('operator_idle_identity_mismatch')
        proofs = [boot.idle_probe(tag, instance_id) for boot in self.slots]
        proof_type = idle_proof_type(self.provider_id)
        if not proofs or any(
            not isinstance(p, proof_type) or p.instance_id != instance_id
            for p in proofs
        ):
            return None
        return proof_type(
            instance_id,
            min(p.observed_at for p in proofs),
            max(p.idle_since for p in proofs),
            all(p.idle for p in proofs),
        )

    def shutdown_status(self):
        """Local ownership proof only; never proof of remote removal/settlement."""
        known, done = True, True
        for boot in self.slots:
            pending = getattr(boot, 'preparation_pending', None)
            if callable(pending) and pending():
                done = False
            directory = boot.config.work_dir / self.intent_id
            worker_id = (
                self.provider_id
                + '-'
                + self.intent_id.replace('-', '')
                + '-gpu'
                + str(boot.config.profile_slot_index)
            )
            with self.repo.engine.connect() as conn:
                worker = (
                    conn.execute(
                        select(registered_workers).where(
                            registered_workers.c.id == worker_id
                        )
                    )
                    .mappings()
                    .first()
                )
            if worker and worker['current_job_id'] is not None:
                done = False
            if boot.fleet:
                config = getattr(boot.fleet, 'config', None)
                expected = (
                    {s.spec.worker_id for s in config.slots if s.enabled}
                    if config
                    else set(boot.fleet.children)
                )
                if not expected or set(boot.fleet.children) != expected:
                    known, done = False, False
                elif any(p.poll() is None for p in boot.fleet.children.values()):
                    done = False
            elif (
                worker is not None
                or (directory / 'fleet.json').exists()
                or (directory / 'fleet' / 'fleet-state.json').exists()
            ):
                known, done = False, False
            else:
                receipt = directory / 'bootstrap-state.json'
                if receipt.exists():
                    state = json.loads(receipt.read_text())
                    if (
                        state.get('phase') in {'fleet_starting', 'fleet_started'}
                        or 'fleet_recipe_ids' in state
                    ):
                        known, done = False, False
        return {'ownership_known': known, 'children_done': done}

    def release_after_drain(self):
        if not self.stopping or self.shutdown_status() != {
            'ownership_known': True,
            'children_done': True,
        }:
            raise BootError('operator_local_shutdown_unconfirmed')
        for boot in self.slots:
            boot.close()

    def close(self):
        # Called after authoritative destruction, never to prove it is safe.
        with self.repo.engine.connect() as conn:
            intent = (
                conn.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == self.intent_id
                    )
                )
                .mappings()
                .one()
            )
        if intent['state'] != 'destroyed':
            raise BootError('operator_close_requires_confirmed_destroyed')
        for boot in self.slots:
            pending = getattr(boot, 'preparation_pending', None)
            if callable(pending) and pending():
                raise BootError('operator_collection_still_running')
            if boot.fleet:
                fleet_config = getattr(boot.fleet, 'config', None)
                if fleet_config is not None:
                    expected = {
                        s.spec.worker_id for s in fleet_config.slots if s.enabled
                    }
                    if not expected or set(boot.fleet.children) != expected:
                        raise BootError('operator_child_ownership_unconfirmed')
                if any(p.poll() is None for p in boot.fleet.children.values()):
                    raise BootError('operator_collection_still_running')
            else:
                directory = boot.config.work_dir / self.intent_id
                if (directory / 'fleet.json').exists() or (
                    directory / 'fleet' / 'fleet-state.json'
                ).exists():
                    raise BootError('operator_child_ownership_unconfirmed')
                receipt = directory / 'bootstrap-state.json'
                if receipt.exists():
                    state = json.loads(receipt.read_text())
                    if (
                        state.get('phase') in {'fleet_starting', 'fleet_started'}
                        or 'fleet_recipe_ids' in state
                    ):
                        raise BootError('operator_child_ownership_unconfirmed')
                worker_id = (
                    self.provider_id
                    + '-'
                    + self.intent_id.replace('-', '')
                    + '-gpu'
                    + str(boot.config.profile_slot_index)
                )
                with self.repo.engine.connect() as conn:
                    retained = conn.execute(
                        select(registered_workers).where(
                            registered_workers.c.id == worker_id
                        )
                    ).first()
                if retained is not None:
                    raise BootError('operator_child_ownership_unconfirmed')
        for boot in self.slots:
            boot.close()


def create_boot(repo, provider, binding, intent, selection, runtime_config):
    return OperatorBoot(repo, provider, binding, intent, selection, runtime_config)


def _admission_window(repo, intent_id, binding):
    """Read the shortened provider window; loss of proof only blocks new work."""
    with repo.engine.connect() as conn:
        node = (
            conn.execute(
                select(operator_nodes).where(operator_nodes.c.intent_id == intent_id)
            )
            .mappings()
            .one()
        )
        intent = (
            conn.execute(
                select(instance_intents).where(instance_intents.c.id == intent_id)
            )
            .mappings()
            .one()
        )
    now = repo.clock()
    deadline = min(intent['hard_deadline'], binding.expires_at)
    from .worker_admission import managed_window_reason

    reason = managed_window_reason(intent, node, now, binding=binding)
    stop = reason not in {None, 'managed_provider_lifetime_unverified'}
    from .operator_controller import provider_lifetime_current

    verified = provider_lifetime_current(node.get('payload', {}), intent, now)
    return stop, deadline, verified


def run_worker(runtime_path, intent_id, fleet_path, worker_id, expected_hash):
    from .operator_runtime import load_runtime_config, create_registry
    from .queued_task_runner import QueuedTaskRunner, read_verification_summary
    from .settings import Settings

    runtime = load_runtime_config(runtime_path)
    registry = create_registry(runtime_path)
    settings = Settings.from_environment()
    repo = Repository(settings.database_url)
    try:
        with repo.engine.connect() as conn:
            intent = (
                conn.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
            node = (
                conn.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == intent_id
                    )
                )
                .mappings()
                .one()
            )
        binding = registry.get(node['binding_id'])
        fleet = read_fleet(Path(fleet_path))
        if (
            fleet.fingerprint() != expected_hash
            or len(fleet.slots) != 1
            or binding.fingerprint != node['binding_hash']
        ):
            raise BootError('operator_worker_binding_changed')
        spec = fleet.slot(worker_id).spec
        index = int(worker_id.rsplit('-gpu', 1)[1])
        expected_path = (
            Path(runtime['work_dir'])
            / 'boot'
            / intent_id
            / str(index)
            / intent_id
            / 'fleet.json'
        )
        if Path(fleet_path).resolve() != expected_path.resolve():
            raise BootError('operator_worker_path_changed')
        state = json.loads((expected_path.parent / 'bootstrap-state.json').read_text())
        identity = state['identity']
        if (
            identity.get('intent_id') != intent_id
            or identity.get('instance_id') != intent['provider_instance_id']
            or spec.provider != intent['provider']
            or spec.provider != binding.launch.provider
            or spec.provider != 'lium'
            and identity.get('provider') != spec.provider
            or identity.get('sources') != binding.boot['source_sha256'][index]
            or spec.configuration_id != binding.configuration_id
            or spec.model_id != binding.model_id
            or spec.pool != binding.pool
            or spec.instance_id != intent['provider_instance_id']
            or spec.engine_manifest_digest != binding.engine_manifest_digest
            or spec.output_delivery != 'native-frames-v1'
            or tuple(spec.recipe_ids) != binding.recipe_ids
            or state.get('runtime_validation')
            != {
                'profile': QUEUED_TASK_PROFILE,
                'state': 'runtime_ready',
                'generation_verified': False,
            }
            or state.get('phase') not in ('fleet_starting', 'fleet_started')
        ):
            raise BootError('operator_worker_identity_mismatch')
        evidence_identity = {
            k: identity[k]
            for k in (
                'intent_id',
                'instance_id',
                'configuration_id',
                'sources',
                'backend',
                'engine_manifest_digest',
                'output_delivery',
            )
        }
        evidence_identity['qualification_profile'] = QUEUED_TASK_PROFILE
        evidence = expected_path.parent / 'queued-task-evidence.json'
        summary = read_verification_summary(
            evidence, expected_identity=evidence_identity
        )
        if (
            summary.get('worker_id', worker_id) != worker_id
            or summary.get('model_id', spec.model_id) != spec.model_id
        ):
            raise BootError('operator_worker_evidence_identity_conflict')
        with repo.engine.connect() as conn:
            worker = (
                conn.execute(
                    select(registered_workers).where(
                        registered_workers.c.id == worker_id
                    )
                )
                .mappings()
                .first()
            )
        quarantined = bool(
            summary.get('runtime_quarantined')
            or worker is not None
            and worker['drain_requested']
        )
        if quarantined:
            if worker is not None:
                WorkerControl(repo).drain(worker_id)
            if worker is None or worker['current_job_id'] is None:
                # run_slot may mark an idle worker ready; never clear a durable
                # drain by restarting it. Bound attempts still need collection.
                return 0

        def stop_new():
            return quarantined or _admission_window(repo, intent_id, binding)[0]

        def job_allowed(job):
            from .worker_admission import COMPLETION_MARGIN_SECONDS

            stop, deadline, verified = _admission_window(repo, intent_id, binding)
            duration = job.get('expected_runtime_s')
            return (
                not quarantined
                and not stop
                and verified
                and job['request'].get('deployment_profile_id')
                == binding.runtime_profile_id
                and job['execution_plan'].get('configuration_id')
                == binding.configuration_id
                and type(duration) in (int, float)
                and math.isfinite(duration)
                and duration > 0
                and repo.clock() + duration + COMPLETION_MARGIN_SECONDS < deadline
            )

        return (
            run_slot(
                fleet,
                worker_id,
                settings,
                repository=repo,
                runner_factory=lambda *a, **kw: QueuedTaskRunner(
                    *a,
                    stop_new=stop_new,
                    job_allowed=job_allowed,
                    collection_lock_dir=Path(runtime['work_dir']) / 'collection-lock',
                    qualification_evidence_file=evidence,
                    evidence_identity=evidence_identity,
                    **kw,
                ),
            )
            or 0
        )
    finally:
        repo.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--runtime-config', required=True)
    parser.add_argument('--intent', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--slot', required=True)
    parser.add_argument('--config-hash', required=True)
    args = parser.parse_args(argv)
    if not args.worker:
        return 0
    try:
        return run_worker(
            args.runtime_config, args.intent, args.config, args.slot, args.config_hash
        )
    except Exception:
        print(json.dumps({'state': 'failed', 'code': 'operator_worker_startup_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
