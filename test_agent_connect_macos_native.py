"""Real macOS Keychain checks using isolated synthetic records, never an API.

No default/search-list changes: the fixture owns one temporary Keychain under
the current user's home and injects its retained reference into the same native
adapter shipped in connect.py. Linux/Windows skips are not native evidence.
"""
from importlib.util import module_from_spec, spec_from_file_location
import ctypes
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest


HELPER_PATH = Path(__file__).resolve().parent / "skills/sixnine-yingxu/scripts/connect.py"
spec = spec_from_file_location("sixnine_keychain_native_test", HELPER_PATH)
helper = module_from_spec(spec)
spec.loader.exec_module(helper)


@unittest.skipUnless(sys.platform == "darwin", "requires actual macOS Security.framework")
class NativeKeychainTests(unittest.TestCase):
    def setUp(self):
        import pwd
        self.home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.temporary = tempfile.TemporaryDirectory(prefix="sixnine-keychain-fixture-", dir=self.home)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "isolated.keychain"
        self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ref, status = ctypes.c_void_p, ctypes.c_int32
        signatures = {
            "SecKeychainCreate": ([ctypes.c_char_p, ctypes.c_uint32, ref, ctypes.c_ubyte, ref,
                                   ctypes.POINTER(ref)], status),
            "SecKeychainOpen": ([ctypes.c_char_p, ctypes.POINTER(ref)], status),
            "SecKeychainDelete": ([ref], status),
            "SecKeychainLock": ([ref], status),
            "SecKeychainUnlock": ([ref, ctypes.c_uint32, ref, ctypes.c_ubyte], status),
            "SecKeychainGetUserInteractionAllowed": ([ctypes.POINTER(ctypes.c_ubyte)], status),
            "SecKeychainSetUserInteractionAllowed": ([ctypes.c_ubyte], status),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.security, name)
            function.argtypes, function.restype = args, result
        self.cf.CFRelease.argtypes, self.cf.CFRelease.restype = [ref], None
        self.password = b"synthetic-isolated-keychain-test-only"
        self.ref = ref()
        result = self.security.SecKeychainCreate(os.fsencode(self.path), len(self.password),
            ctypes.create_string_buffer(self.password), False, None, ctypes.byref(self.ref))
        self.assertTrue(result == 0 and bool(self.ref.value), "isolated_keychain_creation_failed")
        self.addCleanup(self._remove_exact_fixture)
        # Prepare only this test-owned fixture; the shipped helper never unlocks.
        self.assertTrue(self.security.SecKeychainUnlock(self.ref, len(self.password),
            ctypes.create_string_buffer(self.password), True) == 0, "isolated_fixture_unlock_failed")
        self.store = helper.SecureStore.__new__(helper.SecureStore)
        self.store.platform = "macos"
        self.store.lock_home = self.home
        self.store.lock_directory = self.root / "locks"
        self.store.keychain = helper._MacOSKeychain(keychain_ref=self.ref.value)
        self.addCleanup(self.store.keychain.close)
        self.origin = "https://native-test.example"
        self.connection_id = "connection-" + secrets.token_hex(16)

    def _remove_exact_fixture(self):
        # Only this fixture's exact CF reference, never the user's real store.
        if self.ref.value:
            try:
                self.security.SecKeychainUnlock(self.ref, len(self.password),
                    ctypes.create_string_buffer(self.password), True)
                result = self.security.SecKeychainDelete(self.ref)
                self.assertTrue(result == 0, "isolated_keychain_removal_failed")
            finally:
                self.cf.CFRelease(self.ref)
                self.ref = ctypes.c_void_p()

    def record(self, *, state="pending"):
        return {"api_key": "synthetic-native-test-key", "verifier": "synthetic-verifier",
            "status": state, "expected": {"origin": self.origin, "connection_id": self.connection_id,
                                         "owner": "synthetic-owner"}, "note": "测试 UTF-8"}

    def ui_allowed(self):
        value = ctypes.c_ubyte()
        self.assertTrue(self.security.SecKeychainGetUserInteractionAllowed(ctypes.byref(value)) == 0,
                        "interaction_state_read_failed")
        return value.value

    def test_native_roundtrip_duplicate_update_and_origin_isolation(self):
        original_flag = self.ui_allowed()
        record = self.record()
        self.assertIsNone(self.store.load(self.origin, self.connection_id))
        self.store.save(self.origin, self.connection_id, record)
        self.assertTrue(self.store.load(self.origin, self.connection_id) == record, "native_roundtrip_failed")
        self.assertIsNone(self.store.load("https://other-native-test.example", self.connection_id))
        with self.assertRaisesRegex(helper.HelperError, "connection_already_saved"):
            self.store.save(self.origin, self.connection_id, record)
        reference = self.store.reference(self.origin, self.connection_id)
        # Direct Add confirms real SecItem duplicate semantics, not just load().
        with self.assertRaisesRegex(helper.HelperError, "connection_already_saved"):
            self.store.keychain.write(reference, helper.canonical(record).encode())
        changed = {**record, "api_key": "different-synthetic-key"}
        with self.assertRaisesRegex(helper.HelperError, "saved_authorization_mismatch"):
            self.store.save(self.origin, self.connection_id, changed, replace=True)
        self.store.save(self.origin, self.connection_id, self.record(state="connected"), replace=True)
        self.store.save(self.origin, self.connection_id, self.record(state="unknown"), replace=True)
        self.assertTrue(self.store.load(self.origin, self.connection_id)["status"] == "connected",
                        "connected_state_regressed")
        self.assertTrue(self.ui_allowed() == original_flag, "interaction_flag_not_restored")
        # The only ordinary files used by the helper are empty synchronization locks.
        self.assertTrue(all(path.name.endswith(".lock") and path.stat().st_size == 0
                            for path in self.store.lock_directory.iterdir()), "credential_written_to_lock_file")

    def test_locked_native_store_stops_before_exchange_and_restores_ui_state(self):
        class NoHTTP:
            calls = 0
            def request(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("unexpected_network_request")
        http = NoHTTP()
        original_flag = self.ui_allowed()
        self.assertTrue(self.security.SecKeychainLock(self.ref) == 0, "fixture_lock_failed")
        client = helper.AgentClient(self.origin, store=self.store, http=http)
        with self.assertRaisesRegex(helper.HelperError, "os_credential_storage_failed"):
            client.connect(code="sxc_" + "x" * 43, connection_id=self.connection_id,
                owner="synthetic-owner", tenant="synthetic", fingerprint="0" * 64)
        self.assertTrue(http.calls == 0, "exchange_attempted_with_locked_store")
        self.assertTrue(self.ui_allowed() == original_flag, "failure_did_not_restore_interaction_flag")

    def test_already_disabled_process_ui_remains_disabled(self):
        original_flag = self.ui_allowed()
        self.assertTrue(self.security.SecKeychainSetUserInteractionAllowed(False) == 0,
                        "fixture_interaction_set_failed")
        try:
            self.store.save(self.origin, self.connection_id, self.record())
            self.assertTrue(self.store.load(self.origin, self.connection_id) == self.record(),
                            "disabled_ui_roundtrip_failed")
            self.assertTrue(self.ui_allowed() == 0, "helper_enabled_previously_disabled_ui")
        finally:
            self.assertTrue(self.security.SecKeychainSetUserInteractionAllowed(original_flag) == 0,
                            "fixture_interaction_restore_failed")

    def test_two_native_processes_cannot_downgrade_saved_connected_state(self):
        self.store.save(self.origin, self.connection_id, self.record())
        child_code = r'''
import ctypes, importlib.util, json, os, sys, time
from pathlib import Path
spec=importlib.util.spec_from_file_location("sixnine_native_race",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=Path(sys.argv[2]);role=sys.argv[6]
s=ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
s.SecKeychainOpen.argtypes=[ctypes.c_char_p,ctypes.POINTER(ctypes.c_void_p)];s.SecKeychainOpen.restype=ctypes.c_int32
ref=ctypes.c_void_p()
if s.SecKeychainOpen(os.fsencode(root/"isolated.keychain"),ctypes.byref(ref))!=0 or not ref.value:
    raise RuntimeError("fixture_open_failed")
store=m.SecureStore.__new__(m.SecureStore);store.platform="macos"
store.lock_home=Path(sys.argv[3]);store.lock_directory=root/"locks"
store.keychain=m._MacOSKeychain(keychain_ref=ref.value)
store.keychain.cf.CFRelease(ref)
try:
    if role=="unknown":
        original=store._preserve_record
        def pause(existing,incoming):
            (root/"stale-read").touch();deadline=time.monotonic()+10
            while not (root/"release-stale").exists():
                if time.monotonic()>deadline:raise RuntimeError("fixture_deadline")
                time.sleep(.02)
            return original(existing,incoming)
        store._preserve_record=pause
    else:(root/"connected-started").touch()
    record={"api_key":"synthetic-native-test-key","verifier":"synthetic-verifier","status":role,
        "expected":{"origin":sys.argv[4],"connection_id":sys.argv[5],"owner":"synthetic-owner"},"note":"测试 UTF-8"}
    store.save(sys.argv[4],sys.argv[5],record,replace=True)
    (root/(role+"-done")).touch()
finally:store.keychain.close()
'''
        def wait_for(name):
            deadline = time.monotonic() + 10
            while not (self.root / name).exists():
                self.assertTrue(time.monotonic() < deadline, "native_race_barrier_not_reached")
                time.sleep(.02)
        arguments = [sys.executable, "-c", child_code, str(HELPER_PATH), str(self.root),
                     str(self.home), self.origin, self.connection_id]
        stale = subprocess.Popen([*arguments, "unknown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        connected = None
        try:
            wait_for("stale-read")
            connected = subprocess.Popen([*arguments, "connected"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            wait_for("connected-started")
            time.sleep(.2)
            self.assertFalse((self.root / "connected-done").exists(), "native_writer_bypassed_lock")
            (self.root / "release-stale").touch()
            self.assertTrue(stale.wait(timeout=15) == 0, "stale_native_child_failed")
            self.assertTrue(connected.wait(timeout=15) == 0, "connected_native_child_failed")
            self.assertTrue(self.store.load(self.origin, self.connection_id)["status"] == "connected",
                            "native_concurrent_state_regressed")
        finally:
            (self.root / "release-stale").touch()
            for child in (stale, connected):
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
