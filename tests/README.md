# Test layout

Run commands from the repository root. Tests and their synthetic fixture data
live here; application and deployment sources stay in their existing locations.

The authoritative CI runner preserves the original `unittest` module names,
`load_tests` hooks, module/class fixtures and the maintained PostgreSQL module
list. Imported aliases of the same test ID run once; inherited tests on distinct
classes still run. Measured module timings balance the six parallel shards,
with test counts as the fallback for new modules:

```sh
python tools/ci_test_shards.py --suite python --plan
python tools/ci_test_shards.py --suite python --shard-index 0 --shard-count 6
python tools/ci_test_shards.py --suite postgres --plan
```

CI runs all money, GPU, queue and authentication invariants. Only the explicitly
listed slow CPU/media and PostgreSQL backup suites may be skipped for unrelated
reviewed source paths. Unknown paths, executable additions/removals, sensitive
changes and manual platform checks run the full suites. `--slow-modules` consumes
the classifier's JSON list; omitting it (or passing `null`) means full coverage.
Prose-only additions and archive moves do not require runtime tests. The single
required `test` check still rejects failures, cancellations and accidental skips.

`tools/ci_test_timings.json` comes from the linked successful hosted run, not a
performance promise. Refresh it from the existing per-shard timing artifacts
when balancing drifts. Test time, artifact publication and staging deployment
are separate measurements: shortening the tests alone cannot bound deployment.

For a focused test module, expose this directory on the test process import path:

```sh
PYTHONPATH=tests python -m unittest test_ci_changes test_ci_test_shards -v
```

In PowerShell, set `$env:PYTHONPATH = "tests"` before the same `python -m unittest`
command. The CI workflows set this path for focused commands. Complete discovery
also works with `python -m unittest discover -s tests -p 'test_*.py'`.

Pytest adoption is a separate collection-compatibility step: its normal unittest
collector does not support the existing `load_tests` hooks. Do not replace the
shard runner without preserving hook-selected cases and fixture behaviour.

## Python style

Install the separately pinned developer tooling without changing runtime locks:

```sh
python -m pip install --require-hashes -r requirements.dev.lock.txt
python -m ruff format --check
python -m ruff check
```

Ruff 0.17.0 formats `platform_app.py`, `studio_platform/` and this test directory.
The installed host evaluator `studio_platform/alert_conditions.py`, deployment
helpers, public Agent helper scripts and release tools are deliberately outside
this slice. The explicit source scope also includes tracked modules matched by
the repository's legacy private-folder ignore rules. Runtime requirements and
Docker inputs do not include the developer lock.

Use `python -m ruff format` for formatting only. Import sorting, automatic lint
fixes, unsafe fixes and docstring code rewriting are disabled. The initial lint
gate covers fatal correctness diagnostics (`E9`, `F63`, `F7`, `F82`); broader
lint is a later baseline, not a reason to mix business fixes into formatting.
Nine existing docstrings have narrow formatting guards to preserve their exact
string values. This batch verifies every selected file's complete AST, including
type comments, with source locations excluded.

AST equivalence does not imply unchanged release fingerprints. The normal
release contract hashes runtime Python bytes, so formatting application source
changes API/worker compatibility fingerprints and still needs the normal tested
staging and identical-artifact production acceptance.
