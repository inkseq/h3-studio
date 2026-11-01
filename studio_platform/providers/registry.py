"""The one table of capacity backends and ledger providers (#931 step 1).

A backend is the name a run spec carries (dstack backend or a dial-out lane);
its provider is the name written on intents, nodes, workers and bills. Every
set of provider names elsewhere derives from this table, so adding a lane is
one row here plus its adapter. Pure data: no imports with I/O, no credentials.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Backend:
    id: str
    provider: str


BACKENDS = (
    Backend("vastai", "vast"),
    Backend("runpod", "runpod"),
    Backend("lium", "lium"),
    Backend("cloudrift", "cloudrift"),
    # RunPod Serverless jobs (dial-out only) bill to the same RunPod account.
    Backend("runpod-serverless", "runpod"),
    # Modal sandboxes running the dial-out worker (#859); not the older Modal
    # lane, which keeps provider "modal" on the modal-v1 capacity backend.
    Backend("modal-dialout", "modal-dialout"),
)

# Backend id -> ledger provider.
BACKEND_PROVIDERS = {backend.id: backend.provider for backend in BACKENDS}
# Backends the dstack-v1 capacity ledger can rent through.
DSTACK_BACKENDS = frozenset(BACKEND_PROVIDERS)
# Ledger providers of dstack-v1 rentals (continuing, no hard deadline).
DSTACK_V1_PROVIDERS = frozenset(BACKEND_PROVIDERS.values())
# The older Modal lane (modal-v1), retired by #931 step 7.
LEGACY_MODAL_PROVIDER = "modal"
# (capacity backend, provider) pairs a managed worker admission may carry.
ADMISSION_PAIRS = frozenset(
    {("dstack-v1", provider) for provider in DSTACK_V1_PROVIDERS}
    | {("modal-v1", LEGACY_MODAL_PROVIDER)}
)
