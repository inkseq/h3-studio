# Story auto-queue, Inky fix-all, and hosted coordination

Status: proposal for owner review, 2026-11-01. It builds on the provider architecture in [PROVIDER-ARCHITECTURE.md](PROVIDER-ARCHITECTURE.md) (owner approved 2026-11-01) and Inky's backend in [INKY-BACKEND.md](INKY-BACKEND.md) (#679, #684).

## Owner asks (2026-11-01)

1. **Auto-queue switch per story.** When it is on and Jev judges a clip card ready, the card is generated in the background, queued without hurry.
2. **One-click fix-all.** Inky gets one click that fixes every flagged clip card. The AI decides how to fill each card and which existing story elements to use.
3. **Hosted coordination.** How these background jobs and EveryFrame work together.
4. **Seeds.** Users don't need them. Remove the seed option from the creator UI. The API keeps it for our own use.
5. **Economy tier** (owner, via the taomate H3 session). The auto-queue toggle is a cheap tier: always 768P at half the fast 768P price, served by Vivix W1 on EveryFrame.

There is no auto-queue switch today. Generation always starts from an explicit click or chat confirmation, and "Generate all shots" is a one-time batch.

## 1. Readiness: one verdict per card

Every card gets one **readiness verdict**, recomputed whenever the card or a card it uses changes:

| Verdict | Meaning |
|---|---|
| `ready` | Generation can start |
| `needs_material` | Something must be added; carries the list of findings |
| `stale` | The card changed after its current take |
| `has_take` | Current take matches the card |

The verdict comes from two layers. The deterministic layer runs first so most cards never need a model call.

**Layer 1: deterministic checks.** These run in code, free and instant:

- **People.** Each named or recurring person in the shot has a character card with a picture, or the shot has a first frame.
- **Speech.** A speaking part has lines in quotes with the speaker named. A silent shot is explicitly marked "no dialogue".
- **Shot basics.**
  - The duration is in range.
  - The quality tier is set.
  - References are within the model limits (count and size).
  - A voice reference has a matching speaker.
- **Brand safety.** The story risk lint passes (`STORY-RISK-LINT.md`). Well-known IP is ours to catch, because neither H3 nor the vendor filters it.

**Layer 2: Jev.** Jev runs only on cards that pass layer 1. It judges whether the action is drawable, using today's kinds:

- `vague_action`;
- `needs_keyframe`;
- `emotion_not_shown`;
- `multiple_camera_moves`;
- `action_too_long`.

It uses the existing probability rule: argmax expected score ≥ 2, untied. A malformed or unavailable answer means "unknown". Unknown is never `ready`, so the auto-queue waits.

The verdict is stored per card version. The existing Inky source identity covers the shot, its version, the resolved cast, place and props, and the rubric revision. When the context changes, the verdict goes stale, as Inky advice does today.

## 2. Auto-queue switch = economy tier

**Where.** One toggle on the story (storyboard), "Auto-generate ready shots (economy)". It defaults to off. Turning it on shows two things:

- **Credit cap for this story.** Defaults to the economy cost of the current shots plus 20%. The cap is a hold limit, not a price.
- **Speed hint.** "Shots finish one after another in the background." No model name and no per-clip price; users see the tier name and the usage bar (existing rules).

**What it does.** When a card reaches `ready` and has no take yet, a job is created in tier `economy` with priority class `economy`:

- Credits are held at creation, exactly as for a clicked job. If the cap or the balance would be exceeded, the card waits with the reason "credit cap reached", and nothing is charged.
- Cards go in story order (chapter, then shot order). Fair-dispatch rules still interleave owners (`QUEUE-FAIRNESS.md`).
- **Debounce.** A card is queued 60 s after its last edit and only while its verdict is still `ready`. Typing does not create jobs.
- **Not-ready cards** stay put, and Inky says what is missing (section 3 can fix them in one click).

**Price (owner chose, 2026-11-02).** The economy tier is always 768P, at 2 credits per output second. There is no economy 480P: Vivix renders 720p and costs the same at any size.

