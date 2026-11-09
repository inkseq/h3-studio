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
  slow or stalled download, model load failure, worker lost at a time, tunnel
  lost on app restart (`"returns"` after 5 min or `"never"`), provider removal,
  run turning terminal, stop delay and price. dstack `max_duration` from the run
  spec is honoured.
- `events`: `restart_app`, `demand_off`, `demand_on`, `offers_off`, `offers_on`,
  `edit_demand_config` (value: keys merged into `policy.demand_start`, then the
  controller restarts), `corrupt_reservation` (value: columns set on the
  latest seeded historical intent, e.g. an unknown pool).
- World options: `max_machines`, `inventory` (`fresh`, `stale`, `slow`,
  `failed`), `history_removed` and `history_invoiced`, `test_guard` with
  `spent_fraction`, `owner_profiles`, `lookahead`, `tick_jitter`,
  `prepare_seconds`, `observation_fresh_seconds`, `provider_latency`.
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
