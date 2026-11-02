"""RunPod Serverless as a dstack-shaped backend for dial-out workers (#859).

The endpoint's start command runs deploy/runpod-serverless/handler.py, which
executes the run spec's own boot command with its own env, exactly as a dstack
container does. The worker then dials out, so readiness, dispatch, budget and
idle shutdown stay on the existing dstack capacity path. This client only maps
plan/apply/get/stop onto RunPod's /run, /status and /cancel.

One RunPod job is one run. The local journal (one private file per run name)
binds run name, dstack-style run id, job id and the exact applied spec; a
missing or unbound journal is never read as absence of a job.

Credentials: a protected JSON file named by the operator config's
`serverless_file`:
{"api_key", "endpoint_id", "gpu_name", "gpu_memory_gib", "cpus", "memory_gib",
 "disk_gib", "hourly_microusd", "state_dir"}.
"""

from pathlib import Path
import re
import time

import httpx

from .dstack_capacity import DstackError
from .providers.registry import RUNPOD_SERVERLESS
from .dialout_capacity import (
    BOOT_ENV as BOOT_ENV,
    JOB_ID as JOB_ID,
    LIVE as LIVE,
    MAX_EXECUTION_SECONDS as MAX_EXECUTION_SECONDS,
    RUN_NAME as RUN_NAME,
    STATUSES as STATUSES,
    DialoutCapacityClient,
    RoutedClient as RoutedClient,
    _require as _require,
    boot_input as boot_input,
)

BACKEND = RUNPOD_SERVERLESS.id
API = "https://api.runpod.ai/v2/"
ENDPOINT_ID = re.compile(r"[a-z0-9]{1,64}")
CONFIG_KEYS = frozenset(
    {
        "api_key",
        "endpoint_id",
        "gpu_name",
        "gpu_memory_gib",
        "cpus",
        "memory_gib",
        "disk_gib",
        "hourly_microusd",
        "state_dir",
    }
)


def validate_config(value):
    _require(
        isinstance(value, dict)
        and set(value) == CONFIG_KEYS
        and isinstance(value["api_key"], str)
        and 16 <= len(value["api_key"]) <= 4096
        and not any(c.isspace() for c in value["api_key"])
        and isinstance(value["endpoint_id"], str)
        and ENDPOINT_ID.fullmatch(value["endpoint_id"])
        and isinstance(value["gpu_name"], str)
        and re.fullmatch(r"[A-Za-z0-9 _-]{1,64}", value["gpu_name"])
        and all(
            type(value[k]) is int and 1 <= value[k] <= 100_000
            for k in ("gpu_memory_gib", "cpus", "memory_gib", "disk_gib")
        )
        and type(value["hourly_microusd"]) is int
        and 1 <= value["hourly_microusd"] <= 50_000_000
        and isinstance(value["state_dir"], str)
        and Path(value["state_dir"]).is_absolute(),
        "runpod_serverless_config_invalid",
    )
    return value


class RunPodServerlessClient(DialoutCapacityClient):
    """RunPod provider calls behind the shared native capacity lifecycle."""

    backend = BACKEND

    def __init__(self, config, project, *, http=None, clock=time.time):
        super().__init__(validate_config(config), project, clock=clock)
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)

    def _call(self, method, path, body=None):
        url = API + self.config["endpoint_id"] + path
        headers = {"Authorization": "Bearer " + self.config["api_key"]}
        try:
            response = self.http.request(method, url, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise self._provider_error(
                "request", "dstack_api_unavailable", error
            ) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise DstackError("dstack_api_unavailable")
        if response.status_code >= 400:
            raise DstackError("dstack_api_rejected")
        try:
            value = response.json()
        except ValueError:
            raise DstackError("dstack_response_invalid") from None
        _require(isinstance(value, dict), "dstack_response_invalid")
        return value

    def _status(self, record):
        """Live status; a terminal one is journaled once, since RunPod expires it."""
        final = record.get("final")
        if isinstance(final, dict):
            return final
        status = self._call("GET", "/status/" + record["job_id"])
        if status.get("status") in STATUSES and status["status"] not in LIVE:
            kept = {
                k: status[k]
                for k in ("status", "workerId", "executionTime")
                if k in status
            }
            record["final"] = kept
            self._write(record["run_name"], record, create=False)
        return status

    @property
    def instance_name(self):
        return self.config["endpoint_id"]

    def _launch(self, run_name, job, seconds):
        return self._call(
            "POST",
            "/run",
            {
                "input": job,
                "policy": {
                    "executionTimeout": seconds * 1000,
                    "ttl": (seconds + 3600) * 1000,
                },
            },
        )

    def _cancel(self, job_id):
        self._call("POST", "/cancel/" + job_id)

    def _live(self):
        """The provider's own count of queued plus running jobs."""
        health = self._call("GET", "/health").get("jobs")
        _require(isinstance(health, dict), "dstack_provider_inventory_unconfirmed")
        return health.get("inQueue", -1) + health.get("inProgress", -1)
