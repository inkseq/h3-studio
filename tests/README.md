# Test layout

Run commands from the repository root. Tests and their synthetic fixture data
live here; application and deployment sources stay in their existing locations.

The authoritative CI runner preserves the original `unittest` module names,
`load_tests` hooks, module/class fixtures, PostgreSQL module list, and duplicate
test-ID occurrences:

```sh
python tools/ci_test_shards.py --suite python --plan
python tools/ci_test_shards.py --suite python --shard-index 0 --shard-count 6
python tools/ci_test_shards.py --suite postgres --plan
```

For a focused test module, expose this directory on the test process import path:

```sh
PYTHONPATH=tests python -m unittest test_ci_changes test_ci_test_shards -v
```

In PowerShell, set `$env:PYTHONPATH = "tests"` before the same `python -m unittest`
command. The CI workflows set this path for focused commands. Complete discovery
also works with `python -m unittest discover -s tests -p 'test_*.py'`.

Pytest adoption is a separate collection-compatibility step: its normal unittest
collector does not support the five existing `load_tests` hooks. Do not replace
the shard runner until collection parity, including hook-selected cases and
duplicate occurrences, has been demonstrated. No tests have been removed or
rewritten as pytest fixtures in this layout change.
