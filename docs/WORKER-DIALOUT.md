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
