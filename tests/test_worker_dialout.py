"""Dial-out worker pieces (#859); no chisel, GPU, provider or network."""

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

import httpx

from studio_platform import dialout_boot
from studio_platform.dialout_host import DialoutHost
from studio_platform.native_bootstrap import BootError
from studio_platform.storage import ObjectAlreadyExists
from studio_platform.worker_boot import boot_commands, grant_path
from studio_platform.worker_grant import GrantWriter

INTENT = "0b8f6a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
TOKEN = "t" * 64


class Signed:
    def __init__(self, url):
        self.url = url

    def reveal(self):
        return self.url


class Objects:
    def __init__(self):
        self.data = {}

    def put(self, key, source, *, max_bytes, expected_sha256):
        if key in self.data:
            raise ObjectAlreadyExists(key)
        value = source.read()
        assert len(value) <= max_bytes
        assert hashlib.sha256(value).hexdigest() == expected_sha256
        self.data[key] = value

    def presign_download(self, key, *, expires_seconds):
        assert key in self.data and expires_seconds == 3600
        return Signed("https://objects.example/" + key + "?sig=1")


class GrantTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        for name in (
            "wangp-bootstrap.py",
            "wangp-manifest.json",
            "wangp-runtime.json",
            "wangp-package.tar.gz",
        ):
            (self.sources / name).write_bytes(name.encode())
        self.chisel = self.root / "chisel"
        self.chisel.write_bytes(b"chisel-binary")
        self.work = self.root / "work" / INTENT
        self.work.mkdir(parents=True)
        token = self.work / "wangp-token"
        token.write_text(TOKEN)
        token.chmod(0o600)
        self.objects = Objects()
        self.writer = GrantWriter(
            self.root / "tunnel",
            self.objects,
            "https://staging.inkseq.com",
            chisel=self.chisel,
        )
        (self.root / "tunnel").mkdir()
        self.config = SimpleNamespace(
            profile_slot_index=0,
            deployment_profile_id="profile",
            local_port=19100,
            status_port=19101,
            runtime_python="/venv/main/bin/python",
            source_dir=self.sources,
        )

    def test_grant_is_written_once_with_signed_exact_bytes(self):
        identity = {"intent_id": INTENT}
        with patch("studio_platform.worker_grant.ensure_key", return_value="fp="):
            self.writer({"intent_id": INTENT}, self.config, identity, self.work)
            first = json.loads(grant_path(self.root / "tunnel", INTENT).read_text())
            self.writer({"intent_id": INTENT}, self.config, identity, self.work)
        self.assertEqual(
            json.loads(grant_path(self.root / "tunnel", INTENT).read_text()), first
        )
        self.assertEqual(first["token"], TOKEN)
        self.assertEqual(first["remote_root"], "/workspace/h3-studio/profile-slot-0")
        self.assertEqual(first["tunnel"]["remotes"], [[19100, 8199], [19101, 8198]])
        self.assertEqual(
            first["tunnel"]["url"], "https://staging.inkseq.com/v1/worker-tunnel"
        )
        for name, source in first["sources"].items():
            self.assertEqual(
                source["sha256"], hashlib.sha256(name.encode()).hexdigest()
            )
            self.assertIn(source["sha256"], source["url"])
        self.assertEqual(
            first["chisel"]["sha256"], hashlib.sha256(b"chisel-binary").hexdigest()
        )
        users = json.loads((self.root / "tunnel" / "users.json").read_text())
        rules = [
            v for k, v in users.items() if k.startswith(first["tunnel"]["user"] + ":")
        ]
        self.assertEqual(
            rules, [[r"^R:127\.0\.0\.1:19100$", r"^R:127\.0\.0\.1:19101$"]]
        )
        self.writer.revoke(INTENT)
        users = json.loads((self.root / "tunnel" / "users.json").read_text())
        self.assertFalse(any(k.startswith(first["tunnel"]["user"]) for k in users))

    def test_plain_http_origin_is_refused(self):
        with self.assertRaises(ValueError):
            GrantWriter(self.root, self.objects, "http://staging.inkseq.com")


class HostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        work = Path(temp.name)
        (work / INTENT).mkdir()
        token = work / INTENT / "wangp-token"
        token.write_text(TOKEN)
        token.chmod(0o600)
        self.config = SimpleNamespace(
            work_dir=work,
            deployment_profile_id="",
            profile_slot_index=0,
            local_port=19100,
            provider="vast",
        )
        self.coordinates = {
            "host": "127.0.0.1",
            "port": 19101,
            "username": "root",
            "instance_id": "1",
            "slot_key": INTENT,
        }
        self.replies, self.requests = [], []

    def handler(self, request):
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer " + TOKEN:
            return httpx.Response(401)
        if request.url.path == "/boot/health":
            return httpx.Response(200, content=b"{}")
        status, exit_code, body = self.replies.pop(0)
        return httpx.Response(
            status, headers={"X-Inkseq-Exit": str(exit_code)}, content=body
        )

    def host(self):
        return DialoutHost(
            self.config,
            self.coordinates,
            transport=httpx.MockTransport(self.handler),
        )

    def test_run_keeps_the_ssh_exec_contract(self):
        host = self.host()
        self.replies = [(200, 0, b'{"ok": true}'), (200, 1, b""), (504, 0, b"")]
        self.assertEqual(host.run("print(1)"), {"ok": True})
        with self.assertRaisesRegex(BootError, "command_failed"):
            host.run("raise SystemExit(1)")
        with self.assertRaisesRegex(BootError, "timeout"):
            host.run("import time; time.sleep(99)", timeout=1)
        posted = [r for r in self.requests if r.url.path == "/boot/run"]
        self.assertEqual(posted[0].content, b"print(1)")
        self.assertEqual(posted[2].headers["x-inkseq-timeout"], "1")

    def test_upload_and_start_only_verify_what_the_worker_did(self):
        host = self.host()
        files = {"a": b"one", "b": b"two"}
        digests = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
        self.replies = [
            (200, 0, json.dumps(digests).encode()),
            (200, 0, json.dumps({**digests, "b": None}).encode()),
            (200, 0, json.dumps({"intent_id": INTENT}).encode()),
            (200, 0, json.dumps({"intent_id": "other"}).encode()),
        ]
        host.upload(files)
        with self.assertRaisesRegex(BootError, "existing_source_mismatch"):
            host.upload(files)
        self.assertEqual(
            host.start({"intent_id": INTENT}), {"state": "already_reserved"}
        )
        with self.assertRaisesRegex(BootError, "identity_conflict"):
            host.start({"intent_id": INTENT})
        host.open_tunnel(19100)
        with self.assertRaises(BootError):
            host.open_tunnel(19102)

    def test_slotted_profiles_rewrite_the_remote_root_exactly_once(self):
        self.config.deployment_profile_id = "profile"
        host = self.host()
        self.assertEqual(host.remote_root, "/workspace/h3-studio/profile-slot-0")
        digest = hashlib.sha256(b"one").hexdigest()
        self.replies = [
            (200, 0, json.dumps({"a": digest}).encode()),
            (200, 0, json.dumps({"intent_id": INTENT}).encode()),
        ]
        host.upload({"a": b"one"})
        host.start({"intent_id": INTENT})
        for request in [r for r in self.requests if r.url.path == "/boot/run"]:
            code = request.content.decode()
            self.assertIn("/workspace/h3-studio/profile-slot-0", code)
            self.assertNotIn("profile-slot-0/profile-slot-0", code)

    def test_unreachable_worker_reads_as_not_yet_connected(self):
        def down(request):
            raise httpx.ConnectError("refused")

        with self.assertRaisesRegex(BootError, "unavailable"):
            DialoutHost(
                self.config, self.coordinates, transport=httpx.MockTransport(down)
            )


class BootScriptTests(unittest.TestCase):
    def test_run_command_inlines_only_public_code(self):
        self.assertEqual(boot_commands(None), ["exec sleep infinity"])
        (command,) = boot_commands("x" * 43)
        self.assertTrue(command.startswith("exec python3 - <<'INKSEQ_BOOT_SCRIPT'\n"))
        self.assertTrue(command.endswith("\nINKSEQ_BOOT_SCRIPT"))
        self.assertNotIn("x" * 43, command)
        compile(command.split("\n", 1)[1].rsplit("\n", 1)[0], "boot", "exec")

    def test_exchange_retries_until_ready_and_stops_on_refusal(self):
        calls, waits = [], []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def opener(codes):
            class Opener:
                def open(self, request, timeout):
                    calls.append(json.loads(request.data))
                    code = codes.pop(0)
                    if code == 200:
                        return Reply(b'{"grant": 1}')
                    raise urllib.error.HTTPError(request.full_url, code, "", {}, None)

            return Opener()

        import urllib.error

        with patch.object(
            dialout_boot, "_opener", return_value=opener([425, 503, 200])
        ):
            self.assertEqual(
                dialout_boot.exchange(
                    "https://x/v1/worker/boot", INTENT, "c", wait=waits.append
                ),
                {"grant": 1},
            )
        self.assertEqual(waits, [5, 5])
        self.assertEqual(calls[0], {"intent_id": INTENT, "token": "c"})
        with patch.object(dialout_boot, "_opener", return_value=opener([409])):
            with self.assertRaises(SystemExit):
                dialout_boot.exchange("https://x/v1/worker/boot", INTENT, "c")
        with self.assertRaises(SystemExit):
            dialout_boot.exchange("http://x/v1/worker/boot", INTENT, "c")

    def test_status_endpoint_requires_the_worker_token(self):
        import http.server

        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), dialout_boot.status_handler(TOKEN)
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]
        with httpx.Client(base_url=base, trust_env=False) as client:
            self.assertEqual(client.get("/boot/health").status_code, 401)
            auth = {"Authorization": "Bearer " + TOKEN}
            self.assertEqual(client.get("/boot/health", headers=auth).status_code, 200)
            done = client.post(
                "/boot/run",
                content=b"import json; print(json.dumps({'v': 2}))",
                headers=auth,
            )
            self.assertEqual(done.headers["x-inkseq-exit"], "0")
            self.assertEqual(json.loads(done.content), {"v": 2})
            self.assertEqual(
                client.post("/boot/run", content=b"print(1)").status_code, 401
            )


if __name__ == "__main__":
    unittest.main()
