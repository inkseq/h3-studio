"""Dial-out GPU worker boot (#859); stdlib only, run by the dstack run command.

The run env carries only INKSEQ_BOOT_URL, INKSEQ_INTENT and a one-time
INKSEQ_BOOT_TOKEN. One exchange returns this worker's grant: signed source
and chisel URLs with sha256, the runtime token, the tunnel user and the
identity document. The worker then does what the controller's SSH start step
did, dials out with chisel, and serves a loopback exec endpoint that only the
controller reaches through the tunnel (the same trust as its old SSH exec).
"""

import fcntl
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

STATUS_PORT = 8198
EXCHANGE_DEADLINE = 50 * 60
RUN_OUTPUT_LIMIT = 4 * 1024 * 1024
CHISEL = Path("/tmp/inkseq-chisel")


def log(message):
    # Container stdout is what the dstack logs API shows; never log secrets.
    print("inkseq-boot: " + message, flush=True)


def _opener():
    # Signed URLs and the exchange only: no proxy environment, no redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def exchange(url, intent, token, *, clock=time.monotonic, wait=time.sleep):
    if not url.startswith("https://"):
        raise SystemExit("inkseq_boot_url_invalid")
    body = json.dumps({"intent_id": intent, "token": token}).encode()
    started = clock()
    while clock() - started < EXCHANGE_DEADLINE:
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with _opener().open(request, timeout=30) as response:
                return json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as error:
            # 425: the controller has not prepared the grant yet. 5xx: retry.
            if error.code in (403, 409):
                raise SystemExit("inkseq_boot_refused_%d" % error.code) from None
            log("exchange waiting (HTTP %d)" % error.code)
        except OSError as error:
            log("exchange unreachable (%s)" % type(error).__name__)
        wait(5)
    raise SystemExit("inkseq_boot_exchange_timeout")


def fetch(url, target, sha256):
    digest, temporary = hashlib.sha256(), target.with_name(target.name + ".part")
    with _opener().open(urllib.request.Request(url), timeout=60) as response:
        with temporary.open("wb") as out:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(chunk)
                out.write(chunk)
    if digest.hexdigest() != sha256:
        temporary.unlink()
        raise SystemExit("inkseq_boot_source_mismatch")
    temporary.replace(target)


def write_once(path, data, mode):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        if path.read_bytes() != data:
            raise SystemExit("inkseq_boot_identity_conflict") from None
        return
    with os.fdopen(descriptor, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def start_runtime(grant, root):
    """The controller's former SSH start step: one identity, one bootstrap."""
    identity = grant["identity"]
    with (root / "sixnine-bootstrap.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = root / "sixnine-bootstrap-identity.json"
        if marker.exists():
            if json.loads(marker.read_text()) != identity:
                raise SystemExit("inkseq_boot_identity_conflict")
            return
        write_once(marker, json.dumps(identity).encode(), 0o600)
        command = [
            grant["runtime_python"],
            "-u",
            str(root / "wangp-bootstrap.py"),
            "--config",
            str(root / "wangp-runtime.json"),
            "--slot-key",
            identity["intent_id"],
            "--token-file",
            str(root / "wangp-token"),
        ]
        with (root / "bootstrap-controller.log").open("ab") as log:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )


def keep_tunnel(grant):
    tunnel = grant["tunnel"]
    command = [
        str(CHISEL),
        "client",
        "--fingerprint",
        tunnel["fingerprint"],
        "--auth",
        tunnel["user"] + ":" + tunnel["password"],
        tunnel["url"],
        *("R:127.0.0.1:%d:127.0.0.1:%d" % (a, b) for a, b in tunnel["remotes"]),
    ]
    while True:
        # chisel retries by itself; restart only if the process exits.
        subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
        time.sleep(5)


def status_handler(token):
    expected = ("Bearer " + token).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            provided = self.headers.get("Authorization", "").encode()
            return hmac.compare_digest(provided, expected)

        def do_GET(self):
            if not self._authorized():
                return self._reply(401)
            self._reply(200 if self.path == "/boot/health" else 404, b"{}")

        def do_POST(self):
            # Same contract as the controller's old `ssh python3 -c` exec.
            if not self._authorized() or self.path != "/boot/run":
                return self._reply(401 if not self._authorized() else 404)
            size = int(self.headers.get("Content-Length", "0"))
            timeout = float(self.headers.get("X-Inkseq-Timeout", "25"))
            if not 0 < size <= 1024 * 1024 or not 0 < timeout <= 900:
                return self._reply(400)
            script = self.rfile.read(size).decode()
            try:
                done = subprocess.run(
                    ["python3", "-c", script],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return self._reply(504)
            output = done.stdout[: RUN_OUTPUT_LIMIT + 1]
            self.send_response(200)
            self.send_header("X-Inkseq-Exit", str(done.returncode))
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

        def log_message(self, *args):
            pass

    return Handler


def main(environ=os.environ):
    grant = exchange(
        environ["INKSEQ_BOOT_URL"],
        environ["INKSEQ_INTENT"],
        environ["INKSEQ_BOOT_TOKEN"],
    )
    root = Path(grant["remote_root"])
    root.mkdir(parents=True, exist_ok=True)
    for name, source in sorted(grant["sources"].items()):
        fetch(source["url"], root / name, source["sha256"])
    fetch(grant["chisel"]["url"], CHISEL, grant["chisel"]["sha256"])
    CHISEL.chmod(0o755)
    log("sources and chisel verified")
    write_once(root / "wangp-token", grant["token"].encode(), 0o600)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", STATUS_PORT), status_handler(grant["token"])
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=keep_tunnel, args=(grant,), daemon=True).start()
    log("tunnel and status endpoint started")
    try:
        start_runtime(grant, root)
        log("runtime launched")
    except (OSError, SystemExit) as error:
        # Stay reachable so the controller can read why; its startup
        # deadline still stops and replaces this machine.
        log(
            "runtime launch failed (%s)"
            % (error.code if isinstance(error, SystemExit) else type(error).__name__)
        )
    # The container lives as long as the run; the controller stops the run.
    threading.Event().wait()


if __name__ == "__main__":
    sys.exit(main())
