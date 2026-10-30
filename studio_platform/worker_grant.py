"""Controller-side grant for one dial-out GPU worker (#859).

Written once per intent after the run is observed and the launch is
journaled. The app's one-time boot exchange hands it to the worker. Sources
and the chisel binary are content-addressed in object storage so the worker
pulls exact bytes by signed URL; nothing is pushed into the host.
"""

import hashlib
import io
from pathlib import Path

from .storage import ObjectAlreadyExists
from .wangp_bootstrap import REMOTE_ROOT, SOURCE_NAMES
from .worker_boot import grant_path, write_grant
from .worker_tunnel import CHISEL, TunnelUsers, ensure_key

STATUS_REMOTE_PORT = 8198
SIGNED_SECONDS = 3600


class GrantWriter:
    def __init__(self, root, objects, public_origin, *, chisel=CHISEL):
        self.root, self.objects, self.chisel = Path(root), objects, Path(chisel)
        self.tunnel_url = public_origin.rstrip("/") + "/v1/worker-tunnel"
        if not self.tunnel_url.startswith("https://"):
            raise ValueError("worker_grant_origin_invalid")
        self.users = TunnelUsers(self.root / "users.json")

    @property
    def server_key(self):
        return self.root / "server.key"

    def _signed(self, data):
        sha = hashlib.sha256(data).hexdigest()
        key = "worker-sources/" + sha
        try:
            self.objects.put(
                key,
                io.BytesIO(data),
                max_bytes=len(data),
                expected_sha256=sha,
            )
        except ObjectAlreadyExists:
            pass  # Content-addressed: the same bytes are already there.
        url = self.objects.presign_download(key, expires_seconds=SIGNED_SECONDS)
        return {"url": url.reveal(), "sha256": sha}

    def __call__(self, binding, config, identity, directory):
        intent_id = binding["intent_id"]
        if grant_path(self.root, intent_id).exists():
            return  # Written once; the worker may already hold it.
        from .runtime_hosts.wangp_http import private_token_file

        slot = config.profile_slot_index
        remote_root = REMOTE_ROOT + (
            "/profile-slot-" + str(slot) if config.deployment_profile_id else ""
        )
        runtime_port = 8199 + slot if config.deployment_profile_id else 8199
        user, password = self.users.grant(
            intent_id, [config.local_port, config.status_port]
        )
        grant = {
            "identity": identity,
            "remote_root": remote_root,
            "runtime_python": config.runtime_python,
            "token": private_token_file(Path(directory) / "wangp-token"),
            "sources": {
                name: self._signed((config.source_dir / name).read_bytes())
                for name in sorted(SOURCE_NAMES)
            },
            "chisel": self._signed(self.chisel.read_bytes()),
            "tunnel": {
                "url": self.tunnel_url,
                "fingerprint": ensure_key(self.server_key),
                "user": user,
                "password": password,
                "remotes": [
                    [config.local_port, runtime_port],
                    [config.status_port, STATUS_REMOTE_PORT],
                ],
            },
        }
        write_grant(self.root, intent_id, grant)

    def revoke(self, intent_id):
        self.users.revoke(intent_id)
