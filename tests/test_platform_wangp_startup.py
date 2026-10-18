"""Offline startup observability; no model, runtime server, provider or network."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from studio_platform.runtime_hosts.wangp_startup import read_startup_failure, write_startup_failure


class StartupFailureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/"failure.json"
        self.identity = dict(slot_key="slot-1", manifest_digest="a"*64, launch_id="b"*32, pid=os.getpid())

    def write(self):
        return write_startup_failure(self.path, **self.identity, phase="runtime_verification",
                                     error=ValueError("wangp_component_hash_mismatch"))

    def test_identity_bound_exclusive_publication(self):
        original = self.write()
        self.assertEqual(read_startup_failure(self.path, **self.identity), original)
        for field, wrong in (("pid", os.getpid()+1), ("slot_key", "other"),
                             ("manifest_digest", "c"*64), ("launch_id", "d"*32)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                read_startup_failure(self.path, **{**self.identity, field:wrong})
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertEqual(read_startup_failure(self.path, **self.identity), original)
        self.assertEqual(list(self.path.parent.glob(".startup-*.tmp")), [])

    def test_unknown_fields_and_nonliteral_codes_are_rejected(self):
        original = self.write()
        for change in ({"prompt":"must never be projected"}, {"error_code":"private-secret"},
                       {"inference_verified":True}, {"pid":True}, {"observed_at":float("nan")}):
            self.path.write_text(json.dumps({**original, **change}))
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                read_startup_failure(self.path, **self.identity)


if __name__ == "__main__":
    unittest.main()
