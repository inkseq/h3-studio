# Reference-video price and concurrency contract

Refs #758 / #739 / #374 / #692 G3. Opt-in through the existing audited
`POST /v1/admin/credits/prices`, expected revision and idempotency journal.
The optional configuration/public price field is
`reference_video_surcharge_bps_per_second: {"480P": 1000, "768P": 2500}`.
It requires adopted resolution multipliers; basis points keep coefficients
exact. Omission retains the original immutable configuration. No read or
deployment changes prices, accepted quotes, balances or charging switches.

The quote uses `ceil(output_frames / output_fps * effective_rate *
(1 + k * reference_video_frames / 24))`, rounding only once. Native compiler
normalization provides used video frames after alignment/trimming and excerpt
selection. It counts reference and in-context videos, including a video guide,
deduplicates identities as the native mapper does, and excludes denoise controls.
Images/audio have no surcharge. Unsupported video compilers or inconsistent
native snapshots refuse new quotes, never silently discount them.

The quote and existing breakdown expose `base_credits`,
`reference_video_credits`, `reference_video_frames`, `reference_video_seconds`,
`reference_video_k`, `reference_video_multiplier`, `denoise_control_unpriced`.
The integer components sum to the rounded total: base uses its existing ceiling,
reference is the remainder. The original price revision stays frozen.
CPU render/image prices are unchanged. Denoise is flagged as unpriced.

A requested five-second video has124 native frames. Arithmetic illustrations
using exactly120 frames are approximate: a124-frame Sharp output with one
124-frame reference costs95 credits (42 base +53 reference). Both durations
come from the compiled facts, never a rounded requested duration.

The owner's unmeasured longer-Sharp gate is enforced for new opt-in quotes:
Sharp with reference video and more than124 output frames refuses with
`reference_video_duration_unqualified`. A staging measurement and reviewed
follow-up must qualify longer output before production adoption. Accepted
quotes are unaffected. This slice runs no GPU measurement.

The existing queue permits at most one nonterminal Sharp reference-video
generation per tenant/owner across pools. Width/height and conditioning/input
identity come from frozen compiled scalars, not caller tier labels/current
prices. Standard, denoise and other-owner work remain eligible. Unknown,
recovery-held, cancelling and collecting originals occupy the slot until
terminal; attempts, holds and reconciliation remain intact.

PostgreSQL claims use a nonblocking transaction advisory lock for the exact
tenant/owner, then recheck original active jobs. They never wait for this lock
while holding a pool/job row; another pool yields without creating an attempt.
SQLite keeps its write transaction reservation. Candidate/advisory order use
the same occupied-owner filter and four JSON scalars rather than loading queued
prompts/media. No new table, queue or background worker; no replay or preemption.
