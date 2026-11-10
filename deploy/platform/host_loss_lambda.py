"""Off-host stopper for paid GPUs of a lost CPU host (#816).

Subscribed to the operator alert topic. Acts only when the production
MonitorHeartbeat alarm enters ALARM and the site's own health check also fails
three times. Then it acts on the rentals the controller last listed as ours
and reports what it did to the same topic. supervan's machines and machines of
unknown owner are never in that list. Per provider, the gentlest action that
ends GPU billing:

- Vast: stop (PUT state=stopped). Reversible from the Vast console; disk
  storage keeps billing.
- RunPod: stop (POST rest.runpod.io/v1/pods/{id}/stop). Releases the GPU and
  keeps the volume disk, which keeps billing; reversible from the console.
- Lium: delete (DELETE lium.io/api/pods/{id}). Lium has no stop or pause for
  renters, only removal, so this is not reversible. Our pods are disposable
  dstack workers; outputs live off the pod, and the controller reconciles a
  removed pod as gone when the host returns. LIUM_ACTION=report lists them as
  unconfirmed (an alert) instead of deleting.

Each provider uses its own secret, loaded only when the list names it. Anything
that is not confirmed is reported as NOT confirmed and raises, so the async
invocation retries and the alert reaches the owner.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

REGION = "ap-southeast-1"
ALARM = os.environ.get("ALARM_NAME", "Inkseq-production-MonitorHeartbeat")
HEALTH_URL = os.environ.get("HEALTH_URL", "https://inkseq.com/healthz")
PARAMETER = os.environ.get("STOP_LIST", "/sixnine/production/host-loss/stoppable")
SECRETS = {
    "vast": os.environ.get("VAST_SECRET", "/sixnine/production/vast-host-loss"),
    "runpod": os.environ.get("RUNPOD_SECRET", "/inkseq/production/runpod-host-loss"),
    "lium": os.environ.get("LIUM_SECRET", "/inkseq/production/lium-host-loss"),
}
LIUM_ACTION = os.environ.get("LIUM_ACTION", "delete")
TOPIC = os.environ.get("ALERT_TOPIC", "")
VAST = "https://console.vast.ai/api/v0/instances/"
RUNPOD = "https://rest.runpod.io/v1/pods/"
LIUM = "https://lium.io/api/pods"
# Instance id forms, as in studio_platform/host_loss.py (this file runs alone).
FORMS = {
    "vast": re.compile(r"[0-9]{1,20}"),
    "runpod": re.compile(r"[A-Za-z0-9]{1,64}"),
    "lium": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}"),
}
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# Lium results that prove the pod no longer bills; integers are HTTP statuses.
GONE = frozenset({"absent", "deleting"})


def alarm_fired(event):
    for record in event.get("Records", []):
        try:
            message = json.loads(record["Sns"]["Message"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            message.get("AlarmName") == ALARM
            and message.get("NewStateValue") == "ALARM"
        ):
            return True
    return False


def site_down(fetch, sleep, attempts=3, gap=20):
    """True only if every attempt fails; one healthy answer means the host lives."""
    for index in range(attempts):
        if index:
            sleep(gap)
        try:
            if fetch(HEALTH_URL) == 200:
                return False
        except Exception:
            pass
    return True


def http_status(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def call(method, url, headers, body=None):
    """(status, decoded JSON or None); an HTTP error status is returned, not raised."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, None
    try:
        # A stop's 2xx stands on its own; only the Lium pod list needs the body.
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def status_of(call, *args):
    """The HTTP status, or the error name; one failure never skips the others."""
    try:
        return call(*args)[0]
    except Exception as error:
        return type(error).__name__


def stop_vast(key, ids, *, call=call):
    headers = {"Authorization": "Bearer " + key}
    body = {"state": "stopped"}
    return {i: status_of(call, "PUT", VAST + i + "/", headers, body) for i in ids}


def stop_runpod(key, ids, *, call=call):
    headers = {"Authorization": "Bearer " + key}
    return {i: status_of(call, "POST", RUNPOD + i + "/stop", headers) for i in ids}


