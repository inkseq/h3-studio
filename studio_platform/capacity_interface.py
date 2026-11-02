"""Native-worker capacity contracts, separate from per-job InferenceBackend.

All current rental lanes use one ledger-backed CapacityService. Transport calls
are the existing compatibility payloads, not a second reservation authority.
A provider being stopped/absent or ready says nothing about its outstanding bill.
The existing ledger remains responsible for reservations, ownership and settlement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .dstack_capacity import CapacityRequest


class CapacityTransport(Protocol):
    """Provider transport underneath the original shared ledger coordinator.

    Existing dstack-shaped specs and observations remain wire compatibility
    contracts. apply is the sole create attempt; get/stop address that original.
    Failed or incomplete inventory must raise, never return empty/zero capacity.
    Native host-evidence capabilities remain explicit at their existing callers;
    this surface does not make their absence permission to bypass ownership checks.
    """

    project: str

    def configured_backends(self): ...
    def plan(self, spec): ...
    def apply(self, spec): ...
    def get(self, *, run_name=None, run_id=None): ...
    def stop(self, run_name): ...
    def provider_inventory(self, backend, *, clock=None): ...


class CapacityService(Protocol):
    """One plan/start/observe/stop/reconcile boundary for native worker hosts.

    plan is advisory. start commits the original ledger before provider mutation;
    replay reconciles the same binding. stop uses the existing ledger ownership,
    jobs, fences and durable retry gate. Observations and reconcile may record
    evidence and perform already-authorized readiness work; they are not generic
    permission to launch, reselect a provider, refund or release an obligation.

    observe_terminal is deliberately distinct: it reads only the original
    terminal allocation without readiness/bootstrap or renewed funding.
    """

    client: CapacityTransport

    def plan(self, request: CapacityRequest, ssh_key_pub): ...
    def start(self, request: CapacityRequest, ssh_key_pub): ...
    def observe(self, intent_id, *, _on_observed=None): ...
    def stop(self, intent_id): ...
    def reconcile(self, intent_id): ...
    def observe_terminal(self, intent_id): ...
