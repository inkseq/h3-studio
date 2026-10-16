"""Explicit portable-backup transport; no clients, credentials or IO on import.

This is an operator copy of the existing verified business backup, not another
asset store, live database snapshot, retention service or automatic failover.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid

from .backup import (BackupError, MIB, _absolute, _digest, _private_new_directory,
                     _regular, _write_json, restore_local, verify_local)
from .storage import _sync_directory

FORMAT = "sixnine-private-backup-copy-v1"
FORMAT_V2 = "sixnine-private-backup-copy-v2"
MAX_FILE = 512*MIB
MAX_TOTAL = 40*1024*MIB + 272*MIB
MAX_JSON = 16*MIB
MAX_FILES = 100002


class RemoteBackupError(Exception):
    """Static error code only; never propagate provider details or private paths."""


def need(value, code):
    if not value:
        raise RemoteBackupError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _record(path, value):
    _write_json(path,value)
    _sync_directory(path.parent)


def _identity(info):
    # On Windows Path.stat and fstat can expose different ctime semantics.
    # Compare their stable identity here; ctime is compared within each API.
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns


@contextmanager
def _source_file(path, expected):
    path=_regular(path,max_bytes=MAX_FILE);before=path.stat()
    fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_BINARY",0))
    with os.fdopen(fd,"rb") as incoming:
        opened=os.fstat(incoming.fileno())
        need(_identity(opened)==_identity(before),"backup_changed_before_open")
        digest=hashlib.sha256();size=0
        for block in iter(lambda:incoming.read(MIB),b""):
            size+=len(block);need(size<=expected["size_bytes"],"backup_changed_during_copy")
            digest.update(block)
        need(size==expected["size_bytes"] and digest.hexdigest()==expected["sha256"],"backup_changed_during_copy")
        incoming.seek(0)
        yield incoming
        after_fd=os.fstat(incoming.fileno());after_path=_regular(path).stat()
        need(_identity(after_fd)==_identity(opened) and after_fd.st_ctime_ns==opened.st_ctime_ns
             and _identity(after_path)==_identity(before) and after_path.st_ctime_ns==before.st_ctime_ns,
             "backup_changed_during_copy")


@dataclass(frozen=True)
class BackupTarget:
    account_id: str
    region: str
    bucket: str
    prefix: str
    kms_key_arn: str
    role_arn: str

    def __post_init__(self):
        need(bool(re.fullmatch(r"[0-9]{12}", self.account_id or "")), "backup_account_required")
        need(bool(re.fullmatch(r"[a-z]{2}-[a-z]+-\d", self.region or "")), "backup_region_invalid")
        need(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket or ""))
             and not self.bucket.startswith("sixnine-platform-releases-"), "dedicated_backup_bucket_required")
        need(bool(re.fullmatch(r"business-backups/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                              self.prefix or "")), "fresh_opaque_backup_prefix_required")
        need(bool(re.fullmatch(r"arn:aws:kms:"+re.escape(self.region)+":"+self.account_id+
                              r":key/[0-9a-f-]{36}", self.kms_key_arn or "")), "same_account_backup_key_required")
        need(bool(re.fullmatch(r"arn:aws:iam::"+self.account_id+
                              r":role/(?:[A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]{1,64}",
                              self.role_arn or "")), "explicit_same_account_backup_role_required")

    def request(self, name):
        return dict(Bucket=self.bucket, Key=self.prefix+"/"+name, ExpectedBucketOwner=self.account_id)


def _files(directory, manifest):
    names={"manifest.json", "database.sqlite3"} | {"media/"+item["file"] for item in manifest["objects"]}
    need(len(names)<=MAX_FILES, "backup_file_count_limit")
    # Ignore no extras: a credential/staging file accidentally placed here must
    # never be copied, nor silently treated as covered by this backup receipt.
    need({p.name for p in directory.iterdir()}=={"manifest.json", "database.sqlite3", "media"},
         "backup_directory_has_unexpected_entries")
    need({p.name for p in (directory/"media").iterdir()}=={item["file"] for item in manifest["objects"]},
         "backup_media_directory_has_unexpected_entries")
    files=[]
    for name in sorted(names-{"manifest.json"})+["manifest.json"]:
        path=_regular(directory/name, max_bytes=MAX_FILE)
        if os.name!="nt":
            need(not path.stat().st_mode & 0o077, "backup_source_not_private")
        files.append(dict(name=name, size_bytes=path.stat().st_size, sha256=_digest(path)))
    need(sum(item["size_bytes"] for item in files)<=MAX_TOTAL, "backup_total_size_limit")
    return files


def plan_copy(backup_directory, target, *, allow_incomplete=False):
    need(type(allow_incomplete) is bool, "backup_opt_in_invalid")
    source=_absolute(backup_directory)
    manifest=verify_local(source, allow_incomplete=allow_incomplete)
    files=_files(source, manifest)
    plan=dict(format=FORMAT, target=asdict(target), files=files,
                snapshot_manifest_sha256=files[-1]["sha256"],
                objects=len(manifest["objects"]), bytes=sum(item["size_bytes"] for item in files),
                staging_included=False, authentication_included=False)
    if manifest["version"] == 2:
        plan.update(format=FORMAT_V2, coverage=manifest["coverage"])
    # Refuse before any remote writes if even valid returned version IDs could
    # make the final transfer receipt unreadable. Accumulate individual encoded
    # entries rather than allocating an unbounded worst-case manifest.
    size=len(canonical({**plan,"files":[],"operation_id":"0"*36,"phase":"copy_complete"}))
    for index,item in enumerate(files):
        size+=len(canonical({**item,"version_id":"\\"*1024}))+(1 if index else 0)
        need(size<=MAX_JSON,"backup_completion_limit")
    return plan


def _preflight(client, target, *, fresh=False):
    args=dict(Bucket=target.bucket, ExpectedBucketOwner=target.account_id)
    try:
        versioning=client.get_bucket_versioning(**args)
        public=client.get_public_access_block(**args)["PublicAccessBlockConfiguration"]
        ownership=client.get_bucket_ownership_controls(**args)["OwnershipControls"]["Rules"]
        encryption=client.get_bucket_encryption(**args)["ServerSideEncryptionConfiguration"]["Rules"]
        need(versioning.get("Status")=="Enabled", "backup_bucket_versioning_required")
        need(all(public.get(k) is True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")),
             "backup_bucket_public_access_block_required")
        need(ownership==[{"ObjectOwnership":"BucketOwnerEnforced"}], "backup_bucket_acl_disabled_required")
        need(len(encryption)==1 and encryption[0].get("ApplyServerSideEncryptionByDefault")=={
            "SSEAlgorithm":"aws:kms", "KMSMasterKeyID":target.kms_key_arn}, "backup_bucket_key_mismatch")
        if fresh:
            history=client.list_object_versions(**args, Prefix=target.prefix+"/", MaxKeys=1)
            need(not history.get("Versions") and not history.get("DeleteMarkers")
                 and not history.get("IsTruncated"), "backup_prefix_already_used")
    except RemoteBackupError:
        raise
    except Exception:
        raise RemoteBackupError("backup_bucket_preflight_unconfirmed") from None


def _version(value):
    need(isinstance(value,str) and 0<len(value)<=1024 and value!="null"
         and not any(ord(c)<33 or ord(c)>126 for c in value), "backup_version_missing")
    return value


def _metadata(snapshot, expected):
    return {"format":FORMAT, "snapshot":snapshot, "sha256":expected["sha256"]}


def _check(response, target, snapshot, expected, *, version=None):
    actual=_version(response.get("VersionId"))
    need(version is None or actual==version, "backup_object_version_mismatch")
    need(response.get("ServerSideEncryption")=="aws:kms" and response.get("SSEKMSKeyId")==target.kms_key_arn,
         "backup_object_key_mismatch")
    need(response.get("ContentLength")==expected["size_bytes"]
         and response.get("ChecksumSHA256")==base64.b64encode(bytes.fromhex(expected["sha256"])).decode()
         and response.get("Metadata")==_metadata(snapshot,expected), "backup_object_metadata_mismatch")
    return actual


def _read(client, target, name, expected, snapshot, *, version=None, output=None):
    args=target.request(name)
    if version is not None:
        args["VersionId"]=_version(version)
    try:
        response=client.get_object(**args, ChecksumMode="ENABLED")
        with response["Body"] as body:
            actual=_check(response,target,snapshot,expected,version=version)
            digest=hashlib.sha256();size=0
            while True:
                data=body.read(min(MIB,expected["size_bytes"]-size+1))
                if not data:break
                size+=len(data)
                need(size<=expected["size_bytes"], "backup_object_size_mismatch")
                digest.update(data)
                if output is not None:output.write(data)
            need(size==expected["size_bytes"] and digest.hexdigest()==expected["sha256"],
                 "backup_object_content_mismatch")
        return actual
    except RemoteBackupError:
        raise
    except Exception:
        raise RemoteBackupError("backup_readback_unconfirmed") from None


def _put(client, target, name, source, expected, snapshot):
    try:
        response=client.put_object(**target.request(name), Body=source, ContentLength=expected["size_bytes"],
            IfNoneMatch="*", ServerSideEncryption="aws:kms", SSEKMSKeyId=target.kms_key_arn,
            ChecksumSHA256=base64.b64encode(bytes.fromhex(expected["sha256"])).decode(),
            Metadata=_metadata(snapshot,expected))
    except Exception as error:
        # Collision is never adopted. Other outcomes get one exact content/key
        # readback, without replaying PUT or treating 404 as proof of failure.
        status=getattr(error,"response",{}).get("ResponseMetadata",{}).get("HTTPStatusCode")
        need(status!=412, "backup_object_already_exists")
        return _read(client,target,name,expected,snapshot)
    try:
        version=_version(response.get("VersionId"))
        head=client.head_object(**target.request(name),VersionId=version,ChecksumMode="ENABLED")
        return _check(head,target,snapshot,expected,version=version)
    except RemoteBackupError:
        raise
    except Exception:
        raise RemoteBackupError("backup_write_receipt_unconfirmed") from None


def copy_backup(backup_directory, target, receipt_directory, *, client, allow_incomplete=False):
    """One explicit new-prefix copy; retains local evidence after any uncertainty."""
    source=_absolute(backup_directory)
    plan=plan_copy(source,target,allow_incomplete=allow_incomplete)
    receipt_directory=_absolute(receipt_directory)
    need(not receipt_directory.is_relative_to(source) and not source.is_relative_to(receipt_directory),
         "backup_receipts_overlap_source")
    receipts=_private_new_directory(receipt_directory)
    snapshot=plan["snapshot_manifest_sha256"]
    intent={"format":FORMAT,"operation_id":str(uuid.uuid4()),"phase":"before_remote_write",**plan}
    _record(receipts/"intent.json",intent)
    try:
        _preflight(client,target,fresh=True)
        claim=canonical({"format":plan["format"],"operation_id":intent["operation_id"],
                         "snapshot_manifest_sha256":snapshot,"target":asdict(target)})
        _put(client,target,"claim.json",io.BytesIO(claim),dict(size_bytes=len(claim),sha256=sha(claim)),snapshot)
        files=[]
        for item in plan["files"]:
            with _source_file(source/item["name"],item) as incoming:
                version=_put(client,target,item["name"],incoming,item,snapshot)
            files.append({**item,"version_id":version})
        completion={**plan,"files":files,"operation_id":intent["operation_id"],"phase":"copy_complete"}
        data=canonical(completion);need(len(data)<=MAX_JSON,"backup_completion_limit")
        version=_put(client,target,"complete.json",io.BytesIO(data),dict(size_bytes=len(data),sha256=sha(data)),snapshot)
        result=dict(format=plan["format"],target=asdict(target),phase="copied_not_restored",completion_version_id=version,
                    completion_sha256=sha(data),completion_bytes=len(data),snapshot_manifest_sha256=snapshot,
                    objects=plan["objects"],bytes=plan["bytes"],restore_verified=False)
        if "coverage" in plan:result["coverage"]=plan["coverage"]
        _record(receipts/"completion.json",result)
        return result
    except Exception as error:
        code=str(error) if isinstance(error,RemoteBackupError) else "backup_copy_requires_reconciliation"
        _record(receipts/"uncertain.json",dict(phase="reconcile_exact_prefix_no_replay",code=code))
        raise RemoteBackupError(code) from None


def coverage_fields(value):
    """Closed versioned transport coverage; counts describe available bytes only."""
    need(isinstance(value, dict) and value.get("format") in (FORMAT, FORMAT_V2), "backup_coverage_invalid")
    if value["format"] == FORMAT:
        need("coverage" not in value, "backup_coverage_invalid")
        return {}
    c=value.get("coverage")
    keys={"status","referenced_objects","available_objects","missing_objects","referenced_bytes","available_bytes","missing_bytes"}
    need(isinstance(c,dict) and set(c)==keys and all(type(c[k]) is int and c[k]>=0 for k in keys-{"status"}), "backup_coverage_invalid")
    need(c["referenced_objects"]==c["available_objects"]+c["missing_objects"] and c["referenced_bytes"]==c["available_bytes"]+c["missing_bytes"]
         and c["status"]==("incomplete" if c["missing_objects"] else "complete") and c["available_objects"]==value.get("objects")
         and (c["missing_objects"]==0)==(c["missing_bytes"]==0), "backup_coverage_invalid")
    return {"coverage":c}


def _completion(value, target, snapshot):
    need(isinstance(value,dict) and value.get("format") in (FORMAT,FORMAT_V2) and value.get("phase")=="copy_complete"
         and value.get("target")==asdict(target) and value.get("snapshot_manifest_sha256")==snapshot,
         "backup_completion_identity_mismatch")
    coverage_fields(value)
    files=value.get("files")
    need(isinstance(files,list) and 2<=len(files)<=MAX_FILES,"backup_completion_file_limit")
    names=set();total=0
    for item in files:
        need(isinstance(item,dict) and set(item)=={"name","size_bytes","sha256","version_id"},
             "backup_completion_file_invalid")
        name=item["name"]
        need(isinstance(name,str) and (name in ("database.sqlite3","manifest.json")
             or re.fullmatch(r"media/[0-9a-f]{64}\.bin",name)) and name not in names,
             "backup_completion_path_invalid")
        names.add(name);_version(item["version_id"])
        need(type(item["size_bytes"]) is int and 0<item["size_bytes"]<=MAX_FILE
             and isinstance(item["sha256"],str) and re.fullmatch(r"[0-9a-f]{64}",item["sha256"]),
             "backup_completion_file_invalid")
        total+=item["size_bytes"]
    need(total<=MAX_TOTAL and {"database.sqlite3","manifest.json"}<=names
         and files[-1]["name"]=="manifest.json" and files[-1]["sha256"]==snapshot,
         "backup_completion_manifest_mismatch")
    if value["format"] == FORMAT_V2:
        need(value["coverage"]["available_bytes"] == sum(x["size_bytes"] for x in files if x["name"].startswith("media/"))
             and value["coverage"]["available_objects"] == len(files)-2, "backup_coverage_inventory_mismatch")
    return files


def restore_copy(target, receipt, download_directory, restore_directory, *, client, allow_incomplete=False):
    """Fetch exact pinned versions, verify, then use the existing disabled restore."""
    need(receipt.get("format") in (FORMAT,FORMAT_V2) and receipt.get("target")==asdict(target)
         and receipt.get("phase")=="copied_not_restored", "backup_transfer_receipt_invalid")
    need(type(allow_incomplete) is bool, "backup_opt_in_invalid")
    coverage=coverage_fields(receipt)
    need(allow_incomplete or coverage.get("coverage",{}).get("status") != "incomplete", "backup_incomplete_requires_explicit_opt_in")
    expected=dict(size_bytes=receipt.get("completion_bytes"),sha256=receipt.get("completion_sha256"))
    snapshot=receipt.get("snapshot_manifest_sha256")
    completion_version=_version(receipt.get("completion_version_id"))
    need(type(expected["size_bytes"]) is int and 0<expected["size_bytes"]<=MAX_JSON
         and isinstance(expected["sha256"],str) and re.fullmatch(r"[0-9a-f]{64}",expected["sha256"])
         and isinstance(snapshot,str) and re.fullmatch(r"[0-9a-f]{64}",snapshot), "backup_transfer_receipt_invalid")
    destination=_absolute(restore_directory)
    download_directory=_absolute(download_directory)
    need(not destination.is_relative_to(download_directory) and not download_directory.is_relative_to(destination),
         "backup_download_and_restore_must_be_separate")
    need(not destination.exists() and not destination.is_symlink() and destination.parent.is_dir(),
         "restore_target_must_be_new")
    download=_private_new_directory(download_directory)
    _record(download/"download-intent.json",dict(format=FORMAT,completion_sha256=expected["sha256"],
                target=asdict(target),phase="readback_before_restore"))
    _preflight(client,target)
    output=io.BytesIO()
    _read(client,target,"complete.json",expected,snapshot,version=completion_version,output=output)
    completed=json.loads(output.getvalue())
    files=_completion(completed,target,snapshot)
    need(completed["format"]==receipt["format"] and coverage_fields(completed)==coverage, "backup_coverage_mismatch")
    incoming=_private_new_directory(download/"backup")
    (incoming/"media").mkdir(mode=0o700)
    for item in files:
        path=incoming/item["name"]
        with path.open("xb") as target_file:
            if os.name!="nt":os.fchmod(target_file.fileno(),0o600)
            _read(client,target,item["name"],item,snapshot,version=item["version_id"],output=target_file)
            target_file.flush();os.fsync(target_file.fileno())
    manifest=verify_local(incoming,allow_incomplete=allow_incomplete)
    need(({"coverage":manifest["coverage"]} if manifest["version"]==2 else {})==coverage, "backup_coverage_mismatch")
    result=restore_local(incoming,destination,allow_incomplete=allow_incomplete)
    need(result.get("state")=="restored_isolated" and result.get("execution_enabled") is False,
         "isolated_restore_unconfirmed")
    summary=dict(state="off_device_copy_restored_isolated",completion_sha256=expected["sha256"],
                 objects=result["objects"],held_jobs=result["held_jobs"],execution_enabled=False,
                 native_postgres_restore=False,staging_restored=False,authentication_reprovision_required=True)
    summary.update(coverage)
    if coverage:summary["media_recovery_complete"]=coverage["coverage"]["status"]=="complete"
    _record(download/"verified-restore.json",summary)
    return summary
