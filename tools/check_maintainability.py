"""Backend debt ratchet; parses source only, never imports application modules.

Lower the snapshot after reductions with --write-baseline. CI also compares the
snapshot with the PR base (or push parent), so raising a number cannot mask debt.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "tools/maintainability-baseline.json"
LIMIT = 1500
ZERO_RULES = ("F401", "F841", "B023", "S608")


def ratcheted(code):
    return code in {"F401", "F841", "C901", "S608"} or code.startswith("B")


def inventory(root):
    # Use Git's finite public source set, including explicitly allowlisted new
    # files locally. Private experiments and ignored assets are not scanned.
    raw = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
    )
    paths = sorted(set(raw.decode("utf-8").split("\0")) - {""})
    return {
        name: len((root / name).read_text(encoding="utf-8").splitlines())
        for name in paths
        if (
            name == "platform_app.py"
            or name.startswith("studio_platform/")
            and name.endswith(".py")
        )
        and (root / name).is_file()
    }


def lint(root):
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format", "json"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode not in (0, 1):
        raise ValueError(
            "Ruff failed; a tool/configuration error is not a clean report"
        )
    findings = json.loads(result.stdout)
    if not isinstance(findings, list):
        raise ValueError("Invalid Ruff report")
    fatal = [x for x in findings if not ratcheted(x["code"])]
    counts = Counter({code: 0 for code in ZERO_RULES})
    counts.update(x["code"] for x in findings if ratcheted(x["code"]))
    return dict(sorted(counts.items())), fatal


def snapshot(counts, sizes):
    return {
        "version": 1,
        "max_module_lines": LIMIT,
        "rules": counts,
        "oversized_modules": {p: n for p, n in sorted(sizes.items()) if n > LIMIT},
    }


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "max_module_lines", "rules", "oversized_modules"}
        or value["version"] != 1
        or value["max_module_lines"] != LIMIT
    ):
        raise ValueError("Invalid baseline format or module limit")
    for key in ("rules", "oversized_modules"):
        if not isinstance(value[key], dict) or any(
            type(n) is not int or n < 0 for n in value[key].values()
        ):
            raise ValueError("Invalid baseline counts")
    if any(not ratcheted(code) for code in value["rules"]):
        raise ValueError("Fatal diagnostics cannot be baselined")
    return value


def compare(current, recorded, previous=None):
    validate(recorded)
    errors = []
    # Improvements pass immediately; only increases fail. Refresh the baseline
    # after reductions to carry the lower allowance into subsequent PRs.
    for code, count in current["rules"].items():
        if count > recorded["rules"].get(code, 0):
            errors.append(
                f"{code}: {count} > recorded {recorded['rules'].get(code, 0)}"
            )
    for path, lines in current["oversized_modules"].items():
        cap = recorded["oversized_modules"].get(path, LIMIT)
        if lines > cap:
            errors.append(f"{path}: {lines} lines > recorded cap {cap}")
    if previous is not None:
        validate(previous)
        for code, count in recorded["rules"].items():
            if count > previous["rules"].get(code, 0):
                errors.append(
                    f"{code}: {count} > prior {previous['rules'].get(code, 0)}"
                )
        for path, lines in recorded["oversized_modules"].items():
            cap = previous["oversized_modules"].get(path, LIMIT)
            if lines > cap:
                errors.append(f"{path}: {lines} lines > prior cap {cap}")
    return errors


def prior_baseline(root, ref):
    # Fail closed on a missing/shallow ref; absence of the file is allowed only
    # for introducing the first baseline, not a failed Git command.
    subprocess.run(
        ["git", "rev-parse", "--verify", ref + "^{commit}"],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    files = subprocess.check_output(
        ["git", "ls-tree", "--name-only", ref, "--", BASELINE], cwd=root
    )
    if not files.strip():
        return None
    return validate(
        json.loads(
            subprocess.check_output(["git", "show", f"{ref}:{BASELINE}"], cwd=root)
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", help="PR base SHA or push parent; required in CI")
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args(argv)
    counts, fatal = lint(ROOT)
    current = snapshot(counts, inventory(ROOT))
    if fatal:
        for item in fatal:
            print(
                f"{item['code']} {item['filename']}:{item['location']['row']}: {item['message']}"
            )
        return 1
    if args.write_baseline:
        (ROOT / BASELINE).write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    recorded = json.loads((ROOT / BASELINE).read_text(encoding="utf-8"))
    errors = compare(
        current,
        recorded,
        prior_baseline(ROOT, args.base_ref) if args.base_ref else None,
    )
    print(
        json.dumps(
            {
                "rules": counts,
                "oversized_modules": len(current["oversized_modules"]),
                "errors": errors,
            },
            indent=2,
        )
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
