# Fair generation dispatch

Refs #745 / #746; roadmap #692 G2. This slice only orders claims on existing
ready slots. It cannot rent hosts, change a price/budget or preempt a running job.

Tenants with an adopted capacity policy and `per_user_machine_share_policy =
equal_active_users` use per-owner FIFO queues projected from existing jobs.
Owners with fewer active attempts get the next slot first; equal-occupancy
owners rotate using the existing per-pool `scheduler_state.last_owner` cursor.
The cursor advances in the same locked SQL transaction as a successful
generation claim. A rejected selected delivery or rollback cannot advance it.
An owner is identified by the existing tenant/owner tuple hash. Within each
owner, creation time and job ID keep deterministic FIFO order. A large batch
does not acquire global FIFO priority merely by waiting 15 minutes.

Advisory order interleaves one candidate per owner before second candidates;
actual claims recheck readiness, account status and the original worker filters.
The ordering is shared by DBOS delivery checks and the existing queue-position
endpoint. Queue position remains advisory and has no fabricated ETA.

No one-slot-per-user limit is imposed: when other owners have comparable active
occupancy, another available slot may serve the same owner's next shot. Existing
unknown/recovery attempts count as occupied and retain their original fences,
budget reservations and reconciliation behavior. They never reenter generation.

Unadopted tenant candidates retain their legacy sorted positions. Collection and
reconciliation keep the original ordering. No schema, new queue, background
process or second scheduling database is introduced. Worker/broker restarts
reuse the durable cursor. The old estimated-work counters remain for legacy
sorting and are not reset.

Reference implementation reviewed: Kubernetes apiserver's
[per-flow fair queues](https://github.com/kubernetes/apiserver/blob/master/pkg/util/flowcontrol/fairqueuing/queueset/queueset.go).
We retain its useful separation of FIFO within a flow and serialized fairness
between flows, while reusing Inkseq's existing SQL scheduler rather than adding
a queue framework. No upstream code is copied.

CR76/77's target/ETA projection, hourly spend-bound Modal expansion, admission
caps and measured latency percentiles remain separate work. This change alone
does not establish a five-minute service guarantee or enable a provider.

## Optional administrator controls

The existing spending-settings preview/confirm API accepts `fair_share` (boolean)
and three wait targets: `first_video_max_seconds` (recommended 300),
`next_video_interval_seconds` (120), and `modal_trigger_seconds` (180).
All four changes require a reason, the original profile snapshot, confirmation
digest and settings-version CAS; the existing settings event records history.
The wait targets remain pending implementation and the inventory reports them
as ineffective until an execution slice connects them. They do not enable Modal,
start hosts, change hourly limits or promise an ETA.

Old policies and accepted authority snapshots may omit all four fields. Reads,
previews and unrelated writes never inject them. Administrators must explicitly
save each optional value to adopt it. With `fair_share` absent, adopted policies
retain the fair ordering described above; explicitly saving `false` restores
legacy candidate ordering. It does not cancel/preempt work or change claims,
fences, settlement or recovery. Turning it on again resumes fair ordering with
the original durable cursor. The three target values may be stored independently;
when both first-video and Modal-trigger targets are present, trigger cannot
exceed the first-video target.