| 5 s | Credits | Speed |
|---|---|---|
| Economy 768P | 10 | Queued |
| Fast 480P | 10 | Now |
| Fast 768P | 20 | Now |

1 credit = USD 0.01. Fast 480P and economy 768P cost the same on purpose: speed versus sharpness.

| Route for 5 s at 768P | Cost |
|---|---|
| Vivix | $0.01 |
| Our machine (backlog) | about $0.009 |
| Fallback to turbo | $0.06 |

Every route earns money at list credit price. Recheck if packs sell below $0.01 per credit.

**Edited cards.** A card edited after it already has a take becomes `stale`, and it is **not** re-run by default. Each stale card offers "Generate again", plus a story-level "Generate all changed". A sub-option, "Also re-generate edited shots", defaults to off, because re-runs cost credits the user may not expect.

**Depends-on cards.** A shot that needs a character picture that doesn't exist yet stays `needs_material`. If the user accepts that picture in fix-all, the picture job runs first. When it lands, the shot turns `ready` and queues itself.

**Turning it off.** No new shot joins the queue. Inky asks once: "已经排队的 N 个要保留吗？" with [保留] [全部取消]. The default is keep. Cancelled waiting shots release their holds. A shot already rendering always finishes, because that render is already paid.

### Two ways to use it, mixed freely in one project

- **A. Hands-off (the toggle).** Every shot that becomes ready joins the queue by itself, including later ones as the user fixes them.
  - Turning the toggle on shows one confirm: the usage bar for everything that would queue now, plus an optional cap ("最多用 N 积分").
  - Past the cap, auto-queue pauses and Inky says so.
  - A single shot can opt out ("这条不要自动").
- **B. Picky (per shot).** Each ready shot offers [排队（省积分）] and [马上生成]. Multi-select works for both.

Queue order is the user's storyboard order. Across users the queue is round-robin, so one 100-shot project does not block anyone else.

