"""Shared existing application services; no HTTP app or workflow executor."""

from dataclasses import dataclass

from .assets import AssetService
from .auth import Auth
from .execution_policy import ExecutionPolicies
from .generation_admission import GenerationAdmission
from .generation_services import GenerationAccess, GenerationPlanning, GenerationRead
from .repository import Repository
from .render_sources import RenderSources
from .storage import LocalObjectStore, S3ObjectStore
from .storage_config import S3StorageConfig, load_configured_storage_credentials

# Preserve additive metadata registration before the existing create_schema call.
from . import operator_capacity, capacity_market, dstack_workspace  # noqa: F401


@dataclass(repr=False)
class CoreServices:
    settings: object
    repository: object
    storage: object
    auth: object
    assets: object
    policies: object
    access: object
    read: object
    planning: object
    admission: object
    render_sources: object


def create_core(settings, *, repository=None, storage=None, capabilities_provider=None):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    repo = repository or Repository(settings.database_url)
    if settings.generation_queue == "dbos":
        from .dbos_transport import configure_producer

        configure_producer(repo)
    repo.create_schema()
    if storage is None:
        if settings.storage_provider == "local":
            storage = LocalObjectStore(settings.data_dir / "objects")
        elif settings.storage_provider == "r2":
            import os

            cfg = S3StorageConfig(
                "r2",
                settings.storage_endpoint,
                settings.storage_region,
                settings.storage_bucket,
                "cloudflare-r2",
                settings.storage_profile,
                enabled=True,
            )
            credentials = load_configured_storage_credentials(
                cfg,
                credentials_file=settings.storage_credential_file,
                registry_root=os.environ.get("AI_REGISTRY_ROOT"),
            )
            storage = S3ObjectStore(cfg, credentials)
        else:
            raise ValueError(
                "此远端存储需要显式注入已审阅的store与凭据入口；不使用默认AWS账户"
            )
    from .staging_qa import enabled as staging_qa_enabled

    auth = Auth(
        repo.engine,
        tenant=settings.tenant_id,
        mode=settings.auth_mode,
        session_seconds=settings.session_seconds,
        staging_qa=staging_qa_enabled(settings),
    )
    asset_service = AssetService(
        repo.engine,
        storage,
        settings.data_dir,
        tenant=settings.tenant_id,
        max_bytes=settings.max_upload_bytes,
    )
    execution_policies = ExecutionPolicies(settings, repo)
    generation_access = GenerationAccess(repo, settings.tenant_id)
    generation_read = GenerationRead(repo, generation_access)
    generation_planning = GenerationPlanning(
        repo=repo,
        assets=asset_service,
        settings=settings,
        policies=execution_policies,
        access=generation_access,
        read=generation_read,
    )
    generation_admission = GenerationAdmission(
        repo=repo,
        assets=asset_service,
        settings=settings,
        policies=execution_policies,
        access=generation_access,
        planning=generation_planning,
        capabilities_provider=capabilities_provider,
    )

    return CoreServices(
        settings,
        repo,
        storage,
        auth,
        asset_service,
        execution_policies,
        generation_access,
        generation_read,
        generation_planning,
        generation_admission,
        RenderSources(repo, settings, asset_service, generation_access),
    )


def create_quick_chat(
    core, *, hooks=None, assistant=None, assistant_enabled=False, title_generator=None
):
    from .quick_chat import QuickChatHooks, QuickChatService
    from .quick_chat_assistant import QuickChatAssistant

    repo, access, read, admission = (
        core.repository,
        core.access,
        core.read,
        core.admission,
    )

    def quick_cancel(principal, job_id):
        job = access.job(principal, job_id, "jobs:write")
        return read.public_job(
            repo.request_cancel(access.scope(principal, job["project_id"]), job_id)
        )

    hooks = hooks or QuickChatHooks(
        preflight=admission.preflight,
        create_planned=admission.create_planned,
        enqueue=lambda p, j, confirmation_required=False, spending_settings_version=None: (
            admission.enqueue(
                p,
                j,
                business=True,
                confirmation_required=confirmation_required,
                spending_settings_version=spending_settings_version,
            )
        ),
        public_job=read.job,
        cancel=quick_cancel,
        refresh_planned=admission.refresh_planned,
    )
    return QuickChatService(
        repo,
        core.assets,
        core.settings,
        core.storage,
        hooks=hooks,
        assistant=assistant or QuickChatAssistant(core.assets, core.storage),
        assistant_enabled=assistant_enabled,
        title_generator=title_generator,
    )


@dataclass(repr=False)
class AccountServices:
    workos: object
    sessions: object
    deletion: object


def create_accounts(core, quick_chat):
    from .workos_auth import WorkOSAuth
    from .account_sessions import AccountSessions
    from .account_deletion import AccountDeletion

    repo, auth, settings = core.repository, core.auth, core.settings
    workos = WorkOSAuth(repo, auth, settings)
    sessions = AccountSessions(repo, auth, settings, workos)
    auth.session_checker = sessions.check
    deletion = AccountDeletion(repo, auth, settings, workos, quick_chat)
    return AccountServices(workos, sessions, deletion)


@dataclass(repr=False)
class ControllerServices:
    film: object
    auth: object
    accounts: AccountServices


def create_controller_services(settings, *, repository):
    from .batches import BatchAdmission
    from .film_runs import FilmExecution

    core = create_core(settings, repository=repository)
    # The existing batch schema and QuickChat cleanup service remain shared.
    chat = create_quick_chat(core)
    batches = BatchAdmission(core)
    accounts = create_accounts(core, chat)
    film = FilmExecution(
        repo=core.repository,
        settings=core.settings,
        access=core.access,
        submit_batch=batches.submit,
        evaluate_policy=core.policies.evaluate,
        resolve_source=core.render_sources.resolve,
    )
    return ControllerServices(film, core.auth, accounts)
