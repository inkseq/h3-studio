#!/usr/bin/env python3
"""Public Inkseq connection helper; standard library, no private AI Registry.

Read this file before running it.  A one-time code is supplied through a hidden
prompt or stdin, never a command argument.  A PAT is generated and saved in the
current user's OS credential store *before* only its digest is registered.
This module does not perform network or filesystem operations on import.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import getpass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROTOCOL = "client-held-pat-v1"
ROUTE_PROTOCOL = "client-held-pat-route-alias-v1"
CANONICAL_ORIGIN = "https://inkseq.com"
LEGACY_ORIGINS = frozenset({"https://sixnine.art", "https://www.sixnine.art"})
# Immutable creator-full version 1, not a live discovery policy or a new grant.
CREATOR_V1_SCOPES = sorted({"projects:read", "projects:create", "projects:write", "assets:read",
    "assets:write", "jobs:read", "jobs:write", "assistant:run"})
MAX_RECORD_BYTES = 32768
MAX_RESPONSE_BYTES = 1024 * 1024
ID = re.compile(r"^connection-[0-9a-f]{32}$")
HEX = re.compile(r"^[0-9a-f]{64}$")
CODE = re.compile(r"^sxc_[A-Za-z0-9_-]{43}$")
_MACOS_INTERACTION_LOCK = threading.RLock()


class HelperError(Exception):
    """Only static codes, never exception bodies, credentials or HTTP headers."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def checked_origin(value):
    if not isinstance(value, str):
        raise HelperError("invalid_origin")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment
            or parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}):
        raise HelperError("https_or_loopback_origin_required")
    try:
        parsed.port
    except ValueError:
        raise HelperError("invalid_origin") from None
    return parsed.scheme + "://" + parsed.netloc.lower()


