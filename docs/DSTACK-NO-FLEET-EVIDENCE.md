# Pinned no-fleet settlement evidence (#659)

The native dstack0.22.5 scheduler rejects an original submission before provisioning
when `_apply_no_fleet_selection` emits exactly:
`No matching fleet found. Possible reasons: https://dstack.ai/docs/guides/troubleshooting/#no-fleets`.

Source: [pinned scheduler](https://github.com/dstackai/dstack/blob/e59a6caa6d3d76f8b8c3430b7e8eeb7271138065/src/dstack/_internal/server/background/pipeline_tasks/jobs_submitted.py).
This is a separate case from a provider create failure or an empty current inventory.

The adapter first validates the exact owned run/spec. The new proof requires one
failed job, exactly one original failed submission, matching latest/history,
`job_failed` / `failed_to_start_due_to_no_capacity`, explicit null provisioning
and runtime fields, no recorded provider identity/bootstrap/runtime history, and
the exact native message. Its source is `native_dstack_0225_no_fleet_v1` with zero
provider attempts and the original run/submission IDs. Hash validation and all
existing ledger locks/transactions remain in use. Different messages, retries,
omitted fields, supplier errors, returned contracts and unknown outcomes refuse.

A qualified observation settles ONLY that original instance reservation at zero,
retains the original amount/IDs, marks the original intent destroyed and records
one no-allocation receipt. Repeated observations do not settle again. Transaction
failure rolls back observation and settlement together. It does not edit jobs,
raise caps, change historical task attribution or issue a replacement/apply call.
Demand-start may then proceed through its existing fresh offer and budget gates.

## Read-only staging evidence

SSM5141b2ef-26df-44af-861e-7fbdf8db1a3a and33e6ce0f-2879-432f-9830-43859f95d1f7
completed successfully on2026-10-27. The first wider diagnostic command963bf7d7
completed but SSM truncated stdout; the bounded read replaces that unusable view.
No operation was replayed or settled.

- Node78017092-adcc-40d9-a383-8365a39665f3: run428825a2-189b-4ec3-8b99-6c9f59f46acb,
  submission0dad635d-4f39-474b-a808-c6b47706d413.
- Node8f4d805d-da22-4717-8829-b18927ba21bd: run3918ea2a-2f49-4385-8f4e-c5f06db9211d,
  submission8e5d4642-775c-4e38-bcf0-dbf3c41d23ad.
- Both satisfy the complete singleton/null-runtime shape and exact no-fleet
  message; neither has a typed provider-rejection proof under the old adapter.
- Original task staging-editor-footage193-20261021 exposure19,616,672 microUSD:
  five older invoice-pending holds of2,770,834 and three holds of1,920,834. All
  actual costs remain NULL. Two qualifying releases would remove3,841,668 of
  exposure; the other six obligations remain unchanged, not rewritten as zero.
- Native staging fleet2a9dd202-4972-474d-9aa0-decdd640a1a3 is active, min0/max1,
  zero instances. The latest FL journal separately reports offer_unavailable.

## Adoption after the single Tier2 review

Merge only on exact-head green CI. Let the normal staging deployment finish;
verify the current source/tuple. Re-read each original owned run and original
ledger before invoking its normal observe path once, if still unresolved. Do not
copy a stale proof into SQL. Recover an unknown result by reading the same
reservation/receipt; never replay apply/rental. Report the original receipt IDs
and before/after commitments on #659. Keep queued jobs f028b9d4 and881a2328 intact.
Claude owns the acceptance observation. No production promotion by Codex.
