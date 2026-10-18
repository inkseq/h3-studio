# Sixnine Cloud stage telemetry

Tracking issue: [#132](https://github.com/inkseq/h3-studio/issues/132). This optional
layer sends stage observations directly to the existing Grafana Cloud OTLP
gateway. Cloud Mimir/Loki/Tempo store metrics/logs/traces; do not deploy Grafana,
Prometheus, Loki or a second telemetry database on the CPU host. PostgreSQL still
owns accepted jobs, attempts, leases, budgets, cancellation and output receipts.

## Configuration and credentials

Install `requirements.lock.txt` alongside the application dependencies using
`uv pip install --require-hashes -r deploy/observability/requirements.lock.txt`.
The API, SDK and HTTP exporter are pinned to 1.45.0. Recheck protobuf export,
sanitized transport and bounded processor shutdown before changing these pins.

Use an existing stack-scoped Cloud access policy with only `metrics:write`,
`logs:write` and `traces:write`; its token is an ingestion credential, **not** a
Grafana dashboard API token. Root/local setup uses the existing protected central
credential process. Linux uses an explicitly protected runtime mount, not a
copied Windows vault or project `.env`. Only the CPU services receive this token.

Set the non-secret `SIXNINE_TELEMETRY_CONFIG_FILE` to an absolute JSON path. Its
mode is 0600 on POSIX, owned by the consuming service; Windows provisioning must
restrict its ACL to the current user. The exact document fields are:

```json
{
  "schema_version": 1,
  "enabled": true,
  "endpoint": "https://otlp-gateway-REGION.grafana.net/otlp",
  "authorization": "Basic BASE64_OF_STACK_USER_ID_COLON_INGESTION_TOKEN",
  "service_name": "sixnine-worker",
  "service_version": "unknown",
  "environment": "staging"
}
```

Use the exact endpoint displayed by the existing Cloud stack. Production accepts
only HTTPS `otlp-gateway-*.grafana.net/otlp` with no userinfo, query or fragment.
Names are `sixnine-api`, `sixnine-worker`, `sixnine-controller` or
`sixnine-acceptance`; environments are `local`, `staging` or `production`. Pin
`service_version` to the deployed source SHA. Invalid/missing configuration
disables optional observation and reports a static status code. It never starts
a GPU, changes environment credentials or blocks application startup.

Deployed CPU controllers and their slot children use the existing protected
`PUBLIC_ORIGIN` (`inkseq.com` or `staging.inkseq.com`) and the exact base-image
reference embedded by `Dockerfile.dispatch` to set resource identity:
`service.name=sixnine-controller`, the matching deployment environment, and the
full release SHA as `service.version`. This replaces stale acceptance labels in
memory only; exporter endpoint/authentication and the protected JSON stay
unchanged. A malformed deployed identity disables optional export instead of
mislabeling production as local. Local acceptance/other worker images retain
their explicit configuration. Disabled exporters remain disabled, including the
current isolated staging configuration; enabling it and verifying live staging
streams is separate #290 observability work. No controller credentials reach a
GPU, and dstack's own OTel switches remain absent.

## Instrumentation boundary

```python
from studio_platform.telemetry import configured_telemetry

telemetry = configured_telemetry()  # Optional; NullTelemetry if not configured.
stage = telemetry.start("generate", {
    "provider": "vast", "profile_id": selected_profile_id, "mode": "fl",
    "gpu_type": "rtx5090", "warmth": "warm", "job_id": job_id,
    "attempt_id": attempt_id, "width": 832, "height": 480, "frames": 124,
    "steps": 20, "gpu_index": 0,
})
# Call the existing execution/ledger operation exactly as before.
stage.finish("success")
telemetry.close()
```

The handle also supports `with telemetry.start(stage, context)` and
`telemetry.call(stage, context, operation, *args, **kwargs)`. The wrapper returns
the operation's original result and re-raises its original error; it records only
`stage_failed`, never exception text. Explicit completion accepts `success`,
`failure`, `unknown` or `cancelled` and a closed static error vocabulary. An
unrecognized error becomes `unknown_error`. Repeated `finish` is inert.

Stages are `queue_wait`, `capacity_provision`, `image_pull`, `weights_download`,
`model_load`, `mode_switch`, `input_transfer`, `generate`, `collect`,
`validate_output`, `upload`, `commit_result`, `end_to_end` and `runtime_ready`. They are observations,
not another state machine. A GPU process must not be called ready solely because
dstack reports `RUNNING`. A completion/end-to-end event is valid only when the
original business operation has durably committed validated video and independent
audio and made them downloadable. Do not instrument each poll as an entire
generation or regenerate to fill gaps in telemetry.

Start logs enqueue immediately, independently of span completion. SDK batches
flush asynchronously every 250 ms; a long stage or later process crash therefore
does not require an ended span before its start can be observed. Crash/network
loss before delivery can still lose an event: these are best-effort diagnostics,
not durable execution/receipt evidence. Live synchronous stages use one process's
monotonic clock. Worker queue/end-to-end intervals and generation recovery use
the original CPU repository timestamps with `timing_basis=durable_cpu_interval`.
`start(..., started_at=epoch_seconds)` reconstructs that span start;
`finish(..., duration_seconds=seconds, timing_basis="durable_cpu_interval")`
accepts only a finite, non-negative bounded interval. Start log time remains the
current observation time, with the original `interval_start_unix_ns` retained.
Never subtract unrelated CPU/GPU wall clocks or invent unobserved phase timing;
GPU-local stages need measured runtime receipts or instrumentation without
provider credentials.

`WorkerRunner(..., telemetry=telemetry, telemetry_context=static_context)` is
optional and defaults to `NullTelemetry`. It instruments the existing operations:
claim queue wait, prepare inputs, submit-to-terminal observation, original output
collection, full media validation, object publication and result transaction.
`generate` starts at the durable submission intent and ends only upon an observed
upstream terminal state. It includes submission transport and terminal polling
delay; it is **not** measured GPU denoise time. The ledger has no separate immutable
accepted-response timestamp. Recovery reconstructs the same original attempt
interval without resubmission. `stage.note(static_error_code)` emits a deduplicated
unknown observation while leaving that interval open; missing/lost responses do
not produce a false completion. Stage measurements can be repeated after a
process failure and are not exactly-once execution counters. `simulation=true`
marks CPU demo observations in logs/spans; they cannot qualify H3 performance.

Only named profile/provider/mode/GPU/warmth choices, trusted job/attempt/runtime
IDs and bounded numeric comparison dimensions are projected. There is no raw
context, exception capture, root logger attachment, HTTP auto-instrumentation,
automatic host/environment resource detection, prompt, user identifier, media,
filename, signed URL or header logging. IDs remain JSON log fields and span
attributes; never promote them to Loki index labels or metric labels.

Metrics are `sixnine.stage.started`, `sixnine.stage.finished` and
`sixnine.stage.duration` (seconds). Cloud's standard Prometheus translation yields
`sixnine_stage_started_total`, `sixnine_stage_finished_total` and the
`sixnine_stage_duration_seconds_{bucket,sum,count}` histogram family. Dimensions
are stage/provider/profile/mode/GPU/warmth and completion outcome only. The process
caps label sets at 128 and collapses excess combinations to `unknown`; each log
and span queue holds at most 256 items. Histograms use explicit buckets rather
than optional native histogram billing. Export timeouts are one second; SDK
shutdown is bounded. No local telemetry files accumulate during outages.

The HTTPS transport refuses redirects and discards response bodies/reasons and
exception text before they can reach SDK logs. `telemetry.status()` exposes only
static state, bounded label-set count and export attempt/failure counts. A
successful HTTP export/flush still does not prove every Cloud signal was ingested;
live acceptance requires reading actual logs, metrics and traces from the stack.

## Dashboard

Import `sixnine-generation-pipeline.json` into the existing Cloud Grafana, selecting
its Mimir/Prometheus and Loki sources. Use a separately scoped dashboard service
account only if API provisioning is necessary; never embed either token in the
JSON or give Grafana SQL access to business tables. The dashboard exposes cold vs
warm stage timings, safe failures and a per-attempt log timeline; use log trace IDs
in the existing Tempo source. Counter increases and percentiles need multiple
samples; a single acceptance run may appear only in logs at first. Telemetry
counts are not the bill, successful-job total or live capacity authority.

Check stack ingestion/series quotas and trial/retention settings before enabling
more services. Do not purchase an upgrade automatically. Direct SDK export is a
small initial deployment, with bounded loss during outages. If stronger delivery
becomes necessary, Grafana's recommended Alloy/Collector can relay the same OTLP
projection to Cloud without adding a self-hosted long-term telemetry database.

### Physical host observations (#177)

Import `sixnine-host-scorecard.json` into the same Cloud stack with its existing
Loki data source. It adds the **机器排行 · Sixnine Vast 宿主** dashboard alongside
the original pipeline dashboard. `machine_id` and allocation/job/attempt IDs are
parsed log fields only; never add them as Mimir metric or Loki index labels.
`host_allocation_observed` identifies the original rental and its typed provider
observation, not an accepted video or an invoice. The optional numeric search
filters physical host fields without creating a regional selection rule.

Generation and cold-phase medians remain separate, with model, mode, dimensions,
steps and cold/warm controls in comparison groups. Missing events show no data,
not zero. Recovery may repeat telemetry; the operator scorecard qualifies and
deduplicates samples against original successful attempts. The dashboard's log
counts/quantiles are diagnostics and must not be used as billing or automatic
denial authority. The console exposes the original-record failure rate and
estimated per-video cost, including measured cold-start amortization only when
its basis is available. Quotes are estimates; unsettled invoices remain unknown.

Host preference/denial is an audited human-admin operation in the original
operator policy/journal. Cloud has no write access to that policy or to the
business database. No dashboard API credential or additional access policy is
required when importing through the owner's existing logged-in Cloud session.

Official sources: [Cloud OTLP gateway](https://grafana.com/docs/grafana-cloud/send-data/otlp/send-data-otlp/),
[OTLP to Cloud signal mapping](https://grafana.com/docs/grafana-cloud/send-data/otlp/otlp-format-considerations/),
[Cloud access policies](https://grafana.com/docs/grafana-cloud/platform/security-and-account-management/security-and-access/authentication-and-permissions/access-policies/),
[Python SDK](https://opentelemetry-python.readthedocs.io/en/latest/sdk/_logs.html).

Local tests use fake Cloud transport and decoded real OTLP protobuf; they do not
certify real Cloud ingestion or production instrumentation. Cloud provisioning,
actual queries, service configuration and runtime startup integration remain the
root rollout scope. Worker tests exercise the original CPU video/audio and ledger
path, failure/recovery and unknown-response behavior; no paid generation is used.
