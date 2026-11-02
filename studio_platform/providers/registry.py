"""Capacity supplier identities and separate execution classifications.

A backend is the name a run spec carries (dstack backend or a dial-out lane);
its provider is the name written on intents, nodes, workers and bills. Every
set below derives from its own table: an execution engine is never implicitly
admitted to rental/financial accounting. Pure data: no I/O or credentials.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Backend:
    id: str
    provider: str


VASTAI = Backend("vastai", "vast")
RUNPOD = Backend("runpod", "runpod")
LIUM = Backend("lium", "lium")
CLOUDRIFT = Backend("cloudrift", "cloudrift")
# Dial-out workers retain distinct allocator IDs and their original ledger owner.
RUNPOD_SERVERLESS = Backend("runpod-serverless", "runpod")
MODAL_DIALOUT = Backend("modal-dialout", "modal-dialout")
BACKENDS = (
    VASTAI,
    RUNPOD,
    LIUM,
    CLOUDRIFT,
    # RunPod Serverless jobs (dial-out only) bill to the same RunPod account.
    RUNPOD_SERVERLESS,
    # Modal sandboxes running the dial-out worker (#859); not the older Modal
    # lane, which keeps provider "modal" on the modal-v1 capacity backend.
    MODAL_DIALOUT,
)

# Backend id -> ledger provider.
BACKEND_PROVIDERS = {backend.id: backend.provider for backend in BACKENDS}
# Backends the dstack-v1 capacity ledger can rent through.
DSTACK_BACKENDS = frozenset(BACKEND_PROVIDERS)
# Ledger providers of dstack-v1 rentals (continuing, no hard deadline).
DSTACK_V1_PROVIDERS = frozenset(BACKEND_PROVIDERS.values())
# Historical Modal lane retained for original receipts and settlement readers.
LEGACY_MODAL_PROVIDER = "modal"
# (capacity backend, provider) pairs a managed worker admission may carry.
ADMISSION_PAIRS = frozenset(
    {("dstack-v1", provider) for provider in DSTACK_V1_PROVIDERS}
    | {("modal-v1", LEGACY_MODAL_PROVIDER)}
)


@dataclass(frozen=True)
class ExecutionBackend:
    id: str
    kind: str  # native, local adapter, or mock; local can still spend upstream.
    provider: str = ""  # Native engines bind their supplier when registered.
    service_switch: str = ""
    per_job_supplier: bool = False
    primary: bool = False
    standalone: bool = False


MOCK = ExecutionBackend("mock", "mock", "mock", primary=True, standalone=True)
COMFY_WORKER = ExecutionBackend("comfy-worker", "native", primary=True, standalone=True)
WANGP_WORKER = ExecutionBackend("wangp-worker", "native", primary=True)
CPU_RENDER = ExecutionBackend("cpu-render", "local", "local-cpu", "render_enabled")
RUNPOD_ENHANCE = ExecutionBackend(
    "runpod-enhance", "local", "runpod", "enhance_enabled", per_job_supplier=True
)
EVERYFRAME_RENDER = ExecutionBackend(
    "everyframe-render",
    "local",
    "everyframe",
    "everyframe_enabled",
    per_job_supplier=True,
)
EXECUTION_BACKENDS = (
    MOCK,
    COMFY_WORKER,
    WANGP_WORKER,
    CPU_RENDER,
    RUNPOD_ENHANCE,
    EVERYFRAME_RENDER,
)
WORKER_BACKENDS = frozenset(backend.id for backend in EXECUTION_BACKENDS)
REAL_GPU_BACKENDS = frozenset(
    backend.id for backend in EXECUTION_BACKENDS if backend.kind == "native"
)
# No physical-device identity or demand polling; mock still requires a device.
LOCAL_BACKENDS = frozenset(
    backend.id for backend in EXECUTION_BACKENDS if backend.kind == "local"
)
# No remote engine endpoint or idle probe. Mock is included only in this set.
NO_ENDPOINT_BACKENDS = WORKER_BACKENDS - REAL_GPU_BACKENDS
SERVICE_SWITCHES = {
    backend.id: backend.service_switch
    for backend in EXECUTION_BACKENDS
    if backend.service_switch
}
# Native rental spend is already accounted by its capacity ledger; do not
# count the job again. Hosted adapters instead incur their own per-job spend.
INTERNAL_BACKENDS = frozenset(
    backend.id for backend in EXECUTION_BACKENDS if not backend.per_job_supplier
)
PRIMARY_EXECUTION_BACKENDS = frozenset(
    {"disabled"} | {backend.id for backend in EXECUTION_BACKENDS if backend.primary}
)
# Preserve the standalone CLI's smaller, ordered choice list (no WangP).
STANDALONE_EXECUTION_BACKENDS = (
    "disabled",
    *(backend.id for backend in EXECUTION_BACKENDS if backend.standalone),
)
