"""Mocked Security/CoreFoundation semantics; no macOS, network or real keys."""
from contextlib import contextmanager
import ctypes
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path, PurePosixPath
import stat
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HELPER_PATH = Path(__file__).resolve().parent / "skills" / "inkseq" / "scripts" / "connect.py"
spec = spec_from_file_location("sixnine_connect_macos_test", HELPER_PATH)
helper = module_from_spec(spec)
spec.loader.exec_module(helper)


class Function:
    def __init__(self, action):
        self.action = action
    def __call__(self, *args):
        return self.action(*args)


class Framework:
    pass


class NativeFixture:
    """CF ownership and native query semantics, including separate keychains."""
    def __init__(self):
        self.cf, self.security = Framework(), Framework()
        self.objects, self.next_ref, self.constants, self.records = {}, 10, {}, {}
        self.interaction, self.interactions, self.native_calls = True, [], []
        self.failures, self.status, self.read_pointer_calls = {}, 7, 0
        self.lookup_kind, self.lookup_length, self.corrupt_reads = "data", None, False
        self.default = self.allocate("keychain", "default")
        self.other = self.allocate("keychain", "other")
        for name in ("kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
                "kSecUseAuthenticationUI", "kSecUseAuthenticationUIFail", "kSecUseKeychain",
                "kSecMatchSearchList", "kSecMatchLimit", "kSecMatchLimitOne", "kSecReturnData",
                "kSecValueData", "kCFBooleanTrue"):
            self.constants[name] = self.allocate("constant", name)
        actions = {
            "CFStringCreateWithBytes": lambda _, raw, length, encoding, external: self.allocate("string", ctypes.string_at(raw, length).decode("utf-8")),
            "CFDataCreate": lambda _, raw, length: self.allocate("data", ctypes.string_at(raw, length)),
            "CFDictionaryCreate": self.dictionary, "CFArrayCreate": self.array,
            "CFRelease": self.release, "CFRetain": self.retain,
            "CFGetTypeID": lambda ref: 1 if self.objects[ref]["kind"] == "data" else 2,
            "CFDataGetTypeID": lambda: 1, "CFDataGetLength": self.length, "CFDataGetBytePtr": self.pointer,
        }
        for name, action in actions.items():
            setattr(self.cf, name, Function(action))
        actions = {
            "SecKeychainCopyDefault": self.copy_default, "SecKeychainGetStatus": self.get_status,
            "SecKeychainGetUserInteractionAllowed": self.get_interaction,
            "SecKeychainSetUserInteractionAllowed": self.set_interaction,
            "SecItemCopyMatching": self.lookup, "SecItemAdd": self.add, "SecItemUpdate": self.update,
        }
        for name, action in actions.items():
            setattr(self.security, name, Function(action))

    def allocate(self, kind, value, children=()):
        ref, self.next_ref = self.next_ref, self.next_ref + 1
        self.objects[ref] = {"kind": kind, "value": value, "count": 1, "children": tuple(children)}
        for child in children:
            self.retain(child)
        return ref
    def retain(self, ref):
        self.objects[ref]["count"] += 1
        return ref
    def release(self, ref):
        obj = self.objects[ref]
        obj["count"] -= 1
        if obj["count"] < 0:
            raise AssertionError("native reference released twice")
        if obj["count"] == 0:
            for child in obj["children"]:
                self.release(child)
    def dictionary(self, _, keys, values, length, key_callbacks, value_callbacks):
        pairs = tuple((keys[i], values[i]) for i in range(length))
        return self.allocate("dictionary", pairs, [ref for pair in pairs for ref in pair])
    def array(self, _, refs, length, callbacks):
        values = tuple(refs[i] for i in range(length))
        return self.allocate("array", values, values)
    def length(self, ref):
        return self.lookup_length if self.lookup_length is not None else len(self.objects[ref]["value"])
    def pointer(self, ref):
        self.read_pointer_calls += 1
        obj = self.objects[ref]
        obj["buffer"] = ctypes.create_string_buffer(obj["value"])
        return ctypes.addressof(obj["buffer"])
    def copy_default(self, result):
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = self.retain(self.default)
        return self.failures.get("copy_default", 0)
    def get_status(self, ref, result):
        ctypes.cast(result, ctypes.POINTER(ctypes.c_uint32))[0] = self.status
        return self.failures.get("get_status", 0)
    def get_interaction(self, result):
        ctypes.cast(result, ctypes.POINTER(ctypes.c_ubyte))[0] = self.interaction
        return self.failures.get("get_interaction", 0)
    def set_interaction(self, allowed):
        self.interactions.append(bool(allowed))
        code = self.failures.get("restore_interaction" if allowed else "disable_interaction", 0)
        if not code:
            self.interaction = bool(allowed)
        return code
    def query(self, query, *, add=False):
        pairs = {self.objects[key]["value"]: value for key, value in self.objects[query]["value"]}
        if self.interaction or self.objects[pairs["kSecUseAuthenticationUI"]]["value"] != "kSecUseAuthenticationUIFail":
            raise AssertionError("credential operation allowed native UI")
        if self.objects[pairs["kSecClass"]]["value"] != "kSecClassGenericPassword":
            raise AssertionError("unexpected item class")
        prohibited = {"kSecAttrAccess", "kSecAttrAccessControl", "kSecAttrAccessible", "kSecAttrAccessGroup",
            "kSecAttrSynchronizable", "kSecUseDataProtectionKeychain"}
        if prohibited.intersection(pairs):
            raise AssertionError("credential access policy was broadened")
        if add:
            keychain = pairs["kSecUseKeychain"]
            if "kSecMatchSearchList" in pairs:
                raise AssertionError("ambiguous add keychain")
        else:
            selected = self.objects[pairs["kSecMatchSearchList"]]["value"]
            if len(selected) != 1 or "kSecUseKeychain" in pairs:
                raise AssertionError("lookup/update was not bound to one keychain")
            keychain = selected[0]
        service = self.objects[pairs["kSecAttrService"]]["value"]
        account = self.objects[pairs["kSecAttrAccount"]]["value"]
        self.native_calls.append({"operation": "add" if add else "query", "keychain": keychain,
            "service": service, "account": account, "attributes": set(pairs)})
        return (keychain, service, account), pairs
    def lookup(self, query, result):
        key, _ = self.query(query)
        if self.failures.get("lookup"):
            return self.failures["lookup"]
        if key not in self.records:
            return -25300
        raw = b"{}" if self.corrupt_reads else self.records[key]
        ref = self.allocate(self.lookup_kind, raw)
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = ref
        return 0
    def add(self, query, result):
        key, pairs = self.query(query, add=True)
        if self.failures.get("add"):
            return self.failures["add"]
        if key in self.records:
            return -25299
        self.records[key] = self.objects[pairs["kSecValueData"]]["value"]
        return 0
    def update(self, query, attributes):
        key, _ = self.query(query)
        pairs = {self.objects[k]["value"]: v for k, v in self.objects[attributes]["value"]}
        if set(pairs) != {"kSecValueData"}:
            raise AssertionError("update changed credential identity or access policy")
        if self.failures.get("update"):
            return self.failures["update"]
        if key not in self.records:
            return -25300
        self.records[key] = self.objects[pairs["kSecValueData"]]["value"]
        return 0
    def library(self, path):
        if path == "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation":
            return self.cf
        if path == "/System/Library/Frameworks/Security.framework/Security":
            return self.security
        raise AssertionError("non-system framework loaded")
    def adapter(self, **kwargs):
        with patch.object(helper.sys, "platform", "darwin"), patch.object(helper.os, "getuid", return_value=501, create=True), \
                patch.object(helper.ctypes, "CDLL", side_effect=self.library), \
                patch.object(helper._MacOSKeychain, "_constant", side_effect=lambda _, name: self.constants[name]), \
                patch.object(helper._MacOSKeychain, "_callbacks", return_value=1), \
                patch.object(helper.subprocess, "run", side_effect=AssertionError("no credential subprocess")):
            return helper._MacOSKeychain(**kwargs)
    def live_temporary_references(self):
        retained = {self.default, self.other, *self.constants.values()}
        return [ref for ref, obj in self.objects.items() if ref not in retained and obj["count"]]


