"""Fail-closed CI selection from the complete local Git diff, never an API list.

Only reviewed paths may reduce checks. Prose-only additions/archive moves use
the same light gate as prose edits. Executable additions/removals, type changes,
malformed or unknown paths require the complete platform checks.
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
import urllib.request

if __package__:
    from .ci_test_shards import SLOW_MODULE_PATHS
else:
    from ci_test_shards import SLOW_MODULE_PATHS


# This exact allowlist is also the release preparation dirty-document exemption.
# Keep CI-only governance paths separate: lightweight checks do not authorize
# preparing a release while its governing instructions are uncommitted.
DOCUMENTS = frozenset("""
docs/archive/AGENT-API-RELEASE-20261004.zh-CN.md docs/AGENT-ONBOARDING.zh-CN.md
docs/API-PROVIDER-COMPATIBILITY.zh-CN.md docs/API-USAGE.zh-CN.md docs/ARCHITECTURE.zh-CN.md
docs/ASSET-RECOVERY.zh-CN.md docs/archive/BACKEND-HANDOFF-20261004.zh-CN.md docs/BACKUP-RECOVERY.zh-CN.md
docs/CAPTION-BURN-IN-CONTRACT.zh-CN.md
docs/CHAPTER-RENDER-CONTRACT.md docs/COLD-START-CONTRACT.zh-CN.md docs/CONTROLS.zh-CN.md
docs/DELIVERY-REPORT.zh-CN.md DEVELOPMENT-RELEASE.zh-CN.md docs/EVIDENCE-INDEX.zh-CN.md docs/FLEET-CONTRACT.md
docs/FREESTYLE-ADMISSION.zh-CN.md docs/FREESTYLE-AGENT.zh-CN.md docs/HIPPIUS-STORAGE-REVIEW.zh-CN.md
docs/ITERATIONS.zh-CN.md docs/LAUNCH-SCALE-NOVEL-PLAN.zh-CN.md docs/LIGHTSAIL.md
docs/LIUM-BOOTSTRAP.zh-CN.md docs/LIUM-PRODUCTION-INTEGRATION.zh-CN.md docs/LIUM-PROVIDER-CONTRACT.md
docs/ON-DEMAND-GPU.zh-CN.md docs/OPERATIONS.zh-CN.md docs/archive/OVERNIGHT-HANDOFF.md
docs/PRODUCTION-SCALER.zh-CN.md docs/PRODUCTION-WORKER.zh-CN.md docs/QUEUE-CONTRACT.md README.md
docs/SCALER-CONTRACT.md docs/SCALING.zh-CN.md docs/SIXNINE-READINESS.zh-CN.md docs/START-HERE.zh-CN.md
docs/VIDEO-API-V1-DRAFT.zh-CN.md docs/WORKER-CLI.zh-CN.md docs/WORKER-CONTRACT.md
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
AGENTS.md WORKFLOW.md docs/WORKFLOW.zh-CN.md docs/PROJECT-PLAN.md
docs/CURRENT-BASELINE.md docs/GENERATION-CONTRACT.md docs/GENERATION-FOUNDATION-RESULT.md
docs/PLANNING-INDEX.zh-CN.md docs/UNIFIED-BACKEND-API-PLAN.zh-CN.md docs/REUSE-AND-MIGRATION-DECISION.zh-CN.md
workflow/owner-changes.md
""".split())

FRONTEND_ROOT = frozenset({"inkseq-web/index.html", "inkseq-web/package.json", "inkseq-web/package-lock.json",
                           "inkseq-web/vite.config.js", "inkseq-web/source-manifest.json",
                           "inkseq-web/src/NOVICE-STORIES-AC.md"})
# Reviewed backend changes run both suites and the platform image; only the
# explicit slow-module dependency list can narrow unrelated CPU integration.
# Security, money, queue and provider code falls back to every current check.
BACKEND_MODULES = frozenset({"agent_discovery", "caption_server", "diagnostics", "generation_draft",
                             "guided", "guided_schema", "media", "media_process", "project_activity",
                             "project_validation", "render_backend", "render_cli", "render_plans"})
GATES = ("python", "postgres", "frontend", "containers", "frontend_tools", "macos_helper")
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = "inkseq/h3-studio"
WORKFLOW_PATH = ".github/workflows/ci.yml"
MAX_RUN_PAGES = 3
RUNS_PER_PAGE = 10


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ValueError("staging_baseline_redirect_refused")


def github_json(path):
    """Read only fixed-repository Actions metadata; never log the runner token."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or not token
        or len(token) > 4096
        or any(ord(c) < 33 for c in token)
        or not path.startswith("/repos/" + REPOSITORY + "/actions/")
    ):
        raise ValueError("staging_baseline_unavailable")
    url = "https://api.github.com" + path
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            if response.status != 200 or response.url != url:
                raise ValueError()
            raw = response.read(2 * 1024**2 + 1)
        if len(raw) > 2 * 1024**2:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except Exception:
        raise ValueError("staging_baseline_unavailable") from None


