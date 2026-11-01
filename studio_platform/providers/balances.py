"""What each provider has left, for the admin page and on-call (#950).

The controller reads every 4 h (owner 2026-11-01) on a background thread, so a
slow vendor never delays a control tick, and keeps the rows in one file in the
data volume. A provider without a balance API shows its console link, never an
invented number. Keys stay in this call's memory and values are never logged.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time

import httpx

from .everyframe import API as EVERYFRAME_API
from .everyframe import FILENAME as EVERYFRAME_FILE
from .everyframe import load_config

READ_SECONDS = 4 * 3600
FILENAME = "provider-balances.json"
TIMEOUT_SECONDS = 15
RUNPOD_QUERY = {"query": "query { myself { clientBalance } }"}


def _vast(client, key):
    answer = client.get(
        "https://console.vast.ai/api/v0/users/current/",
        headers={"Authorization": "Bearer " + key},
    )
    return answer.raise_for_status().json()["credit"]


def _runpod(client, key):
    answer = client.post(
        "https://api.runpod.io/graphql",
        headers={"Authorization": "Bearer " + key},
        json=RUNPOD_QUERY,
    )
    return answer.raise_for_status().json()["data"]["myself"]["clientBalance"]


def _lium(client, key):
    answer = client.get("https://lium.io/api/users/me", headers={"X-API-Key": key})
    return answer.raise_for_status().json()["balance"]


def _cloudrift(client, key):
    answer = client.post(
        "https://api.cloudrift.ai/api/v1/account/info",
        headers={"X-API-Key": key},
        json={},
    )
    return answer.raise_for_status().json()["data"]["balance"]


def _everyframe(client, key):
    answer = client.get(
        EVERYFRAME_API + "/key", headers={"Authorization": "Bearer " + key}
    )
    return answer.raise_for_status().json()["balanceUsd"]


@dataclass(frozen=True)
class Source:
    provider: str
    console_url: str
    warn_usd: float | None = None
    read: object = None
    # The dstack backend holding the key; None for a key held elsewhere.
    backend: str | None = None
    unit: str = "usd"


SOURCES = (
    Source("vast", "https://cloud.vast.ai/billing/", 10.0, _vast, "vastai"),
    Source("runpod", "https://console.runpod.io/user/billing", 10.0, _runpod, "runpod"),
    Source("lium", "https://lium.io", 10.0, _lium, "lium"),
    # The reply carries no currency, and one reading did not settle its unit.
    Source(
        "cloudrift",
        "https://console.cloudrift.ai",
        None,
        _cloudrift,
        "cloudrift",
        "raw",
    ),
    Source("everyframe", "https://everyframe.studio", 10.0, _everyframe),
    # No key on the servers, or post-paid: read these in their consoles.
    Source("modal", "https://modal.com/settings/usage"),
    Source("openrouter", "https://openrouter.ai/settings/credits"),
    Source("aws", "https://console.aws.amazon.com/billing/home"),
    Source("google", "https://console.cloud.google.com/billing"),
)


def _path(settings):
    return Path(settings.data_dir) / FILENAME


def read_stored(settings):
    """The stored rows, or every provider as not yet read."""
    try:
        stored = json.loads(_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    stored = stored if isinstance(stored, dict) else {}
    rows = stored.get("rows")
    rows = rows if isinstance(rows, dict) else {}
    result = []
    for source in SOURCES:
        row = rows.get(source.provider)
        row = row if isinstance(row, dict) else {}
        amount = row.get("amount")
        amount = amount if isinstance(amount, (int, float)) else None
        result.append(
            {
                "provider": source.provider,
                "console_url": source.console_url,
                "readable": source.read is not None,
                "amount": amount,
                "unit": source.unit,
                "read_at": row.get("read_at"),
                "last_read_failed": row.get("last_read_failed") is True,
                "low": (
                    amount is not None
                    and source.warn_usd is not None
                    and amount < source.warn_usd
                ),
            }
        )
    return {
        "read_every_seconds": READ_SECONDS,
        "attempted_at": stored.get("attempted_at"),
        "rows": result,
    }


def _key(source, settings, client):
    if source.backend is not None:
        return client.backend_api_key(source.backend)
    return load_config(
        Path(settings.data_dir) / "runtime-secrets" / EVERYFRAME_FILE
    ).api_key


def read_all(settings, client, *, now=None, transport=None):
    """Read every provider once and store the rows; one failure never blocks the rest."""
    now = time.time() if now is None else now
    previous = {row["provider"]: row for row in read_stored(settings)["rows"]}
    rows = {}
    with httpx.Client(
        transport=transport,
        timeout=TIMEOUT_SECONDS,
        trust_env=False,
        follow_redirects=False,
    ) as session:
        for source in SOURCES:
            if source.read is None:
                continue
            old = previous[source.provider]
            try:
                amount = source.read(session, _key(source, settings, client))
                if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                    raise ValueError("provider_balance_unconfirmed")
                rows[source.provider] = {"amount": amount, "read_at": now}
            except Exception:
                rows[source.provider] = {
                    "amount": old["amount"],
                    "read_at": old["read_at"],
                    "last_read_failed": True,
                }
    path = _path(settings)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(
        json.dumps({"attempted_at": now, "rows": rows}), encoding="utf-8"
    )
    os.replace(scratch, path)
    # Names only: on-call reads these lines; amounts stay on the admin page.
    for row in read_stored(settings)["rows"]:
        if row["low"]:
            print("provider_balance_low " + row["provider"], flush=True)


_running = threading.Lock()


def refresh_in_background(settings, client, now):
    """Start one read when due; returns whether it started."""
    attempted = read_stored(settings)["attempted_at"]
    if isinstance(attempted, (int, float)) and now - attempted < READ_SECONDS:
        return False
    if not _running.acquire(blocking=False):
        return False

    def run():
        try:
            read_all(settings, client, now=now)
        except Exception:
            print("provider_balance_read_failed", flush=True)
        finally:
            _running.release()

    threading.Thread(target=run, name="provider-balances", daemon=True).start()
    return True
