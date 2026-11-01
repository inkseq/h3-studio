"""Modal sandboxes running the dial-out worker (#859): one sandbox per run.

Same contract as runpod_serverless: the sandbox runs the run spec's own boot
command with its own env, dials out, and lives until the controller terminates
it. The verified model Volume is mounted and named by INKSEQ_MODEL_CACHE
(#924), so a cold start skips the download. This is a separate provider
("modal-dialout") from the older Modal lane (provider "modal"), whose
dispatch, admission and lifecycle code stays untouched.

Credentials: a protected JSON file named by the operator config's
`modal_dialout_file`:
{"token_id", "token_secret", "app_name", "image", "volume_id", "gpu",
 "gpu_name", "gpu_memory_gib", "cpus", "memory_gib", "disk_gib",
 "hourly_microusd", "state_dir"}.
The Modal SDK is loaded on first provider use only.
"""

from pathlib import Path
import re
import time

from .dstack_capacity import DstackError
from .runpod_serverless import RunPodServerlessClient, _require

BACKEND = "modal-dialout"
MOUNT = "/ckpts"
CACHE = MOUNT + "/ckpts"  # The verified model files under the Volume root.
CONFIG_KEYS = frozenset(
    {
        "token_id",
        "token_secret",
        "app_name",
        "image",
        "volume_id",
        "gpu",
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
        and all(
            isinstance(value[k], str) and 8 <= len(value[k]) <= 256
            for k in ("token_id", "token_secret")
        )
        and isinstance(value["app_name"], str)
        and re.fullmatch(r"[a-z0-9-]{1,64}", value["app_name"])
        and isinstance(value["image"], str)
        and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}", value["image"]
        )
        and isinstance(value["volume_id"], str)
        and re.fullmatch(r"vo-[A-Za-z0-9]{1,64}", value["volume_id"])
        and value["gpu"] in {"RTX-PRO-6000", "B200", "L40S"}
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
        "modal_dialout_config_invalid",
    )
    return value


class ModalDialoutClient(RunPodServerlessClient):
    backend = BACKEND

    def __init__(self, config, project, *, sdk=None, clock=time.time):
        self.config = validate_config(config)
        self.project, self.clock = project, clock
        self.root = Path(config["state_dir"])
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._sdk, self._client, self._app = sdk, None, None

    @property
    def instance_name(self):
        return self.config["app_name"]

    def _modal(self):
        """(sdk, client, app), created on first provider use only."""
        if self._app is None:
            try:
                if self._sdk is None:
                    import modal

                    self._sdk = modal
                c = self.config
                self._client = self._sdk.Client.from_credentials(
                    c["token_id"], c["token_secret"]
                )
                self._app = self._sdk.App.lookup(
                    c["app_name"], create_if_missing=True, client=self._client
                )
            except Exception:
                raise DstackError("dstack_api_unavailable") from None
        return self._sdk, self._client, self._app

    def _launch(self, run_name, job, seconds):
        sdk, client, app = self._modal()
        c = self.config
        try:
            sandbox = sdk.Sandbox.create(
                "bash",
                "-c",
                job["command"],
                app=app,
                name=run_name,
                # The image's own ENTRYPOINT is the Vast base start script,
                # which exits on Modal before the boot command runs; the run
                # spec's command is the whole container, as under dstack.
                image=sdk.Image.from_registry(c["image"]).entrypoint([]),
                secrets=[
                    sdk.Secret.from_dict({**job["env"], "INKSEQ_MODEL_CACHE": CACHE})
                ],
                volumes={MOUNT: sdk.Volume.from_id(c["volume_id"], client=client)},
                gpu=c["gpu"],
                cpu=c["cpus"],
                memory=c["memory_gib"] * 1024,
                timeout=seconds,
                client=client,
            )
        except Exception:
            raise DstackError("dstack_api_rejected") from None
        identity = sandbox.object_id
        return {"id": identity, "status": "IN_PROGRESS", "workerId": identity}

    def _status(self, record):
        final = record.get("final")
        if isinstance(final, dict):
            return final
        sdk, client, _ = self._modal()
        try:
            code = sdk.Sandbox.from_id(record["job_id"], client=client).poll()
        except Exception:
            raise DstackError("dstack_api_unavailable") from None
        if code is None:
            return {"status": "IN_PROGRESS", "workerId": record["job_id"]}
        status = {
            "status": "CANCELLED"
            if record.get("cancelled")
            else ("COMPLETED" if code == 0 else "FAILED"),
            "workerId": record["job_id"],
        }
        record["final"] = status
        self._write(record["run_name"], record, create=False)
        return status

    def stop(self, run_name):
        record = self._read(run_name)
        _require(record is not None and record.get("job_id"), "dstack_exact_run_absent")
        record["cancelled"] = True
        self._write(run_name, record, create=False)
        return super().stop(run_name)

    def _cancel(self, job_id):
        sdk, client, _ = self._modal()
        try:
            sdk.Sandbox.from_id(job_id, client=client).terminate()
        except Exception:
            raise DstackError("dstack_api_unavailable") from None

    def _live(self):
        """Running sandboxes in this dedicated app, by Modal's own listing."""
        sdk, client, app = self._modal()
        try:
            return sum(1 for _ in sdk.Sandbox.list(app_id=app.app_id, client=client))
        except Exception:
            raise DstackError("dstack_provider_inventory_unconfirmed") from None
