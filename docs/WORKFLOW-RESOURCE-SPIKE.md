# Held workflow resource gate (#295)

This is a staging-only experiment, not an application queue migration. No
production service imports the script or either SDK. The normal application and
dispatch locks remain unchanged. No GPU, provider, credential, budget, business
job or artifact API is used. Source preparation does not authorize execution.

## Pinned source and execution boundary

Separate Python 3.12 environments install `requirements.workflow-dbos.lock.txt`
and `requirements.workflow-procrastinate.lock.txt` with `--require-hashes`.
There is no change to the production application dependency lock or image.
The roots are the normal staging database and a private, canonical directory
`<private>/workflow-spike295/<run-id>`; the existing protected database URL-file
reader is reused. Never print the DSN, install an extension, add credentials,
create a database/role, or run either candidate in production.

The public APIs were checked against installed DBOS **3.3.0** and Procrastinate
**3.10.0** source. Primary references:
[DBOS configuration](https://docs.dbos.dev/python/reference/configuration),
[DBOS contexts](https://docs.dbos.dev/python/reference/contexts), and
[Procrastinate reference](https://procrastinate.readthedocs.io/en/stable/reference.html).
In this pinned DBOS release, a queue is registered with `DBOS.register_queue`
or `DBOSClient.register_queue`; direct `Queue(...)` construction is rejected.
The synchronous registration runs outside the asyncio loop. Producers/status
checks use `DBOSClient`, never another DBOS executor.

Root must first confirm the accepted Alembic adoption, stable same-host release
lane, and normal staging host/container/image/database bindings. All commands
require `INKSEQ_DEPLOYMENT_TARGET=staging` plus `--authorized-staging-spike`.
Those are scope guards, not a substitute for the original protected operational
authorization. Linux `flock` on one private `executor.lock` permits only one
experimental initializer/executor/recovery command across both engines.
Submit/status may run while that original executor is active.

## Temporary schemas and backup contract

Only two fixed, disposable, stub-only schemas may be initialized:

| Engine | Schema | Application version / executor |
| --- | --- | --- |
| DBOS | `inkseq_spike295_dbos` | `inkseq-film-spike-v1` / `inkseq-film-spike-staging` |
| Procrastinate | `inkseq_spike295_procrastinate` | Same stub revision / named staging worker |

Record the before/after table/constraint/function/trigger catalog for those exact
schemas. Initialization writes an exclusive original request file first and
refuses an already-existing schema. A partial or unknown initialization must be
inspected, not blindly replayed. Workers do not run DBOS migrations; Procrastinate
schema initialization is a separate explicit command. The pinned Procrastinate
schema checks the already-installed `plpgsql` extension in `pg_catalog`; this
experiment must not add an extension or change public application objects.

Before schema writes, inspect the installed backup helper. The current portable
business exporter enumerates the original `public` schema and excludes auth and
the known Alembic marker. It **does not back up experiment workflow state**.
Do not claim durable workflow-state backup from that business export. Normal
full PostgreSQL dumps include separate schemas; root must verify the actual
installed backup policy, not assume that path was used. Keep experiment receipts
and original IDs for the resource gate.

The adopted baseline's strict checker intentionally rejects these extra schemas
and triggers while the spike is present. Do not widen its or the production
backup allowlist. After both candidate tests finish, root may remove **only**
the inspected, known disposable schemas after ensuring their objects/rows are
exclusively these stub identities and preserving the evidence. Then re-run the
original baseline check and show it is green again. A separate full workflow
state backup/restore, schema ownership and upgrade contract is required before
any real queue integration. No cleanup command is automated by this script.

## Functional scenario and original-ID recovery

For each candidate, initialize once, then use a fresh run directory and invoke
`submit` **once**. The immutable `submission-request.json` precedes enqueue;
`submitted.json` records returned IDs. If a producer response is lost, inspect
those exact IDs/schema rows; never invoke `submit` again with another run/key.

The scenario is three stub shots, then chapter, captions and export, alongside
20 independent stub jobs. DBOS stores one film workflow with six durable steps
and 20 bulk workflows. Procrastinate stores six chained stage tasks and 20 bulk
tasks. Do not compare those different SDK row counts as if they were duplicate
business jobs. Each immutable output is identified by original run/key, producing
26 tiny completion files. Output files are synthetic hashes, not actual media.

Run `worker --step-seconds 8 --seconds 240` so root can kill **only its recorded
benchmark PID**, mid-step, after at least one completion. Preserve original
worker receipt/start identity and output hashes. Restart `worker` against the
same run/root/version with no producer replay. DBOS recovers the stable executor's
pending workflows. Procrastinate does not automatically requeue dead workers'
`doing` jobs: after its normal stalled-heartbeat window, invoke `recover` with the
original dead PID. That command requires its matching original worker receipt,
absence of that PID, no active experiment executor, and exact run/key matches;
it calls the public retry API on the **same original SDK job IDs**. Then restart
the worker. Do not relabel this explicit recovery glue as DBOS-equivalent native
workflow recovery. Inspect `status` once at boundaries; do not add a polling
reconciler. Status includes first submit/defer-to-claim latency per original ID.

Proof requires all 26 exact output hashes, terminal original SDK IDs, completed
step reuse, and no unintended duplicate original stage/bulk identities. The
in-flight stub may re-execute after a crash; it reuses its immutable completion.
This does **not** prove the real GPU submission/lease/charge domain fences. Those
stay a later acceptance gate, including the remaining #267 scenarios.

## Serial resource windows

Use fresh run directories for each window after functional queues are terminal.
The fixed queue name avoids accumulating one poller per run. Execute serially on
the same staging host, with no release/other experiment or generation workload.
Bind observed process IDs rather than guessing them. Run, in order:

| Window | Command | Polling | Requested empty-queue duration |
| --- | --- | --- | --- |
| Current queue | `idle --engine baseline --pid <original-controller-pid>` | Existing config unchanged | 1800 s |
| DBOS default | `idle --engine dbos --seconds 120` | 1 s | 120 s SQL-rate sample only |
| DBOS tuned | `idle --engine dbos --polling 5` | 5 s | 1800 s |
| Procrastinate default | `idle --engine procrastinate --seconds 120` | 5 s | 120 s SQL-rate sample only |
| Procrastinate tuned | `idle --engine procrastinate --polling 10` | 10 s | 1800 s |

Every invocation also supplies the original `--run-id`, canonical `--work-dir`,
and scope flag. Bind the same observable original host process set through
`--pid` when comparing aggregate RSS, and report the candidate's added RSS
separately. `/proc/stat` yields whole-host aggregate CPU percentage; `/proc/<pid>`
RSS is only the explicitly observed set, not host memory. Preserve OS CPU credit
and overlapping workload observations in the result, rather than attributing all
host changes to an SDK. Root owns the original finite background command ID;
do not resubmit it just because a tool wait times out.

This bounded overnight proposal replaces the earlier five-by-30-minute choice:
three full 30-minute idle CPU/RSS windows (baseline and both tuned candidates),
plus explicitly shorter default-polling SQL samples. Record the decision in
#295 before launch. The 120-second samples do not satisfy a 30-minute CPU/RSS
comparison and must retain their actual duration in the table.

The sampler writes private JSONL every 5 seconds, plus an exact elapsed summary.
SDK startup/DDL/queue registration is outside the empty window. `worker` samples
are explicitly labelled `functional_run_not_idle`. An interrupted or shortened
window is partial; do not call it a 30-minute result. DBOS's SDK internal queue
and listener may retain their defaults even when the named queue is tuned; the
counter measures their actual work, so report this limitation. Each worker gets
a fixed one-second startup allowance before observation; asynchronous listener
setup may still finish near that boundary. SDK row/event signatures must remain
unchanged with no active stub rows across an accepted idle window.

SQL measurement definitions:

- Worker `execute`/`executemany` counters use official caller-supplied psycopg
  cursor factories, including DBOS raw LISTEN and Procrastinate listener
  connections. Batch rows count individual statement executions. SQL and
  parameters are never retained. Implicit protocol BEGIN/COMMIT frames and other
  applications' connections are outside this worker-client counter.
- Metadata snapshots before/after report database transactions as **tx/s**, not
  queries. They also record SDK table counts and database/candidate connection
  counts. Observer statements and transactions are disclosed as included in
  whole-database counters, outside the SDK cursor counter.
- Read existing `pg_stat_statements` only if installed and readable. A reset,
  missing/preload error or insufficient visibility yields SQL QPS **unknown**.
  Never install/restart PostgreSQL to improve the measurement. The current queue
  is not instrumented by this experiment, so its SQL QPS may remain unknown.

Post the actual same-host table: elapsed seconds, host CPU, observed/incremental
RSS, worker SQL rate, whole-database SQL rate or unknown, tx/s, table/connection
delta, first claim latency, and adapter/glue lines. Choose DBOS unless measured
idle resource cost is clearly worse. No result is supplied by source preparation.
