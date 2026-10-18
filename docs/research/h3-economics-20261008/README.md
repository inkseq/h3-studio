# H3 deployment, capability and GPU economics research

Research date: **2026-10-08**. Owner: [#66](https://github.com/apedintensor/h3-studio/issues/66), under runtime work package [#4](https://github.com/apedintensor/h3-studio/issues/4). Sources were read, not executed. No GPU was rented, model downloaded, generation submitted or deployment changed for this research.

This is a decision input, not a new architecture or an accepted runtime switch. [DEC-002/003](../../DECISIONS.md) remain in force. Our existing WanGP adapter, upstream advertised support, a measured recipe and a currently enabled product feature are four separate things.

## 1. What the evidence supports

- A 5090 can run useful H3 configurations. It is a credible low-cost candidate, particularly for text-only accelerated output. It is not proof that every maximum-size reference combination fits.
- A 96GB RTX PRO 6000 Blackwell is a strong candidate for broader controls and lower offload pressure. Its capacity advantage does not imply a threefold speed advantage over a 32GB 5090.
- H100, H200, B200 and B300 are conditional choices for latency, larger envelopes or proven parallelism. No evidence justifies B200 as an unconditional default.
- The fastest public numbers generally use text-only inputs and changed sampling/model components. They must not price full multimodal REF jobs.
- There is **no located six-GPU, same-recipe, same-quality, complete-service benchmark**. This research narrows candidates; it does not establish a long-term cost winner.
- Fixing repeat preparation is economically material. One recent B200 REF attempt spent 26.98 minutes before runtime submission and 7.35 minutes in generation/collection. Paying for faster silicon does not remove downloads and installation.

## 2. What “full functionality” means

Use separate acceptance dimensions: (a) inputs and controls, (b) numerical/quality recipe, (c) maximum shape/reference envelope, and (d) service reliability. Passing one does not pass the others.

Official H3 Base has distinct FL2VA and Ref2VA transformers. FL covers text and first/last frames; REF covers image/video/audio references. The published reference limits are 9 images, 3 videos and 3 audios, at most 12 files; video and audio each have a 15-second total budget, with individual clips 2–15 seconds. Context-IR orchestration and Regenerate-2K are separate hosted components, not a complete local Base release. [MiniMax official source](https://github.com/MiniMax-AI/MiniMax-H3)

An API accepting an image argument does not prove its distilled checkpoint was trained for conditioning. Native generated audio is also different from accepting an audio reference or preserving a supplied soundtrack.

### Model / acceleration families

| Family | Published task scope | Benefit | Limitation requiring acceptance |
|---|---|---|---|
| Official Base, original precision | FL and REF with matching components | Best control/quality reference | Largest memory/compute footprint; serving wrapper still needs complete mapping |
| CPU/layer offload | Placement change, not intentional removal of modalities | Fit smaller VRAM | RAM, PCIe, CPU and disk can dominate; not a free latency improvement |
| INT8 / FP8 / NVFP4 / GGUF | Depends on the exact FL/REF checkpoint and kernels | Smaller weights, sometimes faster compute | Precision and backend compatibility; quantization alone does not prove speed or quality parity |
| AdaLN-pruned exports | FL and REF exports exist | Smaller modulation representation | Approximate curve-AdaLN; not bitwise identical |
| Fixed-schedule AdaLN cache | Original modulation outputs at specified timesteps | Avoid recomputing/loading large branches | Only the explicitly supported schedule; not the same operation as pruning |
| LightX2V Turbo | Separate FL and REF 4/8-step files | Few-step generation with a REF candidate | Match task, revision, resolution training and sampler; not one universal LoRA |
| PAI PDD acceleration | Separate FL and REF 8-step adapters | Another broad-control acceleration candidate | Matching inference/output-head/sampling implementation; all controls still need qualification |
| Larry Turbo v4 | Documented FL/T2VA acceleration | Few-step candidate | No established full REF parity in this research |
| VDN-H3 | FL/T2VA; experimental upstream image-only “REF-like” mode | Hybrid attention and few-step inference | REF-like uses FL weights, not native multimodal Ref2VA; current SGLang VDN path rejects REF |
| FastH3 Preview v1 | T2VA only, four DiT evaluations | Very low text-generation latency | No distilled FL or REF; task-specific attention backend |
| FastH3 V2 Consumer | T2VA only, eight steps | Very fast single-card candidate | Quantization, sparse attention and replacement lightweight video VAE |
| FastH3 Trim | Experimental eight-step T2VA | Lower footprint and further speed | Removes 8 of 50 blocks plus low-rank modulation; detail/quality tradeoff |
| Sol / sparse attention / caching | Engine and selected recipe dependent | Accelerates attention, communication or repeated computation | Runtime support for REF does not establish REF support of an optional T2VA-only four-step adapter |

Sources: [AdaLN-pruned author](https://huggingface.co/multimodalart/MiniMax-H3-Pruned), [LightX2V code](https://github.com/ModelTC/Minimax-H3-Turbo) and [files](https://huggingface.co/lightx2v/Minimax-h3-Turbo/tree/main), [PAI model card](https://huggingface.co/alibaba-pai/MiniMax-H3-Acc-LoRAs), [Larry model card](https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora), [OpenVDN](https://github.com/OpenVDN/vdn-minimax-h3), [FastH3 V1](https://huggingface.co/FastVideo/FastVideo-FastH3-4-step-Preview-v1-VSA-DataFree), [V2 scope](https://huggingface.co/FastVideo/FastVideo-FastH3-8-Step-V2), [Consumer components](https://huggingface.co/FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer), [Trim](https://huggingface.co/FastVideo/FastVideo-FastH3-Trim-8-Step-NVFP4).

Correction to an earlier conversation: precomputation does not establish that every file called `pruned` is lossless. The pruned author's curve approximation, a fixed-timestep cache and Trim's removal of transformer blocks are different techniques. Also, some current upstream implementations combine REF with keyframe guides; the old UI's mutual exclusion is not a universal H3 law.

### Runtime/API choices

| Runtime | What is useful for Sixnine | Boundary / unresolved control coverage |
|---|---|---|
| WanGP | Existing selected adapter; memory scheduling, broad creative workflows | Keep pinned version; current main's new MMGP and reference features are not our deployed version |
| ComfyUI | Explicit FL/REF graphs, media VAEs, arbitrary-time guides and latent masks | A graph runner needs our API, immutable recipes and recovery; custom-node availability is not product acceptance |
| SGLang Diffusion | Native serving, topology/precision choices, documented hybrid REF plus keyframes | Not every Comfy/WanGP control has an equivalent; capability depends on recipe |
| vLLM-Omni | Serving lifecycle and shared components | Current REF endpoint accepts one-image-plus-one-audio OR video references without separate audio; one request per diffusion batch |
| Diffusers ModularPipeline | Explicit Python workflows and component control | We must provide durable jobs, cancellation, storage and recovery around it |
| FastVideo / Sol / OpenVDN | Specialized optimized inference paths | New adapters would need the same Sixnine lifecycle gates; benchmark speed is not service readiness |

Sources: [WanGP](https://github.com/deepbeepmeep/Wan2GP), [Comfy native tutorial](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-native), [SGLang](https://docs.sglang.io/cookbook/diffusion/MiniMax/MiniMax-H3), [vLLM-Omni limitations](https://recipes.vllm.ai/MiniMaxAI/MiniMax-H3#known-limitations), [Diffusers](https://huggingface.co/docs/diffusers/main/en/api/pipelines/minimax_h3).

ControlNet, pose/depth/edge conditioning, inpainting/outpainting and third-party upscalers can be framework extensions with extra weights. Do not describe all such extensions as native Base API features. No single candidate above has established parity for every product control.

## 3. GPU capabilities and dated prices

USD per GPU-hour. Lium values below are the saved **2026-10-08T05:52:09.813Z** feed snapshot (the exact original timestamp is in [JSON](lium-prices.json)); Runpod is its public Pods card read on October 8, page updated September 27. These are asks/card prices, not reservations, contractual quotes or runtime-qualified offers.

| GPU | Memory; indicative bandwidth | Lium snapshot | Runpod Pods card | Practical distinction |
|---|---|---:|---:|---|
| RTX 5090 | 32GB GDDR7; 1.792TB/s | $0.59–0.85 | $0.99 | Lowest rental floor here; offload/host configuration can limit full Base and references |
| RTX PRO 6000 Blackwell Server | 96GB ECC; 1.597TB/s | $1.19–1.69 | $2.09 (edition not distinguished) | More working memory; does not have datacenter NVLink topology |
| RTX PRO 6000 Blackwell Workstation | 96GB ECC; 1.792TB/s | $1.45 | Not separately quoted | Keep distinct from Server, Max-Q, Ada and MIG partitions |
| H100 PCIe | 80GB; SKU-dependent bandwidth | $1.95–2.05 | $2.89 | Not equivalent to H100 SXM or NVL |
| H100 HBM3 / SXM-class | 80GB; SXM 3.35TB/s | $1.85–2.75 (feed says HBM3) | $3.49 SXM | Verify actual interconnect/SKU; Lium label alone is not topology proof |
| H200 | 141GB HBM3e; 4.8TB/s | $5.75 | $4.59 | More capacity/bandwidth; availability can erase nominal price advantage |
| B200 | 180GB offered; up to 8TB/s | $5.50–5.60 | $6.79 | Large resident recipes and NVLink parallelism; high cold/idle cost |
| B300 | 288GB; up to 8TB/s | $12.95 | $7.89 | Extra capacity; not automatically cheaper or twice as fast as B200 |

[Lium feed](https://lium.io/pricing.json), [feed semantics](https://docs.lium.io/developers/pricing-feed), [Runpod pricing](https://www.runpod.io/pricing). Lium H200 NVL $2.90 and H100 NVL $1.11 had **zero availability** and are reference prices, not available bargains. Cached webpage and JSON prices differed during this research; use the saved snapshot rather than mixing minima from different moments. GB300 is not the same deployment as B300.

Public node metadata was also filtered for `min_rentable_gpu_count=1`, recorded without node identifiers or addresses in [single-allocation offers](lium-single-allocation-offers.json). This checks allocation granularity only. It does not verify actual allocated host RAM, P2P, bandwidth, model cache, disk performance, availability at checkout or H3 compatibility. Whole multi-GPU pods may require paying for all GPUs.

NVIDIA specifications: [5090](https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/), [PRO workstation](https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-6000/), [PRO server](https://www.nvidia.com/en-gb/data-center/rtx-pro-6000-blackwell-server-edition/), [H100 variants](https://www.nvidia.com/en-us/data-center/h100/), [HGX H200/B200/B300](https://docs.nvidia.com/enterprise-reference-architectures/hgx-ai-factory/latest/components.html). Bandwidth/TFLOPS are explanatory specifications, not a way to extrapolate H3 latency. Current Vast prices were not reliably extracted from its dynamic [official price page](https://vast.ai/pricing); no third-party “from” price is substituted.

## 4. Public measurements: compare within a cohort

Every row below is an external author's measurement, not a Sixnine test. Millisecond-looking precision does not imply a large sample. “Five seconds” is often 124/24 = 5.167 seconds; 345 frames is 14.375 seconds, whereas 362 frames is 15.083 seconds. Preserve native ending semantics when comparing.

### A. FastH3 V2 / Trim consumer recipe

Eight steps, NVFP4, native audio, 832×480 or 1344×768, approximately five-second outputs. Warm prompt-to-MP4 timing, median over two runs on each of two prompts.

| One GPU | V2 480p | V2 768p | Trim 480p | Trim 768p |
|---|---:|---:|---:|---:|
| 5090 | 14.8s | 38.6s | 13.4s | 35.4s |
| PRO 6000 96GB | 13.5s | 36.5s | 12.0s | 32.5s |

Both consumer packages use a lightweight LynnReal video decoder with INT8 weights, rather than the original H3 video VAE. Trim additionally removes blocks. These are T2VA cost candidates, not full-reference measurements. The author's favorable quality claim does not replace our visual/audio acceptance. [Author benchmark, October 6](https://haoailab.com/blogs/fasth3-rtx/)

### B. One identical Comfy graph on different rented hosts

Comfy 0.35 / Torch 2.9 CUDA13, INT8 DiT and encoder, original video FP16/audio FP32 VAEs, 20 steps, 864×480, 124 frames, T2VA, no LoRA. Warm means mean of runs 2–3 with cached conditioning; fresh prompts add encoding time.

| GPU/host | Warm execution | Historical all-in hourly price | Warm derived cost | Whole three-clip rental cost per clip |
|---|---:|---:|---:|---:|
| 5090 Community | 68.7s | $0.707 | $0.0135 | $0.049 |
| 5090 Secure | 100.2s | $1.007 | $0.0280 | $0.083 |
| PRO 6000 Blackwell | 46.9s | $2.107 | $0.0275 | $0.164 |
| H100 PCIe | 67.2s | $2.007 | $0.0375 | $0.127 |

The slower 5090 spent 37.5s in CPU MP4 encoding. Evidence is a small September 11–13 campaign by an interested service operator; useful, not independent large-scale validation. [QRUN first-hand measurements](https://qrun.cloud/measurements), [tester discussion](https://www.reddit.com/r/comfyui/comments/1we5nzn/minimax_h3_on_rented_gpus_measured_seconds_and/).

### C. SGLang qualified examples

| Hardware / recipe | Shape / schedule | Published seconds | Timing boundary |
|---|---|---:|---|
| 1×5090, original BF16, tuned offload | 864×480,124f,20 steps | 112.1–112.2 | Request; specialized RAM/swap setup |
| 2×5090, original BF16/FP32, TP2/offload | 1344×768,124f,50 steps | 559.67 | Reported total; denoise525.05/decode33.61 |
| 4×H200, resident Base | 1344×768,124f,50 steps | 74.38 | Warm end-to-end |
| 1×PRO 6000 Server, VDN MXFP8 | 1344×768,345f,8 NFE | 180.6 | Denoise160.0 + decode20.6 only |
| 1×B200, VDN MXFP8 | Same VDN workload | 56.3 | Denoise47.6 + decode8.7 only |

Only the final two rows compare a matched acceleration workload. None proves REF timing. [Pinned SGLang cookbook](https://github.com/sgl-project/sglang/blob/ac656ee79af3fbf40778189eb537d357be56c3e4/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx)

OpenVDN separately reports single H200 FP8 **90.5s denoising only** for 345-frame/768p/8-NFE output; it excludes loading, warmup, decoding and MP4. Do not compare 90.5 directly against 56.3 as an end-to-end ratio. [OpenVDN results](https://github.com/OpenVDN/vdn-minimax-h3#results)

### D. H100 engine optimization

NVIDIA's four-H100 study reports 81.47s dense versus 22.89s optimized for BF16, 1344×768,124 frames,50 steps and audio. Optimization includes sparse attention and FirstBlockCache, not just exact kernels. Exact H100 form factor is absent. This is neither a single-H100 result nor an all-controls quality proof. [NVIDIA experiment](https://nvlabs.github.io/Sana/Sol-Engine/H3-DataCenter/)

### E. B200 / B300 parallel examples

B200 author study: warm full MP4/file timing; one warmup then three-run median, 1344×768, audio. Base uses 49 DiT evaluations, V1 uses four.

| B200 count | Base,124f | FastH3 V1 VSA,124f | Base,345f | V1 VSA,345f |
|---|---:|---:|---:|---:|
| 1 |132.5s|16.2s|678.7s|47.2s|
| 4 |40.6s|6.1s|193.1s|15.5s|
| 8 |Not quoted here|6.84s|Not quoted here|12.88s|

More GPUs did not improve every short request. [FastVideo Preview v1 benchmark](https://haoailab.com/blogs/fasth3-preview/)

B300 NVIDIA study: 1344×768, audio, T2VA, matched prompt/seed, three-run median after warmup. Includes encoder/denoise/VAEs but **excludes final MP4, loading and compilation**.

| B300 count | Base50,124f | Sol4,124f | Base50,362f | Sol4,362f |
|---|---:|---:|---:|---:|
| 1 |129.898s|11.484s|746.885s|50.707s|
| 4 |35.328s|2.918s|194.930s|12.542s|
| 8 |18.250s|1.653s|99.513s|6.612s|

Sol4 uses a different few-step recipe; multi-GPU rows additionally use sparse attention and compressed communication. Do not rank it directly against B200 VSA with different duration/export boundaries. [NVIDIA Sol-H3](https://nvlabs.github.io/Sana/Sol-Engine/Sol-H3/)

Eight cooperating GPUs accelerate one request; eight independent replicas serve eight requests. Neither arrangement guarantees linear throughput. Two independent nodes also have redundancy value that one multi-GPU host does not provide.

## 5. Our historical measurements and their limits

Protected local receipts were read without cloud calls. Paths are operator evidence references, not files assumed present in a fresh clone. No user prompts, media, credentials or signed URLs are included here.

| Hardware / recipe | Output | Measured execution | Scope |
|---|---|---:|---|
| 5090 INT8 DiT/NVFP4 encoder, Base20 |768p124f audio|265.338s|Comfy execution start→success |
| Same5090, Turbo8 |768p124f audio|113.976–114.413s|Same phase; sampler/LoRA differs from Base |
| Same5090, Turbo8 first image |768p124f audio|126.719s|One I2V example, not REF |
| Same5090, Turbo8 |768p362f audio|742.874s|15.083s output |
| Same5090, Turbo8 |480p362f audio|157.520s|Lower resolution; not comparable quality |
| PRO6000 BF16 Base20 |768p5s, assorted input modes|339.11–374.58s|Load/encode/infer/decode/output; excludes initial preparation |
| PRO6000 BF16 FL50, CPU encoder |768p124 native→120 delivered|557.222s mean|8 tasks, two separate single-GPU workers; not tensor parallel |
| H10080 BF16 FL50, CPU encoder |768p124f audio|1105.1s|Qualification submission→full validation; cold/mixed boundary |
| B200 WanGP BF16 FL50 SDPA |480p124 native→120 delivered|123.944s warm;380.584s first|Same worker, not a 768p comparison |
| B200 WanGP BF16 REF50, one image |480p124f native audio|440.815s runtime→persist|Plus1618.526s preparation; next video-reference attempt failed |

Evidence locations under `C:/Users/danmo/Desktop/inference/`:

- `h3-benchmark/REPORT.zh-CN.md`: initial September20 runs, exact hardware and component sizes.
- `h3-extended/REPORT.zh-CN.md:17–63`:14 successful runs, timings, I2V and15-second outputs. This expands the earlier limited T2VA report; no5090 full-REF proof.
- `h3-studio/DEPLOYMENT.zh-CN.md:18–72`, `RESULTS.zh-CN.md`:October3 BF16 modes including actual video/audio reference encoding. Old Comfy evidence does not qualify the newer WanGP adapter.
- `h3-studio/.platform-gpu-live/GPU-ACCEPTANCE.zh-CN.md:16–54`:October4 PRO6000 batch, historical$1.29/h and final rental statements.
- `h3-studio/.platform-demand-live/OVERNIGHT-FIX-20261005.zh-CN.md:19–53`:H100 mixed-phase timings.
- `h3-studio/.architecture-research/b3-warm-idle-checkpoint-20261008.md`:B200 warm request and whole rental.
- `h3-studio/.architecture-research/ref-owner-proof-20261008/github-image-video-evidence.md`:latest image-reference timestamps and removed-pod final charge.

Historical5090 rent was$0.65/h; PRO6000 runs used$1.46/h or$1.29/h; B200 final receipt implies$5.60/h. Do not relabel these as today's prices. REF pod final cost$3.422932842 covers2200.456827 billed seconds, including one successful image-reference attempt and one failed video-reference attempt. Preparation is78.594% of the successful attempt's accepted→persist elapsed time. Internal quota reservations are not extra supplier invoices.

Current acceptance state remains in [CURRENT-BASELINE.md](../../CURRENT-BASELINE.md), not this historical performance table. The local preserved checkout may contain an older baseline; research was based on remote main `9da1541` plus the explicitly dated receipts. No new visual/audio quality benchmark was run.

## 6. Cost calculation

Use **total paid cost per technically successful and quality-accepted output**, separately by task class. A sample that decodes but misses a character, endpoint or spoken line is not economically equivalent to a usable sample.

For a measured rental session:

```text
session_cost = sum(all actual billed node charges)
             + storage + transfer + CPU service + paid preprocessing
cost_per_usable_output = session_cost / number_of_usable_outputs

planning approximation for one slot:
slot_hourly_price = sum(hourly prices of all GPUs/nodes required for that slot)
cost_per_usable_output ≈ slot_hourly_price * mean_service_seconds
                         / (3600 * utilization * technical_success * quality_pass)
```

Here `technical_success` is the fraction of attempts that produce valid deliverables, and `quality_pass` is the acceptance rate **among those technically successful outputs**. The approximation assumes failed and accepted attempts have similar average service time; prefer measured totals when they do not. Utilization includes paid preparation and idle overhead, so **do not add those costs twice**. Model parallelism uses the entire slot price. Storage/egress/CPU/preprocessing are additional terms. Queue waiting costs GPU money only while a GPU is billed; it still costs user latency either way.

### What today's prices imply, before cold/idle/quality costs

These are arithmetic scenarios using external timings at other hosts, not measured Lium performance. Hardware edition and host equivalence remain unverified.

| Scenario | Price assumption | Execution-only estimate |
|---|---:|---:|
| FastH3 V2 5090,768p,38.6s |$0.59–0.85/h|$0.00633–0.00911/output |
| V2 PRO6000,768p,36.5s |Workstation$1.45/h proxy|$0.01470/output |
| VDN PRO6000 Server,180.6s |$1.19–1.69/h|$0.05970–0.08478/output **for two phases only** |
| VDN B200,56.3s |$5.50–5.60/h|$0.08601–0.08758/output **for two phases only** |

In the VDN pair, B200 is3.208× faster for those phases, but costs3.25–4.71× as much per hour across these asks. It therefore does not establish cheaper generation. With PRO at$1.19/h, equal phase-cost requires B200 at approximately$3.817/h. With B200 at$5.50/h, PRO's break-even rate is approximately$1.7145/h. Full pipeline overhead can change this conclusion.

For text-only V2, the38.6s versus36.5s gap is just1.058×; an expensive card needs extra throughput, lower failures or a larger qualified input envelope to justify its price. At fixed quality and utilization, a more expensive slot must be proportionally faster to reduce cost.

Illustrative 5090 session using **$0.70/h**,38.6s service,15-minute preparation and10-minute idle tail: one output costs$0.29917;20 outputs share that overhead at$0.02209 each. The warm-only lower bound is$0.00751. These assumptions are hypothetical, not a claimed cold-start measurement. The four warm calculation rows are also available as [CSV](cost-scenarios.csv), with their timing boundaries and price assumptions.

Ten idle minutes cost one node$0.098–0.142 on5090,$0.198–0.282 on PRO Server,$0.917–0.933 on B200, or$2.158 on B300 at the saved asks. Two nodes double this component. The existing600-second policy is unchanged; analyze arrival gaps and startup latency before revising it.

At730 continuously billed hours, compute-only one-card cost is5090$430.70–620.50, PRO Server$868.70–1233.70, B200$4015–4088, B300$9453.50. Those are utilization scenarios, not a recommendation for always-on capacity or a monthly reservation quote. Self-purchase TCO is **not** evaluated here; it additionally needs a current whole-system quote, electricity, cooling, network, maintenance and depreciation.

## 7. Proposed comparison order and acceptance gates

The broad cohorts below are historical research alternatives, not the current execution queue. The user's later 2026-10-08 direction selects inexpensive RTX 5090 / INT8 FL2VA and Ref2VA for future tests: see [DEC-011](../../DECISIONS.md#dec-011--use-rtx-5090-and-int8-for-future-low-cost-h3-tests) and the [bounded pilot proposal](5090-test-plan.md). Other hardware/acceleration cohorts are deferred unless explicitly selected later. Neither document authorizes an experiment to start.

1. **Remove avoidable preparation.** Existing[#61](https://github.com/apedintensor/h3-studio/issues/61) covers prepared dependency image identity. Model cache/volume locality needs separate measured design under[#22](https://github.com/apedintensor/h3-studio/issues/22); a prebuilt Python image alone does not eliminate model downloads. Do not assume a mounted network volume has local-NVMe bandwidth or exists in every region.
2. **Full-control cohort:** matched Base FL and REF on5090 versus PRO6000; fix component files, precision, steps, scheduler and output framing. If an identical recipe does not fit, record that boundary, then introduce a separately named lower-memory recipe. H100 is a price-dependent alternate. Evaluate H200/B200 only for a demonstrated envelope/latency need.
3. **Broad-control acceleration cohort:** matching LightX2V and PAI FL/REF recipes versus their Base parents, including endpoint fidelity, motion/video references, audio reference, mixed inputs, timed guides and all controls actually advertised. Their files' existence is not acceptance;[#29](https://github.com/apedintensor/h3-studio/issues/29) owns REF qualification.
4. **Text-only fast cohort:** FastH3 V2 Consumer on5090 and PRO6000; VDN for FL tasks. Trim remains an explicitly experimental option. Do not route a REF request to a T2VA model to make it faster.
5. **High-end cohort only when justified:** B200/B300 single-card and connected multi-GPU layouts. Compare latency, totalGPU-seconds and independent-replica throughput separately. Do not multiply a single-card marketplace quote into a claim that an NVLink pod is available.

For each selected cohort, use the same licensed/synthetic scenarios and prompts across candidates: text scene, first+last endpoints, character/product images, motion reference, audio reference with visible subject, combined inputs, timed anchors;5/10/15-second and480p/768p boundaries; portrait/landscape. Unsupported is an explicit outcome, not an omitted row.

Record fresh-prompt and cached-prompt runs separately. Capture provision, image pull, dependency setup, weights fetch/verify, model load, encode, denoise, video/audio decode, MP4, upload/validation and download availability. Distinguish download-cold, disk-cached and model-warm states. Keep exact source/image/model hashes, sampler schedule/NFE, per-component precision, VAE identity, host RAM/PCIe/disk/network, GPU power/interconnect and allocation size.

A proposed small pilot is3 repetitions across representative prompts per accepted recipe, followed by a larger run for tail latency/failure rates; a tiny pilot does not establish p95 or SLA. Blind visual/audio review should include motion, identity, first/last adherence, reference adherence, artifacts, intelligibility and lip synchronization. If published benchmarks are used, name their exact version and dataset; do not claim VBench from file decoding or ASR alone.

Use the same business API and explicit recipe capability descriptors. Qualify service recovery/cancellation/output preservation under[#15](https://github.com/apedintensor/h3-studio/issues/15) and public delivery under[#16](https://github.com/apedintensor/h3-studio/issues/16) before activation. Costs of unsuccessful technical attempts and user rejection must remain visible.

## 8. Evidence gaps and provenance

- No uniform six-GPU full-control benchmark, no established maximum REF envelope on5090, no full native/control parity for the accelerated candidates.
- Almost all public fast results are warm, serial and small-sample. Cold-start p95, capacity availability, multi-user throughput, rejection rates and long-run stability are unknown.
- SGLang's H100 topology-only13.25s row was intentionally excluded from cross-GPU pricing because its local workload metadata was insufficient. It must not become a “50-step768p H100” claim.
- Public source revisions observed: SGLang`ac656ee79af3fbf40778189eb537d357be56c3e4`; WanGP`6479db36bdc2619a904a852bba9c2d78e1a83f82`; OpenVDN`e262cb5770b3dc992d4f44f251cd17bbfe0c4d8b`; LightX2V`02e26d591f7a04d5d1a074c9566d5dd4f22f6225`; vLLM-Omni`c548a110a5a9bc278c39cfa81e673a8d505a24dd`. Mutable model-card/blog links are dated observations, not pinned install instructions.
- Blog and model-card quality language sometimes differs. FastH3 V2's blog reports favorable quality while its model card retains difficult-motion/audio caveats. Neither substitutes for testing our workload.
- Findings are owned by[#66](https://github.com/apedintensor/h3-studio/issues/66); implementation/qualification remains in[#22](https://github.com/apedintensor/h3-studio/issues/22),[#29](https://github.com/apedintensor/h3-studio/issues/29),[#61](https://github.com/apedintensor/h3-studio/issues/61). No new automatic fallback or second ledger is proposed.
