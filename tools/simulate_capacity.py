"""Run the controller simulator (#1034): real controller, fake clock and provider.

    python tools/simulate_capacity.py                  # every scenario
    python tools/simulate_capacity.py --scenario burst --scenario no_offers_then_offers
    python tools/simulate_capacity.py --scenario burst --trace   # tick-by-tick
    python tools/simulate_capacity.py --list
    python tools/simulate_capacity.py --json > report.json

No network, provider, GPU or spend: every socket connect is refused while a
scenario runs, and each world is a throwaway SQLite ledger. Exit status 1 when
a scenario breaks an invariant, or when a known-bug scenario starts passing
(then remove its ``expected_failure``). See docs/CAPACITY-SIMULATOR.md.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    for path in (ROOT, ROOT / "tests"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from capacity_scenarios import SCENARIOS
    import capacity_sim

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument(
        "--fixture",
        action="append",
        default=[],
        help="an exported incident JSON (tools/export_capacity_incident.py)",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--trace", action="store_true", help="print every tick of the run"
    )
    args = parser.parse_args(argv)
    extra = [capacity_sim.incident_scenario(path) for path in args.fixture]
    names = {scenario.name for scenario in SCENARIOS + extra}
    unknown = sorted(set(args.scenario) - names)
    if unknown:
        parser.error("unknown scenario: " + ", ".join(unknown))
    if args.list:
        for scenario in SCENARIOS:
            marker = " [known bug]" if scenario.expected_failure else ""
            print("%-28s %s%s" % (scenario.name, scenario.description, marker))
        return 0
    capacity_sim.World.tracing = args.trace
    if extra and not args.scenario:
        selected = extra
    else:
        selected = [
            s for s in SCENARIOS + extra if not args.scenario or s.name in args.scenario
        ]
    reports = []
    for scenario in selected:
        started = time.monotonic()
        report = capacity_sim.run_scenario(scenario, seed=args.seed)
        report["wall_seconds"] = round(time.monotonic() - started, 1)
        reports.append(report)
        if not args.json:
            print(capacity_sim.format_report(report), flush=True)
    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
    else:
        print(capacity_sim.format_summary(reports))
    bad = [r for r in reports if r["status"] in ("fail", "unexpected_pass")]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
