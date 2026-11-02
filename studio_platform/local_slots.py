"""Existing CPU/hosted slot ownership; still in the controller process (#999)."""

from contextlib import contextmanager
import os
import stat
import threading

from .repository import Conflict
from .providers.registry import (
    CPU_RENDER,
    EVERYFRAME_RENDER,
    LOCAL_BACKENDS,
    RUNPOD_ENHANCE,
)
from .slot_turns import run_selected_slot


def _local_spec(backend, worker, pool, recipes, model, configuration):
    from .control import WorkerSpec

    return WorkerSpec(
        worker,
        pool,
        backend.provider,
        worker,
        (),
        recipes,
        model,
        configuration,
        backend.id,
    )


def _render_specs():
    from .render_plans import POOL, MODEL, RECIPE, CONFIGURATIONS

    for configuration in sorted(set(CONFIGURATIONS.values())):
        yield _local_spec(
            CPU_RENDER, "dbos-" + configuration, POOL, (RECIPE,), MODEL, configuration
        )


def _enhance_specs():
    from . import enhance_plans as plan

    for index in range(plan.SLOTS):
        # The first keeps its original name, so its row carries over.
        worker = "dbos-" + plan.CONFIGURATION + (f"-{index}" if index else "")
        yield _local_spec(
            RUNPOD_ENHANCE,
            worker,
            plan.POOL,
            (plan.RECIPE,),
            plan.MODEL,
            plan.CONFIGURATION,
        )


def _everyframe_specs():
    from .providers import hosted_models as plan

    for model in plan.MODELS.values():
        for index in range(model.concurrency or plan.SLOTS_PER_MODEL):
            yield _local_spec(
                EVERYFRAME_RENDER,
                f"dbos-{model.configuration_id}-{index}",
                plan.POOL,
                plan.RECIPES,
                model.id,
                model.configuration_id,
            )


# Ordered local capabilities only; native capacity still arrives through ensure.
# Render is required; optional families retain their own failure event and switch.
LOCAL_SLOT_FAMILIES = (
    (CPU_RENDER, _render_specs, None),
    (RUNPOD_ENHANCE, _enhance_specs, "dbos_enhance_slot_unavailable"),
    (EVERYFRAME_RENDER, _everyframe_specs, "dbos_everyframe_slot_unavailable"),
)


class LocalSlotOwner:
    """Own local contexts and finite operations in a supplied original registry.

    No DBOS executor, job scan, new fence or service is created by this owner.
    The combined facade retains global ordering and the sole supervisor fence.
    """

    def __init__(self, contexts):
        self.contexts = contexts
        self.operation_lock = threading.Lock()

    def ensure(self, intent_id, slot):
        self.contexts.open(intent_id, slot)

    def run_selected_slot(self, slot, job_id):
        return run_selected_slot(slot, job_id, local_owner=self)

    def heartbeat_slot(self, slot):
        worker = slot.runner.control.get(slot.spec.worker_id)
        if worker["expires_at"] > self.contexts.repo.clock() and worker[
            "state"
        ] not in {"unknown", "retired"}:
            slot.runner.control.heartbeat(
                slot.spec.worker_id, worker["fence"], lease_seconds=3600
            )

    def heartbeat(self):
        with self.contexts._lock:
            for slot in self.contexts._slots.values():
                if slot.spec.backend in LOCAL_BACKENDS:
                    self.heartbeat_slot(slot)

    def add_slots(self):
        """Register enabled local families without changing their failure boundaries."""
        for backend, specs, unavailable in LOCAL_SLOT_FAMILIES:
            enabled = (
                getattr(self.contexts.settings, backend.service_switch)
                if unavailable is None
                else getattr(self.contexts.settings, backend.service_switch, False)
            )
            if not enabled:
                continue
            from .fleet import SlotConfig

            for spec in specs():
                try:
                    self.ensure(spec.worker_id, SlotConfig(spec, enabled=True))
                except Exception as exc:
                    if unavailable is None:
                        raise
                    # Keep prior registrations and continue the next family.
                    print(unavailable, "error_type=" + type(exc).__name__, flush=True)
                    break
            else:
                self.heartbeat()

    @contextmanager
    def cpu_fence(self):
        """Fence all CPU configurations, including a surviving encoder child."""
        if os.name != "posix":
            yield True, None
            return
        import fcntl

        descriptor = os.open(
            self.contexts.work_dir / "cpu-render-operation.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if not (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077
            ):
                raise Conflict("dbos_cpu_operation_fence_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False, None
                return
            yield True, descriptor
        finally:
            # Child OFD ownership survives closing this parent FD after a crash.
            os.close(descriptor)