def tested_in_pull_request(root, head, *, read_json=None):
    """Whether this repository's CI already passed a pull request on this exact tree.

    Branch protection requires an up-to-date branch, so a squash merge has the
    tree its pull request tested. Any doubt returns False and the tests run.
    """
    try:
        tree = subprocess.check_output(
            ["git", "rev-parse", head + "^{tree}"], cwd=root, stderr=subprocess.DEVNULL
        ).decode().strip()
        runs = (read_json or github_json)(
            "/repos/" + REPOSITORY + "/actions/runs?event=pull_request&status=success&per_page=50"
        ).get("workflow_runs")
        return any(
            run.get("path") == ".github/workflows/ci.yml"
            and run.get("conclusion") == "success"
            and (run.get("head_repository") or {}).get("full_name") == REPOSITORY
            and (run.get("head_commit") or {}).get("tree_id") == tree
            for run in runs
        )
    except (ValueError, TypeError, AttributeError, OSError, subprocess.SubprocessError):
        return False


def git_ancestor(root, base, head):
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head], cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )


def staging_platform_baseline(root, head, *, read_json=None):
    """Find a completed platform deployment, not merely a green test/publish.

    The attempt-specific jobs endpoint prevents mixing an older successful job
    with a failed rerun. Bounded or unavailable history falls back to full CI.
    Changed paths still come exclusively from the complete local Git diff.
    """
    read_json = read_json or github_json
    prefix = "/repos/" + REPOSITORY + "/actions/"
    for page in range(1, MAX_RUN_PAGES + 1):
        value = read_json(prefix + "workflows/ci.yml/runs?branch=main&event=push"
                          + "&status=success&per_page=" + str(RUNS_PER_PAGE)
                          + "&page=" + str(page))
        if not isinstance(value, dict):
            raise ValueError("staging_baseline_invalid")
        runs = value.get("workflow_runs")
        if not isinstance(runs, list) or len(runs) > RUNS_PER_PAGE:
            raise ValueError("staging_baseline_invalid")
        for run in runs:
            if not isinstance(run, dict) or (
                not isinstance(run.get("repository"), dict)
                or run["repository"].get("full_name") != REPOSITORY
                or run.get("path") != WORKFLOW_PATH
                or run.get("event") != "push" or run.get("head_branch") != "main"
            ):
                raise ValueError("staging_baseline_invalid")
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                continue
            commit, run_id, attempt = run.get("head_sha"), run.get("id"), run.get("run_attempt")
            if (not isinstance(commit, str) or not SHA.fullmatch(commit)
                    or type(run_id) is not int or run_id <= 0
                    or type(attempt) is not int or attempt <= 0):
                raise ValueError("staging_baseline_invalid")
            jobs_value = read_json(prefix + "runs/" + str(run_id) + "/attempts/"
                                   + str(attempt) + "/jobs?per_page=100&page=1")
            if not isinstance(jobs_value, dict):
                raise ValueError("staging_baseline_invalid")
            jobs = jobs_value.get("jobs")
            if (not isinstance(jobs, list) or type(jobs_value.get("total_count")) is not int
                    or jobs_value["total_count"] != len(jobs) or len(jobs) > 100):
                raise ValueError("staging_baseline_invalid")
            selected = {}
            for job in jobs:
                if (not isinstance(job, dict) or type(job.get("run_id")) is not int
                        or job.get("run_id") != run_id or not isinstance(job.get("name"), str)
                        or job.get("head_sha") != commit
                        or "run_attempt" in job and type(job["run_attempt"]) is not int
                        or job.get("run_attempt", attempt) != attempt):
                    raise ValueError("staging_baseline_invalid")
                name = job.get("name")
                if name in {"containers", "deploy-staging-aws"}:
                    if name in selected:
                        raise ValueError("staging_baseline_invalid")
                    selected[name] = job.get("status") == "completed" and job.get("conclusion") == "success"
            if selected == {"containers": True, "deploy-staging-aws": True}:
                git_ancestor(root, commit, head)
                return {"commit": commit, "run_id": run_id, "run_attempt": attempt}
        if len(runs) < RUNS_PER_PAGE:
            break
    raise ValueError("staging_baseline_unavailable")


