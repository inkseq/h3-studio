"""Optional staging fallback in the existing CPU controller, default OFF."""

from dataclasses import asdict
import json
from pathlib import Path
import subprocess

from sqlalchemy import select

from .dstack_factory import _json
from .modal_admission import ModalAdmission
from .modal_host import HostSpec, ModalHosts
from .modal_lifecycle import ModalLifecycle
from .modal_worker_binding import ModalWorkerBindings
from .repository import instance_intents

SECRET = '/sixnine/staging/modal'
REGION = 'ap-southeast-1'


def require(value):
    if not value:
        raise ValueError('modal_controller_configuration_unconfirmed')


def client_from_staging_secret(spec):
    """Existing EC2 role and existing secret only; never mutate SDK environment."""
    import boto3
    import modal
    from modal.config import config
    from .modal_client import ModalSandboxClient

    try:
        require(modal.__version__ == '1.6.3')
        require(config['server_url'] == 'https://api.modal.com,https://api.modal2.com')
        secret = boto3.client('secretsmanager', region_name=REGION).get_secret_value(
            SecretId=SECRET, VersionStage='AWSCURRENT'
        )
        value = json.loads(secret['SecretString'])
        require(set(value) == {'MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET'})
        require(
            all(isinstance(v, str) and v.strip() == v and v for v in value.values())
        )
        client = modal.Client.from_credentials(
            value['MODAL_TOKEN_ID'], value['MODAL_TOKEN_SECRET']
        )
        provider = ModalSandboxClient(modal, client, spec)
        return provider, lambda identity: modal.Sandbox.from_id(identity, client=client)
    except Exception:
        raise ValueError('modal_existing_staging_client_unavailable') from None


def load_extension(service, work_dir, *, client_factory=client_from_staging_secret):
    """Absent config performs no credential/SDK I/O and cannot activate a host."""
    path = Path(work_dir) / 'modal.json'
    with service.repo.engine.connect() as c:
        originals = (
            c.execute(
                select(instance_intents.c.id)
                .where(
                    instance_intents.c.provider == 'modal',
                )
                .limit(1)
            ).first()
            is not None
        )
    if not path.exists():
        require(not originals)
        return None
    value = _json(path)
    require(set(value) == {'version', 'enabled', 'host'})
    require(type(value['version']) is int and value['version'] == 1)
    require(type(value['enabled']) is bool)
    require(service.settings.public_origin == 'https://staging.inkseq.com')
    require(service.settings.generation_queue == 'dbos')
    spec = HostSpec(**value['host'])
    require(value['host'] == asdict(spec))
    if not value['enabled'] and not originals:
        return None
    client, connect = client_factory(spec)
    hosts = ModalHosts(
        service.repo, client, spec, environment='staging', enabled=value['enabled']
    )
    bindings = ModalWorkerBindings(
        service.repo,
        Path(work_dir) / 'modal-bindings',
        connect,
        stderr_sink=subprocess.DEVNULL,
    )
    return ModalExtension(service, hosts, bindings, enabled=value['enabled'])


class ModalExtension:
    def __init__(self, service, hosts, bindings, *, enabled):
        self.service, self.hosts, self.bindings = service, hosts, bindings
        self.admission = ModalAdmission(
            service, hosts, environment='staging', enabled=enabled
        )

    def wrap(self, original):
        self.original = original
        self.lifecycle = ModalLifecycle(
            self.service.repo,
            self.hosts,
            self.bindings,
            original.worker_process,
            self.service.settings,
            report=self.report,
        )
        return self

    def __getattr__(self, name):
        return getattr(self.original, name)

    def report(self, identity, receipt):
        telemetry = self.original.telemetry
        grafana_ok = True
        if telemetry.enabled:
            duration = telemetry.start(
                'generate',
                {
                    'job_id': receipt['job_id'],
                    'attempt_id': receipt['attempt_id'],
                    'node_id': identity,
                },
            ).finish('failure', error_code=receipt['reason'], duration_seconds=0)
            grafana_ok = (
                duration is not None and telemetry.force_flush(1000) is not False
            )
        reporter = self.original.reclaim_reporter
        sentry_ok = reporter is None or reporter(receipt) is not None
        return grafana_ok and sentry_ok

    def has_demand(self):
        # Preserve the original scan/notifications and keep paid-host safety fast.
        return self.lifecycle.has_hosts() or self.original.has_demand()

    def tick(self):
        nodes = self.lifecycle.sweep()
        fallback = self.admission.tick()
        # Modal gets the first opportunity; the original Vast lookahead still
        # runs in this same loop and retains all of its rollout/start-rule gates.
        result = self.original.tick()
        return {**result, 'modal': {'nodes': nodes, 'admission': fallback}}

    def safety_sweep(self):
        nodes = self.lifecycle.sweep()
        return {**self.original.safety_sweep(), 'modal': {'nodes': nodes}}
