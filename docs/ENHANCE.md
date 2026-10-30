# Enhance a finished take to 1080P / 2K (#795)

Owner 2026-10-30: build enhance end to end now; prices fixed on #795; keep the ComfyUI pipeline (#866 A/B).

## User flow
1. On a finished video take: **提升清晰度 / Enhance**, then choose 1080P or 2K.
2. The confirmation shows the credits **usage bar only**, never a per-clip price.
3. Progress is shown while it runs. The result is a new version next to the original, which is kept; the user can switch between them.

## Shape: a job like chapter render, with a serverless backend
Enhance reuses the existing plan → job → worker → collection path, so holds, capture, release, spending limits, the content policy, output validation and the artifact writer stay the reviewed code. Only these parts are new:

| piece | where | notes |
|---|---|---|
| recipe `h3-enhance-v1` | `enhance_plans.py` | Body `{client_ref: {project_id, entity_id}, tier: "1080p"\|"2k"}`. The source is resolved with the render source resolver: owner/project checked, immutable object, duration and size. Output spec: 1920×1080 or 2560×1440, same frames and fps as the source |
| plan route | `POST /v1/enhance-plans` | Same as `/v1/render-plans`: compile → policy → `credits_pricing.quote` → `create_plan`. The job then starts through the normal `/v1/jobs` with `plan_id` |
| price | `credits_pricing` | Enhance branch (owner 2026-10-30): 1080P 100 credits per 5 s clip +10 per extra s; 2K 150 +30. Rounded up per started second. Never shown as money |
| policy | `execution_policy.evaluate` | Pool `enhance`, backend `runpod-enhance`. Blocked unless `settings.enhance_enabled` and an enhance worker is registered |
| backend | `enhance_backend.py` | RunPod Serverless: `prepare` presigns the source GET and a temp output PUT; `submit` → `/run`; `poll` → `/status`; `cancel` → `/cancel`; `fetch` downloads the temp output into the target dir. The worker never holds storage or RunPod credentials |
| worker | controller | One CPU `WorkerRunner` slot on pool `enhance` with the `runpod-enhance` backend, registered while `enhance_enabled` |
| frontend | studio-app → inkseq-web | Enhance action, usage-bar confirm, progress, version switcher |

## Worker contract (exists: `deploy/enhance/handler.py`)
- Input: `{video_url, res: "1080"|"2k", steps, sigma, audio: "draft", unet: "int8", output_put_url}`.
- Output: `{seconds, bytes, uploaded: true}`.
- Tiers:
  - 1080P = steps 2, sigma 0;
  - 2K = steps 3, sigma 0.8.
- Attention: sage on sm120, otherwise sdpa at 2K (sage2 int8 QK blurs on sm89/sm90).

## Lanes
- Primary: RunPod Serverless RTX PRO 6000. Network volumes in 2–3 datacenters; idle timeout 60–120 s.
- Second: Modal RTX PRO 6000, after it is measured.
- 5090: 1080P only.
- H100/L40S: backstop only, sdpa at 2K.

## Safety
- The source must be the caller's own immutable video, size-capped like render (≤ 512 MiB).
- The output is validated as for generation: dimensions, duration within a frame, audio present. It is written by the artifact writer under a fixed key.
- Failure or cancel releases the hold; success captures. Uncertain RunPod outcomes keep the original RunPod job id and never resubmit blindly.
- Supplier cost per job comes from the RunPod status (`executionTime` + `delayTime`) and is logged for margin review against the ≥ 2.5x cold-start basis.

## PR order
- A: recipe, plan route, price, policy (flag off).
- B: RunPod backend and controller slot.
- C: frontend.
- D: staging acceptance (1080P and 2K, frame check, credit and RunPod cost reconciliation), then production.
