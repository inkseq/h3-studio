# Controller simulator

`tools/simulate_capacity.py` runs the real capacity controller against a fake
provider, fake workers and a fake clock, so hours of queue run in seconds. It
exists to find controller rule bugs locally, many cases at once, instead of one
staging incident at a time (#1034, #1140, #1068).

```sh
python tools/simulate_capacity.py                                 # all scenarios
python tools/simulate_capacity.py --list
python tools/simulate_capacity.py --scenario burst --trace        # tick by tick
python tools/simulate_capacity.py --json > report.json
PYTHONPATH=tests python -m unittest test_capacity_simulator       # what CI runs
```

The tool puts the repository and `tests/` on the import path itself. For the
unittest on Windows use `$env:PYTHONPATH = "tests;."`.

## What is real and what is fake

Real, unchanged: `DstackController.tick` (funding, observation, idle drain,
download and startup stops, registration), demand start (`start_once`, job
eligibility, the cold admission marker, lookahead and machine limits), the
health check and its demand pause, the watchdog (`reclaim_node`, provider
removal, no-progress), `WorkerControl` claims, `worker_admission` window checks,
`TaskQueue` and the original ledger on a throwaway SQLite file. The world is
built with the existing test fixtures (`LedgerCase`,
`DstackOperatorTests._setup_dstack`) and an adopted admin capacity policy.

Fake, in `tests/capacity_sim.py`:

- `SimProvider`: dstack run JSON (`latest_job_submission.job_provisioning_data`)
  and provider inventory (`{backend, complete, observed_at, instance_ids,
  aliases}`) in the shapes the code already accepts, scripted per machine.
- `SimRuntime.readiness`: native boot progress (download, model load, failure,
  ready/busy) as `DstackError` codes with preparation receipts or `HostReadiness`.
- `SimSlots` and `World.work/submit`: the DBOS slot and the worker loop
  (consumer proof with the slot's own native readiness read, heartbeat, claim,
  window check before the POST, progress, collection). Media evidence validation is skipped; completion uses one fixed
  artifact.
- The clock: one controller tick, then workers claim 2 s later and check the
  window `prepare_seconds` later. Ticks are 60 s, or random within
  `tick_jitter` with `--seed`.
- Provider latency (`provider_latency`, seconds per provider read: run GET,
  inventory, plan, Vast offer mapping, reusable hosts): each read moves the
  clock, and workers run concurrently with the tick. A worker turn due during
  a read runs at its time, unless the controller holds the global capacity
  lock (the consumer write before claim and submit needs it) or SQLite holds an
  open connection; then it waits until the lock is free. The report's
  `metrics` give the longest worker wait, the longest claim-to-submit time and
  the provider seconds spent under the lock (#1148).

Every socket connect and DNS lookup is refused while a scenario runs.

Not covered yet: the dial-out lanes (`RoutedClient`, RunPod/Modal adapters),
hosted (EveryFrame) routing, credits and invoices settlement.

## Invariants (checked every tick and at the end)

- No job lost (failed, recovery hold, submission unknown, or never finished
  when capacity allows) and none run twice (more submissions than lost attempts
  plus one).
- Provider-billed machines never exceed `max_machines`.
- A booted, billed machine is not idle for longer than the health window while
  queued work it could still admit waits.
- A machine is not kept on longer than idle shutdown plus grace when no queued
  job can be admitted on it.
- Queued work does not sit for 30 minutes with no machine at all while demand
  is on and the scenario says capacity and budget allow it.
- Every demand refusal, reconciliation and stop has a reason code.
- Billed spend never exceeds the staging test task cap.
- With provider latency: no provider read runs while the global capacity lock
  is held.
- A ready test machine does not stop for idleness or its runtime window while
  its window still fits two of the waiting jobs (true runtime plus the 120 s
  completion margin each).

The report gives simulated hours, jobs done, billed and idle machine-hours and
every reason code seen (demand refusals, node codes, stops, worker deferrals
with the underlying `worker_window_reason`).

## Observability check (every scenario)

Each problem the simulator can see from its ground truth must leave
persisted, operator-visible evidence with the exact reason, not a log line
(controller and app logs last hours: docker json-file, 5 MB x 2). A problem
counts once it lasts 5 minutes; evidence must follow within 10 more minutes
(`tests/capacity_sim_evidence.py`), else the run fails with
`unobserved_<kind>`:

| Problem (ground truth) | Accepted evidence (outbox) |
|---|---|
| `paid_idle_with_queue`: booted, billed, idle, admittable work waits | timeline `claim_refused` with reasons, `capacity_paid_idle_queue` alert, or a stop |
| `lost_worker`: worker gone while the machine bills | timeline `worker_expired`/`worker_unknown`, a runtime read leaving `serving` with its code, a worker-lost stop or alert |
| `slow_download`: stalled or below the 50 MB/s floor | timeline `download_stalled`/`download_slow`, the download stop, or `capacity_download_overdue` |
| `start_blocked_no_machine`: queued, no machine, demand on | demand timeline entry with the blocker code; after 30 min a `capacity_start_blocked` alert with `detail` = exact code |
| `stuck_job`: queued 30 min without a break, or held by a removed machine | `capacity_queue_age` alert; `capacity.attempt_lost` or the removal on the timeline |
| `stuck_collect`: a finished run whose result collection keeps failing | timeline `collect_failed`/`collect_output_stored`, `capacity_collect_stuck` alert, or `capacity.attempt_lost` |
| `refused_start`: demand blocked, create failed, no capacity | demand entry with `reason_code` and raw `detail_code` (or its count in a summary); create error snapshot or no-allocation proof |
| `refused_claim`: a claimed job's submission deferred | timeline `claim_refused` containing `defer:<exact reason>` |
| `budget_block`: demand refused by a budget limit | demand entry with the raw limit code (e.g. `dstack_test_task_limit_reached`) |

The first run of this check (before the timeline existed) found 88 gaps over
33 scenarios: blocked demand decisions (56: `capacity_limit`, `backoff`,
`budget_limit`, `offer_unavailable` and their raw codes lived only in the
tick result), create failures and no-capacity starts (part of those 56),
6 starts blocked with no machine, 5 lost workers (a busy worker keeps a
900 s lease, so nothing showed for 15 minutes), 2 idle machines with work
waiting and 2 budget blocks. The other 17 were queued-job ages that the
existing `capacity_queue_age` alert does cover; the first version of the
check measured from job creation instead of the unbroken wait. All are now
recorded or alerted.

## Result collection that cannot finish

A job in `collecting` is collected only by the worker that ran it. When that
host is gone, or collection keeps failing, nothing else ever collects it
(staging job 27b6213f sat in `collecting`/`collection_failed` for hours after
its machine was destroyed). The watchdog (`capacity_collect.py`, read from
committed rows only) now decides each tick:

- **Output already outside the host** (every expected role verified in
  storage by its sealed size and hash, or files staged on the collector for
  CPU delivery): never regenerated. The job is held and, after 15 minutes,
  the `capacity_collect_stuck` alert carries `detail=collection_output_stored`.
- **Host removed** (provider removal proof): the attempt is lost; it is
  reclaimed as `capacity_provider_removed`, like a running job on that host.
- **Collection keeps failing on a live host**: after 10 failures, or a failed
  collection no worker retried for 10 minutes, the attempt is reclaimed as
  `capacity_collection_failed` and the machine is stopped.

A reclaim uses the audited `capacity_reclaim` path: the attempt is fenced
(`attempt_lost`), the job is requeued with a `capacity.attempt_lost` receipt
(a third lost attempt fails the job and releases its credit hold), the node's
worker is retired so it no longer points at the job, and a running collect
turn is never interrupted. The customer's single credit hold stays on the job
and is captured once, by the attempt that delivers; the lost attempt's supplier
cost stays unknown until invoice reconciliation, never settled at zero. The
timeline records `collect_failed`, `collect_output_stored` and
`collect_reclaimed` per attempt.

Scenarios: `collect_fails_then_machine_removed` and `collect_keeps_failing`.

## Recorded capacity timeline (staging and production)

The controller records after each tick, never deciding anything
(`studio_platform/capacity_timeline.py`, outbox events, no new table):

- `capacity.timeline`, aggregate `<intent_id>`: one entry per change, each
  with `at` and the raw code: start requested (profile, demand job, test task,
  expected runtime), offer chosen (provider, GPU, region, hourly price),
  allocated, run status, boot phase and runtime reason, download milestones
  every 25 % with bytes and rate (plus `download_slow`/`download_stalled`),
  model load, runtime ready, worker state (`worker_expired`), first claim,
  idle episode, `claim_refused` summaries (per-reason counts per 5 minutes,
  from the claim and submission checks and a read-only probe of the real claim
  gates while idle), stop requested with reason, removal confirmed, billing
  state, intent state.
- `capacity.timeline`, aggregate `demand:<tenant>`: demand decisions on
  change (`reason_code`, raw `detail_code`, queued, live machines, since when
  blocked). Over 6 changes in 5 minutes become one per-code count summary.
- `capacity.provider_snapshot`, aggregate `<intent_id>`: sanitized dstack run
  reads (status, termination reason, provisioning identity: backend, instance
  id, region, price, instance type), inventory presence of that instance,
  create/stop/read error codes. Stored on change only. Allowlisted fields
  only: no env, credentials, tokens, IPs, hostnames, SSH users or keys,
  emails, project or account names, free-text messages
  (`capacity_snapshots.py`, tested in `test_capacity_timeline.py`).

Bounds: 240 timeline and 80 snapshot rows per machine, 5-minute refusal
windows, and both event types are pruned after 30 days (hourly, 5,000 rows
per pass). A machine's timeline closes once it is destroyed and billed.

New health alerts (capacity health -> outbox -> Sentry, one per episode):
`capacity_download_overdue` (a download past bytes / 50 MB/s + 5 min:
`download_slow`, `download_stalled`, `download_unobserved`) and
`capacity_start_blocked` (queued demand work, no live machine, starts refused
for 30 min; `detail` is the exact blocker code). `capacity_paid_idle_queue`
(ready, no claim for 10 min with work queued) now carries the top claim
refusal as `detail`.

Read it with the read-only CLI (inside the app or controller container, e.g.
over SSM, or from a checkout with the database URL set):

```sh
python -m studio_platform.capacity_timeline_cli <intent_id or 8+ char prefix>
python -m studio_platform.capacity_timeline_cli --recent 10
python -m studio_platform.capacity_timeline_cli demand
python tools/capacity_timeline.py ...     # same, from a checkout
```

It prints every entry with its offset from the start and a "where it got
stuck" summary (the first lifecycle stage not reached, or the stop reason
and what is still pending, plus the problem entries and alerts).

## From incident to regression test

1. **Incident.** A machine misbehaves. Read its timeline:
   `python -m studio_platform.capacity_timeline_cli <intent_id>`.
2. **Export.** In the same container:
   `python -m studio_platform.capacity_incident_export <intent_id> [--since T --until T] > incident.json`
   (or `python tools/export_capacity_incident.py ...`). Read-only. The JSON has
   a `machine` (MachineScript: allocation, download, stall, model load,
   worker loss, provider end, stop delay, price, run read error windows), a
   `run` overlay (recorded region and price), a `scenario` (the jobs that
   machine ran at their median measured runtime, the work still queued when
   it stopped, the test runtime window) and `recorded` (outcome, timeline,
   provider snapshots). It carries no owner, account, IP, budget or spend; a
   test guard gets a synthetic cap.
3. **Scenario.** Save it as `tests/fixtures/capacity_sim/incident_<name>.json`
   (already allowlisted). Every `incident_*.json` there joins the scenario
   catalog; try it alone with
   `python tools/simulate_capacity.py --fixture incident.json`. The report line
   `recorded machine: stop X, claims N | replay: stop Y, claims M` compares
   the recorded and replayed outcome of that machine.
4. **Fix.** If the replay breaks an invariant because of a controller rule
   bug, add `"expected_failure": "<what breaks> (file:line, issue)"` to the
   JSON, fix the rule, and remove the marker when the scenario reports
   `unexpected_pass`.
5. **Regression.** The fixture stays in the catalog and runs in CI with the
   other scenarios.

`test_capacity_timeline.IncidentRoundTripTests` proves the loop: it runs a
scenario, exports one machine from the throwaway ledger with the real export
code, replays the export and expects the same stop reason and claim count
(a worker lost mid-queue: `capacity_no_progress` after 2 claims; a download
stalled at 40 %: `dstack_download_too_slow` with none).

## Adding a scenario

Add a `Scenario` to `tests/capacity_scenarios.py`:

```python
Scenario(
    "my_case",
    "One line on what happens.",
    hours=3,
    jobs=[JobBatch(0, 10), JobBatch(30 * 60, 5, owner="supervan")],
    machines=[MachineScript(create="reply_lost"), MachineScript(load_fails=True)],
    events=[Event(20 * 60, "restart_app")],
)
```

- `machines` are used in create order; later creates use `default_machine`.
  `MachineScript` covers: create ok or reply lost, never allocates, no capacity,
  slow or stalled download, model load failure, every result collection
  failing (`collect_fails`), worker lost at a time, tunnel
  lost on app restart (`"returns"` after 5 min or `"never"`), provider removal,
  run turning terminal, stop delay, price and `read_errors` (windows in which
  a run read raises a recorded error code). dstack `max_duration` from the run
  spec is honoured.
- `hosts` instead of `machines` gives the provider a market of named physical
  hosts (`Host("19901", [MachineScript(...), ...])`): every plan offers all of
  them, cheapest first, a create lands on the first one the frozen selector
  names, and each host plays its own rentals in order (the last repeats). Use
  it for rules that depend on which host is rented, such as a host that never
  becomes reachable.
- `events`: `restart_app`, `demand_off`, `demand_on`, `offers_off`, `offers_on`,
  `edit_demand_config` (value: keys merged into `policy.demand_start`, then the
  controller restarts; a new id in `profile_ids` gets a copy of the first
  profile, owned by the owner named in `profile_owners` when given, and a new
  `test_task_id` starts a fresh task cap for the spend check, as does a new
  Sydney day, since demand starts fund from `<test_task_id>:<YYYYMMDD>`),
  `corrupt_reservation` (value: columns set on the
  latest seeded historical intent, e.g. an unknown pool).
- World options: `max_machines`, `inventory` (`fresh`, `stale`, `slow`,
  `failed`), `history_removed` and `history_invoiced`, `test_guard` with
  `spent_fraction`, `owner_profiles`, `lookahead`, `start_rules` (saved
  `job_seconds`/`boot_seconds`/`minimum_jobs` for every demand profile, as the
  admin start-rules API stores them), `idle_shutdown_seconds` (the policy
  default; lookahead starts use the rules' boot seconds), `tick_jitter`,
  `prepare_seconds`, `observation_fresh_seconds`, `provider_latency`.
- Exact expectations: `expect_starts` (number of machines created) and
  `expect_idle_stop_seconds` (every idle stop comes that long after the
  machine's last finished job or readiness, at most one tick later, and at
  least one happens), and `expect_host_rentals` (host id -> the most times
  that host may be rented). The report counts lookahead decisions as
  `lookahead:<phase>:<reason>`.
- A run stops early once every job is finished and no machine bills, or 30
  simulated minutes after its first violation.
- A recorded provider response can become a fixture: put
  `{"machine": {...MachineScript fields...}}` in
  `tests/fixtures/capacity_sim/<name>.json` (allowlist it in `.gitignore`) and
  use `MachineScript.from_fixture("<name>")`.

If the scenario fails because of a real controller rule bug, do not bend the
simulator to pass. Set `expected_failure="<what breaks> (file:line, issue)"`.
The suite stays green and the bug stays listed; once the rule is fixed the
scenario reports `unexpected_pass` and the marker must be removed.

## Design references

Ideas only, no code (both Apache-2.0):

- KWOK (kubernetes-sigs/kwok): the real control plane runs against fake nodes
  whose lifecycle is a declared sequence of stages with delays and jitter.
  Taken: per-machine scripted stages with delays, seeded jitter, the real
  controller untouched. Left: the selector and weighted stage engine and a
  separate fake API server process; plain Python data is enough here.
- cluster-autoscaler test and fake cloud providers
  (kubernetes-sigs/cluster-autoscaler `pkg/cloudprovider/test`): in-memory node
  groups with injectable scale-up and scale-down callbacks, and toggles for
  provider quirks such as node objects lingering after their VM is deleted.
  Taken: per-call failure injection (lost create reply, no capacity) and
  provider-record lag (stale or failed inventory, removed runs). Left: the
  node-group abstraction and scheduler simulation.
