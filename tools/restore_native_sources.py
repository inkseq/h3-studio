"""Restore two explicitly reviewed versioned objects; never execute native bytes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

from tools import native_source_retention as retention

ACCOUNT = "829135631045"
REGION = "ap-southeast-1"
BUCKET = "sixnine-platform-releases-829135631045-ap-southeast-1"
LIMITS = {"migration-manifest.json": 1024 * 1024, "source-set.tar.gz": retention.MAX_ARCHIVE}


def validate_binding(value, inputs):
    if (type(value) is not dict or set(value) != {
            "version", "repository", "account", "region", "bucket", "run", "commit", "index_sha256", "objects"}
            or type(value["version"]) is not int or value["version"] != 1
            or value["repository"] != retention.REPOSITORY or value["account"] != ACCOUNT
            or value["region"] != REGION or value["bucket"] != BUCKET
            or retention.binding(inputs) is None
            or dict(zip(retention.INPUTS, (value["run"], value["commit"], value["index_sha256"]))) != retention.binding(inputs)
            or type(value["objects"]) is not dict or set(value["objects"]) != set(LIMITS)):
        raise ValueError("native_restore_binding_invalid")
    for name, maximum in LIMITS.items():
        item = value["objects"][name]
        if (type(item) is not dict or set(item) != {"version_id", "sha256", "size"}
                or type(item["version_id"]) is not str
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,1024}", item["version_id"])
                or item["version_id"] == "null"
                or type(item["sha256"]) is not str or not retention.DIGEST.fullmatch(item["sha256"])
                or type(item["size"]) is not int or not 0 < item["size"] <= maximum):
            raise ValueError("native_restore_object_binding_invalid")
    return value


def key(value, name):
    return "releases/" + value["commit"] + "/migration/" + name


def session_policy(value):
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObjectVersion",
         "Resource": "arn:aws:s3:::" + BUCKET + "/" + key(value, name),
         "Condition": {"StringEquals": {"s3:VersionId": item["version_id"]}}}
        for name, item in value["objects"].items()]}


def verify_publication(run, jobs, inputs):
    retention.verify_run_identity(run, inputs)
    if (type(jobs) is not dict or type(jobs.get("total_count")) is not int
            or not 1 <= jobs["total_count"] <= 100 or type(jobs.get("jobs")) is not list
            or len(jobs["jobs"]) != jobs["total_count"]):
        raise ValueError("native_restore_jobs_incomplete")
    matches = [job for job in jobs["jobs"] if type(job) is dict and job.get("name") == "publish-aws"]
    if (len(matches) != 1 or matches[0].get("run_id") != int(inputs[retention.INPUTS[0]])
            or matches[0].get("head_sha") != inputs[retention.INPUTS[1]]
            or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success"):
        raise ValueError("native_restore_publication_invalid")


def restore(value, inputs, run, jobs, destination, *, s3, sts):
    value = validate_binding(value, inputs)
    verify_publication(run, jobs, inputs)
    if sts.get_caller_identity().get("Account") != ACCOUNT:
        raise ValueError("native_restore_account_invalid")
    # Fresh directory only: failed restores cannot overwrite/reuse partial files.
    destination = Path(destination)
    destination.mkdir(parents=False, exist_ok=False)
    for name, item in value["objects"].items():
        response = s3.get_object(Bucket=BUCKET, Key=key(value, name), VersionId=item["version_id"])
        body = response["Body"]
        try:
            if (response.get("VersionId") != item["version_id"]
                    or response.get("ContentLength") != item["size"]
                    or response.get("ServerSideEncryption") != "AES256"):
                raise ValueError("native_restore_object_identity_invalid")
            digest, size = hashlib.sha256(), 0
            with (destination / name).open("xb") as output:
                while True:
                    chunk = body.read(min(1024 * 1024, item["size"] + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > item["size"]:
                        raise ValueError("native_restore_object_size_invalid")
                    digest.update(chunk)
                    output.write(chunk)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise ValueError("native_restore_object_digest_invalid")
        finally:
            body.close()
    manifest = retention._json((destination / "migration-manifest.json").read_bytes())
    if (type(manifest) is not dict or manifest.get("version") != 1
            or manifest.get("commit") != value["commit"]
            or manifest.get("source_index_sha256") != value["index_sha256"]
            or (manifest.get("files") or {}).get("source-set.tar.gz") != value["objects"]["source-set.tar.gz"]["sha256"]):
        raise ValueError("native_restore_manifest_binding_invalid")
    # Packaging still verifies index, every package and each original public Git blob.
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--index-sha256", required=True)
    parser.add_argument("--policy-only", action="store_true")
    parser.add_argument("--run-json", type=Path)
    parser.add_argument("--jobs-json", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    try:
        inputs = dict(zip(retention.INPUTS, (args.run, args.commit, args.index_sha256)))
        value = validate_binding(retention._json(retention._regular(args.binding, 16384)), inputs)
        if args.policy_only:
            print(json.dumps(session_policy(value), separators=(",", ":")))
            return
        if not all((args.run_json, args.jobs_json, args.destination)):
            raise ValueError("native_restore_arguments_missing")
        # Imported only in the explicit network CLI; offline checks need no AWS SDK.
        import boto3
        from botocore.config import Config
        config = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2, "mode": "standard"})
        restore(value, inputs, retention._json(retention._regular(args.run_json, 1024 * 1024)),
                retention._json(retention._regular(args.jobs_json, 4 * 1024 * 1024)), args.destination,
                s3=boto3.client("s3", region_name=REGION, endpoint_url="https://s3.ap-southeast-1.amazonaws.com", config=config),
                sts=boto3.client("sts", region_name=REGION, endpoint_url="https://sts.ap-southeast-1.amazonaws.com", config=config))
        print("Exact reviewed native transport restored; packaging verification still required")
    except Exception:
        parser.exit(1, "Native source restore failed; no fallback or native rebuild permitted\n")


if __name__ == "__main__":
    main()
