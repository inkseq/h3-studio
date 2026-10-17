"""Fail-closed CI selection from the complete local Git diff, never an API list.

Only modifications to reviewed paths may reduce checks. Added, removed, renamed,
type-changed, malformed or unknown paths require the complete platform checks.
An explicit frontend release has its own artifact contract and targeted gates;
it is not an assertion that unrelated backend changes have passed their tests.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


# This exact allowlist is also the release preparation dirty-document exemption.
# Keep CI-only governance paths separate: lightweight checks do not authorize
# preparing a release while its governing instructions are uncommitted.
DOCUMENTS = frozenset("""
AGENT-API-RELEASE-20261004.zh-CN.md AGENT-ONBOARDING.zh-CN.md
API-PROVIDER-COMPATIBILITY.zh-CN.md API-USAGE.zh-CN.md ARCHITECTURE.zh-CN.md
ASSET-RECOVERY.zh-CN.md BACKEND-HANDOFF-20261004.zh-CN.md BACKUP-RECOVERY.zh-CN.md
BOYESIR-ADAPTER-CONTRACT.zh-CN.md CAPTION-BURN-IN-CONTRACT.zh-CN.md
CHAPTER-RENDER-CONTRACT.md COLD-START-CONTRACT.zh-CN.md CONTROLS.zh-CN.md
DELIVERY-REPORT.zh-CN.md DEVELOPMENT-RELEASE.zh-CN.md EVIDENCE-INDEX.zh-CN.md FLEET-CONTRACT.md
FREESTYLE-ADMISSION.zh-CN.md FREESTYLE-AGENT.zh-CN.md HIPPIUS-STORAGE-REVIEW.zh-CN.md
ITERATIONS.zh-CN.md LAUNCH-SCALE-NOVEL-PLAN.zh-CN.md LIGHTSAIL.md
LIUM-BOOTSTRAP.zh-CN.md LIUM-PRODUCTION-INTEGRATION.zh-CN.md LIUM-PROVIDER-CONTRACT.md
ON-DEMAND-GPU.zh-CN.md OPERATIONS.zh-CN.md OVERNIGHT-HANDOFF.md
PRODUCTION-SCALER.zh-CN.md PRODUCTION-WORKER.zh-CN.md QUEUE-CONTRACT.md README.md
SCALER-CONTRACT.md SCALING.zh-CN.md SIXNINE-READINESS.zh-CN.md START-HERE.zh-CN.md
VIDEO-API-V1-DRAFT.zh-CN.md WORKER-CLI.zh-CN.md WORKER-CONTRACT.md
deploy/platform/DNS-CUTOVER.zh-CN.md deploy/platform/FONTS.zh-CN.md
deploy/platform/GPU-SCALER.zh-CN.md deploy/platform/HOST-PREFLIGHT.zh-CN.md
deploy/platform/INFRASTRUCTURE.zh-CN.md deploy/platform/READINESS-REVIEW.zh-CN.md
deploy/platform/README.zh-CN.md deploy/platform/RELEASE.zh-CN.md
deploy/platform/RUNTIME-SECRETS.zh-CN.md deploy/platform/STACK-VALIDATION.zh-CN.md
deploy/platform/ec2/CD.zh-CN.md deploy/platform/ec2/README.zh-CN.md
""".split())

# Only modifications to these reviewed prose files use the lightweight path.
# Additions/deletions/type changes, unknown Markdown, published Skills, and
# executable workflow/configuration files still require complete checks.
GOVERNANCE_DOCUMENTS = frozenset("""
AGENTS.md WORKFLOW.md WORKFLOW.zh-CN.md PROJECT-PLAN.md
CURRENT-BASELINE.md GENERATION-CONTRACT.md GENERATION-FOUNDATION-RESULT.md
PLANNING-INDEX.zh-CN.md UNIFIED-BACKEND-API-PLAN.zh-CN.md REUSE-AND-MIGRATION-DECISION.zh-CN.md
workflow/owner-changes.md
""".split())

FRONTEND_ROOT = frozenset({"yingxu/index.html", "yingxu/package.json", "yingxu/package-lock.json",
                           "yingxu/vite.config.js", "yingxu/source-manifest.json",
                           "yingxu/src/NOVICE-STORIES-AC.md"})
# Ordinary backend changes still run the complete Python and PostgreSQL suites
# plus the platform image/runtime check. Security, money, queue and provider
# code is deliberately absent: it falls back to all checks, including legacy/UI.
BACKEND_MODULES = frozenset({"agent_discovery", "caption_server", "diagnostics", "generation_draft",
                             "guided", "guided_schema", "media", "media_process", "project_activity",
                             "project_validation", "render_backend", "render_cli", "render_plans"})
GATES = ("python", "postgres", "frontend", "containers", "legacy", "frontend_tools", "macos_helper")
SHA = re.compile(r"[0-9a-f]{40}")


def path_kind(path: str) -> str:
    if (not path or path.startswith("/") or "\\" in path or ":" in path
            or any(ord(c) < 32 for c in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        return "full"
    if path in DOCUMENTS or path in GOVERNANCE_DOCUMENTS:
        return "docs"
    if path in FRONTEND_ROOT or re.fullmatch(r"yingxu/src/(?:[\w.-]+/)*[\w.-]+\.(?:js|jsx|css|json)", path):
        return "frontend"
    if path in {f"studio_platform/{name}.py" for name in BACKEND_MODULES}:
        return "backend"
    if path in {f"test_platform_{name}.py" for name in BACKEND_MODULES}:
        return "backend"
    return "full"


def selection(kinds, *, reason: str) -> dict:
    kinds = set(kinds)
    full = "full" in kinds
    backend = full or "backend" in kinds
    frontend = full or "frontend" in kinds
    return {
        "category": "full" if full else "+".join(sorted(kinds)) or "unchanged",
        "reason": reason,
        "python": backend, "postgres": backend, "frontend": frontend,
        "containers": backend, "legacy": full,
        # The full Python suite already includes the frontend release tool tests.
        "frontend_tools": frontend and not backend,
        # Credential helpers and unknown/critical changes take the complete
        # path. Native Keychain proof cannot be replaced by Linux mocks.
        "macos_helper": full,
    }


def classify(changes: list[tuple[str, str]]) -> dict:
    kinds = []
    for status, path in changes:
        # --no-renames produces A+D for moves. Never hide a backend deletion
        # behind the new frontend/doc path. Type changes also force full checks.
        if status != "M":
            return selection({"full"}, reason="non-modification-requires-full")
        kind = path_kind(path)
        if kind == "full":
            return selection({"full"}, reason="unknown-or-critical-path")
        kinds.append(kind)
    return selection(kinds, reason="complete-local-diff")


def parse_diff(raw: bytes) -> list[tuple[str, str]]:
    if not raw:
        return []
    fields = raw.decode("utf-8", errors="strict").split("\0")
    if fields.pop() != "" or len(fields) % 2:
        raise ValueError("malformed_diff")
    changes = list(zip(fields[::2], fields[1::2]))
    if any(status not in {"A", "D", "M", "T", "U", "X", "B"} or not path for status, path in changes):
        raise ValueError("unexpected_diff_status")
    return changes


def git_changes(root: Path, base: str, head: str) -> list[tuple[str, str]]:
    if not SHA.fullmatch(base or "") or not SHA.fullmatch(head or "") or base == "0" * 40:
        raise ValueError("unavailable_baseline")

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)

    if git("rev-parse", "HEAD").decode("ascii").strip() != head:
        raise ValueError("checkout_does_not_match_tested_sha")
    # No arbitrary revisions/options are accepted, and we do not fetch using a
    # PR-supplied remote. Checkout fetch-depth: 0 supplies the local history.
    git("cat-file", "-e", base + "^{commit}")
    git("cat-file", "-e", head + "^{commit}")
    return parse_diff(git("diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                         "--name-status", "-z", base, head, "--"))


def truth(value) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value is None or value in ("false", ""):
        return False
    raise ValueError("invalid_boolean_input")


def dispatch_action(inputs: dict, ref: str) -> str:
    if __package__:
        from .native_source_retention import binding
    else:
        from native_source_retention import binding
    prepare = truth(inputs.get("deploy"))
    kind = inputs.get("release_kind") or "platform"
    approved = inputs.get("approved_commit") or ""
    frontend = inputs.get("approved_frontend_commit") or ""
    promoted = inputs.get('promote_commit') or ''
    resume = inputs.get("resume_command") or ""
    if kind not in {"platform", "frontend"}:
        raise ValueError("invalid_release_kind")
    if sum((prepare, bool(approved), bool(frontend), bool(promoted))) > 1:
        raise ValueError("preparation_and_approved_deployment_are_exclusive")
    if any(value and not SHA.fullmatch(value) for value in (approved, frontend, promoted)):
        raise ValueError("approved_commit_must_be_full_sha")
    if resume and not (approved or frontend or promoted):
        raise ValueError("resume_requires_an_approved_deployment")
    if resume and not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", resume):
        raise ValueError("invalid_resume_command")
    financial_hotfix_prepare = (ref == "refs/heads/codex/financial254-a76-hotfix-20261017"
        and prepare and kind == "platform" and not (approved or frontend or promoted or resume)
        and (inputs.get("retain_native_sources_transport") or "github") == "github")
    if (prepare or approved or frontend or promoted) and ref != "refs/heads/main" and not financial_hotfix_prepare:
        raise ValueError("release_actions_require_main")
    if binding(inputs) is not None and not (prepare and kind == "platform" and not approved and not frontend and not promoted):
        raise ValueError("native_retention_requires_platform_preparation")
    transport = inputs.get("retain_native_sources_transport") or "github"
    if transport not in {"github", "s3"}:
        raise ValueError("native_retention_transport_invalid")
    if transport == "s3" and (binding(inputs) is None or not prepare or kind != "platform"):
        raise ValueError("native_retention_s3_requires_explicit_binding")
    if approved:
        raise ValueError('production_requires_staging_accepted_promotion')
    if frontend:
        raise ValueError('production_requires_staging_accepted_promotion')
    if promoted:
        return 'promote'
    return "prepare-" + kind if prepare else "check"


def plan(event_name: str, event: dict, head: str, ref: str, root: Path) -> dict:
    if not SHA.fullmatch(head or ""):
        raise ValueError("invalid_tested_sha")
    action = dispatch_action(event.get("inputs") or {}, ref) if event_name == "workflow_dispatch" else "check"
    if action.startswith("deploy-") or action == 'promote':
        result = selection([], reason="independently-approved-deployment")
    elif action == "prepare-frontend":
        result = selection({"frontend"}, reason="independent-frontend-artifact-contract")
    elif event_name == "workflow_dispatch":
        # Platform preparation always retests the exact SHA completely. Manual
        # verification also takes the safe full path; no operator path override.
        result = selection({"full"}, reason="explicit-platform-verification")
    else:
        base = (event.get("pull_request") or {}).get("base", {}).get("sha") if event_name == "pull_request" else event.get("before")
        try:
            if event_name not in {"push", "pull_request"}:
                raise ValueError("unsupported_event")
            changes = git_changes(root, base, head)
            result = classify(changes)
            result["changed_count"] = len(changes)
        except (ValueError, UnicodeError, OSError, subprocess.SubprocessError):
            # Missing history, unreadable or malformed paths must never produce
            # an empty successful docs-only run. No raw paths/errors are logged.
            result = selection({"full"}, reason="diff-unavailable-requires-full")
    if event_name == 'push' and ref == 'refs/heads/main':
        if result['containers']:
            # A platform candidate includes the matching static/API pair.
            result['frontend'] = True
            action = 'stage-platform'
        elif result['frontend']:
            action = 'stage-frontend'
    return {**result, "action": action, "tested_sha": head}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-path", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--head", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default="ci-plan.json")
    args = parser.parse_args()
    if not args.event_path:
        raise SystemExit("CI event file is required")
    try:
        result = plan(args.event_name, json.loads(Path(args.event_path).read_text(encoding="utf-8")),
                      args.head, args.ref, args.root)
    except ValueError as exc:
        # Errors above are fixed descriptions, never credentials or event data.
        raise SystemExit(str(exc)) from None
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if target := os.environ.get("GITHUB_OUTPUT"):
        with open(target, "a", encoding="utf-8") as stream:
            for key in (*GATES, "category", "action", "tested_sha"):
                value = result[key]
                stream.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
