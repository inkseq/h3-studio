"""Reverse-tunnel admission for dial-out GPU workers (#859); no provider calls.

GPU workers run a pinned chisel client and dial the platform. The chisel
server lives in the app network namespace. Each intent gets one user that may
open only its own loopback ports, so the CPU keeps reaching a worker at
127.0.0.1:<port>, exactly as through the SSH tunnel it replaces.

chisel 1.12 turns authentication off when its authfile has no users. The file
therefore always keeps one sentinel user that may open nothing.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

CHISEL = "/usr/local/bin/chisel"
VERSION = "1.12.0"
SENTINEL = "inkseq-sentinel"
_USER = re.compile(r"w[0-9a-f]{32}")


def user_for(intent_id):
    user = "w" + str(intent_id).replace("-", "")
    if not _USER.fullmatch(user):
        raise ValueError("worker_tunnel_intent_invalid")
    return user


def _remote(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("worker_tunnel_port_invalid")
    return r"^R:127\.0\.0\.1:%d$" % port


class TunnelUsers:
    """Atomic authfile edits; chisel reloads it for new connections only."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("worker_tunnel_authfile_invalid")

    def _locked(self, change):
        lock = self.path.with_name(self.path.name + ".lock")
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            users = self._read()
            change(users)
            # The sentinel survives every write; see the module docstring.
            users = {
                key: value
                for key, value in users.items()
                if not key.startswith(SENTINEL + ":")
            }
            users[SENTINEL + ":" + secrets.token_urlsafe(24)] = []
            temporary = self.path.with_name(self.path.name + ".tmp")
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "w") as out:
                json.dump(users, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)

    def _read(self):
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError("worker_tunnel_authfile_invalid")
        return value

    def grant(self, intent_id, ports):
        """Return the one-time credential that may open exactly these ports."""
        user = user_for(intent_id)
        allowed = [_remote(port) for port in ports]
        if not allowed:
            raise ValueError("worker_tunnel_port_invalid")
        password = secrets.token_urlsafe(32)

        def change(users):
            for key in [k for k in users if k.split(":", 1)[0] == user]:
                del users[key]
            users[user + ":" + password] = allowed

        self._locked(change)
        return user, password

    def revoke(self, intent_id):
        user = user_for(intent_id)

        def change(users):
            for key in [k for k in users if k.split(":", 1)[0] == user]:
                del users[key]

        self._locked(change)


def fingerprint(keyfile):
    """chisel's SHA256 server fingerprint for a `--keygen` key file."""
    from cryptography.hazmat.primitives import serialization

    text = Path(keyfile).read_text().strip()
    if not text.startswith("ck-"):
        raise ValueError("worker_tunnel_key_invalid")
    der = text[3:] + "=" * (-len(text[3:]) % 4)
    key = serialization.load_der_private_key(base64.b64decode(der), None)
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    raw = base64.b64decode(public.split()[1])
    return base64.b64encode(hashlib.sha256(raw).digest()).decode()


def ensure_key(keyfile):
    keyfile = Path(keyfile)
    if not keyfile.exists():
        subprocess.run(
            [CHISEL, "server", "--keygen", str(keyfile)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        keyfile.chmod(0o600)
    return fingerprint(keyfile)


def server_command(port, authfile, keyfile):
    # Bound inside the app network namespace; only Caddy reaches it.
    return [
        CHISEL,
        "server",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--reverse",
        "--authfile",
        str(authfile),
        "--keyfile",
        str(keyfile),
    ]


def client_command(url, server_fingerprint, user, password, forwards):
    """The worker side, for its boot script; `forwards` maps public→local port."""
    remotes = [
        "R:127.0.0.1:%d:127.0.0.1:%d" % (public, local)
        for public, local in sorted(forwards.items())
    ]
    return [
        CHISEL,
        "client",
        "--fingerprint",
        server_fingerprint,
        "--auth",
        user + ":" + password,
        url,
        *remotes,
    ]


SERVER_PORT = 8846


def serve(root, *, popen=subprocess.Popen, wait=None):
    """Keep one chisel server running for the controller's lifetime (daemon)."""
    import threading
    import time

    root = Path(root)
    users = TunnelUsers(root / "users.json")
    users._locked(lambda current: None)  # Never start without the sentinel.
    ensure_key(root / "server.key")
    command = server_command(SERVER_PORT, root / "users.json", root / "server.key")

    def run():
        while True:
            process = popen(command, stdin=subprocess.DEVNULL)
            process.wait()
            (wait or time.sleep)(5)

    thread = threading.Thread(target=run, name="worker-tunnel-server", daemon=True)
    thread.start()
    return thread
