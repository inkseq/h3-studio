"""One reviewed financial-only source tuple; never treat a branch run as main.

The normal main publisher and its event guards stay unchanged. This separate
entry requires a main-pinned source binding and the original full CI run on that
exact source. It can publish a staging candidate, never approve a host release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPOSITORY = "inkseq/h3-studio"
BRANCH = "codex/financial254-a76-hotfix-20261017"
BASE = "a76e2171fe6f2066beb60c24e686815ffd327ace"
WORKFLOW = ".github/workflows/financial254-hotfix.yml"
BINDING = ROOT / "workflow/financial254-hotfix.json"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")


def need(value, code):
    if not value:
        raise ValueError(code)


def binding(path=BINDING):
    value = json.loads(Path(path).read_bytes())
    need(set(value) == {"version", "enabled", "issue", "repository", "source_branch", "base_commit",
                        "source_commit", "source_tree", "diff_sha256", "changed_paths"}, "binding_fields")
    need(value["version"] == 1 and value["enabled"] is True and value["issue"] == 254, "binding_disabled")
    need(value["repository"] == REPOSITORY and value["source_branch"] == BRANCH
         and value["base_commit"] == BASE, "binding_scope")
    need(SHA.fullmatch(value["source_commit"]) and SHA.fullmatch(value["source_tree"])
         and DIGEST.fullmatch(value["diff_sha256"]), "binding_identity")
    paths = value["changed_paths"]
    need(isinstance(paths, list) and paths and paths == sorted(set(paths)), "binding_paths")
    need(all(isinstance(p, str) and p and not p.startswith(("/", "-")) and "\\" not in p
             and ":" not in p and all(s not in {"", ".", ".."} for s in p.split("/")) for p in paths),
         "binding_unsafe_path")
    return value


def trusted_context(env):
    need(env.get("GITHUB_REPOSITORY") == REPOSITORY and env.get("GITHUB_REF") == "refs/heads/main"
         and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "trusted_main_dispatch_required")
    need(env.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + WORKFLOW + "@refs/heads/main",
         "trusted_workflow_required")
    need(SHA.fullmatch(env.get("GITHUB_SHA", "")) and env.get("GITHUB_WORKFLOW_SHA") == env["GITHUB_SHA"],
         "trusted_workflow_revision_required")


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)


def source_proof(value, root):
    commit = value["source_commit"]
    need(git(root, "rev-parse", "HEAD").decode().strip() == commit, "source_checkout_split")
    need(git(root, "rev-parse", commit + "^{tree}").decode().strip() == value["source_tree"], "source_tree_split")
    git(root, "merge-base", "--is-ancestor", BASE, commit)
    raw = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", BASE, commit, "--")
    need(hashlib.sha256(raw).hexdigest() == value["diff_sha256"], "source_diff_split")
    names = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                BASE, commit, "--").decode("utf-8").split("\0")
    need(names.pop() == "" and sorted(names) == value["changed_paths"], "source_paths_split")
    need(not git(root, "status", "--porcelain", "--untracked-files=no"), "source_checkout_dirty")


def ci_proof(value, run, jobs, artifacts, requested_run):
    need(type(requested_run) is int and requested_run > 0 and run.get("id") == requested_run, "ci_run_split")
    need(run.get("repository", {}).get("full_name") == REPOSITORY
         and run.get("head_repository", {}).get("full_name") == REPOSITORY, "ci_repository_split")
    need(run.get("event") == "workflow_dispatch" and run.get("path") == ".github/workflows/ci.yml"
         and run.get("head_branch") == BRANCH and run.get("head_sha") == value["source_commit"], "ci_source_split")
    need(run.get("status") == "completed" and run.get("conclusion") == "success"
         and type(run.get("run_attempt")) is int and run["run_attempt"] > 0, "ci_not_successful")
    # Fetch every page with gh --paginate --slurp. A partial list fails totals.
    need(isinstance(jobs, list) and jobs and all(isinstance(p, dict) for p in jobs), "ci_jobs_pages")
    rows = [j for page in jobs for j in page.get("jobs", [])]
    total = jobs[0].get("total_count")
    need(type(total) is int and len(rows) == total and len({j["id"] for j in rows}) == total, "ci_jobs_incomplete")
    by_name = {}
    for row in rows:
        need(row.get("run_id") == requested_run and row.get("head_sha") == value["source_commit"]
             and row.get("run_attempt") == run["run_attempt"], "ci_job_source_split")
        need(row["name"] not in by_name, "ci_duplicate_job")
        by_name[row["name"]] = row
    required = {"changes", "test", "frontend", "containers", "macos-helper (macos-15)", "macos-helper (macos-15-intel)"}
    required |= {f"python ({i})" for i in range(6)} | {f"postgres ({i})" for i in range(6)}
    need(required <= set(by_name), "ci_required_job_missing")
    need(all(by_name[n].get("status") == "completed" and by_name[n].get("conclusion") == "success"
             for n in required), "ci_required_job_failed")
    need("frontend-tools" in by_name and by_name["frontend-tools"].get("conclusion") == "skipped",
         "ci_full_frontend_tools_selection_split")
    forbidden = {"publish-aws", "publish-frontend-aws", "publish-staging-candidate", "deploy-staging-aws", "promote-accepted-aws"}
    need(all(j.get("conclusion") == "skipped" for n, j in by_name.items() if n in forbidden), "ci_branch_privilege_unexpected")
    need(isinstance(artifacts, list) and artifacts, "ci_artifact_pages")
    items = [a for page in artifacts for a in page.get("artifacts", [])]
    need(len(items) == artifacts[0].get("total_count") and len({a["id"] for a in items}) == len(items), "ci_artifacts_incomplete")
    result = {}
    for kind in ("platform", "migration", "frontend"):
        name = f"sixnine-{kind}-{value['source_commit']}"
        matches = [a for a in items if a.get("name") == name]
        need(len(matches) == 1, "ci_exact_artifact_missing")
        item = matches[0]
        need(item.get("expired") is False and type(item.get("size_in_bytes")) is int and item["size_in_bytes"] > 0,
             "ci_artifact_unavailable")
        need(type(item.get("id")) is int and item["id"] > 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("digest", "")),
             "ci_artifact_identity_invalid")
        origin = item.get("workflow_run") or {}
        need(origin.get("id") == requested_run and origin.get("head_sha") == value["source_commit"]
             and origin.get("head_branch") == BRANCH, "ci_artifact_source_split")
        result[kind] = {k: item[k] for k in ("id", "name", "size_in_bytes", "digest")}
    return {"version": 1, "source_commit": value["source_commit"], "source_tree": value["source_tree"],
            "source_branch": BRANCH, "source_base": BASE, "source_diff_sha256": value["diff_sha256"],
            "run_id": requested_run, "run_attempt": run["run_attempt"], "artifacts": result,
            "attribution": "reviewed_financial_hotfix_branch_full_ci_not_main_source"}


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def extract_verified_archive(archive, directory, expected_digest):
    need(file_digest(archive) == expected_digest, "download_digest_split")
    # A new empty destination and regular-only entries prevent zip traversal,
    # symlink redirection, duplicates and shadowing the trusted main checkout.
    directory = Path(directory)
    need(not directory.exists() and not directory.is_symlink(), "download_destination_exists")
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        need(infos and len(infos) <= 10000 and sum(i.file_size for i in infos) <= 16 * 1024**3, "download_zip_bounds")
        names = set()
        for info in infos:
            name = info.filename.rstrip('/') if info.is_dir() else info.filename
            need(name and len(name) <= 2048 and not name.startswith('/') and '\\' not in name and ':' not in name
                 and all(p not in {'', '.', '..'} for p in name.split('/')) and name not in names,
                 "download_zip_path")
            names.add(name)
            mode = info.external_attr >> 16
            need(not stat.S_ISLNK(mode) and (not stat.S_IFMT(mode) or stat.S_ISREG(mode) or stat.S_ISDIR(mode)),
                 "download_zip_type")
        directory.mkdir(mode=0o700)
        for info in infos:
            destination = directory.joinpath(*info.filename.rstrip('/').split('/'))
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, destination.open('xb') as target:
                    while chunk := source.read(1024 * 1024): target.write(chunk)


def download(proof, root=Path('.')):
    for kind, artifact in proof['artifacts'].items():
        archive = root / (kind + '.verified-artifact.zip')
        with archive.open('xb') as out:
            result = subprocess.run(['gh', 'api', f"repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"],
                                    stdout=out, stderr=subprocess.PIPE, check=False)
        need(result.returncode == 0, "artifact_download_failed_preserve_original")
        extract_verified_archive(archive, root / (kind + '-release'), artifact['digest'])


def validate_downloads(proof, root=Path('.')):
    for kind, artifact in proof['artifacts'].items():
        need(file_digest(root / (kind + '.verified-artifact.zip')) == artifact['digest'], "consumed_artifact_split")
    from tools.check_platform_bundle import verify
    from tools.publish_staging_candidate import build
    from deploy.platform.frontend_bundle import validate
    build(proof['source_commit'], 'platform', proof['run_id'], proof['run_attempt'],
          platform=root/'platform-release', migration=root/'migration-release', frontend=root/'frontend-release')
    verify(root / 'platform-release', proof['source_commit'])
    validate(root / 'frontend-release', proof['source_commit'])
    # Validate BOTH derivative descriptors before the first immutable S3 write.
    import importlib.util
    previous = list(sys.path)
    sys.path.insert(0, str(ROOT / 'deploy/platform'))
    try:
        spec = importlib.util.spec_from_file_location('financial254_trusted_migration', ROOT / 'deploy/dstack/maintenance.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        directory = root / 'migration-release'
        value = module.manifest(directory, proof['source_commit'])
        module.release.validate_image_archive(directory / 'dispatch-image.tar.gz', {'commit': proof['source_commit'], **value['dispatch']})
        need('dstack' in value, "financial_hotfix_requires_all_three_services")
        module.release.validate_image_archive(directory / 'dstack-server-image.tar.gz', {'commit': proof['source_commit'], **value['dstack']})
    finally:
        sys.path[:] = previous


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("verify", "download", "preflight", "candidate"))
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args(argv)
    trusted_context(os.environ)
    value = binding()
    source_proof(value, args.source)
    proof = ci_proof(value, json.loads(args.run.read_bytes()), json.loads(args.jobs.read_bytes()),
                     json.loads(args.artifacts.read_bytes()), args.run_id)
    if args.phase == "verify":
        print(json.dumps(proof, sort_keys=True))
    elif args.phase == 'download':
        download(proof)
        print(json.dumps({'state':'exact_artifact_ids_and_zip_digests_verified','proof':proof}, sort_keys=True))
    else:
        validate_downloads(proof)
        if args.phase == 'preflight':
            print(json.dumps({'state':'all_three_archives_and_frontend_verified_before_oidc','proof':proof},sort_keys=True))
            return
        # This explicit entry does not modify/spoof GITHUB_SHA/REF/EVENT_NAME.
        from tools.publish_staging_candidate import build, publish
        candidate = build(value["source_commit"], "platform", proof["run_id"], proof["run_attempt"],
                          platform="platform-release", migration="migration-release", frontend="frontend-release")
        print(json.dumps({"proof": proof, "publication": publish(candidate)}, sort_keys=True))


if __name__ == "__main__":
    main()
