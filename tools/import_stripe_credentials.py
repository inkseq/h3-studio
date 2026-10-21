"""Import the owner's fixed LIVE drop into the fixed production secret store.

This is an explicit operation, never an import-time action. It does not enable
charging, create Stripe objects, rotate credentials or change host configuration.
Only non-secret status codes and version IDs leave the operation.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import ctypes
from ctypes import wintypes
import hmac
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from studio_platform.stripe_config import _unique, validate_credentials
from tools.provision_staging_qa import ProvisionError, _private

ACCOUNT = "829135631045"
REGION = "ap-southeast-1"
SECRET = "/sixnine/platform/stripe"
SOURCE = Path(
    "C:/Users/danmo/Desktop/inference/h3-studio/.local/secret-drop/stripe-live.env"
)
RECEIPT = SOURCE.parent.parent / "stripe-live-import.json"
VERSION = re.compile(r"[A-Za-z0-9-]{32,64}")
MAX_BYTES = 4096


class ImportErrorSafe(Exception):
    """Static diagnostics; never wrap source, provider or credential text."""


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "Invalid arguments; credential values cannot be passed in argv.\n")


class _DiscardSDKOutput:
    """Discard SDK console text without retaining a second credential copy."""

    encoding = "utf-8"

    def write(self, value):
        return len(value)

    def flush(self):
        pass


@contextmanager
def _silence_sdk_logs():
    """Suppress child DEBUG loggers and Stripe's independent console path."""
    previous_disable = logging.root.manager.disable
    stripe_state = None
    sink = _DiscardSDKOutput()
    with redirect_stdout(sink), redirect_stderr(sink):
        logging.disable(logging.CRITICAL)
        try:
            import stripe
            from stripe import _util

            stripe_state = (stripe.log, _util.STRIPE_LOG, _util._console_log_level)
            stripe.log = None
            _util.STRIPE_LOG = None
            _util._console_log_level = lambda: None
            yield
        finally:
            if stripe_state is not None:
                stripe.log, _util.STRIPE_LOG, _util._console_log_level = stripe_state
            logging.disable(previous_disable)


def selection(target):
    if target != "production":
        raise ImportErrorSafe("stripe_import_target_invalid")
    return SECRET


def _regular(info):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and not getattr(info, "st_file_attributes", 0) & 0x400
    )


