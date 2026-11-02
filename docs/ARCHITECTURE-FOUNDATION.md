# Architecture foundation

Status: owner approved 2026-11-02. This is the long-term direction; `PROVIDER-ARCHITECTURE.md` holds the capacity detail. Measured on main at cd4c0ef7.

## Direction

One codebase with clear module boundaries, split into a few processes by how they fail. Infrastructure that is not the product gets replaced by something maintained. The team stays at three people or fewer, so maintenance is the main cost.

Where the code is today: about 130,000 lines in 301 modules, 555 recorded cross-domain edges. Capacity alone is 36,000 lines, more than story creation at 29,000, and it is not what users pay for. Backup is about 2,800 lines, release and image retention about 2,000.

## Rules

1. **Every state has an exit.** Jobs, machines and payments each name who moves them on and after how long. "Unknown" is a state with an owner, not a place to wait forever.
2. **Money and capacity are separate facts.** What we may owe a provider and what can take work now are recorded apart. The ledger only appends.
3. **One interface per kind of provider.** Adding a provider changes one place.
4. **Frontend and backend share generated types.** A field the server removes breaks the build, not a customer.
5. **Processes split by failure and resources.** API, scheduling, media and GPU workers run from one image with different roles, so media memory cannot take scheduling down.
6. **A failure says why.** No swallowed errors. Waiting time and cost per finished clip are measured end to end.

## What we keep and what we replace

| Area | Today | Decision |
|---|---|---|
| Credits ledger, tier routing, story model, one-attempt rule | Ours | **Keep.** This is the product. |
| GPU start | dstack plus our capacity code | Keep dstack behind our interface; shrink our code around it. |
| Durable work | DBOS, plus older queue paths and idle Hatchet | DBOS only. Remove the rest. |
| Old Modal lane, SSH transport | About 2,900 lines plus SSH code, off in production | **Delete.** |
| Database and backup | Postgres in a container on the app host; our own backup | Managed Postgres, pending the owner's price approval. Otherwise pgBackRest. Media keeps its versioned off-host copy. |
| API client | Handwritten | Generate from OpenAPI with Hey API, route by route. |
| Frontend language | JavaScript | Move to TypeScript gradually, API layer first. |
| Alerts | Our monitor, mail to an unread inbox | CloudWatch alarms plus a hosted alert service. |
| Vendor HTTP | One per vendor | One small client on httpx. Never resubmit on an uncertain answer. |
| Secrets | One script per vendor | One generic script (#976). |
| Release | Ours | Narrow first (#815), then evaluate Kamal once the database is off the host. |

## Not now

- Kubernetes, microservices, Kafka: more to run than this team can carry.
- Temporal: DBOS already covers durable work on our Postgres.
- Lago or another billing platform: Stripe is the merchant of record, and the ledger is small.
- A rule engine: a rules table and pure functions are enough.

Second app host, read replicas and more GPU pools wait for a measured need: waiting time or concurrency at its limit.

## Order

1. Fix the known faults first, because the refactor must not change behaviour: the host-loss report and #982.
2. Delete: old lanes and unused queues.
3. Interfaces: provider registry and interface, generation gateway, generated types.
4. Processes: media out of the controller.
5. Infrastructure: database and alerts, run by Claude because it needs a maintenance window.
6. Split large files last, so nothing about to be deleted gets split first.

## Media isolation preparation (#899, 2026-11-03)

`LocalSlotOwner` owns local registration, heartbeats and CPU operation fences;
`SlotContexts` and the finite-turn helpers retain the original shared ordered
registry, context shutdown and job/attempt behavior. `DurableSlots` still runs
both local and native work in the controller process under one supervisor fence.
This extraction does **not** isolate media memory or startup failures. The actual
same-image media service, independent cgroup, resource qualification and adoption
remain [#999](https://github.com/inkseq/h3-studio/issues/999); #813 is not complete.

## References

- FastAPI, generating clients: https://fastapi.tiangolo.com/advanced/generate-clients/
- pgBackRest: https://github.com/pgbackrest/pgbackrest
- DBOS Transact: https://github.com/dbos-inc/dbos-transact
- dstack: https://www.dstack.ai/
