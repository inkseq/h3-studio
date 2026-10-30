"""Controller side of a dial-out GPU worker (#859); the SSH host's interface.

The worker dialed out with chisel, so the controller reaches its loopback exec
endpoint at 127.0.0.1:<status port> and its runtime at 127.0.0.1:<local port>.
`run` keeps the old `ssh python3 -c` contract, so every existing report and
preparation check is reused unchanged. Sources and the launch now happen on
the worker from its one-time grant; here they are only verified.
"""

import hashlib
import json

import httpx

from .native_bootstrap import BootError
from .wangp_bootstrap import WanGPSSHHost


class DialoutHost(WanGPSSHHost):
    def __init__(self, config, coordinates, *, transport=None):
        self._transport = transport
        self._http = None
        super().__init__(config, coordinates)

    def _token(self):
        from .runtime_hosts.wangp_http import private_token_file

        intent = self.coordinates["slot_key"]
        return private_token_file(self.config.work_dir / intent / "wangp-token")

    def _client(self):
        if self._http is None:
            self._http = httpx.Client(
                base_url="http://127.0.0.1:%d" % self.coordinates["port"],
                headers={"Authorization": "Bearer " + self._token()},
                transport=self._transport,
                timeout=15,
                follow_redirects=False,
                trust_env=False,
            )
        return self._http

    def ensure_connected(self):
        try:
            response = self._client().get("/boot/health")
        except Exception:
            raise BootError("bootstrap_ssh_unavailable_or_host_key_untrusted") from None
        if response.status_code != 200:
            raise BootError("bootstrap_ssh_unavailable_or_host_key_untrusted")

    def _connect(self):
        self.ensure_connected()

    def run(self, code, *, limit=4 * 1024 * 1024, timeout=25):
        if getattr(self.config, "deployment_profile_id", ""):
            # Same slot isolation rewrite as the SSH host.
            from .wangp_bootstrap import REMOTE_ROOT

            code = code.replace(REMOTE_ROOT, self.remote_root)
            code = code.replace("==8199 and", "==" + str(self.remote_port) + " and")
        try:
            response = self._client().post(
                "/boot/run",
                content=code.encode(),
                headers={"X-Inkseq-Timeout": str(timeout)},
                timeout=timeout + 15,
            )
        except Exception:
            raise BootError("bootstrap_remote_response_unconfirmed") from None
        if response.status_code == 504:
            raise BootError("bootstrap_ssh_command_timeout")
        if response.status_code != 200 or len(response.content) > limit:
            raise BootError(
                "bootstrap_report_too_large"
                if len(response.content) > limit
                else "bootstrap_remote_response_unconfirmed"
            )
        if response.headers.get("X-Inkseq-Exit") != "0":
            raise BootError("bootstrap_remote_command_failed")
        try:
            return json.loads(response.content)
        except ValueError:
            raise BootError("bootstrap_remote_response_unconfirmed") from None

    def upload(self, files, *, progress=None, should_stop=None):
        """The worker fetched these by sha256; confirm the exact bytes are there."""
        expected = {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        }
        found = self.run(
            "import hashlib,json\nfrom pathlib import Path\nroot=Path(%r)\n"
            "print(json.dumps({n:(hashlib.sha256((root/n).read_bytes()).hexdigest() "
            "if (root/n).is_file() and not (root/n).is_symlink() else None) "
            "for n in %r}))" % (self.remote_root, sorted(expected))
        )
        if found != expected:
            raise BootError("bootstrap_existing_source_mismatch")

    def start(self, identity):
        """The worker launched once under its identity lock; confirm that identity."""
        marker = self.run(
            "import json\nfrom pathlib import Path\n"
            "p=Path(%r)/'sixnine-bootstrap-identity.json'\n"
            "print(json.dumps(json.loads(p.read_text()) if p.is_file() else None))"
            % self.remote_root
        )
        if marker != identity:
            raise BootError("wangp_boot_token_identity_conflict")
        return {"state": "already_reserved"}

    def open_tunnel(self, port):
        # chisel already binds 127.0.0.1:<local port> to the worker runtime.
        if port != self.config.local_port:
            raise BootError("bootstrap_tunnel_port_already_in_use")

    def close(self):
        if self._http is not None:
            self._http.close()
            self._http = None
