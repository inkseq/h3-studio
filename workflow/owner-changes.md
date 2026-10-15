# Owner change requests

These entries record owner decisions, not task status. The latest applicable CR wins over older wording. Detailed technical contracts remain linked from their issue.

| ID | Date (Australia/Sydney) | Decision | Issues / source |
|---|---|---|---|
| CR-20261015-1 | 2026-10-15 | No reserved always-on supervan group. She starts/stops her own production machines; developer tests use separate machines. | [#193](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6183439332) |
| CR-20261015-2 | 2026-10-15 | Open every genuinely model/API-supported control and input combination now; no pre-release qualification whitelist/test prerequisite. Keep hard validity, ownership, resources and budgets; record actionable failures. | [#126](https://github.com/inkseq/h3-studio/issues/126#issuecomment-6183470849) |
| CR-20261015-3 | 2026-10-15 | Release the original budget reservation when the failed start is positively proven to have allocated no machine; retain uncertainty only where real allocation/billing is unknown. Includes the 02:19 failed start under #193. | [#193](https://github.com/inkseq/h3-studio/issues/193), owner instruction 2026-10-15 |
| CR-20261015-4 | 2026-10-15 | With no machines, jobs or unresolved start/stop outcomes, controller/dispatch have no work loop: event wakeup plus one safety check every15–30minutes. | [#193](https://github.com/inkseq/h3-studio/issues/193#issuecomment-6183407125) |
| CR-20261015-5 | 2026-10-15 | Order: #193 + #200 → #126 → #194 (then finish #182) → #195 staging → #196 → #197 and #130 after staging; defer #198. Small P0 production fixes are allowed. After #195, development/GPU tests run in staging and production promotes the same accepted version. | [#193](https://github.com/inkseq/h3-studio/issues/193), [#200](https://github.com/inkseq/h3-studio/issues/200), owner instruction 2026-10-15 |
| CR-20261015-6 | 2026-10-15 | Risk-tiered process, Chinese owner handoff ≤about 10 lines, two-hour no-direction escalation, one request-to-usable duration metric and a short mandatory entry. | [#200](https://github.com/inkseq/h3-studio/issues/200) |
| CR-20261015-7 | 2026-10-15 | Standing test-GPU authorization up to USD 20 per Australia/Sydney calendar day; test machines only, staging once available, max_price + idle stop, stop after tests, never touch supervan's production machines. | [#200 owner comment](https://github.com/inkseq/h3-studio/issues/200#issuecomment-6183602897) |

Append a numbered entry for a new owner decision and update affected current wording in place. Do not copy operational receipts or routine implementation choices here.