Auto mode never edits a shot; it only queues what is ready. Fixing shots is the explicit fix-all click in section 3 (Inky v1 advises; fix-all is the user-approved "帮我改" step, #679).

### Shot states (per shot version)

`not ready` → `ready` → `waiting` (economy queue) → `rendering` (at the vendor or on our fleet) → `done`. Two more:

- `generating now`: normal tier, clicked;
- `failed`: refunded.

Credits move with the state:

- reserved when a shot enters `waiting`;
- charged on success;
- released on cancel or final failure.

**One result per shot version.** Auto mode never regenerates a shot that already has a result for its current version, so it cannot loop. A new take is always an explicit user action. The queue key is the shot version, so an edit replaces a queued entry and never duplicates it.

### Cases

| # | Situation | What happens |
|---|---|---|
| 1 | Waiting, user wants it now | One confirm showing the usage-bar change, never a per-clip price: "马上生成会多用积分" [马上生成] [继续排队]. If the ETA is under about 2 min: "大约1分钟就好，要加速吗？". Yes: release the economy hold, hold the normal tier, start now. "全部马上生成" does every waiting shot with one confirm |
| 2 | Already rendering, user wants it now | No upgrade: the vendor can't cancel, so it would pay twice. Show "已经在生成，马上就好" with progress |
| 3 | Cancel | While waiting: free and immediate, hold released. While rendering: it can't be stopped; tell the user it will finish, keeps the economy charge, and the result is still delivered |
| 4 | User edits a waiting shot | Replace the queued version in place, keeping its position, and re-run the readiness check. No longer ready: leave the queue, and Inky says why. Already rendering: the old version finishes, and the new version queues again (one more charge). In auto mode, confirm once; in per-shot mode ask "新版本也要排队吗？" |
| 5 | Toggle turned off mid-way | See "Turning it off" above (default keep) |
| 6 | Not enough credits | Shots stop entering the queue; nothing fails silently. Auto-queue pauses, Inky offers a top-up, and the queue resumes by itself after the top-up while the toggle is on |
| 7 | Jev unsure, or not representable on economy | Over 5 reference images, 21:9, over 15 s, 2K or enhance. "这条不能省积分排队", with [马上生成] or [修改] |
| 8 | Waiting past the deadline (about 30 min) | Promote silently to `h3-max-turbo`, then our fleet, at the same economy charge. We absorb the difference, and the margin stays positive. The ETA updates |
| 9 | Vendor refusal or failure | Next model in the chain, same charge. If every model fails: `failed`, hold released, Inky explains in plain words with no model names |
| 10 | User closes the tab | The queue runs server-side. In-app notice plus the existing done notification per shot and per project |
| 11 | Mixed tiers in one film | Economy output is normalised to the project's resolution (480P/768P), so it cuts together with normal shots. Enhance and 2K stay separate paid steps after generation |
| 12 | ETA | Per shot: queue position × live per-clip time, never hard-coded. If the ETA passes the deadline, show [马上生成] next to it |
| 13 | Brand / IP safety | Our own risk lint runs before queueing. Vendors don't filter |
| 14 | Vendor balance low | The economy queue pauses at Vivix and falls through to turbo or our fleet, and the operator is alerted. Never a user-visible error |

**Visibility.**

- Each card shows its state: waiting, queued (position and ETA), generating, done, or blocked with a reason.
- The production queue gains an "economy" filter.
- Inky's chat line summarises progress: "4 of 12 shots done, 2 need material".

## 3. Inky fix-all

**Entry.** Inky's suggestions panel gets "Fix all (N shots)" when two or more cards are `needs_material` or flagged by Jev.

**Plan, then preview, then apply.** Nothing is edited without the user seeing it. This follows ESLint's fix model: each fix is a precise edit against a known base, and a dry run is shown first.

1. **Plan.** For each flagged card, build a patch from the findings:
   - **Rule-based fixes first.** No model call:
     - add a named character's existing card to the cast;
     - attach that character's picture;
     - pick the place card mentioned in the text;
     - reuse an existing first frame the user uploaded for this shot.
   - **Terra for writing.** Draft dialogue in quotes with the speaker named, or mark "no dialogue" when the scene has no speech. Also tighten a vague action, or split two camera moves into two shots (shown as a proposal to split). Terra sees only that card's context (scene, cast, place, props, neighbouring shots), the same scope Inky examples use today. Cards go in batches of up to 8 per call.
   - **Missing elements.** Nothing exists to reuse, for example a character with no picture. The patch proposes "Draw a picture of Mia" as a separate item with its credit cost shown. It is never bundled silently.
2. **Preview.** The user sees one list of field changes per card: before, after, and why. All are checked by default except generated pictures and shot splits. The user can uncheck any item or edit it inline.
3. **Apply.** Each accepted item becomes a normal card edit with `base_version` compare-and-set, using the same API and undo history as a manual edit.
   - A card that changed since planning is skipped and marked "changed, review again".
   - User-written text is never overwritten. A fix only fills empty fields or replaces text that Jev flagged, and the preview always shows the old text.
4. **Re-check.** Changed cards get a fresh verdict. With auto-queue on, the ones that turn `ready` queue themselves. With it off, Inky offers "Generate the N ready shots".

**Budget.** Fix-all uses the existing `InkyBudget`. It needs its own allowance, because a 40-card story can take 5–6 batched Terra calls (about USD 0.001 each today). The proposal is 10 fix-all calls per account per day, on top of the 30 advice calls. A planned but unapplied fix-all costs only its Terra calls, never credits.

## 4. Coordinating the economy queue with hosted APIs and our fleet

The scheduler (`decide()` in PROVIDER-ARCHITECTURE.md) gets a **priority class** per job:

- `interactive`: anything a person clicked or asked for in chat, at any tier, including "Generate all";
- `economy`: auto-queue jobs.

| | `interactive` | `economy` (auto-queue) |
|---|---|---|
| Wait limit | Tier limit (Fast draft 30 s, Standard 60 s, Refine 120 s) | Deadline 30 min per shot; the card shows its ETA |
| First route | Warm own slot, else the hosted chain at once (turbo → Pika → fast …) | **Vivix W1**, through our own economy queue |
| Fallback | Next model in the chain, then our fleet | After the deadline: `h3-max-turbo`, then our fleet |
| Vendor rate-limit bucket (account: 40 / 15 min) | Reserved share: 25 / 15 min | Share: 15 / 15 min, plus whatever interactive left unused in the window |
| Starting our machines | Counts as demand | Counts as demand only once its jobs fall back |

**Why Vivix needs our own queue.** Vivix renders strictly one at a time across *all* EveryFrame customers, about 17–20 s per clip (about 180 clips per hour, shared). Submitting many at once only queues them at the vendor, where we can't see or order them.

- We keep **one Vivix render in flight** per account and hold the rest in our economy queue, in fair order.
- Each card's ETA is its queue position × the measured Vivix time per clip, over the last 50 renders. That time already includes other customers' share.
- **Deadline.** A shot whose ETA passes its 30 min deadline moves to turbo. A turbo failure or deadline moves it to our fleet. The fallback route still charges the economy credit price; section 2 shows the margin stays positive.
- **Turbo fallback is capped** by a daily economy-fallback budget (start: USD 5/day). Past that, late economy shots wait for our fleet, so a backlog spike can't turn into a large vendor bill.

**Inputs and output.**

- Vivix accepts text, first-frame, first-last, up to 5 reference images plus 3 audio, and voice-only.
- Aspects: 16:9, 9:16, 1:1 and 3:4. A 4:5 request comes back as 3:4, and 21:9 as 16:9. Shots in an aspect Vivix can't serve skip straight to turbo.
- No seed and no negative prompt. It passed the face and Demon Slayer moderation round.
- Output is always 1264×720, 24 fps, with sound. It is scaled about 6.7% up to 768P with a crop of about 0.3%.

**Swappable backend.** The Vivix price is SN117-subsidised: below Vivix's own expired $0.003/s promo. The economy tier names a chain in the rules table (`"economy": ["vivix-w1", "h3-max-turbo"]`), not a model. Another cheap model, or our own fleet, can replace it without touching the queue.

**Credits** are the tier price on whichever route served the shot. Each take records:

- route and model;
- vendor or machine cost;
- latency;
- deadline misses and refusals.

These feed the margin report.

**Seeds** are removed from the creator UI. Hosted models don't honour seeds anyway. The API and admin tools keep the field. A job that sets an explicit seed stays on our own fleet, because only v9 reproduces a seed.

## 5. Build order

Each item is one PR, on staging first.

1. **Readiness verdict.** Layer 1 checks plus a verdict API on cards, with a per-card state chip. Remove the seed control from the creator UI.
2. **Economy tier and price.** A rate-table row (2 credits/s, 768P only) behind an owner switch.
3. **Economy queue.** It lands with #931 step 5 (hosted adapter). It covers:
   - one Vivix render in flight per account, with ETA;
   - the 30 min deadline, then turbo fallback with a daily cap, then our fleet;
   - the rate-limit bucket split.
4. **Auto-queue toggle.** Story setting, credit cap, debounce, cancel on off, ETA chips, progress line.
5. **Fix-all.** Rule-based patches, the preview dialog, apply with compare-and-set, then the Terra writing patches behind a flag.
6. **Jev layer 2 in the verdict**, after the Jev qualification still open in INKY-BACKEND.md ("Remaining integration").

Items 1 and 5 do not depend on the hosted work and can start first.

## Open-source references (ideas only, no code copied)

| Project | License | Taken |
|---|---|---|
| ESLint `--fix` / `--fix-dry-run` | MIT | Fixes are precise edits against a known source, preview before apply, conflicting fixes skipped rather than merged |
| Kueue `WorkloadPriorityClass` | Apache-2.0 | Priority as a class on the job, used for ordering only, separate from fairness between users |
| Slurm QOS | GPL-2.0 (ideas only) | A lower-priority class that fills idle capacity but never delays the interactive class |
