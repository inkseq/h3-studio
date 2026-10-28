"""Private CPU adapters for positively journaled, single-slot Modal hosts.

SQL remains the operation authority. Local files retain private transport
identity, never business jobs. Unknown initialization is read back, not retried.
"""

import json
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

from sqlalchemy import insert, select

from .control import WorkerSpec
from .fleet import SlotConfig, slot_runner
from .inference.wangp_contract import HostReadiness
from .inference.wangp_factory import create_backend, read_document
from .inference.wangp_http import HTTPWanGPTransport
from .modal_admission import OPERATION
from .modal_budget import binding, enforce, require
from .modal_entry import _write, launch_document
from .modal_launch import initialize, observe
from .modal_runtime import PROFILE
from .modal_transport import ModalHTTPTransport
from .repository import request_hash, scaler_receipts
from .runtime_catalog import engine_manifest, profile_output_delivery
from .runtime_hosts.wangp_http import private_token_file
from .slot_lock import slot_lock
from .wangp_bootstrap import _write_immutable

STARTED = 'modal_initialize_started'
READY = 'modal_runtime_bound'
ENDPOINT = 'http://127.0.0.1:8199'


def private_directory(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and not path.is_symlink())
    require(
        os.name == 'nt' or (info.st_uid == os.getuid() and not info.st_mode & 0o077)
    )


