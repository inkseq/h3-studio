"""Off-host stopper for paid GPUs of a lost CPU host (#816).

Subscribed to the operator alert topic. Acts only when the production
MonitorHeartbeat alarm enters ALARM and the site's own health check also fails
three times. Then it stops (not destroys) the Vast instances the controller
last listed as ours, and reports what it did to the same topic. supervan's
machines are never in that list. Stopping is reversible from the Vast console.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

REGION = "ap-southeast-1"
ALARM = os.environ.get("ALARM_NAME", "Inkseq-production-MonitorHeartbeat")
HEALTH_URL = os.environ.get("HEALTH_URL", "https://inkseq.com/healthz")
PARAMETER = os.environ.get("STOP_LIST", "/sixnine/production/host-loss/stoppable")
SECRET = os.environ.get("VAST_SECRET", "/sixnine/production/vast-host-loss")
TOPIC = os.environ.get("ALERT_TOPIC", "")
VAST = "https://console.vast.ai/api/v0/instances/"


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


def stop_instance(key, instance_id):
    request = urllib.request.Request(
        VAST + instance_id + "/",
        data=json.dumps({"state": "stopped"}).encode(),
        method="PUT",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status


def run(
    event, *, ssm, secrets, sns, fetch=http_status, stop=stop_instance, sleep=time.sleep
):
    if not alarm_fired(event):
        return {"state": "ignored"}
    if not site_down(fetch, sleep):
        return {"state": "site_alive"}
    listed = json.loads(ssm.get_parameter(Name=PARAMETER)["Parameter"]["Value"])
    key = json.loads(secrets.get_secret_value(SecretId=SECRET)["SecretString"])[
        "api_key"
    ]
    results = {}
    for item in listed.get("instances", []):
        instance_id = item.get("instance_id")
        if not (isinstance(instance_id, str) and instance_id.isdigit()):
            continue
        try:
            results[instance_id] = stop(key, instance_id)
        except Exception as error:
            results[instance_id] = type(error).__name__
    summary = {
        "state": "stopped",
        "list_updated_at": listed.get("updated_at"),
        "results": results,
    }
    if TOPIC:
        sns.publish(
            TopicArn=TOPIC,
            Subject="Inkseq host lost: paid GPUs stopped",
            Message=json.dumps(summary, sort_keys=True),
        )
    return summary


def handler(event, context):
    import boto3

    return run(
        event,
        ssm=boto3.client("ssm", region_name=REGION),
        secrets=boto3.client("secretsmanager", region_name=REGION),
        sns=boto3.client("sns", region_name=REGION),
    )
