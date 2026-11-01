# Scheduling scenarios: where a job runs and when a machine starts

The one place for this logic. If a rule changes, change this table and its test. Design background is in [PROVIDER-ARCHITECTURE.md](PROVIDER-ARCHITECTURE.md) and [STORY-AUTO-QUEUE.md](STORY-AUTO-QUEUE.md); where they disagree with this page, this page wins.

## What decides

Six signals:

1. Is a person waiting (`interactive`), or is it auto-queue (`economy`)?
2. Is a machine of ours free, or about to be?
3. How many hosted renders ran in the last 15 min?
4. How much economy work is queued?
5. Does the vendor balance allow hosted?
6. Is the lane backing off, and is the vendor window used up?

## Scenarios

| # | Scenario | Where it runs | Machine | Status |
|---|---|---|---|---|
| 1 | No machine, an occasional click | Hosted chain once the job passes its wait limit | Not started | Built; passed on staging |
| 2 | Sustained clicks: 5+ hosted renders in 15 min | Hosted until a machine is warm, then the machine | Starts one | Built; not yet seen live |
| 3 | A machine is free | The machine, before the wait limit | Already running | Built |
| 4 | Auto-queue, fewer than 40 shots waiting | Vivix, one at a time | Not started | Built; not yet seen live |
| 5 | Auto-queue, 40+ shots waiting | The machine, until the backlog is done | Starts one, stays while work remains | Built; not yet seen live |
| 6 | The machine is running economy work and someone clicks | The click takes the next free slot; past its wait limit it goes hosted. A running render is never interrupted | Keeps running | Built; not yet seen live |
| 7 | No machine can start | Clicks go hosted; economy goes to Vivix, otherwise waits | Lanes back off and retry | Built; not yet seen live |
| 8 | Vendor balance under USD 1 | Machines only | Starts as demand needs | Built; not yet seen live |
| 9 | Hosted refusal, failure or deadline | Next model; after the last, the machine | Starts if needed | Built |
| 10 | This environment's vendor window is used up | Waits for a machine | Starts after the grace | Built; not yet seen live |
| 11 | Backlog finished | Nothing queued | Idle stop | Built |

## The numbers, and where they come from

All figures are for a 5090 at about USD 0.65/h, 480P, 5 s, about 20 s per clip (about USD 0.004 per clip), and a boot of about 23 min (about USD 0.25). Faster boots lower both break-even points.

| Rule | Value | Why |
|---|---|---|
| Wait limit | fast 30 s, standard 60 s, refine 120 s | Owner's cold-start goal (#918) |
| Machines hold back | Wait limit + 30 s | Stops a machine booting for a job about to go hosted; found on staging |
| Clicks start a machine at | 5 hosted renders in 15 min | Against turbo (USD 0.038) a machine pays back after about 7 clips |
| Economy starts a machine at | 40 shots waiting | Against Vivix (USD 0.01) each clip saves about USD 0.006, so about 40 clips |
| Hosted deadline | 300 s; Vivix 30 min | A move pays twice, and the vendor's batch queue measured 250-290 s |
| Hosted window | Each environment's share in `everyframe-routing.json`; shares must total 40 or less | One key, one vendor limit (owner 2026-11-01) |
| Balance | Read every 4 h; stop under USD 1; warn under USD 10 | Owner 2026-11-01. A refusal costs nothing, so a late read is safe |

## Rules that always hold

- One live attempt per job. A hosted submit is never repeated on an uncertain answer.
- The user pays the tier's credits once, whatever the route.
- A rebind happens only while a job is queued and unsubmitted.
- Models that refuse faces or IP are not in the catalog.
- H3-based hosted models and our v9 look nearly the same given the same references (owner 2026-11-01), so one story may mix routes.

## Vivix sizes

Vivix ignores the requested resolution, so its output is scaled to fill the take's size and centre-cropped. The economy tier is always 768P at 2 credits per second (owner 2026-11-02), and Vivix serves only that tier: it renders one at a time across all vendor customers, so a person waiting never uses it.

| Aspect | Vivix renders | Effect |
|---|---|---|
| 16:9 | 1264x720 | About 6.7% upscale to 1344x768; about 0.3% cropped |
| 9:16 | 720x1264 | Same as 16:9, turned |
| 1:1 | 960x960 | No crop |
| 4:5 | 832x1104 (3:4) | About 6% cropped top and bottom; allowed |
| 21:9 | 16:9 canvas | Would lose about a quarter of the height, so these shots go to turbo |
