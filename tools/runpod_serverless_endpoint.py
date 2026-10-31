"""Create the RunPod Serverless template + endpoint for dial-out workers (#859).

The template runs the pinned WanGP image as is. Its start command installs the
pinned RunPod SDK and execs deploy/runpod-serverless/handler.py, passed as a
base64 template variable (public code only, no secrets). The endpoint scales
to zero (workersMin 0) and needs a CUDA 13 driver for the cu130 v9 image.

    python tools/runpod_serverless_endpoint.py --image <repo@sha256:...> \
        --name inkseq-staging-dialout --gpu "NVIDIA GeForce RTX 5090" [--dry-run]

The API key comes from the central registry (`--profile`); it is never printed.
Prints the template and endpoint ids for the controller's serverless file.
"""

import argparse
import base64
import json
from pathlib import Path
import re
import sys

REST = "https://rest.runpod.io/v1"
SDK = "runpod==1.12.0"
HANDLER = Path(__file__).resolve().parents[1] / "deploy/runpod-serverless/handler.py"
START_COMMAND = (
    "set -e; PY=python3; "
    f"$PY -m pip install -q --no-cache-dir --break-system-packages {SDK} 2>/dev/null"
    f" || $PY -m pip install -q --no-cache-dir {SDK}; "
    'printf %s "$INKSEQ_HANDLER_B64" | base64 -d > /tmp/inkseq-handler.py; '
    "exec $PY /tmp/inkseq-handler.py"
)


def bodies(image, name, gpus, disk_gib, max_workers, execution_seconds):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}", image):
        raise SystemExit("image must be pinned by digest")
    handler = base64.b64encode(HANDLER.read_bytes()).decode()
    template = {
        "name": name,
        "imageName": image,
        "isServerless": True,
        "containerDiskInGb": disk_gib,
        "volumeInGb": 0,
        "ports": [],
        "dockerEntrypoint": ["bash", "-c"],
        "dockerStartCmd": [START_COMMAND],
        "env": {"INKSEQ_HANDLER_B64": handler},
    }
    endpoint = {
        "name": name,
        "computeType": "GPU",
        "gpuTypeIds": gpus,
        "gpuCount": 1,
        "minCudaVersion": "13.0",
        "workersMin": 0,
        "workersMax": max_workers,
        "idleTimeout": 5,
        "flashboot": True,
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 1,
        "executionTimeoutMs": execution_seconds * 1000,
    }
    return template, endpoint


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--gpu", action="append", required=True)
    parser.add_argument("--disk-gib", type=int, default=200)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--execution-seconds", type=int, default=7200)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    template, endpoint = bodies(
        args.image,
        args.name,
        args.gpu,
        args.disk_gib,
        args.max_workers,
        args.execution_seconds,
    )
    if args.dry_run:
        shown = dict(template, env={"INKSEQ_HANDLER_B64": "<handler.py base64>"})
        print(json.dumps({"template": shown, "endpoint": endpoint}, indent=1))
        return 0
    import httpx

    sys.path.insert(0, r"C:\Users\danmo\Desktop\AI-Registry")
    sys.path.insert(0, "/mnt/c/Users/danmo/Desktop/AI-Registry")
    from api_registry import load_api

    key = load_api("runpod", profile=args.profile).api_key
    with httpx.Client(
        base_url=REST, headers={"Authorization": "Bearer " + key}, timeout=60
    ) as http:
        made = http.post("/templates", json=template)
        if made.status_code >= 300:
            raise SystemExit("template create failed: HTTP %d" % made.status_code)
        template_id = made.json()["id"]
        made = http.post("/endpoints", json=dict(endpoint, templateId=template_id))
        if made.status_code >= 300:
            raise SystemExit("endpoint create failed: HTTP %d" % made.status_code)
        print(
            json.dumps({"template_id": template_id, "endpoint_id": made.json()["id"]})
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
