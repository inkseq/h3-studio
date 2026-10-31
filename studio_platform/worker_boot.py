"""One-time boot exchange for dial-out GPU workers (#859).

The dstack run spec is stored by the control plane and shown in its UI, so it
carries only a single-use boot token. The token's hash and expiry live in the
original binding. The controller prepares the worker's grant (tunnel user,
runtime token, signed sources) once it observes the run, and this exchange
hands it over exactly once.
"""

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat

from sqlalchemy import select, update

from .operator_capacity import operator_nodes

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
INTENT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
GRANT = "grant.json"


class BootRefused(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)


def mint():
    """(token for the run env, record for the binding)."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


# A dial-out worker must exchange its boot token within this window.
BOOT_WINDOW_SECONDS = 3600
# A port only appears on the local end-to-end edge (tests/test_dialout_e2e.py).
BOOT_URL = re.compile(r"https://[a-z0-9.-]+(?::[0-9]{2,5})?/v1/worker/boot")


def boot_valid(token, url):
    """CapacityRequest fields: both absent (SSH transport) or both well formed."""
    if token is None and url is None:
        return True
    return bool(
        isinstance(token, str)
        and TOKEN.fullmatch(token)
        and isinstance(url, str)
        and BOOT_URL.fullmatch(url)
    )


def boot_commands(token):
    """SSH workers idle for the controller; dial-out workers boot themselves."""
    if token is None:
        return ["exec sleep infinity"]
    script = (Path(__file__).with_name("dialout_boot.py")).read_text()
    if "INKSEQ_BOOT_SCRIPT" in script:
        raise ValueError("worker_boot_script_invalid")
    # Public code only; the env holds the one-time code and nothing else.
    heredoc = "exec python3 - <<'INKSEQ_BOOT_SCRIPT'" + chr(10)
    return [heredoc + script + "INKSEQ_BOOT_SCRIPT"]


def boot_env(intent_id, token, url):
    """The run env: only what the worker needs to call the exchange once."""
    if token is None:
        return {}
    return {
        "INKSEQ_BOOT_URL": url,
        "INKSEQ_INTENT": intent_id,
        "INKSEQ_BOOT_TOKEN": token,
    }


def boot_record(token, created_at):
    """Binding fields: the token's hash and its exchange deadline only."""
    if token is None:
        return {}
    return {
        "boot": {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": created_at + BOOT_WINDOW_SECONDS,
        }
    }


def grant_path(root, intent_id):
    if not INTENT.fullmatch(intent_id or ""):
        raise ValueError("worker_boot_intent_invalid")
    return Path(root) / intent_id / GRANT


def write_grant(root, intent_id, grant):
    """Controller side: one private file per intent, never overwritten."""
    path = grant_path(root, intent_id)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as out:
        json.dump(grant, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())


def _read_grant(root, intent_id):
    path = grant_path(root, intent_id)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or os.name == "posix"
            and info.st_mode & 0o077
        ):
            raise BootRefused("worker_boot_grant_unprotected", 503)
        return json.loads(source.read(65537))


def exchange(repo, grants_root, intent_id, token):
    """Return the prepared grant once; later or wrong attempts change nothing."""
    if not INTENT.fullmatch(intent_id or "") or not TOKEN.fullmatch(token or ""):
        raise BootRefused("worker_boot_denied", 403)
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = repo.clock()
    with repo.transaction() as connection:
        node = repo._locked(
            connection,
            select(operator_nodes).where(operator_nodes.c.intent_id == intent_id),
        )
        binding = (node or {}).get("payload", {}).get("dstack", {})
        boot = binding.get("boot")
        if not isinstance(boot, dict) or not hmac.compare_digest(
            str(boot.get("token_sha256", "")), digest
        ):
            raise BootRefused("worker_boot_denied", 403)
        if binding.get("boot_used_at") is not None:
            raise BootRefused("worker_boot_used", 409)
        if node["desired_state"] != "running" or not now < boot["expires_at"]:
            raise BootRefused("worker_boot_closed", 409)
        # The controller prepares the grant after it observes the run; the
        # worker retries until then without consuming its one exchange.
        grant = _read_grant(grants_root, intent_id)
        if grant is None:
            raise BootRefused("worker_boot_not_ready", 425)
        payload = copy.deepcopy(node["payload"])
        payload["dstack"]["boot_used_at"] = now
        connection.execute(
            update(operator_nodes)
            .where(operator_nodes.c.intent_id == intent_id)
            .values(payload=payload, updated_at=now)
        )
    return grant


BOOT_PATH = "/v1/worker/boot"


def register_routes(app):
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    @app.post(BOOT_PATH)
    async def boot(request: Request):
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        try:
            raw = await request.body()
            value = json.loads(raw) if len(raw) <= 1024 else None
            if not isinstance(value, dict) or set(value) != {"intent_id", "token"}:
                raise BootRefused("worker_boot_denied", 403)
            state = request.app.state
            grant = await run_in_threadpool(
                exchange,
                state.repository,
                Path(state.settings.data_dir) / "worker-tunnel",
                value["intent_id"],
                value["token"],
            )
        except BootRefused as refused:
            return JSONResponse(
                {"error": refused.code}, status_code=refused.status, headers=headers
            )
        except ValueError:
            return JSONResponse(
                {"error": "worker_boot_denied"}, status_code=403, headers=headers
            )
        return JSONResponse(grant, headers=headers)
