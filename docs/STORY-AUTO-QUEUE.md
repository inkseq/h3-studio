# Story auto-queue, Inky fix-all, and hosted coordination

Status: proposal for owner review, 2026-11-01. It builds on the provider architecture in [PROVIDER-ARCHITECTURE.md](PROVIDER-ARCHITECTURE.md) (owner approved 2026-11-01) and Inky's backend in [INKY-BACKEND.md](INKY-BACKEND.md) (#679, #684).

## Owner asks (2026-11-01)

1. **Auto-queue switch per story.** When it is on and Jev judges a clip card ready, the card is generated in the background, queued without hurry.
2. **One-click fix-all.** Inky gets one click that fixes every flagged clip card. The AI decides how to fill each card and which existing story elements to use.
3. **Hosted coordination.** How these background jobs and EveryFrame work together.
4. **Seeds.** Users don't need them. Remove the seed option from the creator UI. The API keeps it for our own use.

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

## 2. Auto-queue switch

**Where.** A story setting, "Generate ready shots automatically". It defaults to off. Turning it on shows two choices:

- **Quality tier.** Defaults to the story's tier.
- **Credit cap for this story.** Defaults to the cost of the current shots plus 20%. The cap is a hold limit, not a price.

**What it does.** When a card reaches `ready` and has no take yet, a job is created with **priority class `background`**:

- Credits are held at creation, exactly as for a clicked job. If the cap or the balance would be exceeded, the card waits with the reason "credit cap reached", and nothing is charged.
- Cards go in story order (chapter, then shot order). Fair-dispatch rules still interleave owners (`QUEUE-FAIRNESS.md`).
- **Debounce.** A card is queued 60 s after its last edit and only while its verdict is still `ready`. Typing does not create jobs.

**Edited cards.** A card edited after it already has a take becomes `stale`, and it is **not** re-run by default. Each stale card offers "Generate again", plus a story-level "Generate all changed". A sub-option, "Also re-generate edited shots", defaults to off, because re-runs cost credits the user may not expect.

**Depends-on cards.** A shot that needs a character picture that doesn't exist yet stays `needs_material`. If the user accepts that picture in fix-all, the picture job runs first. When it lands, the shot turns `ready` and queues itself.

**Turning it off.** Jobs that have not started are cancelled and their holds released. Running jobs finish. The switch never cancels a submitted render, because that render is already paid.

**Visibility.**

- Each card shows its state: waiting, queued (position), generating, done, or blocked with a reason.
- The production queue gains a "background" filter.
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

## 4. Coordinating background work with hosted APIs

The scheduler (`decide()` in PROVIDER-ARCHITECTURE.md) gets a **priority class** per job.

| | `interactive` (clicked, chat "generate") | `background` (auto-queue, "generate all") |
|---|---|---|
| Wait limit | Tier limit (Fast draft 30 s, Standard 60 s, Refine 120 s) | None by default; the story shows progress |
| Warm own slot free | Takes it first | Takes it when no interactive job is waiting |
| No warm slot | Hosted chain (turbo → Pika → fast …) at once | Waits for our own fleet; **no hosted by default** |
| Starting machines | Counts as demand | Counts as demand. A backlog of background shots is exactly what makes our own machine about 10× cheaper than hosted |
| Vendor rate-limit bucket (40 / 15 min) | Reserved for this class | Not used by default |

**Hosted exceptions for background work.** A background job may go hosted only when one of these holds:

- our fleet cannot start for 30 min, because every lane is in backoff or out of stock;
- the user asked to "Finish now" on the story, which turns its remaining jobs `interactive`. The user can see that this uses the faster path; the credit price is the same.

**Why this split.** Hosted absorbs the moment a person is waiting, and the cold start disappears for them. The bulk of a story renders on our own GPUs, which the backlog itself warms up. Interactive work never waits behind a long background batch, because interactive jobs claim first and background work never takes vendor tokens.

**Credits** are the same tier price on either route. Each take records route, model, vendor or machine cost, latency and refusals, for the margin report.

**Seeds** are removed from the creator UI. Hosted models don't honour seeds anyway. The API and admin tools keep the field. A job that sets an explicit seed stays on our own fleet, because only v9 reproduces a seed.

## 5. Build order

Each item is one PR, on staging first.

1. **Readiness verdict.** Layer 1 checks plus a verdict API on cards, with a per-card state chip. Remove the seed control from the creator UI.
2. **Priority class on jobs.** `interactive` / `background` in the scheduler snapshot and claim order. Hosted stays interactive-only. This depends on #931 steps 1–3 and lands with step 5c.
3. **Auto-queue switch.** Story setting, credit cap, debounce, cancel on off, progress line.
4. **Fix-all.** Rule-based patches, the preview dialog, apply with compare-and-set, then the Terra writing patches behind a flag.
5. **Jev layer 2 in the verdict**, after the Jev qualification still open in INKY-BACKEND.md ("Remaining integration").

Items 1 and 3 can start before the hosted work. Item 3 is then "background on our own fleet only", which is already useful and costs the least.

## Open-source references (ideas only, no code copied)

| Project | License | Taken |
|---|---|---|
| ESLint `--fix` / `--fix-dry-run` | MIT | Fixes are precise edits against a known source, preview before apply, conflicting fixes skipped rather than merged |
| Kueue `WorkloadPriorityClass` | Apache-2.0 | Priority as a class on the job, used for ordering only, separate from fairness between users |
| Slurm QOS | GPL-2.0 (ideas only) | A lower-priority class that fills idle capacity but never delays the interactive class |