class MacOSKeychainTests(unittest.TestCase):
    origin = "https://sixnine.example"
    connection_id = "connection-" + "a" * 32
    def setUp(self):
        self.native = NativeFixture()
        self.adapter = self.native.adapter()
        self.addCleanup(self.adapter.close)
        self.reference = helper.SecureStore.reference(self.origin, self.connection_id)
    def store(self):
        store = helper.SecureStore.__new__(helper.SecureStore)
        store.platform, store.keychain = "macos", self.adapter
        lock = threading.RLock()
        store.lock_events = []
        @contextmanager
        def mocked_lock(reference):
            with lock:
                store.lock_events.append(("acquired", reference))
                try:
                    yield
                finally:
                    store.lock_events.append(("released", reference))
        store._macos_lock = mocked_lock
        return store
    def record(self, status="pending"):
        return {"protocol": helper.PROTOCOL, "status": status, "expected": {"origin": self.origin,
            "owner": "synthetic-account"}, "api_key": "synthetic-macos-unit-key", "code": "synthetic-code"}

    def test_native_roundtrip_bound_to_default_without_subprocess_or_plaintext_file(self):
        store = self.store()
        record = self.record()
        self.native.records[(self.native.other, "sixnine-agent", self.reference)] = b'{"wrong":"keychain"}'
        with patch.object(helper.subprocess, "run", side_effect=AssertionError("no subprocess")), \
                patch.object(helper.os, "open", side_effect=AssertionError("record must not become a file")):
            self.assertIsNone(store.load(self.origin, self.connection_id))
            store.save(self.origin, self.connection_id, record)
            self.assertEqual(store.load(self.origin, self.connection_id), record)
        self.assertTrue(all(call["keychain"] == self.native.default for call in self.native.native_calls))
        self.assertTrue(all(call["service"] == "sixnine-agent" and call["account"] == self.reference for call in self.native.native_calls))
        self.assertEqual(store.lock_events, [("acquired", self.reference), ("released", self.reference)])
        self.assertEqual(self.native.live_temporary_references(), [])
        self.assertTrue(self.native.interaction)

    def test_native_update_payload_only_and_expected_key_immutable(self):
        store = self.store()
        store.save(self.origin, self.connection_id, self.record())
        for field in ("api_key", "expected"):
            changed = self.record()
            changed[field] = "changed"
            with self.assertRaisesRegex(helper.HelperError, "^saved_authorization_mismatch$"):
                store.save(self.origin, self.connection_id, changed, replace=True)
        store.save(self.origin, self.connection_id, self.record("connected"), replace=True)
        store.save(self.origin, self.connection_id, self.record("unknown"), replace=True)
        self.assertEqual(store.load(self.origin, self.connection_id)["status"], "connected")
        with self.assertRaisesRegex(helper.HelperError, "^connection_already_saved$"):
            store.save(self.origin, self.connection_id, self.record())
        self.assertEqual(self.native.live_temporary_references(), [])

    def test_atomic_add_rejects_duplicate_even_if_external_writer_ignored_lock(self):
        self.adapter.write(self.reference, b'{"synthetic":"first"}')
        with self.assertRaisesRegex(helper.HelperError, "^connection_already_saved$"):
            self.adapter.write(self.reference, b'{"synthetic":"second"}')
        self.assertEqual(self.adapter.lookup(self.reference), b'{"synthetic":"first"}')

    def test_no_keychain_created_or_default_changed_and_borrowed_reference_retained(self):
        borrowed = self.native.adapter(keychain_ref=self.native.other)
        self.assertEqual(self.native.objects[self.native.other]["count"], 2)
        try:
            borrowed.write(self.reference, b"{}")
            self.assertIsNone(self.adapter.lookup(self.reference))
            self.assertEqual(borrowed.lookup(self.reference), b"{}")
        finally:
            borrowed.close()
        self.assertEqual(self.native.objects[self.native.other]["count"], 1)
        self.assertEqual(self.native.default, self.adapter.keychain_ref)
        borrowed.close()

    def test_framework_unavailable_fails_static_and_root_is_rejected(self):
        with patch.object(helper.sys, "platform", "darwin"), patch.object(helper.os, "getuid", return_value=501, create=True), \
                patch.object(helper.ctypes, "CDLL", side_effect=OSError("private exception detail")):
            with self.assertRaisesRegex(helper.HelperError, "^os_credential_storage_unavailable$"):
                helper._MacOSKeychain()
        with patch.object(helper.sys, "platform", "darwin"), patch.object(helper.os, "getuid", return_value=0, create=True), \
                patch.object(helper.ctypes, "CDLL", side_effect=AssertionError("root must not open frameworks")):
            with self.assertRaisesRegex(helper.HelperError, "^os_credential_storage_unavailable$"):
                helper._MacOSKeychain()

    def test_failed_default_copy_releases_transferred_reference(self):
        before = self.native.objects[self.native.default]["count"]
        self.native.failures["copy_default"] = -25291
        with self.assertRaisesRegex(helper.HelperError, "^os_credential_storage_unavailable$"):
            self.native.adapter()
        self.assertEqual(self.native.objects[self.native.default]["count"], before)
        self.assertTrue(self.native.interaction)

    def test_locked_unreadable_or_unwritable_storage_fails_before_exchange(self):
        for state in (0, 1, 2, 6, 3):
            with self.subTest(state=state):
                self.native.status = state
                store = self.store()
                http = unittest.mock.Mock()
                client = helper.AgentClient(self.origin, store=store, http=http)
                with self.assertRaisesRegex(helper.HelperError, "^os_credential_storage_failed$"):
                    client.connect(code="sxc_" + "a" * 43, connection_id=self.connection_id,
                        owner="synthetic-account", tenant="sixnine", fingerprint="f" * 64)
                http.request.assert_not_called()
                self.assertTrue(self.native.interaction)

    def test_native_denial_failure_and_readback_failure_prevent_exchange(self):
        for operation in ("lookup", "add", "disable_interaction", "get_interaction"):
            with self.subTest(operation=operation):
                self.native.failures = {operation: -25308}
                store, http = self.store(), unittest.mock.Mock()
                client = helper.AgentClient(self.origin, store=store, http=http)
                with self.assertRaisesRegex(helper.HelperError, "^os_credential_storage_failed$"):
                    client.connect(code="sxc_" + "a" * 43, connection_id=self.connection_id,
                        owner="synthetic-account", tenant="sixnine", fingerprint="f" * 64)
                http.request.assert_not_called()
                self.assertTrue(self.native.interaction)
        self.native.failures = {}
        self.native.corrupt_reads = True
        store, http = self.store(), unittest.mock.Mock()
        client = helper.AgentClient(self.origin, store=store, http=http)
        with self.assertRaisesRegex(helper.HelperError, "^os_credential_storage_failed$"):
            client.connect(code="sxc_" + "a" * 43, connection_id=self.connection_id,
                owner="synthetic-account", tenant="sixnine", fingerprint="f" * 64)
        http.request.assert_not_called()

    def test_ui_flag_restored_on_native_denial_including_previous_false(self):
        self.native.failures["lookup"] = -25293
        for previous in (True, False):
            self.native.interaction = previous
            with self.assertRaisesRegex(helper.HelperError, "^os_credential_storage_failed$"):
                self.adapter.lookup(self.reference)
            self.assertEqual(self.native.interaction, previous)
            self.assertEqual(self.native.interactions[-2:], [False, previous])
        self.assertEqual(self.native.live_temporary_references(), [])

    def test_restore_failure_is_static_and_prevents_exchange(self):
        self.native.failures["restore_interaction"] = -25293
        store, http = self.store(), unittest.mock.Mock()
        client = helper.AgentClient(self.origin, store=store, http=http)
        with self.assertRaisesRegex(helper.HelperError, "^os_credential_storage_failed$"):
            client.connect(code="sxc_" + "a" * 43, connection_id=self.connection_id,
                owner="synthetic-account", tenant="sixnine", fingerprint="f" * 64)
        http.request.assert_not_called()
        self.assertEqual(self.native.live_temporary_references(), [])

    def test_invalid_type_size_and_utf8_native_records_are_bounded_and_released(self):
        self.adapter.write(self.reference, b"{}")
        for kind, length, raw in (("string", None, b"{}"), ("data", helper.MAX_RECORD_BYTES + 1, b"{}"),
                ("data", -1, b"{}"), ("data", None, b"\xff")):
            with self.subTest(kind=kind, length=length):
                self.native.lookup_kind, self.native.lookup_length = kind, length
                self.native.records[(self.native.default, "sixnine-agent", self.reference)] = raw
                reads = self.native.read_pointer_calls
                with self.assertRaisesRegex(helper.HelperError, "^invalid_credential_record$"):
                    self.adapter.lookup(self.reference)
                if kind != "data" or length is not None:
                    self.assertEqual(self.native.read_pointer_calls, reads)
                self.assertEqual(self.native.live_temporary_references(), [])
        for raw in (b"\xff", b"a" * (helper.MAX_RECORD_BYTES + 1)):
            with self.assertRaisesRegex(helper.HelperError, "^invalid_credential_record$"):
                self.adapter.write(self.reference, raw)
        self.assertEqual(self.native.live_temporary_references(), [])

    def test_invalid_json_object_rejected_by_secure_store(self):
        store = self.store()
        for raw in (b"[]", b"null", b"not-json", b'{"bad":'):
            self.native.records[(self.native.default, "sixnine-agent", self.reference)] = raw
            with self.assertRaisesRegex(helper.HelperError, "^invalid_credential_record$"):
                store.load(self.origin, self.connection_id)

    def test_lost_exchange_recovers_same_saved_pat_without_key_rotation(self):
        store = self.store()
        expected = {"origin": self.origin, "connection_id": self.connection_id, "owner": "synthetic-account",
            "tenant": "sixnine", "profile_id": "creator-full", "profile_version": 1}
        authorization = {**expected, "scopes": ["projects:read"], "key_expires_at": "synthetic-expiry"}
        fingerprint = helper.digest(helper.canonical(authorization))
        calls, pats = [], []
        class FakeHTTP:
            def request(_, path, *, method, body):
                saved = store.load(self.origin, self.connection_id)
                pats.append(saved["api_key"])
                self.assertEqual(body["token_hash"], helper.digest(saved["api_key"]))
                self.assertNotIn("api_key", body)
                calls.append(body)
                if len(calls) == 1:
                    raise helper.HelperError("network_result_unknown")
                self.assertEqual(body["recovery_verifier"], saved["verifier"])
                return {"connection": {"id": self.connection_id, "status": "connected", "key_id": "synthetic-key-id",
                    "authorization": authorization, "authorization_fingerprint": fingerprint},
                    "key": {"id": "synthetic-key-id", "prefix": saved["api_key"][:12],
                        "scopes": authorization["scopes"], "expires_at": authorization["key_expires_at"]}, "recovered": True}
        client = helper.AgentClient(self.origin, store=store, http=FakeHTTP())
        with self.assertRaisesRegex(helper.HelperError, "^network_result_unknown$"):
            client.connect(code="sxc_" + "a" * 43, connection_id=self.connection_id,
                owner="synthetic-account", tenant="sixnine", fingerprint=fingerprint)
        before = store.load(self.origin, self.connection_id)
        self.assertEqual(before["status"], "unknown")
        result = client.resume(self.connection_id)
        self.assertTrue(result["recovered"])
        self.assertEqual(pats, [before["api_key"], before["api_key"]])
        self.assertNotIn(before["api_key"], json.dumps(result))
        self.assertEqual(client.resume(self.connection_id)["status"], "connected_locally")
        self.assertEqual(len(calls), 2)