def _parents(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ImportErrorSafe("stripe_local_path_invalid")
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise ImportErrorSafe("stripe_local_parent_invalid")


def _fingerprint(info):
    # On Windows lstat().ctime is metadata-change time while fstat().ctime on
    # supported Python versions can still be creation time. The exclusive
    # source handle prevents changes; content comparison also guards each read.
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _open_locked_windows(path):
    """Deny readers/writers/rename while importing; delete by this exact handle."""
    import msvcrt

    api = ctypes.WinDLL("Kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    # GENERIC_READ | DELETE; OPEN_EXISTING; OPEN_REPARSE_POINT. Share=0 prevents
    # replacement between our final read and same-handle disposition.
    handle = api.CreateFileW(str(path), 0x80010000, 0, None, 3, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ImportErrorSafe("stripe_drop_locked_or_unavailable")
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        count = api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        expected = os.path.normcase(os.path.abspath(path))
        if (
            not 0 < count < len(buffer)
            or os.path.normcase(buffer.value.removeprefix("\\\\?\\")) != expected
        ):
            raise ImportErrorSafe("stripe_drop_path_changed")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # fd now owns the handle.
        return fd
    finally:
        if handle is not None:
            api.CloseHandle(handle)


def _delete_handle(fd):
    if os.name != "nt":
        raise ImportErrorSafe("stripe_drop_delete_requires_windows")
    import msvcrt

    api = ctypes.WinDLL("Kernel32", use_last_error=True)
    api.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    disposition = ctypes.c_ubyte(True)  # FILE_DISPOSITION_INFO uses BOOLEAN.
    if not api.SetFileInformationByHandle(
        msvcrt.get_osfhandle(fd),
        4,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ImportErrorSafe("stripe_drop_delete_failed")


class DropFile:
    def __init__(self, path):
        self.path, self.fd = path, None

    def __enter__(self):
        _parents(self.path)
        before = self.path.lstat()
        if not _regular(before) or not 1 <= before.st_size <= MAX_BYTES:
            raise ImportErrorSafe("stripe_drop_invalid")
        self.fd = (
            _open_locked_windows(self.path)
            if os.name == "nt"
            else os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        )
        try:
            self.fingerprint = _fingerprint(before)
            self.raw = self.read_unchanged()
            self.key = parse_drop(self.raw)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def read_unchanged(self):
        actual = os.fstat(self.fd)
        if not _regular(actual) or _fingerprint(actual) != self.fingerprint:
            raise ImportErrorSafe("stripe_drop_changed")
        os.lseek(self.fd, 0, os.SEEK_SET)
        raw = os.read(self.fd, MAX_BYTES + 1)
        if (
            len(raw) != actual.st_size
            or _fingerprint(os.fstat(self.fd)) != self.fingerprint
        ):
            raise ImportErrorSafe("stripe_drop_changed")
        if hasattr(self, "raw") and not hmac.compare_digest(raw, self.raw):
            raise ImportErrorSafe("stripe_drop_changed")
        return raw

    def delete_unchanged(self):
        self.read_unchanged()
        _delete_handle(self.fd)

    def __exit__(self, *_args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.raw, self.key = b"", ""


def parse_drop(raw):
    """Accept one literal assignment and comments, never dotenv expansion/code."""
    try:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
            raise ValueError()
        lines = raw.decode("utf-8-sig").splitlines()
        values = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(
                r"STRIPE_SECRET_KEY\s*=\s*([\"']?)([A-Za-z0-9_]+)\1\s*(?:#.*)?", line
            )
            if match is None or not re.fullmatch(
                r"(?:rk_live_|sk_live_)[A-Za-z0-9]{20,240}", match[2]
            ):
                raise ValueError()
            values.append(match[2])
        if len(values) != 1:
            raise ValueError()
        return values[0]
    except (ValueError, TypeError, UnicodeError):
        raise ImportErrorSafe("stripe_drop_invalid") from None


@_silence_sdk_logs()
def inspect_live_key(key, *, client_factory=None):
    """Pinned SDK, two read-only calls; no payment/webhook/product mutations."""
    import stripe
    from studio_platform.stripe_client import API_VERSION, _get

    if client_factory is None:
        if stripe.VERSION != "16.0.0":
            raise ImportErrorSafe("stripe_sdk_version_unconfirmed")
        client_factory = lambda value: stripe.StripeClient(
            value,
            stripe_version=API_VERSION,
            base_addresses={"api": "https://api.stripe.com"},
            http_client=stripe.HTTPXClient(timeout=10, allow_sync_methods=True),
            max_network_retries=0,
        )
    client = client_factory(key)
    try:
        account = client.v1.accounts.retrieve_current()
    except stripe.PermissionError:
        raise ImportErrorSafe("stripe_live_permission_required_account_read") from None
    try:
        prices = client.v1.prices.list({"limit": 1})
    except stripe.PermissionError:
        raise ImportErrorSafe("stripe_live_permission_required_prices_read") from None
    account_id = _get(account, "id")
    rows = _get(prices, "data")
    if (
        _get(account, "object") != "account"
        or not isinstance(account_id, str)
        or not re.fullmatch(r"acct_[A-Za-z0-9]{6,80}", account_id)
        or _get(prices, "object") != "list"
        or not isinstance(rows, list)
        or len(rows) != 1
        or _get(rows[0], "object") != "price"
        or _get(rows[0], "livemode") is not True
    ):
        raise ImportErrorSafe("stripe_live_identity_unconfirmed")
    return {"account_id": account_id, "mode": "live"}


def _record(response):
    expected = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{SECRET}-"
    if (
        not isinstance(response, dict)
        or response.get("Name") != SECRET
        or not isinstance(response.get("ARN"), str)
        or not response["ARN"].startswith(expected)
    ):
        raise ImportErrorSafe("stripe_secret_destination_unconfirmed")
    return response


def _value(response):
    _record(response)
    raw, version = response.get("SecretString"), response.get("VersionId")
    if (
        not isinstance(raw, str)
        or len(raw) > MAX_BYTES
        or not isinstance(version, str)
        or not VERSION.fullmatch(version)
    ):
        raise ImportErrorSafe("stripe_managed_secret_invalid")
    try:
        document = json.loads(raw, object_pairs_hook=_unique)
        validate_credentials(document)
    except (ValueError, TypeError):
        raise ImportErrorSafe("stripe_managed_secret_invalid") from None
    return document, version


def _same(left, right):
    return hmac.compare_digest(
        json.dumps(left, sort_keys=True), json.dumps(right, sort_keys=True)
    )


def _receipt(path, value=None):
    """Private durable no-value operation journal; never stores credential hashes."""
    _parents(path)
    if value is None:
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        if not _regular(before) or before.st_size > MAX_BYTES:
            raise ImportErrorSafe("stripe_import_receipt_invalid")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _private(fd)
            if _fingerprint(os.fstat(fd)) != _fingerprint(before):
                raise ImportErrorSafe("stripe_import_receipt_changed")
            raw = os.read(fd, MAX_BYTES + 1)
            return json.loads(raw, object_pairs_hook=_unique)
        finally:
            os.close(fd)
    temporary = path.with_name(path.name + "." + value["operation_id"] + ".tmp")
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        # A crash can leave the already-flushed no-value receipt before rename.
        # Reuse it only byte-semantically unchanged; never delete/overwrite it.
        if _receipt(temporary) != value:
            raise ImportErrorSafe(
                "stripe_import_receipt_partial_requires_review"
            ) from None
    else:
        try:
            _private(fd, protect=True)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                )
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
    if os.name == "nt":
        api = ctypes.WinDLL("Kernel32", use_last_error=True)
        api.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        api.MoveFileExW.restype = wintypes.BOOL
        # REPLACE_EXISTING | WRITE_THROUGH: do not delete the source after a
        # receipt whose directory replacement has merely been queued.
        if not api.MoveFileExW(str(temporary), str(path), 0x9):
            raise ImportErrorSafe("stripe_import_receipt_replace_failed")
    else:
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if _receipt(path) != value:
        raise ImportErrorSafe("stripe_import_receipt_readback_failed")
    return value


def _validate_receipt(receipt):
    if (
        type(receipt) is not dict
        or set(receipt)
        != {
            "version",
            "operation_id",
            "secret_name",
            "aws_account",
            "region",
            "source_fingerprint",
            "state",
            "version_id",
        }
        or type(receipt["version"]) is not int
        or receipt["version"] != 1
        or not isinstance(receipt["operation_id"], str)
        or not VERSION.fullmatch(receipt["operation_id"])
        or receipt["secret_name"] != SECRET
        or receipt["aws_account"] != ACCOUNT
        or receipt["region"] != REGION
        or type(receipt["source_fingerprint"]) is not list
        or len(receipt["source_fingerprint"]) != 4
        or any(
            type(value) is not int or value < 0
            for value in receipt["source_fingerprint"]
        )
        or receipt["state"] not in {"prepared", "verified", "source_deleted"}
        or receipt["version_id"] is not None
        and (
            not isinstance(receipt["version_id"], str)
            or not VERSION.fullmatch(receipt["version_id"])
        )
    ):
        raise ImportErrorSafe("stripe_import_receipt_requires_review")
    return receipt


def _intent(drop):
    receipt = _receipt(RECEIPT)
    if receipt is None:
        receipt = {
            "version": 1,
            "operation_id": str(uuid.uuid4()),
            "secret_name": SECRET,
            "aws_account": ACCOUNT,
            "region": REGION,
            "source_fingerprint": drop.fingerprint,
            "state": "prepared",
            "version_id": None,
        }
        return _receipt(RECEIPT, receipt)
    receipt = _validate_receipt(receipt)
    if receipt["source_fingerprint"] != drop.fingerprint:
        raise ImportErrorSafe("stripe_import_source_changed")
    return receipt


def _source_absent():
    _parents(SOURCE)
    try:
        SOURCE.lstat()
    except FileNotFoundError:
        return
    raise ImportErrorSafe("stripe_import_source_reappeared")


def _reconcile_source_absent(api):
    """Finish only a durably verified import, without a new Stripe/AWS write."""
    _source_absent()
    receipt = _validate_receipt(_receipt(RECEIPT))
    if (
        receipt["state"] not in {"verified", "source_deleted"}
        or receipt["version_id"] is None
    ):
        raise ImportErrorSafe("stripe_import_source_absent_requires_review")
    version = receipt["version_id"]
    exact, exact_version = _value(
        api.get_secret_value(SecretId=SECRET, VersionId=version)
    )
    current, current_version = _value(
        api.get_secret_value(SecretId=SECRET, VersionStage="AWSCURRENT")
    )
    if (
        exact_version != version
        or current_version != version
        or not _same(exact, current)
        or exact.get("version") != 2
        or exact.get("mode") != "live"
    ):
        raise ImportErrorSafe("stripe_secret_current_version_changed")
    _source_absent()
    _receipt(RECEIPT, {**receipt, "state": "source_deleted"})
    return {"status": "verified_source_absent", "version_id": version, "mode": "live"}


@_silence_sdk_logs()
def import_existing(
    target, *, session=None, read_identity=inspect_live_key, read_drop=DropFile
):
    selection(target)
    session = session or boto3.Session(region_name=REGION)
    config = Config(
        connect_timeout=5,
        read_timeout=20,
        retries={"mode": "standard", "total_max_attempts": 2},
    )
    identity = session.client(
        "sts",
        region_name=REGION,
        endpoint_url=f"https://sts.{REGION}.amazonaws.com",
        config=config,
    ).get_caller_identity()
    if not isinstance(identity, dict) or identity.get("Account") != ACCOUNT:
        raise ImportErrorSafe("stripe_aws_account_unconfirmed")
    api = session.client(
        "secretsmanager",
        region_name=REGION,
        endpoint_url=f"https://secretsmanager.{REGION}.amazonaws.com",
        config=config,
    )
    with ExitStack() as stack:
        try:
            drop = stack.enter_context(read_drop(SOURCE))
        except FileNotFoundError:
            return _reconcile_source_absent(api)
        stripe_identity = read_identity(drop.key)
        if (
            type(stripe_identity) is not dict
            or set(stripe_identity) != {"account_id", "mode"}
            or stripe_identity["mode"] != "live"
        ):
            raise ImportErrorSafe("stripe_live_identity_unconfirmed")
        document = {
            "version": 2,
            "mode": "live",
            "account_id": stripe_identity["account_id"],
            "secret_key": drop.key,
            "publishable_key": None,
            "webhook_secret": None,
            "webhook_endpoint_id": None,
        }
        validate_credentials(document)
        receipt = _intent(drop)
        try:
            existing, version = _value(
                api.get_secret_value(SecretId=SECRET, VersionStage="AWSCURRENT")
            )
        except api.exceptions.ResourceNotFoundException:
            existing = None
        if existing is not None:
            if not _same(existing, document):
                raise ImportErrorSafe(
                    "stripe_existing_secret_differs_rotation_not_authorized"
                )
            if receipt["state"] == "verified" and receipt["version_id"] != version:
                raise ImportErrorSafe("stripe_secret_current_version_changed")
        else:
            if receipt["state"] != "prepared":
                raise ImportErrorSafe("stripe_secret_verified_version_missing")
            try:
                description = _record(api.describe_secret(SecretId=SECRET))
            except api.exceptions.ResourceNotFoundException:
                description = None
            # No Secrets Manager compare-and-swap exists for an empty existing
            # container. Do not take AWSCURRENT from a concurrent writer.
            if description is not None:
                raise ImportErrorSafe("stripe_existing_secret_requires_review")
            drop.read_unchanged()
            payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
            token = receipt["operation_id"]
            api.create_secret(
                Name=SECRET,
                ClientRequestToken=token,
                SecretString=payload,
                Description="Owner-existing Inkseq LIVE Stripe credential; charging remains OFF",
                Tags=[
                    {"Key": "project", "Value": "inkseq"},
                    {"Key": "environment", "Value": "production"},
                ],
            )
            version = token
        exact, exact_version = _value(
            api.get_secret_value(SecretId=SECRET, VersionId=version)
        )
        if exact_version != version or not _same(exact, document):
            raise ImportErrorSafe("stripe_secret_readback_mismatch")
        current, current_version = _value(
            api.get_secret_value(SecretId=SECRET, VersionStage="AWSCURRENT")
        )
        if current_version != version or not _same(current, document):
            raise ImportErrorSafe("stripe_secret_current_version_changed")
        receipt = _receipt(
            RECEIPT, {**receipt, "state": "verified", "version_id": version}
        )
        drop.delete_unchanged()
    _receipt(RECEIPT, {**receipt, "state": "source_deleted"})
    return {"status": "verified_source_deleted", "version_id": version, "mode": "live"}


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--target", choices=("production",), required=True)
    args = parser.parse_args(argv)
    try:
        result = import_existing(args.target)
    except ImportErrorSafe as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ClientError, BotoCoreError, ProvisionError, ValueError, TypeError, OSError):
        print("stripe_credential_import_failed_reconcile_receipt", file=sys.stderr)
        return 1
    # SDK exceptions can contain request material; do not expose their repr.
    except Exception:
        print("stripe_credential_import_failed_reconcile_receipt", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