class ModalWorkerBindings:
    def __init__(
        self,
        repo,
        root,
        connect,
        *,
        stderr_sink,
        initializer=initialize,
        observer=observe,
        readiness=None,
        capacity_usage=None,
    ):
        self.repo, self.root, self.connect = repo, Path(root), connect
        require(self.root.is_absolute())
        self.root.mkdir(mode=0o700, exist_ok=True)
        private_directory(self.root)
        self.stderr_sink = stderr_sink
        self.initializer, self.observer = initializer, observer
        self.readiness = readiness or self._readiness
        self.capacity_usage = capacity_usage

    def _receipts(self, c, identity, operation):
        return list(
            c.execute(
                select(scaler_receipts.c.facts).where(
                    scaler_receipts.c.intent_id == identity,
                    scaler_receipts.c.operation == operation,
                )
            ).scalars()
        )

    def _original(self, c, identity):
        intent, _ = binding(c, identity)
        require(intent['state'] in {'starting', 'ready', 'busy'})
        require(
            isinstance(intent['provider_instance_id'], str)
            and re.fullmatch(r'sb-[A-Za-z0-9]+', intent['provider_instance_id'])
        )
        receipts = self._receipts(c, identity, OPERATION)
        require(len(receipts) == 1)
        selection = receipts[0]
        manifest = engine_manifest(PROFILE, selection['mode'])
        require(selection['manifest_digest'] == manifest.digest)
        return intent, selection, manifest

    def _record(self, c, identity, operation, facts):
        c.execute(
            insert(scaler_receipts).values(
                id=str(uuid.uuid4()),
                intent_id=identity,
                operation=operation,
                observed_at=self.repo.clock(),
                facts=facts,
            )
        )

    def _files(self, identity, selection, manifest):
        directory = self.root / identity
        if not directory.exists():
            with self.repo.engine.connect() as c:
                require(not self._receipts(c, identity, STARTED))
            value = dict(
                intent_id=identity,
                mode=selection['mode'],
                manifest_digest=manifest.digest,
                token=secrets.token_hex(32),
            )
            pending = self.root / (identity + '.pending')
            pending.mkdir(mode=0o700, exist_ok=False)
            _write(pending / 'launch.json', json.dumps(value).encode())
            _write(pending / 'token', value['token'].encode())
            _write(pending / 'manifest.json', json.dumps(manifest.document).encode())
            pending.rename(directory)
        private_directory(directory)
        value = launch_document(directory / 'launch.json')
        require(
            value['intent_id'] == identity
            and value['mode'] == selection['mode']
            and value['manifest_digest'] == manifest.digest
        )
        require(private_token_file(directory / 'token') == value['token'])
        require(read_document(directory / 'manifest.json') == manifest.document)
        return directory, value

    def _initialize(self, identity, sandbox, value):
        with self.repo.transaction() as c:
            self.repo._lock_capacity(c)
            intent, _, _ = self._original(c, identity)
            facts = {
                'instance_id': intent['provider_instance_id'],
                'launch_hash': request_hash(value),
            }
            prior = self._receipts(c, identity, STARTED)
            if prior:
                require(prior == [facts])
            else:
                _, bound = binding(c, identity)
                enforce(c, identity, bound['spec'], self.repo.clock())
                require(
                    intent['state'] == 'starting'
                    and intent['hard_deadline'] > self.repo.clock()
                )
                self._record(c, identity, STARTED, facts)
        if prior:
            require(self.observer(sandbox, value, stderr_sink=self.stderr_sink) is True)
        else:
            # The durable marker commits BEFORE this sole external install.
            self.initializer(sandbox, value, stderr_sink=self.stderr_sink)

    def _readiness(self, sandbox, token):
        transport = HTTPWanGPTransport(
            ENDPOINT,
            token,
            transport=ModalHTTPTransport(sandbox, stderr_sink=self.stderr_sink),
        )
        try:
            return transport.readiness()
        finally:
            transport.close()

    def slot(self, identity):
        try:
            require(isinstance(identity, str) and str(uuid.UUID(identity)) == identity)
            with slot_lock(self.root, identity) as acquired:
                require(acquired)
                with self.repo.engine.connect() as c:
                    intent, selection, manifest = self._original(c, identity)
                directory, value = self._files(identity, selection, manifest)
                sandbox = self.connect(intent['provider_instance_id'])
                require(sandbox.object_id == intent['provider_instance_id'])
                self._initialize(identity, sandbox, value)
                native = self.readiness(sandbox, value['token'])
                require(
                    isinstance(native, HostReadiness)
                    and native.manifest_digest == manifest.digest
                    and native.slot_key == 'modal-' + identity
                    and isinstance(native.incarnation, str)
                    and re.fullmatch(r'[0-9a-f]{32}', native.incarnation)
                    and type(native.idle) is bool
                )
                return self._slot(
                    identity, intent, selection, manifest, directory, native
                )
        except Exception:
            raise ValueError('modal_worker_binding_unconfirmed') from None

    def _slot(self, identity, intent, selection, manifest, directory, native):
        facts = dict(
            instance_id=intent['provider_instance_id'],
            manifest_digest=manifest.digest,
            runtime_incarnation=native.incarnation,
        )
        with self.repo.transaction() as c:
            self.repo._lock_capacity(c)
            current, _, _ = self._original(c, identity)
            require(current['provider_instance_id'] == intent['provider_instance_id'])
            prior = self._receipts(c, identity, READY)
            require(not prior or prior == [facts])
            _write_immutable(
                directory / 'client.json',
                dict(
                    version=1,
                    enabled=True,
                    slot_key='modal-' + identity,
                    configuration_id=selection['configuration_id'],
                    manifest_file=str(directory / 'manifest.json'),
                    token_file=str(directory / 'token'),
                    runtime_incarnation=native.incarnation,
                ),
            )
            if not prior:
                self._record(c, identity, READY, facts)
        spec = WorkerSpec(
            'modal-' + identity.replace('-', ''),
            intent['pool'],
            'modal',
            intent['provider_instance_id'],
            ('gpu0',),
            (manifest.document['generation_recipe_id'],),
            manifest.document['model_id'],
            selection['configuration_id'],
            'wangp-worker',
            manifest.digest,
            profile_output_delivery(PROFILE),
            'hatchet-v1',
        )
        return SlotConfig(
            spec,
            enabled=True,
            endpoint=ENDPOINT,
            allowed_origins=(ENDPOINT,),
            confirmed_idle=native.idle,
            runtime_config_file=str(directory / 'client.json'),
        )

    def runner_context(self, config, worker_id, settings, *, repository):
        """Inject only private transport; retain the real WorkerRunner/claim/store."""
        slot = config.slot(worker_id)
        if slot.spec.provider != 'modal':
            return slot_runner(config, worker_id, settings, repository=repository)
        require(callable(self.capacity_usage))

        def factory(selected, directory):
            sandbox = self.connect(selected.spec.instance_id)
            require(sandbox.object_id == selected.spec.instance_id)
            return create_backend(
                selected,
                directory,
                http_transport=ModalHTTPTransport(
                    sandbox, stderr_sink=self.stderr_sink
                ),
            )

        return slot_runner(
            config,
            worker_id,
            settings,
            repository=repository,
            backend_factory=factory,
            capacity_usage=self.capacity_usage,
        )
