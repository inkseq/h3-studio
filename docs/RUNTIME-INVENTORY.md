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
| Historical operator containers/units | Old operator containers are exited; nine failed production units were reversibly retired | Exact original definitions and intent remain in a private archive. No live service was stopped; the current dstack controller is unchanged. |

Both hosts had 59 public business tables / 444 columns; the sampled job states were terminal and workers retired/stopped. These aggregate observations do not settle invoices, release reservations, remove records, or prove provider absence. The retained history includes a staging `creation_unknown` row. Nothing in this source cleanup changes any row or starts/stops a provider instance.

## Shared contracts and retired paths (#328)

The 2026-10-19 source inventory confirms the deployed overlay uses dstack and does not configure `H3_OPERATOR_RUNTIME_CONFIG`. Custom Lium/Targon providers, their old bootstrap/scaler/controller chains and exclusive tests/guides are retired. New custom-runtime configuration and legacy market/start endpoints refuse with authenticated `410 legacy_capacity_runtime_retired`; Vast/RunPod through dstack remain the launch path. Targon is a later dstack integration, not a custom-provider fallback.

Live shared code moves intact into `native_bootstrap` (SSH and sanitized diagnostics), `capacity_types` (ledger value objects), `provider_lifetime` (read-only old-machine eligibility) and `central_registry` (lazy access to the existing Registry). These modules do not rent resources or create another controller.

Keep the original `operator_capacity`, `capacity` and market tables/types used by API/control/admission/history/manual review/settlement. Original provider names, ports, execution plans, `hatchet-v1` identities and unknown invoices remain history. No schema or business row is deleted or rewritten. Native Targon preparation is still part of the pinned GPU package; its name is not evidence of a custom provider client.

Protected host release/rollback inspection helpers and historical cleanup/read-only credential adapters remain where the installed release path still imports them. They do not restore the retired application runtime. Their guard/restore/accounting tests stay; live native REF/FL, failure classification, ownership, queue and money checks are retained when old setup fixtures are removed.

## Removed surface and retained evidence

The removed standalone website had its own `server.py`, `password_auth.py`, `web/{index.html,app.js,styles.css}`, root `Dockerfile`, `deploy/{compose.yaml,Caddyfile,site.env.example,release.sh,check_release.py}` and `tools/{manage_users.py,check_container.py}`. Its five exclusive test modules are retired with that product surface; current platform auth/isolation/media/control tests remain. The expert `h3-ref-bf16-workflow.json` bytes move unchanged into `tests/fixtures/` because shared compiler tests still validate them.

Only the corresponding obsolete CI image/browser/deploy checks are removed. Unknown/security/budget/provider changes still require every current platform gate, including the unified image, dispatch/dstack derivatives, PostgreSQL suite and native helper checks. No protected host release helper is replaced, and original rollback images/artifacts remain available independently of this source removal. Historical standalone documentation is explicitly dated and remains in Git; it does not authorize redeploying the retired path.

Host-path renaming and local-media deletion remain outside this source cleanup. DBOS production activation stays held for Claude's post-M1 staging GPU acceptance under #328 / CR39.

## Reader-before-writer schema compatibility

This preparatory release also introduces a read-only `schema_version` helper without importing Alembic. A private business-backup source may contain only the exact known baseline marker shape and revision; it is validated and excluded from portable business exports, just like authentication is excluded. Unknown or malformed markers still refuse export. No baseline marker is created, changed or removed by this reader.

Only after this reader is the previous rollback image may a later Alembic release adopt the baseline. Otherwise reverting to the old strict backup inventory would make the next daily backup reject `alembic_version`. This is a Medium-risk compatibility prerequisite, separate from the Low-risk standalone source retirement and the separately reviewed host-unit operation. Actual current/previous reader capability must be checked before any live adoption; source merge alone does not satisfy that gate.
