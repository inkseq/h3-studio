"""Domain map and cross-domain import ratchet for studio_platform (#812).

Each module belongs to one domain. Imports between different domains are
recorded in tools/module-boundaries.json; a new cross-domain edge fails the
check, a removed one passes (refresh the baseline with --write). `platform`
holds shared infrastructure every domain may use, so edges into it are free.

Run: python tools/module_boundaries.py [--write]
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "studio_platform"
BASELINE = ROOT / "tools" / "module-boundaries.json"

# First matching prefix wins; order matters for overlapping prefixes.
DOMAINS = (
    (
        "identity",
        (
            "identity",
            "auth",
            "workos",
            "account_",
            "agent_",
            "session_authority",
            "support_view",
            "staging_qa",
        ),
    ),
    (
        "billing",
        (
            "credit",
            "payment",
            "stripe",
            "operator_billing",
            "operator_budget",
            "spending",
            "supplier_spending",
            "budget_policy",
            "invoice_checks",
            "service_credits",
            "reference_video_pricing",
            "settlement_recovery",
            "welcome_eligibility",
            "rent_journal",
        ),
    ),
    (
        "capacity",
        (
            "capacity",
            "dstack",
            "modal",
            "operator_capacity",
            "operator_extensions",
            "operator_host",
            "operator_prices",
            "operator_routes",
            "operator_jobs",
            "native_",
            "fleet",
            "autoscale",
            "host_",
            "provider_lifetime",
            "lium",
            "targon",
            "pool_member",
            "control",
            "worker_admission",
            "demand_wakeup",
            "controller_",
            "slot_lock",
            "drain_safe_runner",
            "boyesir_backend",
            "wangp_bootstrap",
        ),
    ),
    (
        "media",
        (
            "media",
            "asset",
            "artifact_",
            "storage",
            "upload_route",
            "rendition_backfill",
            "clip_sound",
            "caption_server",
            "subtitle_quality",
            "scratch_retention",
            "universal_trash",
        ),
    ),
    (
        "jobs",
        (
            "queue",
            "job_",
            "generation_",
            "batches",
            "worker",
            "dbos_transport",
            "postgres_dispatch",
            "workflow_",
            "domain_turns",
            "result_collection",
            "execution_",
            "dispatch_telemetry",
            "stage_metrics",
            "telemetry",
            "render_",
            "film_",
            "account_workflows",
            "payments_workflows",
        ),
    ),
    (
        "creation",
        (
            "story",
            "quick_chat",
            "guided",
            "project_",
            "script_window",
            "shot_segments",
            "source_snapshot",
            "library",
            "take_",
            "review_",
            "product_feedback",
            "content_",
            "inky",
            "google_",
            "llm_tracing",
            "langfuse_config",
            "assistant_models",
            "quality_",
            "creator_controls",
            "capabilities",
            "h3_profile_support",
            "runtime_catalog",
            "qualification_profiles",
        ),
    ),
)


def domain(module):
    """Domain of a top-level studio_platform module name; shared code is platform."""
    for name, prefixes in DOMAINS:
        if module.startswith(prefixes):
            return name
    return "platform"


def edges(package=PACKAGE):
    """Sorted cross-domain (source, target) module edges, excluding into platform."""
    found = set()
    for path in sorted(package.glob("*.py")):
        source = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.ImportFrom):
                if node.level == 1 and node.module:
                    targets.append(node.module.split(".")[0])
                elif node.level == 1:
                    targets.extend(alias.name for alias in node.names)
                elif node.level == 0 and (node.module or "").startswith(
                    "studio_platform."
                ):
                    targets.append(node.module.split(".")[1])
            elif isinstance(node, ast.Import):
                targets.extend(
                    alias.name.split(".")[1]
                    for alias in node.names
                    if alias.name.startswith("studio_platform.")
                )
            for target in targets:
                if not (package / (target + ".py")).exists():
                    continue  # subpackages (inference, runtime_hosts) stay internal
                a, b = domain(source), domain(target)
                if a != b and b != "platform":
                    found.add((source, target))
    return sorted(found)


def summary(pairs):
    counts = {}
    for source, target in pairs:
        key = domain(source) + " -> " + domain(target)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    current = edges()
    value = {
        "version": 1,
        "summary": summary(current),
        "edges": [f"{s} -> {t}" for s, t in current],
    }
    if "--write" in argv:
        BASELINE.write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")
        print(f"recorded {len(current)} cross-domain edges")
        return 0
    recorded = set(json.loads(BASELINE.read_text(encoding="utf-8"))["edges"])
    added = [edge for edge in value["edges"] if edge not in recorded]
    if added:
        print("New cross-domain imports (#812). Go through the owning domain's")
        print("interface instead, or record a deliberate edge with --write:")
        for edge in added:
            print("  " + edge)
        return 1
    removed = len(recorded) - len(set(value["edges"]) & recorded)
    print(f"{len(current)} cross-domain edges, {removed} fewer than recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