def _migration_source(state, origin, connection_id):
    expected = state.get("expected") if isinstance(state, dict) else None
    if (origin not in LEGACY_ORIGINS or not isinstance(expected, dict)
            or set(expected) != {"origin", "connection_id", "owner", "tenant", "profile_id", "profile_version", "fingerprint"}
            or state.get("protocol") != PROTOCOL or state.get("status") != "connected"
            or expected.get("origin") != origin or expected.get("connection_id") != connection_id
            or expected.get("tenant") != "sixnine" or not isinstance(expected.get("owner"), str) or not expected["owner"]
            or expected.get("profile_id") != "creator-full" or type(expected.get("profile_version")) is not int
            or expected["profile_version"] != 1 or not isinstance(expected.get("fingerprint"), str)
            or not HEX.fullmatch(expected["fingerprint"])
            or not isinstance(state.get("api_key"), str) or not re.fullmatch(r"sxp_[A-Za-z0-9_-]{43}", state["api_key"])
            or not isinstance(state.get("key_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", state["key_id"])
            or type(state.get("expires_at")) not in {int, float} or not math.isfinite(state["expires_at"])
            or state["expires_at"] <= time.time()):
        raise HelperError("origin_migration_source_invalid")
    authorization = {key: value for key, value in expected.items() if key != "fingerprint"}
    authorization.update(scopes=CREATOR_V1_SCOPES, all_projects=True, project_ids=[],
        key_lifetime_days=90, key_expires_at=state["expires_at"])
    if not hmac.compare_digest(digest(canonical(authorization)), expected["fingerprint"]):
        raise HelperError("origin_migration_authorization_mismatch")
    return digest(canonical({"expected": expected, "key_id": state["key_id"],
        "expires_at": state["expires_at"], "token_hash": digest(state["api_key"])}))


def _route_alias(state, origin, connection_id):
    binding = _migration_source(state, origin, connection_id)
    return {"protocol": ROUTE_PROTOCOL, "status": "active", "origin": CANONICAL_ORIGIN,
        "connection_id": connection_id, "source_origin": origin,
        "source_reference": SecureStore.reference(origin, connection_id), "source_binding": binding,
        "authorization_fingerprint": state["expected"]["fingerprint"],
        "key_id": state["key_id"], "expires_at": state["expires_at"]}


def resolve_connection(store, origin, connection_id):
    """Resolve only a previously verified one-hop alias; never follow HTTP redirects."""
    origin = checked_origin(origin)
    SecureStore.reference(origin, connection_id)
    state = store.load(origin, connection_id)
    alias = state if origin == CANONICAL_ORIGIN else store.load(CANONICAL_ORIGIN, connection_id) if origin in LEGACY_ORIGINS else None
    if not isinstance(alias, dict) or alias.get("protocol") != ROUTE_PROTOCOL:
        if (isinstance(state, dict) and state.get("protocol") == PROTOCOL
                and (not isinstance(state.get("expected"), dict) or state["expected"].get("origin") != origin)):
            raise HelperError("saved_authorization_mismatch")
        return origin, state
    source_origin = alias.get("source_origin")
    if source_origin not in LEGACY_ORIGINS or origin not in {source_origin, CANONICAL_ORIGIN}:
        raise HelperError("origin_migration_route_mismatch")
    source = store.load(source_origin, connection_id)
    if alias != _route_alias(source, source_origin, connection_id):
        raise HelperError("origin_migration_binding_mismatch")
    return CANONICAL_ORIGIN, source


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise HelperError("redirect_rejected")


class JsonHTTP:
    def __init__(self, origin):
        self.origin = checked_origin(origin)
        self.opener = build_opener(_NoRedirect())

    def request(self, path, *, method="GET", body=None, token=None, idempotency_key=None):
        if not isinstance(path, str) or not path.startswith(("/v1/", "/api/auth/me")) or path.startswith("//"):
            raise HelperError("invalid_api_path")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or any(c in path for c in ("\r", "\n", "\\")):
            raise HelperError("invalid_api_path")
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise HelperError("invalid_method")
        raw = canonical(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", idempotency_key):
                raise HelperError("invalid_idempotency_key")
            headers["Idempotency-Key"] = idempotency_key
        request = Request(self.origin + path, data=raw, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise HelperError("response_too_large")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise HelperError("invalid_response")
            return value
        except HTTPError as error:
            # Parse only our allowlisted code; do not echo server body/URL.
            try:
                value = json.loads(error.read(8192))
                code = value.get("code") if isinstance(value, dict) else None
            except (ValueError, UnicodeError):
                code = None
            allowed = {"connection_not_exchanged", "connection_expired", "connection_consumed",
                "connection_unavailable", "connection_authority_changed", "connection_exchange_limited",
                "connection_authorization_mismatch", "connection_key_conflict", "too_many_active_keys"}
            raise HelperError(code if code in allowed else "http_request_failed") from None
        except (URLError, TimeoutError, OSError):
            raise HelperError("network_result_unknown") from None
        except (ValueError, UnicodeError):
            raise HelperError("invalid_response") from None


def _dpapi(raw, *, protect):
    """User-scoped Windows DPAPI, UI forbidden; no machine-wide protection."""
    if os.name != "nt":
        raise HelperError("windows_dpapi_unavailable")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
    incoming = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(incoming, ctypes.POINTER(ctypes.c_byte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    action = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    action.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    action.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not action(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise HelperError("os_credential_storage_failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class _MacOSKeychain:
    """SecItem records in one existing file-based user Keychain, without UI.

    An unsigned Python CLI cannot assume data-protection Keychain entitlements.
    The older read-only metadata APIs select/check the existing default; record
    operations use SecItem. No Keychain is created, unlocked or reconfigured.
    The optional borrowed reference exists only for isolated native tests.
    """
    @staticmethod
    def _constant(library, name):
        value = ctypes.c_void_p.in_dll(library, name).value
        if not value:
            raise HelperError("os_credential_storage_unavailable")
        return value

    @staticmethod
    def _callbacks(library, name):
        return ctypes.addressof(ctypes.c_byte.in_dll(library, name))

    def __init__(self, *, keychain_ref=None):
        self.keychain_ref = None
        if sys.platform != "darwin" or os.getuid() == 0:
            raise HelperError("os_credential_storage_unavailable")
        try:
            self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            ref, index, status = ctypes.c_void_p, ctypes.c_long, ctypes.c_int32
            signatures = {
                "CFStringCreateWithBytes": ([ref, ref, index, ctypes.c_uint32, ctypes.c_ubyte], ref),
                "CFDataCreate": ([ref, ref, index], ref),
                "CFDictionaryCreate": ([ref, ctypes.POINTER(ref), ctypes.POINTER(ref), index, ref, ref], ref),
                "CFArrayCreate": ([ref, ctypes.POINTER(ref), index, ref], ref),
                "CFRelease": ([ref], None), "CFRetain": ([ref], ref),
                "CFGetTypeID": ([ref], ctypes.c_ulong), "CFDataGetTypeID": ([], ctypes.c_ulong),
                "CFDataGetLength": ([ref], index), "CFDataGetBytePtr": ([ref], ref),
            }
            for name, (args, result) in signatures.items():
                function = getattr(self.cf, name)
                function.argtypes, function.restype = args, result
            signatures = {
                "SecKeychainCopyDefault": ([ctypes.POINTER(ref)], status),
                "SecKeychainGetStatus": ([ref, ctypes.POINTER(ctypes.c_uint32)], status),
                "SecKeychainGetUserInteractionAllowed": ([ctypes.POINTER(ctypes.c_ubyte)], status),
                "SecKeychainSetUserInteractionAllowed": ([ctypes.c_ubyte], status),
                "SecItemCopyMatching": ([ref, ctypes.POINTER(ref)], status),
                "SecItemAdd": ([ref, ctypes.POINTER(ref)], status), "SecItemUpdate": ([ref, ref], status),
            }
            for name, (args, result) in signatures.items():
                function = getattr(self.security, name)
                function.argtypes, function.restype = args, result
            names = ("kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
                "kSecUseAuthenticationUI", "kSecUseAuthenticationUIFail", "kSecUseKeychain",
                "kSecMatchSearchList", "kSecMatchLimit", "kSecMatchLimitOne", "kSecReturnData", "kSecValueData")
            self.constants = {name: self._constant(self.security, name) for name in names}
            self.constants["kCFBooleanTrue"] = self._constant(self.cf, "kCFBooleanTrue")
            self.key_callbacks = self._callbacks(self.cf, "kCFTypeDictionaryKeyCallBacks")
            self.value_callbacks = self._callbacks(self.cf, "kCFTypeDictionaryValueCallBacks")
            self.array_callbacks = self._callbacks(self.cf, "kCFTypeArrayCallBacks")
            with self._no_ui():
                if keychain_ref is None:
                    result = ref()
                    code = self.security.SecKeychainCopyDefault(ctypes.byref(result))
                    # Copy APIs transfer ownership even if a malformed native
                    # response also supplies a reference on failure.
                    self.keychain_ref = result.value
                    if code != 0 or not self.keychain_ref:
                        raise HelperError("os_credential_storage_unavailable")
                else:
                    if not isinstance(keychain_ref, int) or isinstance(keychain_ref, bool) or keychain_ref <= 0:
                        raise HelperError("os_credential_storage_unavailable")
                    self.keychain_ref = self.cf.CFRetain(keychain_ref)
                    if not self.keychain_ref:
                        raise HelperError("os_credential_storage_unavailable")
                self._available()
        except (OSError, AttributeError, ValueError, TypeError):
            self.close()
            raise HelperError("os_credential_storage_unavailable") from None
        except HelperError:
            self.close()
            raise

    def close(self):
        with _MACOS_INTERACTION_LOCK:
            if self.keychain_ref:
                self.cf.CFRelease(self.keychain_ref)
                self.keychain_ref = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @contextmanager
    def _no_ui(self):
        # File-Keychain SecItem shims do not consistently honor the per-query
        # UI flag. Temporarily suppress UI in this caller process as well. The
        # original flag is restored even on denial; no persistent ACL changes.
        with _MACOS_INTERACTION_LOCK:
            previous = ctypes.c_ubyte()
            if self.security.SecKeychainGetUserInteractionAllowed(ctypes.byref(previous)) != 0:
                raise HelperError("os_credential_storage_failed")
            try:
                if self.security.SecKeychainSetUserInteractionAllowed(False) != 0:
                    raise HelperError("os_credential_storage_failed")
                yield
            finally:
                if self.security.SecKeychainSetUserInteractionAllowed(previous.value) != 0:
                    raise HelperError("os_credential_storage_failed")

    def _available(self, *, write=False):
        state = ctypes.c_uint32()
        if (not self.keychain_ref
                or self.security.SecKeychainGetStatus(self.keychain_ref, ctypes.byref(state)) != 0
                or state.value & (7 if write else 3) != (7 if write else 3)):
            raise HelperError("os_credential_storage_failed")

    def _string(self, value):
        raw = value.encode("utf-8")
        buffer = ctypes.create_string_buffer(raw)
        result = self.cf.CFStringCreateWithBytes(None, buffer, len(raw), 0x08000100, False)
        if not result:
            raise HelperError("os_credential_storage_failed")
        return result

    def _data(self, raw):
        if not isinstance(raw, bytes) or len(raw) > MAX_RECORD_BYTES:
            raise HelperError("invalid_credential_record")
        try:
            raw.decode("utf-8")
        except UnicodeError:
            raise HelperError("invalid_credential_record") from None
        buffer = ctypes.create_string_buffer(raw)
        result = self.cf.CFDataCreate(None, buffer, len(raw))
        if not result:
            raise HelperError("os_credential_storage_failed")
        return result

    def _dictionary(self, pairs):
        ref = ctypes.c_void_p
        keys = (ref * len(pairs))(*(key for key, _ in pairs))
        values = (ref * len(pairs))(*(value for _, value in pairs))
        result = self.cf.CFDictionaryCreate(None, keys, values, len(pairs), self.key_callbacks, self.value_callbacks)
        if not result:
            raise HelperError("os_credential_storage_failed")
        return result

    @contextmanager
    def _query(self, reference, *, add=False, lookup=False, raw=None):
        if not isinstance(reference, str) or not HEX.fullmatch(reference):
            raise HelperError("invalid_connection_id")
        owned = []
        try:
            service = self._string("sixnine-agent")
            owned.append(service)
            account = self._string(reference)
            owned.append(account)
            c = self.constants
            pairs = [(c["kSecClass"], c["kSecClassGenericPassword"]), (c["kSecAttrService"], service),
                (c["kSecAttrAccount"], account), (c["kSecUseAuthenticationUI"], c["kSecUseAuthenticationUIFail"])]
            if add:
                pairs.append((c["kSecUseKeychain"], self.keychain_ref))
            else:
                refs = (ctypes.c_void_p * 1)(self.keychain_ref)
                search = self.cf.CFArrayCreate(None, refs, 1, self.array_callbacks)
                if not search:
                    raise HelperError("os_credential_storage_failed")
                owned.append(search)
                pairs.append((c["kSecMatchSearchList"], search))
            if lookup:
                pairs += [(c["kSecMatchLimit"], c["kSecMatchLimitOne"]), (c["kSecReturnData"], c["kCFBooleanTrue"])]
            if raw is not None:
                data = self._data(raw)
                owned.append(data)
                pairs.append((c["kSecValueData"], data))
            query = self._dictionary(pairs)
            owned.append(query)
            yield query
        finally:
            for value in reversed(owned):
                self.cf.CFRelease(value)

    def lookup(self, reference):
        with self._no_ui():
            self._available()
            with self._query(reference, lookup=True) as query:
                result = ctypes.c_void_p()
                code = self.security.SecItemCopyMatching(query, ctypes.byref(result))
                try:
                    if code == -25300 and not result.value:  # errSecItemNotFound
                        return None
                    if code != 0 or not result.value:
                        raise HelperError("os_credential_storage_failed")
                    if self.cf.CFGetTypeID(result.value) != self.cf.CFDataGetTypeID():
                        raise HelperError("invalid_credential_record")
                    length = self.cf.CFDataGetLength(result.value)
                    if length < 0 or length > MAX_RECORD_BYTES:
                        raise HelperError("invalid_credential_record")
                    pointer = self.cf.CFDataGetBytePtr(result.value)
                    if length and not pointer:
                        raise HelperError("invalid_credential_record")
                    raw = ctypes.string_at(pointer, length) if length else b""
                    try:
                        raw.decode("utf-8")
                    except UnicodeError:
                        raise HelperError("invalid_credential_record") from None
                    return raw
                finally:
                    if result.value:
                        self.cf.CFRelease(result.value)

    def write(self, reference, raw, *, replace=False):
        with self._no_ui():
            self._available(write=True)
            with self._query(reference, add=not replace, raw=None if replace else raw) as query:
                if replace:
                    data = self._data(raw)
                    attributes = None
                    try:
                        attributes = self._dictionary([(self.constants["kSecValueData"], data)])
                        code = self.security.SecItemUpdate(query, attributes)
                    finally:
                        if attributes:
                            self.cf.CFRelease(attributes)
                        self.cf.CFRelease(data)
                else:
                    code = self.security.SecItemAdd(query, None)
                if code == -25299:  # errSecDuplicateItem; never overwrite a racing creator
                    raise HelperError("connection_already_saved")
                if code != 0:
                    raise HelperError("os_credential_storage_failed")


class SecureStore:
    """User OS credentials: DPAPI, macOS Keychain or Linux Secret Service."""
    def __init__(self):
        self.platform = ("macos" if sys.platform == "darwin" else "windows" if os.name == "nt"
            else "linux" if sys.platform.startswith("linux") else "unsupported")
        if self.platform == "windows":
            location = os.environ.get("LOCALAPPDATA")
            if not location or not Path(location).is_absolute():
                raise HelperError("os_credential_storage_unavailable")
            self.directory = Path(location) / "Sixnine" / "agent-credentials"
        elif self.platform == "macos":
            try:
                import pwd
                self.lock_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
            except (ImportError, KeyError, OSError):
                raise HelperError("os_credential_storage_unavailable") from None
            if not self.lock_home.is_absolute():
                raise HelperError("os_credential_storage_unavailable")
            self.lock_directory = self.lock_home / "Library" / "Application Support" / "Sixnine" / "agent-locks"
            self.keychain = _MacOSKeychain()
        elif self.platform == "linux":
            self.command = shutil.which("secret-tool")
            if not self.command or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
                raise HelperError("os_credential_storage_unavailable")
            self.lock_directory = Path(tempfile.gettempdir()) / ("sixnine-agent-locks-" + str(os.getuid()))
        else:
            raise HelperError("os_credential_storage_unavailable")

    @staticmethod
    def reference(origin, connection_id):
        origin = checked_origin(origin)
        if not isinstance(connection_id, str) or not ID.fullmatch(connection_id):
            raise HelperError("invalid_connection_id")
        return digest(origin + "|" + connection_id)

    def _directory(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in [self.directory, self.directory.parent]:
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise HelperError("credential_storage_link_rejected")

    def _secret_service(self, action, reference, payload=None):
        args = [self.command, action]
        if action == "store":
            args += ["--label=Sixnine Agent credential"]
        args += ["application", "sixnine-agent", "reference", reference]
        try:
            result = subprocess.run(args, input=payload, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise HelperError("os_credential_storage_failed") from None
        if result.returncode != 0:
            if action == "lookup" and result.returncode == 1:
                return None
            raise HelperError("os_credential_storage_failed")
        if len(result.stdout) > MAX_RECORD_BYTES:
            raise HelperError("invalid_credential_record")
        return result.stdout

    @contextmanager
    def _linux_lock(self, reference):
        import fcntl
        self.lock_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        metadata = self.lock_directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or self.lock_directory.is_symlink()
                or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
            raise HelperError("credential_storage_link_rejected")
        descriptor = os.open(self.lock_directory / (reference + ".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise HelperError("credential_storage_link_rejected")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _preserve_record(existing, incoming):
        if existing and (existing.get("expected") != incoming.get("expected")
                or existing.get("api_key") != incoming.get("api_key")):
            raise HelperError("saved_authorization_mismatch")
        return existing if existing and existing.get("status") == "connected" and incoming.get("status") != "connected" else incoming

    @contextmanager
    def _macos_lock(self, reference):
        import fcntl
        # Stable passwd-derived home, independent of HOME/TMPDIR overrides.
        # Refuse unsafe existing directories instead of chmod or following them.
        user = os.getuid()
        for path in reversed([self.lock_home, *self.lock_home.parents]):
            metadata = path.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or path.is_symlink()
                    or metadata.st_uid not in {0, user} or metadata.st_mode & 0o022):
                raise HelperError("credential_storage_link_rejected")
        path = self.lock_home
        for part in self.lock_directory.relative_to(self.lock_home).parts:
            path = path / part
            path.mkdir(mode=0o700, exist_ok=True)
            metadata = path.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or path.is_symlink()
                    or metadata.st_uid != user or metadata.st_mode & 0o022
                    or path == self.lock_directory and metadata.st_mode & 0o077):
                raise HelperError("credential_storage_link_rejected")
        directory = os.open(self.lock_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptor = None
        acquired = False
        try:
            metadata = os.fstat(directory)
            if metadata.st_uid != user or metadata.st_mode & 0o077:
                raise HelperError("credential_storage_link_rejected")
            descriptor = os.open(reference + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=directory)
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != user
                    or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
                raise HelperError("credential_storage_link_rejected")
            deadline = time.monotonic() + 15
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise HelperError("os_credential_storage_busy") from None
                    time.sleep(0.05)
            yield
        finally:
            try:
                if descriptor is not None:
                    try:
                        if acquired:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)
            finally:
                os.close(directory)

    @contextmanager
    def _windows_lock(self, reference):
        import msvcrt
        self._directory()
        lock = self.directory / (reference + ".lock")
        if lock.exists() and (lock.is_symlink() or getattr(lock.lstat(), "st_file_attributes", 0) & 0x400):
            raise HelperError("credential_storage_link_rejected")
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOINHERIT, 0o600)
        acquired = False
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or lock.is_symlink()
                    or getattr(metadata, "st_file_attributes", 0) & 0x400
                    or getattr(lock.lstat(), "st_file_attributes", 0) & 0x400):
                raise HelperError("credential_storage_link_rejected")
            deadline = time.monotonic() + 15
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise HelperError("os_credential_storage_busy") from None
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            os.close(descriptor)

    def save(self, origin, connection_id, record, *, replace=False):
        reference = self.reference(origin, connection_id)
        raw = canonical(record).encode("utf-8")
        if len(raw) > MAX_RECORD_BYTES:
            raise HelperError("invalid_credential_record")
        if self.platform == "macos":
            with self._macos_lock(reference):
                existing = self.load(origin, connection_id)
                if not replace and existing is not None:
                    raise HelperError("connection_already_saved")
                record = self._preserve_record(existing, record)
                raw = canonical(record).encode("utf-8")
                self.keychain.write(reference, raw, replace=existing is not None)
                if self.load(origin, connection_id) != record:
                    raise HelperError("os_credential_storage_failed")
            return
        if self.platform == "linux":
            # Secret Service's store operation itself is not create-if-absent.
            # Serialize local writers without putting credentials in lockfiles.
            with self._linux_lock(reference):
                existing = self.load(origin, connection_id)
                if not replace and existing is not None:
                    raise HelperError("connection_already_saved")
                record = self._preserve_record(existing, record)
                raw = canonical(record).encode("utf-8")
                self._secret_service("store", reference, raw)
                if self.load(origin, connection_id) != record:
                    raise HelperError("os_credential_storage_failed")
            return
        with self._windows_lock(reference):
            self._save_windows(origin, connection_id, reference, record, raw, replace=replace)

    def _save_windows(self, origin, connection_id, reference, record, raw, *, replace):
        target = self.directory / (reference + ".dpapi")
        if not replace and target.exists():
            raise HelperError("connection_already_saved")
        if target.exists() and (target.is_symlink() or getattr(target.lstat(), "st_file_attributes", 0) & 0x400):
            raise HelperError("credential_storage_link_rejected")
        if replace:
            record = self._preserve_record(self.load(origin, connection_id), record)
            raw = canonical(record).encode("utf-8")
        encrypted = _dpapi(raw, protect=True)
        temporary = self.directory / (reference + "." + secrets.token_hex(8) + ".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, target)
            else:
                # Windows rename refuses an existing target, preserving an
                # earlier process's token if two agents connect concurrently.
                os.rename(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        if self.load(origin, connection_id) != record:
            raise HelperError("os_credential_storage_failed")

    def load(self, origin, connection_id):
        reference = self.reference(origin, connection_id)
        if self.platform == "macos":
            raw = self.keychain.lookup(reference)
            if raw is None:
                return None
        elif self.platform == "linux":
            raw = self._secret_service("lookup", reference)
            if raw is None:
                return None
        else:
            target = self.directory / (reference + ".dpapi")
            if not target.exists():
                return None
            self._directory()
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or target.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise HelperError("credential_storage_link_rejected")
            with target.open("rb") as stream:
                encrypted = stream.read(MAX_RECORD_BYTES + 1)
            if len(encrypted) > MAX_RECORD_BYTES:
                raise HelperError("invalid_credential_record")
            raw = _dpapi(encrypted, protect=False)
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise HelperError("invalid_credential_record") from None


class AgentClient:
    def __init__(self, origin, *, store=None, http=None, http_factory=JsonHTTP):
        self.origin = checked_origin(origin)
        self.store = store if store is not None else SecureStore()
        self.http_factory = http_factory
        self.http = http if http is not None else http_factory(self.origin)

    def migrate_origin(self, connection_id, target_origin):
        if self.origin not in LEGACY_ORIGINS or checked_origin(target_origin) != CANONICAL_ORIGIN:
            raise HelperError("origin_migration_route_not_allowed")
        SecureStore.reference(self.origin, connection_id)
        state = self.store.load(self.origin, connection_id)
        alias = _route_alias(state, self.origin, connection_id)
        existing = self.store.load(CANONICAL_ORIGIN, connection_id)
        if existing is not None and existing != alias:
            raise HelperError("origin_migration_target_conflict")
        # Same PAT, direct exact HTTPS target, no grant exchange or redirect.
        identity = self.http_factory(CANONICAL_ORIGIN).request("/api/auth/me", token=state["api_key"])
        if (not isinstance(identity, dict) or identity.get("username") != state["expected"]["owner"] or identity.get("machine") is not True
                or identity.get("actor_id") != "key:" + state["key_id"]
                or identity.get("scopes") != CREATOR_V1_SCOPES or identity.get("all_projects") is not True
                or identity.get("project_ids") != []):
            raise HelperError("origin_migration_identity_mismatch")
        if _route_alias(self.store.load(self.origin, connection_id), self.origin, connection_id) != alias:
            raise HelperError("origin_migration_source_changed")
        if existing is None:
            try:
                self.store.save(CANONICAL_ORIGIN, connection_id, alias)
            except HelperError as error:
                if str(error) != "connection_already_saved" or self.store.load(CANONICAL_ORIGIN, connection_id) != alias:
                    raise
        if self.store.load(CANONICAL_ORIGIN, connection_id) != alias:
            raise HelperError("origin_migration_binding_mismatch")
        return {"status": "origin_migrated", "connection_id": connection_id, "owner": state["expected"]["owner"],
            "origin": CANONICAL_ORIGIN, "source_origin": self.origin,
            "key_id": state["key_id"], "expires_at": state["expires_at"], "online_verified": True}

    def connect(self, *, code, connection_id, owner, tenant, fingerprint,
                profile_id="creator-full", profile_version=1):
        if not isinstance(code, str) or not CODE.fullmatch(code) or not HEX.fullmatch(fingerprint or ""):
            raise HelperError("invalid_connection_instruction")
        if not ID.fullmatch(connection_id or "") or not isinstance(owner, str) or not isinstance(tenant, str):
            raise HelperError("invalid_connection_instruction")
        existing = self.store.load(self.origin, connection_id)
        expected = {"origin": self.origin, "connection_id": connection_id, "owner": owner, "tenant": tenant,
                    "profile_id": profile_id, "profile_version": profile_version, "fingerprint": fingerprint}
        if existing:
            if existing.get("expected") != expected:
                raise HelperError("saved_authorization_mismatch")
            return self.resume(connection_id)
        state = {"protocol": PROTOCOL, "status": "pending", "expected": expected,
            "code": code, "api_key": "sxp_" + secrets.token_urlsafe(32), "verifier": secrets.token_urlsafe(32)}
        self.store.save(self.origin, connection_id, state)
        return self._exchange(state, recovery=False)

    def _exchange(self, state, *, recovery):
        expected = state["expected"]
        body = {"code": state["code"], "client_challenge": digest(state["verifier"]),
            "token_hash": digest(state["api_key"]), "key_prefix": state["api_key"][:12],
            "expected_authorization_fingerprint": expected["fingerprint"]}
        if recovery:
            body["recovery_verifier"] = state["verifier"]
        try:
            result = self.http.request("/v1/agent-connect/exchange", method="POST", body=body)
        except HelperError:
            state["status"] = "unknown"
            self.store.save(self.origin, expected["connection_id"], state, replace=True)
            raise
        connection = result.get("connection", {})
        authorization = connection.get("authorization", {})
        key = result.get("key", {})
        if (not isinstance(authorization, dict) or digest(canonical(authorization)) != expected["fingerprint"]
                or connection.get("authorization_fingerprint") != expected["fingerprint"]
                or any(authorization.get(k) != v for k, v in expected.items() if k != "fingerprint")
                or connection.get("id") != expected["connection_id"] or key.get("id") != connection.get("key_id")
                or key.get("prefix") != state["api_key"][:12] or "api_key" in result
                or key.get("scopes") != authorization.get("scopes")
                or key.get("expires_at") != authorization.get("key_expires_at")
                or connection.get("status") != "connected"):
            state["status"] = "unknown"
            self.store.save(self.origin, expected["connection_id"], state, replace=True)
            raise HelperError("exchange_authorization_mismatch")
        state.update(status="connected", key_id=key["id"], expires_at=key["expires_at"])
        # Keep encrypted recovery material until local activation is safely
        # stored.  No raw key reaches the public result or stdout.
        self.store.save(self.origin, expected["connection_id"], state, replace=True)
        return {"status": "connected", "connection_id": connection["id"], "owner": authorization["owner"],
            "origin": self.origin, "profile_id": authorization["profile_id"], "profile_version": authorization["profile_version"],
            "key_id": key["id"], "expires_at": key["expires_at"], "recovered": bool(result.get("recovered"))}

    def resume(self, connection_id):
        effective_origin, active = resolve_connection(self.store, self.origin, connection_id)
        if effective_origin != self.origin or active and active.get("expected", {}).get("origin") != self.origin:
            if not active or active.get("status") != "connected" or active.get("protocol") != PROTOCOL:
                raise HelperError("saved_connection_not_active")
            return {"status": "connected_locally", "connection_id": connection_id, "owner": active["expected"]["owner"],
                "origin": effective_origin, "key_id": active["key_id"], "online_verified": False}
        state = self.store.load(self.origin, connection_id)
        if not state or state.get("protocol") != PROTOCOL or state.get("expected", {}).get("origin") != self.origin:
            raise HelperError("saved_connection_unavailable")
        if state.get("status") == "connected":
            # This is historical local activation, not a live health check.
            return {"status": "connected_locally", "connection_id": connection_id, "owner": state["expected"]["owner"],
                "origin": self.origin, "key_id": state["key_id"], "online_verified": False}
        try:
            return self._exchange(state, recovery=True)
        except HelperError as error:
            if str(error) != "connection_not_exchanged":
                raise
            # Definite unclaimed state: retry the exact saved original material.
            return self._exchange(state, recovery=False)

    def request(self, connection_id, path, *, method="GET", body=None, idempotency_key=None):
        effective_origin, state = resolve_connection(self.store, self.origin, connection_id)
        if not state or state.get("status") != "connected" or state.get("protocol") != PROTOCOL:
            raise HelperError("saved_connection_not_active")
        http = self.http if effective_origin == self.origin else self.http_factory(effective_origin)
        return http.request(path, method=method, body=body, token=state["api_key"], idempotency_key=idempotency_key)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Connect Inkseq without printing or exporting API credentials")
    parser.add_argument("command", choices=("connect", "resume", "whoami", "request", "migrate-origin"))
    parser.add_argument("--server", required=True, help="Exact HTTPS origin; loopback HTTP only for local preview")
    parser.add_argument("--connection", required=True, help="Public connection ID from the website")
    parser.add_argument("--to-server", help="Explicit canonical Inkseq origin for verified legacy-origin routing")
    parser.add_argument("--account", help="Expected account from the website")
    parser.add_argument("--tenant", default="sixnine")
    parser.add_argument("--fingerprint", help="Public authorization fingerprint from the website")
    parser.add_argument("--profile", default="creator-full")
    parser.add_argument("--profile-version", type=int, default=1)
    parser.add_argument("--code-stdin", action="store_true", help="Read the one-time code from stdin, never a CLI argument")
    parser.add_argument("--method", choices=("GET", "POST", "PATCH", "PUT", "DELETE"), default="GET")
    parser.add_argument("--path", help="Same-origin API path, never an absolute URL")
    parser.add_argument("--input", help="JSON business request file; '-' reads stdin. Do not include credentials")
    parser.add_argument("--idempotency-key", help="Stable public operation identifier; keep it after an uncertain response")
    args = parser.parse_args(argv)
    try:
        client = AgentClient(args.server)
        if args.command == "connect":
            if not args.account or not args.fingerprint:
                raise HelperError("connection_instruction_required")
            code = sys.stdin.read(256).strip() if args.code_stdin else getpass.getpass("One-time connection code: ")
            result = client.connect(code=code, connection_id=args.connection, owner=args.account, tenant=args.tenant,
                fingerprint=args.fingerprint, profile_id=args.profile, profile_version=args.profile_version)
        elif args.command == "migrate-origin":
            if not args.to_server:
                raise HelperError("origin_migration_target_required")
            result = client.migrate_origin(args.connection, args.to_server)
        elif args.command == "resume":
            result = client.resume(args.connection)
        elif args.command == "whoami":
            value = client.request(args.connection, "/api/auth/me")
            result = {k: value.get(k) for k in ("username", "machine", "authentication", "scopes", "all_projects")}
        else:
            if not args.path:
                raise HelperError("api_path_required")
            value = None
            if args.input:
                if args.input == "-":
                    raw = sys.stdin.buffer.read(MAX_RESPONSE_BYTES + 1)
                else:
                    with Path(args.input).open("rb") as source:
                        raw = source.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise HelperError("business_request_too_large")
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise HelperError("invalid_business_json") from None
                if not isinstance(value, dict):
                    raise HelperError("invalid_business_json")
            result = client.request(args.connection, args.path, method=args.method, body=value,
                idempotency_key=args.idempotency_key)
        print(canonical(result))
        return 0
    except (HelperError, OSError):
        error = sys.exc_info()[1]
        print(canonical({"status": "not_connected", "code": str(error) if isinstance(error, HelperError) else "local_storage_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
