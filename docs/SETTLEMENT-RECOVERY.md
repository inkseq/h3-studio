# Terminal holds and invoice reconciliation

Terminal execution, physical removal and supplier billing are separate facts.
Native output collection does not supply an invoice: `Worker._cost` returns
`None` without a trusted resolver. The old queue deliberately retained the
reservation, and the dstack terminal observation stopped at `destroying`.
Neither a dstack terminal status nor its modeled `cost` is a supplier invoice.

The queue now releases the admission hold after terminal completion and positive
upstream-stop evidence for **every** attempt. Original reservation amount,
reference, timestamps and unknown actual cost remain as `invoice_pending`.
Settings daily usage still conservatively includes that amount. The staging
Australia/Sydney USD20 guard also retains unknown invoice exposure, including
its existing runtime/price allowance. No caps, idle limits or deadlines change
in this module. A later authoritative actual uses the original settlement path
and adds spend once without subtracting the released hold twice.

For an exact owned terminal dstack run with a previously known provider ID,
the existing provider client may obtain a complete fresh same-account inventory.
Only positive absence of that ID closes the original intent as `destroyed`.
An incomplete/failed/stale inventory, missing run or identity-less uncertain
creation is not absence proof. Original typed no-allocation evidence remains
the separate authority for a confirmed zero charge. Historical worker/attempt
obligations keep their admission hold until positively closed.

## Operator recovery

`studio_platform.settlement_recovery` is a private operator interface, not HTTP
authority. It never rents, stops, retries, deletes or calls a provider. Its
inspection and dry run acquire original row locks but change no data. It uses
the configured database credential loader; it never creates a schema.

1. Independently verify the original provider/account evidence and invoice
   totals. Preserve the evidence file and its SHA256. For issue254, explicitly
   exclude live provider instance `56330848`. Never assign its invoices to
   another instance. Unknown actual amounts must be `null`, not zero.
2. Call `inspect_reference(repo, tenant, "job"|"instance", original_id)` to get
   the exact original reference/attempt/reservation snapshot hash. No prompts,
   media or secret fields are returned.
3. Prepare a private JSON request with exactly `version:1`, UUID `operation_id`,
   `tenant_id`, `actor`, GitHub issue `source`, `evidence_sha256`,
   `excluded_provider_instance_ids`, and `entries`. Each entry contains
   `reference_type`, `reference_id`, `expected_sha256`,
   `actual_cost_microusd` (integer or null), and `provider_absence`.
   Job absence is null. Instance absence contains exactly `provider`,
   `provider_instance_id`, `complete:true`, the complete `instance_ids`,
   `observed_at` (at most120seconds old), and `evidence_sha256`. The trusted
   operator must bind this proof to the correct existing provider account;
   arbitrary caller-supplied JSON is not evidence.
4. Run `python -m studio_platform.settlement_recovery --request PRIVATE.json`.
   Review all references, snapshot hashes and before amounts. Apply the exact
   approved request once with `--apply`. All entries and the audit event commit
   atomically. Unknown command outcomes use the **same operation and body**;
   original receipt replay precedes freshness validation. A changed body under
   the same UUID is refused. Never invent a new UUID to hide an unknown result.
5. Read original budgets/reservations and `budget.recovery_completed` audit.
   Pending invoice is not a confirmed zero-cost settlement. A later invoice is
   a distinct reviewed operation, or the original `settle_completed_job` /
   `settle_instance_cost` method with authoritative actuals.

Do not introduce `invoice_pending` into an old runtime whose ledger readers do
not understand it. Deploy the reviewed compatible source as one bounded batch.
For an urgent incident on an older runtime, only existing actual-cost settlement
methods may be used with independently established actual amounts; this module
does not authorize hot-loading partial state semantics.

## Reviewed historical capacity (version2, issue264)

Version1 and its completed audit receipts remain unchanged. Version2 is an
explicit private recovery request for reviewed historical instances only; it
does not change public APIs, the automatic observer, spend caps or settlement.
Every entry must be `instance` with `actual_cost_microusd:null`.

The version1 top-level fields are retained, with `version:2` and one additional
`reviewed_provider_accounts` map from original provider (`vast`, `lium`,
`targon`) to the SHA256 of the independently verified original account binding.
Exclude both protected owner IDs `56330848` and `56336890` in the reviewed
request. Use `inspect_reference(..., version=2)` for the stronger CAS snapshot.

Each `provider_absence` has exactly `kind:"reviewed_account_empty"`, `provider`,
`provider_instance_id` (including original null), `account_binding_sha256`,
`complete:true`, `instance_ids:[]`, `observed_at`, `evidence_sha256`, and
`owned_run`. Bind the account SHA to the reviewed top-level map. A successful
complete account read must be at most120seconds old **after all locks**; do
the provider read before the database operation, never under its transaction.

For dstack, `owned_run` contains exactly `run_id`, `run_name`, `binding_sha256`,
`original_binding_matched:true`, terminal `status` (`failed`, `terminated`, or
`aborted`), `observed_at`, and `evidence_sha256`. Independently verify the exact
persisted run ID using dstack's existing `_owns` comparison and preserve that
read receipt. `binding_sha256` hashes the entire original persisted dstack
binding via `request_hash`; identity, configuration and observation freshness
must match. A failed run alone, or an empty account in a different provider or
identity, is insufficient. This rule proves ended capacity, **never no charge**.

For legacy Lium/Targon, `owned_run:null` additionally requires the original
valid manual-review receipt and stopped/manually-reviewed node. Missing node
tenant metadata is accepted only for this reviewed legacy case when **all**
original reservation accounts match the requested tenant. No tenant or review
history is backfilled. Historical attempts must be positively stopped and
linked jobs terminal; original start-command siblings must all be destroyed or
included in the same safe batch. A different tenant or live sibling refuses
the whole transaction.

Version2 atomically marks the original intent destroyed (using the legal
draining→destroying transition where needed), its node stopped, and active
original start commands completed. The payload retains its original fields
plus an audit-operation fence. It releases only the exact original reserved
amount into `invoice_pending`, retaining estimates and null actuals. The CAS
also covers original commands, sibling nodes/intents and account counters;
insufficient counters, changed snapshots, protected IDs or active obligations
refuse the batch. Original version1 recovery events are not replayed.

Budget projections expose confirmed generation invoices, confirmed rental
invoices, and pending counts/original estimates separately for each original
account. Confirmed generation zero with pending invoices does not mean free
generation. Do not invent per-video actuals from GPU rental totals, add the
estimate to a confirmed invoice, or add overlapping shared/owner/project layers.