def remove_lium(key, ids, *, call=call):
    """Delete listed pods, resolving a dstack pod name through the pod list."""
    if LIUM_ACTION != "delete":
        return {identity: "not_deleted_by_setting" for identity in ids}
    headers = {"X-API-Key": key}
    status, pods = call("GET", LIUM, headers)
    if status != 200 or not isinstance(pods, list):
        return {identity: "pod_list_unavailable" for identity in ids}
    by_id, by_name = {}, {}
    for pod in pods:
        if isinstance(pod, dict) and isinstance(pod.get("id"), str):
            by_id[pod["id"]] = pod
            for field in ("pod_name", "name"):
                if isinstance(pod.get(field), str):
                    by_name[pod[field]] = pod["id"]
    results = {}
    for identity in ids:
        pod_id = identity if identity in by_id else by_name.get(identity)
        if pod_id is None:
            # A pod id missing from the complete account list no longer bills.
            # A missing name proves nothing (its id may be listed under it).
            results[identity] = "absent" if UUID.fullmatch(identity) else "unresolved"
        elif not UUID.fullmatch(pod_id):
            results[identity] = "unresolved"
        elif by_id[pod_id].get("status") == "DELETING":
            results[identity] = "deleting"
        else:
            answer = status_of(call, "DELETE", LIUM + "/" + pod_id, headers)
            results[identity] = "absent" if answer == 404 else answer
    return results


ACTIONS = {"vast": stop_vast, "runpod": stop_runpod, "lium": remove_lium}


def listed_entries(listed):
    """(provider, instance id) pairs; version 1 lists were Vast-only."""
    version = listed.get("version")
    entries = []
    for item in listed.get("instances", []):
        if not isinstance(item, dict):
            continue
        provider = item.get("provider") if version == 2 else "vast"
        instance_id = item.get("instance_id")
        if (
            provider in FORMS
            and isinstance(instance_id, str)
            and FORMS[provider].fullmatch(instance_id)
        ):
            entries.append((provider, instance_id))
    return entries


def confirmed(result):
    return (type(result) is int and 200 <= result < 300) or result in GONE


def act(entries, secrets, actions):
    """Results keyed provider:instance_id; one provider's failure spares the rest."""
    grouped = {}
    for provider, instance_id in entries:
        grouped.setdefault(provider, []).append(instance_id)
    results = {}
    for provider, ids in sorted(grouped.items()):
        try:
            secret = secrets.get_secret_value(SecretId=SECRETS[provider])
            key = json.loads(secret["SecretString"])["api_key"]
            answers = actions[provider](key, ids)
        except Exception as error:
            answers = {instance_id: type(error).__name__ for instance_id in ids}
        for instance_id in ids:
            results[f"{provider}:{instance_id}"] = answers.get(instance_id, "missing")
    return results


def run(
    event,
    *,
    ssm,
    secrets,
    sns,
    fetch=http_status,
    actions=ACTIONS,
    sleep=time.sleep,
):
    if not alarm_fired(event):
        return {"state": "ignored"}
    if not site_down(fetch, sleep):
        return {"state": "site_alive"}
    listed = json.loads(ssm.get_parameter(Name=PARAMETER)["Parameter"]["Value"])
    if listed.get("version") in (1, 2):
        results = act(listed_entries(listed), secrets, actions)
    else:
        results = {"list": "unsupported_version"}
    # A timeout may still have stopped the machine, so only a 2xx (or a pod
    # proven gone) is confirmed.
    unconfirmed = sorted(
        name for name, result in results.items() if not confirmed(result)
    )
    summary = {
        "state": "stop_unconfirmed" if unconfirmed else "stopped",
        "list_updated_at": listed.get("updated_at"),
        "results": results,
        "unconfirmed": unconfirmed,
    }
    if TOPIC:
        sns.publish(
            TopicArn=TOPIC,
            Subject=(
                "Inkseq host lost: GPU stop NOT confirmed, check the provider"
                if unconfirmed
                else "Inkseq host lost: paid GPUs stopped"
            ),
            Message=json.dumps(summary, sort_keys=True),
        )
    if unconfirmed:
        # Lambda retries a failed async event; stopping a stopped machine changes
        # nothing, and a deleted Lium pod then reads as deleting or absent.
        raise RuntimeError("host_loss_stop_unconfirmed " + ",".join(unconfirmed))
    return summary


def handler(event, context):
    import boto3

    return run(
        event,
        ssm=boto3.client("ssm", region_name=REGION),
        secrets=boto3.client("secretsmanager", region_name=REGION),
        sns=boto3.client("sns", region_name=REGION),
    )
