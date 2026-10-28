# Wait-target planning kernel (CR76/77)

Refs #745 / #746 / #688 / #692 G2. This is an **advisory CPU simulation**,
not the rollout, a provider call, or evidence that the latency target is met.
There is no production or controller invocation in this slice.

`studio_platform.capacity_wait_plan.plan` accepts one finite global snapshot:

- Original queued shots with tenant-qualified owner identity, compatibility
  group, elapsed queue age, and measured delivery durations per backend.
  A group must encode pool, mode, configuration and immutable manifest identity.
  Missing duration is unknown, never Standard40s by default.
- Every outstanding global physical host/intent exactly once, including other
  pools, stopped-but-unconfirmed allocations, starting and stopping obligations.
  Ready slots start at zero; busy slots include remaining original work.
  Starting hosts include qualified remaining startup, so the same incoming
  capacity can cover multiple inputs without another proposed create.
- Audited targets and adopted hourly/machine ceilings. The owner-authorized
  ceiling is20USD/h; a currently adopted lower ceiling stays lower. Neither
  this proposal nor the20USD/h policy enlarges CR73's cumulative50USD task cap.
- A Modal option with an explicit measured-qualification receipt and full
  resource hourly ceiling, not GPU-only price. The receipt must be verified
  by the eventual adapter; a nonempty string alone is not external proof.
- `last_delivery` in seconds since the last completion in each continuously
  queued owner episode. Offline time between episodes is excluded.
  If a running original may be that episode's first completion, provide
  `episode_seconds`; queued siblings alone cannot reconstruct its start time.
  Heavy Sharp reference-video work supplies the same owner-qualified
  `serial_key` on queued and running work, preserving #758's one-active guard.

The kernel preserves per-owner FIFO and interleaves first pending shots before
later shots. It simulates ready/busy/starting slots before proposing new ones,
prefers dedicated slots when readiness ties, and permits parallel owner work
only where no original serial constraint applies. It evaluates individual first
completion waits and maximum subsequent gaps, rather than an average. The
180s trigger is a planning buffer ahead of the300s first-video target.

The smallest simulated count meeting the trigger/interval target is returned.
If the global cap binds, the result retains the projected late completions.
If even extra cold hosts cannot improve the worst overdue outcome, it proposes
no pointless start. Unknown allocation, price, or startup timing blocks new
proposals; missing per-job timings remain explicitly unplaced. No empty-queue
floor is proposed. Unknown outcomes are never modeled as removed resources.

Output includes `advisory_only`, before/projected per-owner metrics, a closed
reason, committed hourly exposure, and `additional_modal`. It is not a creator
response: internal identities must not be returned across account scopes.

## Execution gate (remaining work)

The later adapter must validate the source measurements, scoped identities and
complete fresh physical ledger; reject incomplete snapshots; and run under the
existing serialized capacity admission. It must recheck the current policy,
original reservation, task/account/daily/global limits and provider uncertainty
before each single create. No alternate queue or automatic replay is allowed.
The current fixed staging max2 is unchanged until that reviewed integration and
explicit adoption. Dedicated sustained-load lookahead and its rollout switch
remain unchanged. Real #688 image/Volume qualification and GPU acceptance are
still prerequisites; the assumed120s startup/40s Standard/125s Sharp figures
are not production qualification receipts. Metrics collection is a later
read-only slice, not manufactured by this prediction.

The implementation comparison used the upstream [Cluster Autoscaler FAQ,
scale-up internals](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md#how-does-scale-up-work):
simulate placement and incoming capacity separately from allocation. Its
historical `estimator/binpacking_estimator.go` path is no longer present on the
current branch, so this slice does not claim to have copied that implementation.
No Kubernetes dependency or service is introduced.
