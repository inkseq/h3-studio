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

import logging
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


logger = logging.getLogger(__name__)
# An answer that does not prove RunPod made no job: a timeout may come from a
# proxy after RunPod queued it. Any other 4xx is RunPod declining the request
# (409 when the endpoint allows no workers, 429 for rate limits): a refusal,
# so the next lane is tried at once (#1272).
RETRYABLE = frozenset({408})


class RunPodServerlessClient(DialoutCapacityClient):
    """RunPod provider calls behind the shared native capacity lifecycle."""

    backend = BACKEND
    # Another 4xx is RunPod refusing before any job exists (#1268).
    DEFINITE_REJECTION = True
    # A cold worker shows as initializing within seconds; four minutes with
    # none means no GPU the endpoint may use is free (#1272).
    NO_WORKER_SECONDS = 240

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
        if response.status_code >= 400:
            # The status code only: response bodies may echo the job input.
            logger.warning(
                "capacity_provider_failure backend=%s operation=%s status=%d",
                self.backend,
                path.strip("/").split("/")[0],
                response.status_code,
            )
        if response.status_code in RETRYABLE or response.status_code >= 500:
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
        raw = status.get("status")
        if raw == "CANCELLED" and record.get("no_worker_at"):
            # Our own stop of a job that never got a worker (below).
            status = {"status": "NO_CAPACITY"}
        elif raw == "IN_QUEUE" and (
            record.get("no_worker_at") or self._no_worker(record)
        ):
            # RunPod queues a job even when no GPU it may use is free (the
            # endpoint's data center is pinned by its volume). Waiting out the
            # 20 min start timeout, then retrying the same lane, left shots
            # unmade for good on staging (#1272). This read only marks it: the
            # observation cools the lane and the watchdog stops the node,
            # whose stop cancels the job. Reads never change the provider.
            if not record.get("no_worker_at"):
                record["no_worker_at"] = self.clock()
                self._write(record["run_name"], record, create=False)
            status = {**status, "no_worker": True}
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

    def _no_worker(self, record):
        """A queued job past NO_WORKER_SECONDS while RunPod has no worker at all."""
        created = record.get("created_at")
        if (
            type(created) not in (int, float)
            or self.clock() - created < self.NO_WORKER_SECONDS
        ):
            return False
        try:
            workers = self._call("GET", "/health").get("workers")
        except DstackError:
            return False
        keys = ("initializing", "idle", "ready", "running")
        return (
            isinstance(workers, dict)
            and all(type(workers.get(key)) is int and workers[key] >= 0 for key in keys)
            and sum(workers[key] for key in keys) == 0
        )

    def _unbound_absent(self):
        # With no job queued or running on our endpoint, a lost submit made none.
        try:
            return self._live() == 0
        except DstackError:
            return False

    def _live(self):
        """The provider's own count of queued plus running jobs."""
        health = self._call("GET", "/health").get("jobs")
        _require(
            isinstance(health, dict)
            and all(
                type(health.get(key)) is int and health[key] >= 0
                for key in ("inQueue", "inProgress")
            ),
            "dstack_provider_inventory_unconfirmed",
        )
        return health["inQueue"] + health["inProgress"]
