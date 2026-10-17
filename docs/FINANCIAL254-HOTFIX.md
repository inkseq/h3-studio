# Financial-only production-base correction

Base: a76e2171fe6f2066beb60c24e686815ffd327ace. This branch is not a main rollback.

## Included dependency closure

- Original a76 policy/alert commits 484d886 and e94fe80.
- Settlement recovery from 42131c2 and strict fenced unsubmitted histories from 31e51bd, plus invoice-pending projections/diagnostics from 3ae9029.
- Only the financial source/test delta between a76 and reviewed 96b9a7f: three additive spending tables, original owner/connection spending Settings, funding/submission admission and original-key confirmation. This is needed so retired bootstrap caps are replaced by actual editable limits. No later scoped/paused Agent-management module or its authoring features is imported.
- Original a76 canonical frontend plus only reviewed dc6303f bounded administrator editor, synchronized by the normal source generator (206 files). No #251 interface or v2 authoring UI.
- Exact financial hotfix branch may prepare platform artifacts with full checks. Privileged publication remains main-only and unchanged. Preparation never approves, publishes or deploys. GitHub native-source retention only; no S3 restoration exception.

## Compatibility and acceptance

No schema deletion, ledger rewrite, provider mutation, task reset or data cleanup. Existing invoice_pending rows keep original unknown amounts and later actual settlement subtracts no reservation twice. Retained extra tables from temporary main runtime are not removed. New v2 scoped/paused Agent grants, if any, require a read-only census and explicit compatibility resolution before this older authoring runtime is accepted; absence is not inferred.

All app/controller/dstack financial consumers must use the accepted exact tuple. Root owns branch-artifact publication, staging acceptance and protected promotion. This source is not operational authorization.

The existing alert monitor condition terminal_reservation_stale uses terminal_reservation_seconds (default 3600 seconds); it reports retained terminal reserved obligations, not invented actual invoices. Monitor installation/configuration is separately verified by root.
