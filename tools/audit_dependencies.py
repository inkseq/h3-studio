"""Advisory scan of checked-in pinned requirements; never installs or fixes them."""

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "requirements.lock.txt",
    "requirements.dispatch.lock.txt",
    "requirements.workflow-dbos.lock.txt",
    "requirements.workflow-procrastinate.lock.txt",
    "requirements.dev.lock.txt",
    "requirements.audit.lock.txt",
    "deploy/tls/requirements.dstack.lock.txt",
    "deploy/observability/requirements.lock.txt",
    "deploy/wangp/requirements.ocr.txt",
)


def main():
    directory = ROOT / "dependency-audit"
    directory.mkdir(exist_ok=True)
    results = []
    for index, name in enumerate(FILES):
        output = directory / f"{index}.json"
        output.unlink(
            missing_ok=True
        )  # Never attach an earlier scan to a failed retry.
        # Complete pinned inputs only. No dependency resolution, installation,
        # --fix, application imports or access to deployment credentials.
        command = [
            sys.executable,
            "-m",
            "pip_audit",
            "-r",
            name,
            "--no-deps",
            "--disable-pip",
            "--format",
            "json",
            "--output",
            str(output),
            "--progress-spinner",
            "off",
        ]
        try:
            run = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, timeout=120
            )
            status = "clean" if run.returncode == 0 else "findings_or_error"
            details = run.stderr[-12000:]
            code = run.returncode
        except subprocess.TimeoutExpired:
            status, details, code = (
                "timeout",
                "Audit did not finish within 120 seconds",
                None,
            )
        results.append(
            {
                "requirements": name,
                "status": status,
                "exit_code": code,
                "report": output.name if output.exists() else None,
                "diagnostics": details,
            }
        )
        print(f"{name}: {status}", flush=True)
    (directory / "summary.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    findings = any(row["status"] != "clean" for row in results)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(
                "## Advisory dependency audit\n\nNo automatic fixes; no promotion gate. "
                "See the dependency-audit artifact for details. Errors are not clean results.\n\n"
            )
            for row in results:
                stream.write(f"- `{row['requirements']}`: **{row['status']}**\n")
    if findings and os.environ.get("GITHUB_ACTIONS") == "true":
        print(
            "::warning::Dependency audit has findings or errors; see the advisory artifact and job summary."
        )
    return int(findings)


if __name__ == "__main__":
    raise SystemExit(main())
