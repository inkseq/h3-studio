"""Worker reverse-tunnel admission; no chisel process or network."""

import base64
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from studio_platform.worker_tunnel import (
    SENTINEL,
    TunnelUsers,
    client_command,
    fingerprint,
    server_command,
    user_for,
)

INTENT = "0b8f6a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
OTHER = "1b8f6a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


class WorkerTunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "users.json"
        self.users = TunnelUsers(self.path)

    def read(self):
        return json.loads(self.path.read_text())

    def test_each_intent_opens_only_its_own_loopback_ports(self):
        user, password = self.users.grant(INTENT, [19001, 19002])
        self.assertEqual(user, user_for(INTENT))
        self.users.grant(OTHER, [19003])
        value = self.read()
        allowed = value[user + ":" + password]
        self.assertEqual(
            allowed, [r"^R:127\.0\.0\.1:19001$", r"^R:127\.0\.0\.1:19002$"]
        )
        self.assertTrue(re.fullmatch(allowed[0], "R:127.0.0.1:19001"))
        for denied in ("R:127.0.0.1:190011", "R:0.0.0.0:19001", "R:127.0.0.1:19003"):
            self.assertFalse(any(re.search(rule, denied) for rule in allowed))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        # A new grant replaces the intent's previous credential.
        _, again = self.users.grant(INTENT, [19001])
        keys = [k for k in self.read() if k.startswith(user + ":")]
        self.assertEqual(keys, [user + ":" + again])

    def test_authfile_always_keeps_a_sentinel_that_opens_nothing(self):
        # chisel 1.12 disables authentication for an authfile with no users.
        self.users.grant(INTENT, [19001])
        self.users.revoke(INTENT)
        self.users.revoke(OTHER)
        value = self.read()
        self.assertEqual(len(value), 1)
        ((key, allowed),) = value.items()
        self.assertTrue(key.startswith(SENTINEL + ":"))
        self.assertEqual(allowed, [])

    def test_invalid_identities_and_ports_are_refused(self):
        for intent in ("../x", "", "0b8f6a4e"):
            with self.assertRaises(ValueError):
                self.users.grant(intent, [19001])
        for ports in ([], [80], [70000], ["19001"]):
            with self.assertRaises(ValueError):
                self.users.grant(INTENT, ports)
        self.assertFalse(self.path.exists())

    def test_fingerprint_matches_chisel_sha256_of_the_ssh_public_key(self):
        key = ec.generate_private_key(ec.SECP256R1())
        der = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        keyfile = Path(self.temp.name) / "key"
        # `chisel server --keygen` writes "ck-" + unpadded base64 DER.
        keyfile.write_text("ck-" + base64.b64encode(der).decode().rstrip("="))
        public = key.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        expected = base64.b64encode(
            hashlib.sha256(base64.b64decode(public.split()[1])).digest()
        ).decode()
        self.assertEqual(fingerprint(keyfile), expected)
        keyfile.write_text("-----BEGIN PRIVATE KEY-----")
        with self.assertRaises(ValueError):
            fingerprint(keyfile)

    def test_commands_pin_reverse_mode_authfile_and_server_fingerprint(self):
        server = server_command(8846, "/run/users.json", "/run/key")
        self.assertIn("--reverse", server)
        self.assertEqual(server[server.index("--authfile") + 1], "/run/users.json")
        client = client_command(
            "https://staging.inkseq.com/v1/worker-tunnel",
            "fp=",
            "wabc",
            "secret",
            {19002: 8198, 19001: 8199},
        )
        self.assertEqual(client[client.index("--fingerprint") + 1], "fp=")
        self.assertEqual(
            client[-2:],
            ["R:127.0.0.1:19001:127.0.0.1:8199", "R:127.0.0.1:19002:127.0.0.1:8198"],
        )


if __name__ == "__main__":
    unittest.main()
