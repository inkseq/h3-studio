# Deployed runtime inventory and retirement boundary

Observed read-only on staging and production on 2026-10-18, 17:33–17:49 UTC for #257. These observations establish this retirement slice, not future health or provider stock. Source references below refer to the current unified platform.

| Component | Deployed authority | Decision |
|---|---|---|
| Public website/API | `uvicorn platform_app:app`, password auth in `studio_platform/auth.py` | Keep unified API and current owner isolation. |
| Capacity/controller | `python -m studio_platform.dstack_controller` | Keep current original-ledger policy, fences and provider-intent reconciliation. |
| Capacity engine | Pinned official dstack server, dedicated database | Keep; DBOS is not a GPU provisioner. |
| Generation workers | Controller's `postgres_dispatch` slot children and `wangp-worker` execution backend | Keep current queue until staging-only #295 passes; no dual consumer. |
| Storage | R2 selected in both app and controller | Keep local provider for isolated tests, previews and backup/recovery. No `objects/` or unique user media deletion. |
| Standalone workbench | No deployed `server:app` process/container; unified Dockerfile never copies it | Remove the old server, separate SQLite password auth, web UI and standalone Docker/release/admin/check path. |
| Historical operator containers/units | Old operator containers are exited; nine production units are failed | Not live capacity. Unit retirement is a separate reversible operation after fresh inactivity checks; never stop the current dstack controller. |

Both hosts had 59 public business tables / 444 columns; the sampled job states were terminal and workers retired/stopped. These aggregate observations do not settle invoices, release reservations, remove records, or prove provider absence. The retained history includes a staging `creation_unknown` row. Nothing in this source cleanup changes any row or starts/stops a provider instance.

## Why the other old-looking modules stay

An import or a historical name is not proof of an active rental path, and a process-name inventory is not proof that a library is dead:

- `dstack_capacity` and `dstack_preparation` import `lium_bootstrap` diagnostic code/phase sets. `dstack_runtime` still uses `fleet.SlotConfig`; native bootstrap/adoption modules share the existing SSH and receipt contracts.
- `api`, `control`, availability, host policy, settlement/invoice diagnostics and dstack workspace still use `operator_capacity` tables/types. Those rows are the original machine/accounting authority, not a second queue to discard.
- `capacity_inventory`, old-provider APIs and original-job recovery retain Lium/Targon identity and observation helpers. The pinned dstack configuration covers Vast/RunPod; this slice does not assert that upstream Targon support exists.
- `capacity`, scaler types and pool helpers remain transitively referenced. Decoupling their live value types/diagnostics from retired launch paths is later #257 work, with actual call-path and recovery acceptance before deletion.
- Historical `hatchet-v1` values in immutable execution plans are identity/compatibility data. They are not evidence of a running Hatchet broker and are not renamed or rewritten.

## Removed surface and retained evidence

The removed standalone website had its own `server.py`, `password_auth.py`, `web/{index.html,app.js,styles.css}`, root `Dockerfile`, `deploy/{compose.yaml,Caddyfile,site.env.example,release.sh,check_release.py}` and `tools/{manage_users.py,check_container.py}`. Its five exclusive test modules are retired with that product surface; current platform auth/isolation/media/control tests remain. The expert `h3-ref-bf16-workflow.json` bytes move unchanged into `tests/fixtures/` because shared compiler tests still validate them.

Only the corresponding obsolete CI image/browser/deploy checks are removed. Unknown/security/budget/provider changes still require every current platform gate, including the unified image, dispatch/dstack derivatives, PostgreSQL suite and native helper checks. No protected host release helper is replaced, and original rollback images/artifacts remain available independently of this source removal. Historical standalone documentation is explicitly dated and remains in Git; it does not authorize redeploying the retired path.

Host-path renaming, local media migration/deletion, wholesale provider/scaler removal and DBOS production activation are outside this slice. DBOS remains staging only under CR-20261019-37.
