# Backend maintainability ratchets

Owner direction, 2026-10-26, #257. These checks change no API, scheduler,
generation, billing or deployment policy.

## Lint and module size

Ruff remains pinned to 0.17.0. E9/F63/F7/F82 remain fatal, including in tests.
Backend modules (`platform_app.py`, `studio_platform/**/*.py`) additionally use
F401, F841, bugbear B, C901 with complexity 25, and S608. The existing excluded
installed helper `alert_conditions.py` keeps its bytes; tests keep fatal lint
without inheriting the backend debt allowance.

`tools/maintainability-baseline.json` records current counts **per rule**, not a
combined allowance. Fixing B007 cannot pay for a new B904. Missing rules have zero
allowance. Counts are a coarse ratchet: replacing one finding with another of the
same rule is not detected; review remains necessary.

Run with the pinned development environment:

```sh
python -m ruff format --check
python tools/check_maintainability.py --base-ref origin/main
# After a reduction, lower the snapshot in the same PR:
python tools/check_maintainability.py --write-baseline --base-ref origin/main
```

Measured counts may equal or fall below the checked-in baseline. CI compares the
baseline with the PR base SHA or push parent; raising a baseline count does not
bypass the check. Reductions pass immediately. Refresh the baseline after a
cleanup to carry its lower allowance forward and prevent later reintroduction.
Do not refresh the baseline to accept new debt. Review rule/configuration and
suppression changes alongside code; no blanket B023/S608 exclusions are used.

Physical lines (including comments/blank lines) are counted for public tracked or
allowlisted backend Python modules. New modules must be <=1500 lines. Existing
modules above 1500 may stay or shrink, never grow. Smaller modules can grow up to
1500. Renaming an oversized module is not an exemption; split it first. A module
that shrinks below 1500 loses its oversized allowance. Test/tools/frontend files
are outside this backend module-size scope.

Initial cleanup distinguishes actual unused imports from compatibility exports
and schema-registration imports. Those deliberate imports retain specific
F401 explanations. Unused assignments keep calls with side effects or validation.
B023 explanations identify synchronous invocation within the loop; S608 explains
fixed schema identifiers (or a trusted version constant), never untrusted SQL.

## Dependency reports and updates

The `dependency-audit` CI job uses isolated, hash-locked pip-audit tooling. It
scans checked-in pinned requirements without installing the application or
resolving/fixing dependencies. JSON reports and `summary.json` are artifacts for
14 days. Vulnerabilities, unavailable advisory data and tool errors remain
visible but **do not block the required `test` check or promotion gate**. An
absent/failed report is not a clean security result. No `--fix` is used.

Dependabot checks pip manifests at the root and maintained dependency directories
and GitHub Actions weekly, grouping routine updates. Updates remain PRs, not
automatic merges. Regenerate custom hash locks with the documented uv command
when an upstream updater cannot maintain them; retain separate runtime locks.

## Coverage artifacts only

Owner selected coverage.py (the engine underlying pytest-cov) to preserve the
existing unittest sharder, `load_tests`, fixtures, test selection and exit status.
Each Python and PostgreSQL shard measures its existing command once; tests are
not run a second time. No subprocess instrumentation is enabled, so child
processes, containers and live services are not represented as covered.

`coverage-report` combines those files and uploads XML, HTML, text and a
`completeness.json` for 14 days. Check completeness before interpreting the
percentage: failed/missing shards or path-selected slow suites mean this is not
an all-tests measurement. There is no coverage threshold; report generation is
advisory and is deliberately absent from the required gate's dependencies.
Original test failures still block the normal `test` aggregate.
