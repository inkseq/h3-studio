# GPU worker dial-out (#859)

Owner decision, 2026-10-30: nothing connects into a GPU host. The worker starts itself from the dstack run and dials out to the platform. dstack (and later the serverless lane) only rents and starts the container.

## Shape

```
CPU WorkerRunner ──http://127.0.0.1:<port>──┐  (unchanged: claim, fence, settlement, delivery)
                                            │
platform host: chisel server (app netns) ◄──┘  reverse remote R:127.0.0.1:<port>
     ▲  wss via Caddy :443 /v1/worker-tunnel
     │
GPU container: chisel client ──► local runtime 127.0.0.1:8199+slot, boot status 127.0.0.1:8198
```

The CPU keeps talking HTTP to `127.0.0.1:<port>`, exactly as it does through today's SSH tunnel, so `worker.py` does not change. Only the boot and transport layer in `dstack_runtime.py` and `wangp_bootstrap.py` changes.

Why chisel: MIT licensed; WebSocket over our existing :443; per-user allowed-remote regexes with live authfile reload; and the client pins the server fingerprint. Compared in #859: frp, rathole, `ssh -R`, and the dstack service gateway (measured limits of 64 MB, 300 s, and token forwarding).

## Today's SSH operations and their replacement

| today (controller SSH) | dial-out |
|---|---|
| SFTP the source set (`wangp-bootstrap.py`, manifest, runtime json, package tarball) | The worker GETs each file by presigned S3 URL and checks it against the sha256 from the binding |
| write `wangp-token` once (first bootstrap) | Minted on the CPU before `apply`, passed in the run `env`; the boot script writes it 0600, once |
| run the start snippet (identity marker, lock, `Popen` bootstrap) | The same steps in a pinned boot script run by the dstack `commands` |
| `report()` / system observation / `preparation_idle_report()` over SSH exec | Token-protected boot status server on `127.0.0.1:8198`, reached through the tunnel. It serves the same JSON documents |
| `open_tunnel(8199+slot)` | chisel `R:127.0.0.1:<allocated port>:127.0.0.1:<8199+slot>` (the allocated local port is unchanged) |
| host-key pinning (`known_hosts.<intent>`) | The client pins the server fingerprint. The server admits only that intent's one-time user, limited to `^R:127\.0\.0\.1:<its ports>$` |
| provider identity | Unchanged: dstack JPD plus the provider API |

## Failure visibility (owner question, 2026-10-30)
- **Container or image failures:** reported by dstack run status, as today.
- **Check-in deadline:** if the tunnel user has not connected and `/boot/status` is not readable by the existing startup deadline, the result is `capacity_startup_timeout`, then stop and replace.
- **Boot progress:** the boot status server reports the phase, the file download progress and the tail of the last error.
- **Unreachable worker:** the dstack logs API shows the container stdout/stderr.
- **Running worker:** the existing readiness polling through the tunnel. A lost tunnel means a lost heartbeat, handled by the existing lease and fence rules.

## Order (one PR each, staging first)
1. #860: inputs by signed URL (done).
2. chisel server sidecar plus the Caddy route; the app writes and removes authfile entries per intent.
3. Worker boot script and boot status server; the source set is published to S3 at release.
4. `DstackNativeRuntime` gains the dial-out transport behind a per-profile flag; staging acceptance on Vast, Lium and CloudRift.
5. Default to dial-out, then delete controller SSH, the #858 jump hop and host-key pinning.
6. Serverless lane: Modal and RunPod Serverless start the same worker and dial out the same way.

## RunPod Serverless lane

The serverless lane (order step 6) rents through RunPod Serverless instead of dstack. Everything above the client is unchanged: demand start, budget, the test guard, dial-out boot, readiness and idle stop all treat it as one more backend.

- **Controller.** `studio_platform/runpod_serverless.py`:
  - `RunPodServerlessClient` implements the dstack client calls the capacity path uses. `plan` offers the endpoint's GPU, `apply` POSTs `/run`, `get` reads `/status`, `stop` POSTs `/cancel`, and `provider_inventory` cross-checks live jobs against `/health`.
  - A private journal binds each run name to its run id, job id and exact spec. A job whose id was never journaled reads as unknown, never absent. A terminal status is journaled once, because RunPod expires job status.
  - `RoutedClient` sends `backend: "runpod-serverless"` runs there and everything else to dstack.
  - It is enabled by the operator config key `serverless_file`, a protected JSON file.
- **Worker.** The endpoint template runs the same pinned WanGP image. Its start command installs the pinned RunPod SDK and runs `deploy/runpod-serverless/handler.py`. Each job input is the run spec's single boot command plus its three boot variables, and the handler refuses anything else. The job lives until the controller cancels it.
- **Endpoint.** `tools/runpod_serverless_endpoint.py`:
  - scales to zero (workersMin 0) and requires a CUDA 13 driver (`minCudaVersion: 13.0`) for the cu130 image;
  - takes the API key from the central registry and never prints it.
- **Profiles.** `backend: "runpod-serverless"` with `transport: "dialout"`. Jobs bill to the RunPod provider account.
- **Local check.** `test_dialout_e2e.test_runpod_serverless_job_boots_through_the_real_handler` runs the real handler in a container behind a fake RunPod API, through `DstackCapacity.observe`, then cancels the job.

## Modal lane (dial-out)

`studio_platform/modal_dialout.py` (backend and provider `modal-dialout`) runs the same worker in one Modal sandbox per run.
- It reuses the RunPod Serverless client's journal, dstack-shaped runs, inventory cross-check and routing. Only the provider calls change: create a sandbox, poll, terminate, and list the app's sandboxes.
- The verified model Volume is mounted at `/ckpts`, and the worker gets `INKSEQ_MODEL_CACHE=/ckpts/ckpts` (#924). A cold start skips the download, and `verify_runtime` still hashes every file.
- It is enabled by the operator config key `modal_dialout_file`, a protected JSON file. The Modal SDK loads on first provider use only.
- The older Modal lane (provider `modal`) is untouched and stays available until this lane is accepted.

## Local end-to-end check

`tests/test_dialout_e2e.py` runs the whole path on one machine in about 30 seconds, without renting a GPU:
- the real app (login middleware and boot route);
- Caddy with the staging tunnel route;
- the controller's real chisel server and grant writer;
- the run spec's own boot command in a plain `python:3.12-slim` container.

It passes once the controller reads the worker's report through the tunnel. Only dstack, object storage and the GPU are fakes. It needs Docker and the pinned chisel 1.12.0 binary (checksum in `Dockerfile.dispatch`):

```bash
INKSEQ_DIALOUT_E2E=1 INKSEQ_E2E_CHISEL=~/bin/chisel PYTHONPATH=tests:. python -m unittest test_dialout_e2e -v
```

Run it before each dial-out change goes to staging. On WSL with Docker Desktop, the test binds the chisel server to `127.0.0.1`, because WSL does not relay Go's dual-stack listener to containers.
