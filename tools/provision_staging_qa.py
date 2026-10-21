"""Provision fixed staging QA accounts over HTTPS; credentials stay local/in memory."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from studio_platform.staging_qa import (
    DEFAULT_QA_NAMES,
    ORIGIN,
    PASSWORD,
    QA_NAME_SETS,
    QA_ROLES,
    unique_pairs,
)

ACCOUNTS_FILE = Path(
    "C:/Users/danmo/Desktop/inference/h3-studio/.local/staging-qa-accounts.json"
)
MAX_BYTES = 4096


class ProvisionError(Exception):
    """Static diagnostics only; no request/response/provider exception text."""


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(
            2,
            "Invalid arguments; credentials cannot be supplied on the command line.\n",
        )


def _regular(info):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and not getattr(info, "st_file_attributes", 0) & 0x400
    )


def _windows_acl(fd, *, protect=False):
    """Install/check a protected ACL granting only the current user full access."""
    import msvcrt

    raw = subprocess.run(
        ["C:/Windows/System32/whoami.exe", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    matches = re.findall(r"S-1-(?:\d+-)+\d+", raw)
    if len(matches) != 1:
        raise ProvisionError("qa_private_acl_failed")
    api = ctypes.WinDLL("Advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("Kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    api.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(pointer)]
    api.ConvertStringSidToSidW.restype = wintypes.BOOL
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    api.GetSecurityDescriptorDacl.argtypes = [
        pointer,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(pointer),
        ctypes.POINTER(wintypes.BOOL),
    ]
    api.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    api.SetSecurityInfo.argtypes = [
        pointer,
        ctypes.c_int,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    api.SetSecurityInfo.restype = wintypes.DWORD
    api.GetSecurityInfo.argtypes = [
        pointer,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
    ]
    api.GetSecurityInfo.restype = wintypes.DWORD
    api.GetSecurityDescriptorControl.argtypes = [
        pointer,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.GetSecurityDescriptorControl.restype = wintypes.BOOL
    api.EqualSid.argtypes = [pointer, pointer]
    api.EqualSid.restype = wintypes.BOOL
    api.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
    api.GetAce.restype = wintypes.BOOL
    kernel.ReOpenFile.argtypes = [
        pointer,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel.ReOpenFile.restype = pointer
    kernel.CloseHandle.argtypes = [pointer]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    sid, supplied, returned, handle = pointer(), pointer(), pointer(), pointer()
    try:
        if not api.ConvertStringSidToSidW(matches[0], ctypes.byref(sid)):
            raise ProvisionError("qa_private_acl_failed")
        # CRT file descriptors lack WRITE_DAC. Reopen this same live file
        # object for ACL access, without resolving the pathname a second time.
        handle = pointer(
            kernel.ReOpenFile(pointer(msvcrt.get_osfhandle(fd)), 0x60000, 3, 0)
        )
        if handle.value in (None, ctypes.c_void_p(-1).value):
            raise ProvisionError("qa_private_acl_failed")
        if protect:
            if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "D:P(A;;FA;;;" + matches[0] + ")", 1, ctypes.byref(supplied), None
            ):
                raise ProvisionError("qa_private_acl_failed")
            present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), pointer()
            if (
                not api.GetSecurityDescriptorDacl(
                    supplied,
                    ctypes.byref(present),
                    ctypes.byref(acl),
                    ctypes.byref(defaulted),
                )
                or not present
            ):
                raise ProvisionError("qa_private_acl_failed")
            if api.SetSecurityInfo(handle, 1, 0x80000004, None, None, acl, None):
                raise ProvisionError("qa_private_acl_failed")
        owner, acl = pointer(), pointer()
        if api.GetSecurityInfo(
            handle,
            1,
            5,
            ctypes.byref(owner),
            None,
            ctypes.byref(acl),
            None,
            ctypes.byref(returned),
        ):
            raise ProvisionError("qa_private_acl_failed")
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if (
            not api.EqualSid(owner, sid)
            or not acl
            or not api.GetSecurityDescriptorControl(
                returned, ctypes.byref(control), ctypes.byref(revision)
            )
            or not control.value & 0x1000
        ):
            raise ProvisionError("qa_private_acl_failed")

        class ACL(ctypes.Structure):
            _fields_ = [
                ("revision", ctypes.c_ubyte),
                ("reserved", ctypes.c_ubyte),
                ("size", wintypes.WORD),
                ("count", wintypes.WORD),
                ("reserved2", wintypes.WORD),
            ]

        if ctypes.cast(acl, ctypes.POINTER(ACL)).contents.count != 1:
            raise ProvisionError("qa_private_acl_failed")
        ace = pointer()
        if not api.GetAce(acl, 0, ctypes.byref(ace)):
            raise ProvisionError("qa_private_acl_failed")
        header = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))
        mask = ctypes.cast(
            pointer(ace.value + 4), ctypes.POINTER(wintypes.DWORD)
        ).contents.value
        if (
            header[0] != 0
            or mask != 0x1F01FF
            or not api.EqualSid(pointer(ace.value + 8), sid)
        ):
            raise ProvisionError("qa_private_acl_failed")
    finally:
        for value in (returned, supplied, sid):
            if value:
                kernel.LocalFree(value)
        if handle.value not in (None, ctypes.c_void_p(-1).value):
            kernel.CloseHandle(handle)


def _private(fd, *, protect=False):
    if not _regular(os.fstat(fd)):
        raise ProvisionError("qa_private_file_invalid")
    if os.name == "nt":
        _windows_acl(fd, protect=protect)
    else:
        if protect:
            os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProvisionError("qa_private_file_permissions")


def _validate(value):
    if (
        type(value) is not dict
        or set(value) != {"base_url", "accounts"}
        or value["base_url"] != ORIGIN
        or type(value["accounts"]) is not list
        or len(value["accounts"]) not in (2, 3)
    ):
        raise ProvisionError("qa_private_file_invalid")
    found = set()
    for row in value["accounts"]:
        if (
            type(row) is not dict
            or set(row) != {"username", "password", "role"}
            or not isinstance(row["username"], str)
            or row["username"] not in QA_ROLES
            or row["username"] in found
            or row["role"] != QA_ROLES[row["username"]]
            or not isinstance(row["password"], str)
            or not PASSWORD.fullmatch(row["password"])
        ):
            raise ProvisionError("qa_private_file_invalid")
        found.add(row["username"])
    if frozenset(found) not in QA_NAME_SETS:
        raise ProvisionError("qa_private_file_invalid")
    return value


def credit_fixture_accounts(value, *, token=secrets.token_urlsafe):
    """Prepare exactly one extension in memory; never publish credentials.

    A separately reviewed operator must publish only to the canonical private
    JSON. This helper does not touch a file, log, reset or rotate old material.
    """
    value = _validate(value)
    result = {
        "base_url": value["base_url"],
        "accounts": [dict(row) for row in value["accounts"]],
    }
    if any(row["username"] == "qa-credit" for row in result["accounts"]):
        return result
    result["accounts"].append(
        {"username": "qa-credit", "password": token(36), "role": "user"}
    )
    return _validate(result)


def local_accounts(path=ACCOUNTS_FILE, *, token=secrets.token_urlsafe):
    """Exclusive creation; retries reuse the only durable credential copy."""
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise ProvisionError("qa_private_parent_invalid")
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not _regular(info):
        raise ProvisionError("qa_private_file_invalid")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = os.open(path, flags)
        created = False
    else:
        created = True
    try:
        _private(fd, protect=created)
        if created:
            value = _validate(
                {
                    "base_url": ORIGIN,
                    "accounts": [
                        {
                            "username": username,
                            "password": token(36),
                            "role": QA_ROLES[username],
                        }
                        for username in DEFAULT_QA_NAMES
                    ],
                }
            )
            raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            if len(raw) > MAX_BYTES:
                raise ProvisionError("qa_private_file_invalid")
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
            return value
        raw = os.read(fd, MAX_BYTES + 1)
        if not raw or len(raw) > MAX_BYTES:
            raise ProvisionError("qa_private_file_invalid")
        try:
            return _validate(json.loads(raw, object_pairs_hook=unique_pairs))
        except (ValueError, UnicodeError):
            raise ProvisionError("qa_private_file_invalid") from None
    finally:
        os.close(fd)


def bootstrap_password():
    # Existing loader restricts the exact staging secret; no key rotation/write.
    from deploy.platform import runtime_secrets_aws

    for name in ("boto3", "botocore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    value = runtime_secrets_aws.value(
        runtime_secrets_aws.client(),
        "/sixnine/staging/bootstrap-accounts",
        target="staging",
    )
    return value["superdan"]


def provision(
    value, *, grant_test_credits=False, client=None, read_bootstrap=bootstrap_password
):
    import httpx

    value = _validate(value)
    if type(grant_test_credits) is not bool:
        raise ProvisionError("qa_request_invalid")
    names = {row["username"] for row in value["accounts"]}
    if "qa-credit" in names and grant_test_credits:
        raise ProvisionError("qa_request_invalid")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    owns = client is None
    if owns:
        client = httpx.Client(
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
            verify=True,
            follow_redirects=False,
            trust_env=False,
            timeout=30,
        )
    try:
        account = next(
            row for row in value["accounts"] if row["username"] == "qa-admin"
        )
        response = client.post(
            "/api/auth/login",
            json={"username": "qa-admin", "password": account["password"]},
        )
        if response.status_code == 401 and "qa-credit" not in names:
            password = read_bootstrap()
            try:
                response = client.post(
                    "/api/auth/login",
                    json={"username": "superdan", "password": password},
                )
            finally:
                del password
        if response.status_code != 200:
            raise ProvisionError("qa_admin_login_failed")
        response = client.post(
            "/v1/admin/staging/qa-accounts",
            json={
                "accounts": value["accounts"],
                "grant_test_credits": grant_test_credits,
            },
        )
        if response.status_code != 200:
            raise ProvisionError("qa_provision_refused")
        result = response.json()
        if not isinstance(result, dict) or set(result) != {"accounts", "credit_grant"}:
            raise ProvisionError("qa_response_invalid")
        return {
            "status": "staging_qa_provisioned",
            "accounts": sorted(names),
            "credit_grant_requested": grant_test_credits,
        }
    finally:
        try:
            client.post("/api/auth/logout")
        except Exception:
            pass
        if owns:
            client.close()


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--grant-test-credits", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = provision(local_accounts(), grant_test_credits=args.grant_test_credits)
    except Exception:
        print(
            "staging_qa_provision_failed; the protected local file is preserved for retry",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