class MacOSLockTests(unittest.TestCase):
    """Validate POSIX descriptor/ownership rules without a Windows fcntl shim."""
    def setUp(self):
        self.metadata = {
            "/": self.info(0, 0o755), "/Users": self.info(0, 0o755),
            "/Users/synthetic": self.info(501, 0o700),
        }
        self.links, self.opens, self.closes, self.flocks = set(), [], [], []
        self.file_info = self.info(501, 0o600, directory=False)
        self.block, self.fail_open = False, False
        self.fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=self.flock)
        self.fake_os = SimpleNamespace(getuid=lambda: 501, open=self.open, fstat=self.fstat, close=self.closes.append,
            O_RDONLY=0, O_DIRECTORY=16, O_NOFOLLOW=32, O_CLOEXEC=64, O_CREAT=128, O_RDWR=256)
        fixture = self
        class FakePath:
            def __init__(self, value):
                self.value = PurePosixPath(str(value))
            def __str__(self):
                return str(self.value)
            def __truediv__(self, value):
                return FakePath(self.value / value)
            def __eq__(self, other):
                return isinstance(other, FakePath) and self.value == other.value
            @property
            def parents(self):
                return [FakePath(p) for p in self.value.parents]
            def relative_to(self, other):
                return self.value.relative_to(other.value)
            def lstat(self):
                return fixture.metadata[str(self)]
            def is_symlink(self):
                return str(self) in fixture.links
            def mkdir(self, *, mode, exist_ok):
                fixture.metadata.setdefault(str(self), fixture.info(501, mode))
        self.store = helper.SecureStore.__new__(helper.SecureStore)
        self.store.platform = "macos"
        self.store.lock_home = FakePath("/Users/synthetic")
        self.store.lock_directory = self.store.lock_home / "Library" / "Application Support" / "Sixnine" / "agent-locks"
        self.reference = "f" * 64
    @staticmethod
    def info(uid, mode, *, directory=True, links=1):
        return SimpleNamespace(st_uid=uid, st_mode=(stat.S_IFDIR if directory else stat.S_IFREG) | mode, st_nlink=links)
    def open(self, path, flags, mode=None, *, dir_fd=None):
        self.opens.append((str(path), flags, mode, dir_fd))
        if dir_fd is None:
            return 100
        if self.fail_open:
            raise OSError("synthetic private OS error")
        return 101
    def fstat(self, descriptor):
        return self.metadata[str(self.store.lock_directory)] if descriptor == 100 else self.file_info
    def flock(self, descriptor, flags):
        self.flocks.append((descriptor, flags))
        if self.block and flags != self.fcntl.LOCK_UN:
            raise BlockingIOError
    @contextmanager
    def fake_posix(self):
        with patch.object(helper, "os", self.fake_os), patch.dict(helper.sys.modules, {"fcntl": self.fcntl}):
            yield
    def test_lock_is_stable_user_owned_reference_only_and_closes_descriptors(self):
        with self.fake_posix():
            with self.store._macos_lock(self.reference):
                self.assertEqual(self.closes, [])
        self.assertEqual(self.opens, [(str(self.store.lock_directory), 16 | 32 | 64, None, None),
            (self.reference + ".lock", 128 | 256 | 32 | 64, 0o600, 100)])
        self.assertEqual(self.flocks, [(101, 1 | 2), (101, 8)])
        self.assertEqual(self.closes, [101, 100])
    def test_symlink_or_writable_ancestor_rejected_without_open(self):
        for reason in ("symlink", "writable", "wrong_owner"):
            with self.subTest(reason=reason):
                self.links.clear()
                self.metadata["/Users/synthetic"] = self.info(501, 0o700)
                if reason == "symlink":
                    self.links.add("/Users/synthetic")
                elif reason == "writable":
                    self.metadata["/Users/synthetic"] = self.info(501, 0o770)
                else:
                    self.metadata["/Users/synthetic"] = self.info(502, 0o700)
                with self.fake_posix(), self.assertRaisesRegex(helper.HelperError, "^credential_storage_link_rejected$"):
                    with self.store._macos_lock(self.reference):
                        self.fail("unsafe lock acquired")
                self.assertEqual(self.opens, [])
    def test_nonprivate_lock_directory_rejected_without_chmod(self):
        self.metadata[str(self.store.lock_directory)] = self.info(501, 0o755)
        with self.fake_posix(), self.assertRaisesRegex(helper.HelperError, "^credential_storage_link_rejected$"):
            with self.store._macos_lock(self.reference):
                self.fail("unsafe lock acquired")
        self.assertEqual(self.metadata[str(self.store.lock_directory)].st_mode & 0o777, 0o755)
        self.assertEqual(self.opens, [])
    def test_wrong_owner_nonregular_hardlinked_or_nonprivate_lock_file_rejected(self):
        for metadata in (self.info(502, 0o600, directory=False), self.info(501, 0o600),
                self.info(501, 0o600, directory=False, links=2), self.info(501, 0o640, directory=False)):
            with self.subTest(metadata=metadata):
                self.file_info = metadata
                self.closes.clear()
                with self.fake_posix(), self.assertRaisesRegex(helper.HelperError, "^credential_storage_link_rejected$"):
                    with self.store._macos_lock(self.reference):
                        self.fail("unsafe lock acquired")
                self.assertEqual(self.closes, [101, 100])
                self.assertEqual(self.flocks, [])
    def test_lock_timeout_and_lock_open_failure_release_descriptor(self):
        self.block = True
        with self.fake_posix(), patch.object(helper.time, "monotonic", side_effect=[0, 16]), \
                self.assertRaisesRegex(helper.HelperError, "^os_credential_storage_busy$"):
            with self.store._macos_lock(self.reference):
                self.fail("contended lock acquired")
        self.assertEqual(self.closes, [101, 100])
        self.block, self.fail_open = False, True
        self.closes.clear()
        with self.fake_posix(), self.assertRaises(OSError):
            with self.store._macos_lock(self.reference):
                self.fail("failed lock acquired")
        self.assertEqual(self.closes, [100])
    def test_unlock_failure_still_closes_file_and_directory_descriptors(self):
        def failed_unlock(descriptor, flags):
            if flags == self.fcntl.LOCK_UN:
                raise OSError("synthetic unlock failure")
        self.fcntl.flock = failed_unlock
        with self.fake_posix(), self.assertRaises(OSError):
            with self.store._macos_lock(self.reference):
                pass
        self.assertEqual(self.closes, [101, 100])
    def test_constructor_uses_passwd_home_and_does_not_trust_home_or_tmpdir(self):
        fake_pwd = SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_dir=str(HELPER_PATH.parent)))
        with patch.object(helper.sys, "platform", "darwin"), patch.object(helper.os, "getuid", return_value=501, create=True), \
                patch.dict(helper.sys.modules, {"pwd": fake_pwd}), patch.object(helper, "_MacOSKeychain", return_value=object()), \
                patch.dict(helper.os.environ, {"HOME": "untrusted-home", "TMPDIR": "untrusted-tmp"}):
            store = helper.SecureStore()
        self.assertEqual(store.lock_home, HELPER_PATH.parent)
        self.assertEqual(store.lock_directory, HELPER_PATH.parent / "Library" / "Application Support" / "Sixnine" / "agent-locks")


if __name__ == "__main__":
    unittest.main()
