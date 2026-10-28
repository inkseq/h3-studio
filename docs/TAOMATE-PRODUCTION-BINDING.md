# G1 production TaoMate policy binding — Refs #659

Production's original guarded switch `b3d28c4a-ca69-45d8-b953-3951f6dc8a39`
refused before writing because FL/REF v7 execution policies were absent.
Claude accepted staging in [6458516320](https://github.com/inkseq/h3-studio/issues/659#issuecomment-6458516320).
Read-only SSM `9b41fca1-a855-4eae-bb86-eba1d03b6f84` confirms current v6
production policies and v7 staging policies; `732951a1-c0d8-4fe3-a20d-a057e3905870`
confirms the corresponding operator routes. Source digests are in
`deploy/dstack/taomate-v7.production-bindings.json`.

The reviewed addition has two execution policies and four existing-owner fleet
profiles. Existing production FL/REF pools and job/GPU budget accounts are reused;
new configuration IDs distinguish v7 from accepted v6 work. Old rows are retained.
Native manifests, model, output delivery and support envelope match accepted v7.
The policy narrows steps to 5 and sampler to Euler. Runtime qualification remains
required for every actual worker; the acceptance reference is not a rental grant.
Both owners retain their original project and account attribution. Rentals keep
the existing USD0.85/h maximum, 2h deadline and image; explicit RAM is 41GiB.
Global machine/hourly/account limits and existing reservation amounts do not rise.

`tools/prepare_taomate_bindings.py` takes retained nonsecret execution/operator
documents and a fresh output directory. It refuses any byte change against the
reviewed source digests, or any existing v7 key/ID. It appends the reviewed rows
and demand-selectable profile IDs without changing runtime demand enablement.
It performs no host, database, credential or provider operation. Do not run it
against a secret file, and do not replace a differing original to make it pass.

## Adoption after the single Tier2 PR review

1. Confirm exact-head green CI and unchanged reviewed fragments. Post a #193
   production release hold. Fresh-read current/previous release, app/dispatch
   images, protected helper/config hashes and all nonterminal jobs. Production
   demand runtime must be OFF and there must be zero nonterminal jobs. Check
   original operation receipts first; do not replay the refused default switch.
2. Read the two protected source documents at their mount sources, compare the
   reviewed hashes, and prepare the additive candidates offline. Re-read-back
   with the deployed application's `read_profiles`, `validate_policy` and
   `DstackOperator` validators without a provider call. Confirm v7 FL/REF source
   index entries, immutable package/manifest hashes and their 41GiB planning
   floor. Refuse missing packages, future/expired authority or account/pool
   mismatches. No package replacement or ceiling increase belongs to this slice.
3. Use the existing qualified host operation/release lock and fresh CAS. Retain
   original file bytes, ownership/mode, inode identity, release tuple and a
   durable operation intent before any write. Stop current CPU work producers,
   repeat the zero-job check through the live database, then adopt only the two
   reviewed nonsecret config additions through the protected configuration lane.
   Recreate current approved CPU services and their shared network dependents
   as required for bind mounts. No release promotion, default-profile switch,
   database schema/row change, credential change or provider action.
4. Read back both candidate digests, preserved original rows, unchanged budgets,
   authority-current FL/REF v7 and the guarded default-policy probe. Read back
   zero work, healthy services, demand OFF and charging/Checkout OFF. Record the
   original operation completion on #659 and clear the #193 hold. Separately post
   `Codex → Claude: production v7 policies bound` with needs-claude-review.
   Claude owns the guarded default switch and production acceptance/promotion.
5. Unknown or partial writes require read-only reconciliation against that exact
   operation and saved bytes. No second adoption, rental or job replay. Rollback
   restores only these two saved config documents at the same gates while v6
   remains default; after v7 jobs are accepted, drain/reconcile first. Retain all
   accepted plans, attempts, billing and original acceptance receipts.

CR73's production auto-start permission still depends on Claude accepting the
capacity rule. Adding selectable profiles does not enable demand or override
that gate. This binding slice does not create new pools, accounts or resources.
