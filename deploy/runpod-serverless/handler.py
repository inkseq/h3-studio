"""RunPod Serverless worker for dial-out GPU hosts (#859).

The endpoint template runs the pinned WanGP image unchanged; its start command
installs the pinned RunPod SDK and execs this file (see START_COMMAND in
tools/runpod_serverless_endpoint.py). Each job carries exactly what a dstack run
spec carries: the single boot command and its three boot variables. The command
dials out to the platform and lives until the controller cancels the job, so
the platform never connects in and RunPod never sees generation inputs.

Input:  {command: "exec python3 - <<'INKSEQ_BOOT_SCRIPT'\\n...", env: {INKSEQ_BOOT_URL,
         INKSEQ_INTENT, INKSEQ_BOOT_TOKEN}}
Output: {exit_code} once the boot command exits (normally only on cancel).
"""

import os
import subprocess

PREFIX = "exec python3 - <<'INKSEQ_BOOT_SCRIPT'\n"
BOOT_ENV = {"INKSEQ_BOOT_URL", "INKSEQ_INTENT", "INKSEQ_BOOT_TOKEN"}


def refused(value):
    command = value.get("command") if isinstance(value, dict) else None
    env = value.get("env") if isinstance(value, dict) else None
    return not (
        isinstance(command, str)
        and command.startswith(PREFIX)
        and command.endswith("INKSEQ_BOOT_SCRIPT")
        and isinstance(env, dict)
        and set(env) == BOOT_ENV
        and all(isinstance(v, str) for v in env.values())
    )


def handler(job, run=subprocess.run):
    value = job.get("input") if isinstance(job, dict) else None
    if refused(value):
        # Only the platform's boot script; never an arbitrary command.
        return {"error": "inkseq_boot_input_refused"}
    done = run(
        ["bash", "-c", value["command"]],
        env={**os.environ, **value["env"]},
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return {"exit_code": done.returncode}


if __name__ == "__main__":
    import runpod

    runpod.serverless.start({"handler": handler})
