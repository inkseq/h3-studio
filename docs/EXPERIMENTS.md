# Experiment log

One place for every GPU, provider, model and quality experiment, so nobody repeats one.

**Before you run anything:** search this file for the GPU, provider, model or setting.
- If a row already answers the question, reuse it.
- If you run it anyway, say what changed (driver, version, input).

**After you run anything:** append one row in the right section, newest first. Include:
- the date (AEDT);
- the exact setup;
- the numbers;
- the verdict;
- a link to the issue comment that holds the evidence.

Failed and abandoned experiments belong here too; they are the most useful ones to remember. Keep raw receipts, prompts and secrets out of this file.

## GPU providers

| Date | Provider / GPU | What was tried | Result | Verdict | Evidence |
|---|---|---|---|---|---|
| 2026-10-30 | Vultr (USD 260 credit) | Public plan API + gpuhunt via dstack 0.22.6 | Single-GPU plans:<br>• A40 48GB at $1.71/h, in stock (EWR)<br>• L40S 48GB at $1.54/h, 0 locations in stock<br>H100/A100/B200/L40S come only as 8-GPU bare metal ($16–63/h). GH200 is ARM; MI* are AMD. | Usable via the dstack `vultr` backend, but 3–5x the price of a 5090. Backstop or test use only. | #832 |
| 2026-10-30 | CloudRift RTX 5090 | Local dstack 0.22.6 server, on-demand fleet, `nvidia-smi` task | Got KR (ap_northeast_kr_se_1) at $0.65/h. Provision → running took ~8 min (VM + cloud-init). Driver 580.159.03, cc 12.0. | Works with upstream dstack (API fix #4380 is in 0.22.5+); no upstream PR needed. | #517 comment 6511340038 |
| 2026-10-30 | CloudRift volumes | Instance types API; dstack backend classes | `volume_types_per_dc` is empty in every DC with 5090 stock (KR, US-West). dstack CloudRift supports only instance (local-disk) volumes. | No shared model cache, so every new VM downloads the weights. KR download speed is still unmeasured; Vast KR was slow before. | this file |
| 2026-10-30 | Lium RTX 5090 | Same harness | Got ro-mh at $0.85/h. Provision → running took 107 s. Clean teardown, no leftover pods or templates. | Works with upstream dstack Lium backend (#4377, in 0.22.5+). It also supports S3-backed volumes. | #517 comment 6511340038 |
| 2026-10-30 | RunPod Serverless EU-RO-1 | Network volume (70 GB of weights) + enhance image | A cold worker spends ~170 s loading the model from the network volume, plus the image pull on a brand-new worker. FlashBoot keeps a recent worker at ~1 s delay. | Works. Presigned URLs must outlive the cold start (900 s max). | #795 comment 6510834485 |
| 2026-10-30 | Modal B200 | Four enhance runs queued | No B200 container after 42 min; the runs timed out. | Do not plan on Modal B200 capacity. | #795 |
| 2026-10-29 | Modal L40S (production backstop) | Real production job | Sandbox start ~2 min; job 2e461f8f settled at $0.79. | Live as backstop (guard v3). | #688 |

## Enhance / upscale (#795)

Setup unless stated: talk clip, 121 frames; ComfyUI v0.42.1 + Open-H3-Regenerate 9b686c5 + LBH upscaler 40316cf0; torch 2.10 cu130. "Std" = 2 steps σ0; "Enh" = 3 steps σ0.8. Times are warm run time.

| Date | GPU (arch) | Setting | Time | Frames | Verdict |
|---|---|---|---|---|---|
| 2026-10-30 | RTX PRO 6000 (sm120), RunPod | 1080 Std, sage | 56.5 s | clean | **Best fit for enhance.** |
| 2026-10-30 | RTX PRO 6000 (sm120), RunPod | 2K Enh, sage | 170 s | clean | ″ |
| 2026-10-30 | RTX PRO 6000 (sm120), RunPod | 2K Enh, sdpa | 271 s | clean | sage is faster and just as clean on sm120. |
| 2026-10-30 | RTX 5090 (sm120), RunPod | 1080 Std, sage | 80 s | clean | |
| 2026-10-30 | RTX 5090 (sm120), RunPod | 2K Enh, sage | ~150–190 s (353 s incl. cold load) | clean | |
| 2026-10-30 | RTX 5090 (sm120), RunPod | 2K Enh, sdpa | 925 s | — | Too slow (VRAM pressure). Never force sdpa on a 5090 at 2K. |
| 2026-10-30 | H100 (sm90), Modal | 1080 Std, sage | 84 s | clean | |
| 2026-10-30 | H100 (sm90), Modal | 2K Enh, sage | 189 s | **frames 94–118 pure noise** | sage is broken at 2K on sm89/sm90. |
| 2026-10-30 | H100 (sm90), Modal | 2K Enh, sage16 (int8 QK, fp16 PV, fp32 accum) | 267 s | **same noise** | The cause is int8 QK, not fp8 PV. |
| 2026-10-30 | H100 (sm90), Modal | 2K Enh, sdpa | 317 s | clean | |
| 2026-10-30 | H200 (sm90), Modal | 1080 Std / 2K Enh sage / 2K Enh sdpa | 108 s / 211 s / 311 s | clean / **noise** / clean | |
| 2026-10-30 | L40S (sm89), Modal | 1080 Std / 2K Enh sage (int8 and fp8 unet) / sage16 / sdpa | 137 s / 326–349 s / 436 s / 551 s | clean / **noise** / **noise** / clean | L40S is only usable at 2K with sdpa, and slowly. |
| 2026-10-29 | RTX 5090 (sm120), Lium | 1080 Std/Enh, 2K Std/Enh, sage2, int8 | 70 / 90 s, 145 / 199 s (fp8 2K Std 156 s) | clean; all 121 frames, incl. 94–118 (texture rechecked 10-30) | sm120 sage is clean at 2K (#795 comment 6496770435). |
| 2026-10-29 | RTX 5090 (sm120), Lium | 2K int8 on torch cu128 (eager int8 path) | — | OOM | Fixed by cu130 + sage2. |
| 2026-10-29 | RTX 5090 (sm120), Lium | Masked repaint of one detail (WanGP FL2VA `GVA` + mask, draft audio kept, denoise 0.75–1.0) vs a new prompt during enhance | — | — | A prompt during enhance cannot change content; masked repaint can (a badge was fixed). See #809. |
| 2026-10-29 | RTX 5090 (sm120), Lium | Dialogue: refine holding the draft audio vs silence | — | — | Lips are wrong without the draft audio (owner checked). Always hold it for dialogue. |
| 2026-10-29 | RTX 5090 (sm120), Lium | NVIDIA default start σ0.909 vs the 2-step 0.7 grid, 1080P | — | — | σ0.909 redraws faces (it also fixed a wrong badge); the 2-step grid keeps identity. Basis for Std / Enh. |

Resulting rule, in the image from PR #831:
- sage at 1080 on every GPU;
- sage at 2K on cc 12.x;
- sdpa at 2K on cc 8.9 / 9.0 and on any GPU not yet measured.

## Generation runtime (H3 / WanGP)

| Date | GPU | What was tried | Result | Verdict | Evidence |
|---|---|---|---|---|---|
| 2026-10-30 | RTX PRO 6000 Blackwell Server Edition (cc 12.0), Lium US $1.50/h | v8 stack (torch 2.10 cu130, triton 3.6, sage2 wheel) on WanGP 0e58385. Task settings exactly as the platform compiled them for the 5090 v8 jobs (seed 659). Two arms: as deployed (task `override_attention: sdpa`) vs sage2 (override cleared) | Warm, sdpa vs sage2: FL480 33.0 / 26.5 s, FL768 68.6 / 57.3 s, REF480 29.5 / 27.6 s. All 6 outputs have 124 frames, 0 low-texture frames, coherent to the end. ~35 min, ≈ $0.90 | PRO 6000 runs v8 cleanly in both modes; sage2 is ~17–20% faster at FL. Needs a session device check that admits PRO 6000 for the pruned model, and per-device attention resolution. | #794 |
| 2026-10-30 | RTX 5090 (Vast staging) and L40S (Modal staging) | REF café prompt with `ref_woman` (station-uniform portrait), seed 659: v8 (4890c42e) vs v7 (d3d6adb2) | Both mix the reference's station corridor into the café, burn the spoken line in as a subtitle and add a look-alike in the background | Comes from the REF recipe and prompt, not the runtime. Owner: ship v8 as is. | #794 comment 6508991386 |
| 2026-10-30 | RTX 5090, Vast staging | v8 platform acceptance through the card API (qa-admin, seed 659): FL 480P, FL 768P, REF 480P | FL480 dcf1c6f7 ✅, FL768 aa1ce763 ✅. REF on the first v8 image 87dde957 ❌ `wangp_generation_unclassified`: the SSH-started worker lacks image `ENV`, so Qwen3-VL NVRTC could not find `libnvrtc-builtins.so.13.0`. v8b with `ldconfig` 4890c42e ✅ | PASSED. Register cu13 libs with `ldconfig`, not just `LD_LIBRARY_PATH`. | #794 comment 6502716966, #827 |
| 2026-10-29 | RTX 5090, Lium (driver 595.112.07, CUDA 13.1) | WanGP 0e58385: torch 2.13 cu132 + triton 3.7.1 (sdpa, then self-built sage2 for sm_120) vs torch 2.10 cu130 + prebuilt sage2 | 480P / 768P / REF 768P: cu132 sdpa 28 / 85 / 109 s; cu132 + sage2 23.2 / 55 / 68 s; cu130 + sage2 23.5 / 55 / 68 s | CUDA 13.2 gives no gain, so v8 = torch 2.10 cu130 + the deepbeepmeep wheel. A cu132 sage2 build needs `nvidia-nvvm` pinned to 13.2.x, a `libcudart.so` symlink and a `lib64 -> lib` link. | #794 comment 6492040849 |
| 2026-10-29 | RTX 5090, Lium | WanGP 0e58385 runtime matrix (round 19): sage2 on the production cu128 image; cu130 + triton 3.6 + sage2 (+ Sol); torch compile; profile 3 | sage2 on cu128: 768P 93 → 62 s, REF 117 → 75 s. cu130 + sage2 + Sol: 480P 22 s, 768P 48 s, REF 66 s. Compile and profile 3: no gain | Dense configs keep a near-identical composition per seed; Sol arms draw differently. Owner: no sparse attention. | #794 comment 6490515354 |
| 2026-10-30 | L40S (sm89), Modal staging | v8 runtime (int8, cu130) FL 480P, FL 768P and REF 480P, seed 659, through the real card API. Attention was **sdpa**: the platform compiler sends `override_attention: sdpa` with every task and WanGP gives that priority over the profile's sage2 (`wgp.py:9841`) | 124 frames each, 0 bad frames, coherent to the end | v8 with sdpa is clean on sm89. Says nothing about sage2 on sm89. Production v8 has therefore run sdpa everywhere so far; the sage2 speed-up still needs the fix (#794). Basis for the production v8 switch (#794 comment 6511914037). | staging jobs 3f173359, d4018a33, 1fc1fd22 |
| 2026-10-29 | RTX 5090, Lium | cu130 + sage2 vs the previous runtime | 768P FL 93 → 55 s, REF 117 → 68 s | Basis for v8. | #794, `deploy/dstack/Dockerfile.wangp-cu130` |
| 2026-10-29 | RTX 5090, Lium | ComfyUI v0.42.1 native H3 (not WanGP), 768P T2V, TaoMate 5 steps: torch cu128 → cu130 → + Sol-Attn → + NVIDIA fast VAE; fp8_scaled; TorchCompile | 119 → 74 → 50 → 47 s; fp8 dense 78 s (slower than int8); TorchCompile fails under dynamic VRAM | ComfyUI disables comfy-kitchen CUDA ops below cu130. Sol is sparse: owner rejected it for production. | #794 issue body |
| 2026-10-08 | RTX 5090 / PRO 6000 | Native profile matrix (int8/bf16, 20/50 steps, 480/768) | Per-sample table | Historical baseline. | `docs/research/h3-economics-20261008/runtime-profile-measurements.md` |
