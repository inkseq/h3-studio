# Bounded lookup indexes (#563)

Approved Small: [6404550719](https://github.com/inkseq/h3-studio/issues/563#issuecomment-6404550719).
Revision `20261026_0008` follows `20261022_0007` and adds only four non-unique
B-tree indexes. No business rows, table family, authentication cache, charging,
provider settings or application-startup DDL changes.

| Table | Index | Ordered columns |
| --- | --- | --- |
| platform_outbox | ix_outbox_event_aggregate | event_type, aggregate_id |
| platform_outbox | ix_outbox_aggregate | aggregate_id |
| platform_budget_reservations | ix_budget_reference | reference_type, reference_id |
| platform_scaler_receipts | ix_scaler_intent_operation | intent_id, operation |

The inspector checks definitions before creating anything. An equivalent index
with another name is reused. Partial/expression/unique/INCLUDE/non-B-tree indexes
do not substitute for the approved shape. A conflicting canonical name refuses.
Schema fingerprints retain actual index names. Updated 007 readers tolerate
only the four additive shapes; 008 schema verification requires all four.
Original payment/admission and business-backup readers understand 008 without
importing the migration writer. Frozen older metadata remains unchanged.

## Reader floor, then separately gated adoption

This source change is NOT permission to adopt staging or production. Claude owns
staging acceptance and production promotion. Keep charging/Checkout OFF.

1. Deploy and qualify the readers on unchanged 007 first. Record exact current
   and retained rollback images and their source/build bindings. **Both images
   must contain this reader bridge before DDL.** The current base `0b3ec31` does
   not; do not roll back to it after 008 adoption. Wait for a normal subsequent
   release with these readers retained, not an empty/fabricated promotion.
2. Using the existing release protocol, hold host `release.lock`; prove target
   identity, zero running/nonterminal jobs and no leases, pause writers using
   the approved protocol, keep accepted work intact, and capture native 007
   backup plus original financial/data fingerprints. Rehearse on an isolated
   native copy with both actual images. Never count SQLite unit tests as this
   retained-image qualification.
3. Read-only `python -m studio_platform.schema_baseline check` must match007.
   Record its exact `schema_fingerprint`, actual `pg_indexes`, counts and the
   original operation receipt. Within the same guarded maintenance operation,
   invoke `python -m studio_platform.schema_baseline adopt-lookup-indexes
   --expected-fingerprint <recorded fingerprint>` with existing private app
   configuration; never put a database URL or secret on the command line.
4. The writer takes the account/migration locks and fixed-order SHARE locks on
   the three indexed tables. Each lock wait is capped at 1 s and each SQL
   statement at 3 s. These are per-statement bounds, not a whole-operation SLA.
   Ordinary CREATE INDEX is transactional: any timeout/error rolls back all new
   indexes and the revision marker together. Readers can continue while these
   locks are held; writes to those tables wait. Do not run during active jobs.
   The fingerprint, charging-OFF and quiescence checks precede migration. CLI
   also requires Stripe mode OFF. Generic/unverified Alembic upgrade refuses.
5. Read back exact008 schema/index definitions and unchanged business/financial
   fingerprints, validate both retained/current readers, resume through the
   original release protocol, and post the original operation receipt. On a
   timeout, reconcile terminal command/transaction state and the catalog before
   any retry. On an unknown outcome, do not replay. Exact008 readback can report
   already_adopted using its fresh fingerprint without creating indexes again.
   Keep schema008 when rolling application back to the qualified reader image;
   automatic index drops/revision downgrade are refused.

If real table growth cannot finish within the 3 s statement bound, stop and
review a separate concurrent-index protocol; do not silently increase timeouts
or run CREATE INDEX CONCURRENTLY outside the audited transaction.

## Qualification (local evidence, not production capacity claims)

Production read-only SSM `810fcd9b-e032-4bf4-afc8-a96c27765f6f`, SUCCESS/0:
outbox18,031; reservations740; receipts25,118. None of the four target indexes
exists. An earlier read-only attempt `f8ce77bb-78fb-4904-a67c-b4b475c63dd9`
failed its deployment-seal field lookup before database access. No live DDL.

Disposable local PostgreSQL17, synthetic tables with the same predicate column
types and competing primary/account-leading indexes, 200,000/10,000/260,000 rows:
all four result sets identical, normal planner selects target indexes. Warm
median client times respectively 8.88→0.53, 8.84→0.49, 1.02→0.43 and 10.27→0.48 ms;
index creation134/87/9/130 ms. These are illustrative local measurements, not
production latency promises or a true cold-cache benchmark (no OS cache eviction).
Tests exercise the full native schema, existing alias reuse, exact drift refusal,
charging/quiescence checks, fingerprint mismatch, unverified writer refusal,
caller abort, lock timeout, mid-migration statement timeout, clean retry and
business-backup/restore row preservation without the writer import. The native
test suite is included in the explicit PostgreSQL CI manifest.