def path_kind(path: str) -> str:
    if (not path or path.startswith("/") or "\\" in path or ":" in path
            or any(ord(c) < 32 for c in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        return "full"
    if path in DOCUMENTS or path in GOVERNANCE_DOCUMENTS:
        return "docs"
    if (re.fullmatch(r"docs/(?:[\w.-]+/)*[\w.-]+\.md", path)
            and not re.search(r"(?:^|[._/-])(?:security|auth|credentials?|secrets?)(?:[._/-]|$)",
                              path, re.IGNORECASE)):
        return "docs"
    if path in FRONTEND_ROOT or re.fullmatch(r"inkseq-web/src/(?:[\w.-]+/)*[\w.-]+\.(?:js|jsx|css|json)", path):
        return "frontend"
    if path in {f"studio_platform/{name}.py" for name in BACKEND_MODULES}:
        return "backend"
    if path in {f"tests/test_platform_{name}.py" for name in BACKEND_MODULES}:
        return "backend"
    return "full"


def selection(kinds, *, reason: str, slow_modules=None) -> dict:
    kinds = set(kinds)
    full = "full" in kinds
    backend = full or "backend" in kinds
    frontend = full or "frontend" in kinds
    return {
        "category": "full" if full else "+".join(sorted(kinds)) or "unchanged",
        "reason": reason,
        "python": backend, "postgres": backend, "frontend": frontend,
        "containers": backend,
        # The full Python suite already includes the frontend release tool tests.
        "frontend_tools": frontend and not backend,
        # Credential helpers and unknown/critical changes take the complete
        # path. Native Keychain proof cannot be replaced by Linux mocks.
        "macos_helper": full,
        "slow_modules": None if full else slow_modules,
    }


def classify(changes: list[tuple[str, str]]) -> dict:
    kinds = []
    for status, path in changes:
        # --no-renames produces A+D for moves. Never hide a backend deletion
        # behind a new doc path. Only prose A+D is safe to classify lightly.
        kind = path_kind(path)
        if status != "M" and not (status in {"A", "D"} and kind == "docs"):
            return selection({"full"}, reason="non-modification-requires-full")
        if kind == "full":
            return selection({"full"}, reason="unknown-or-critical-path")
        kinds.append(kind)
    paths = {path for _, path in changes}
    slow_modules = sorted(name for name, dependencies in SLOW_MODULE_PATHS.items()
                          if paths & dependencies)
    return selection(kinds, reason="complete-local-diff", slow_modules=slow_modules)


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
    if (prepare or approved or frontend or promoted) and ref != "refs/heads/main":
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


FULL_RETEST_REASONS = frozenset({"diff-unavailable-requires-full", "staging-baseline-unavailable-requires-full"})


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
        try:
            baseline = staging_platform_baseline(root, head)
            cumulative = classify(git_changes(root, baseline['commit'], head))
            if cumulative['containers']:
                result = selection({'full'}, reason='unshipped-platform-changes')
            result['staging_platform_baseline'] = baseline
        except (ValueError, UnicodeError, OSError, subprocess.SubprocessError):
            result = selection({'full'}, reason='staging-baseline-unavailable-requires-full')
        if result['containers']:
            # A platform candidate includes the matching static/API pair.
            result['frontend'] = True
            action = 'stage-platform'
        elif result['frontend']:
            action = 'stage-frontend'
        # The frontend and containers jobs still run: they build what gets published.
        if result['python'] and result['reason'] not in FULL_RETEST_REASONS and tested_in_pull_request(root, head):
            result.update(python=False, postgres=False, macos_helper=False, tested_in_pr=True)
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
            for key in (*GATES, "category", "action", "tested_sha", "slow_modules"):
                value = result[key]
                rendered = json.dumps(value, separators=(",", ":")) if key == "slow_modules" else (
                    str(value).lower() if isinstance(value, bool) else value)
                stream.write(f"{key}={rendered}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
