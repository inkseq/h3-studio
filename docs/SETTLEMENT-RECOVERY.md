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
