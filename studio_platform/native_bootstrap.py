"""Shared native SSH transport and sanitized bootstrap diagnostics.

No provider clients, rental coordinator, model boot controller or import-time I/O.
"""

from __future__ import annotations

import json
import re
import select as io_select
import shlex
import socketserver
import threading
import time
from uuid import UUID

from .repository import Conflict
from .runtime_hosts.wangp_startup import (
    CODES as STARTUP_CODES,
    PHASES as STARTUP_PHASES,
    TYPES as STARTUP_TYPES,
)


REMOTE_ROOT = "/workspace/h3-studio"


MODEL_REVISION = "e5eb578a89295337b8ff433a035929ce0279e0b6"


COMFY_REVISION = "e9027f2b30f37bb3052714eb08fcf479542f4fc0"


BOOT_FAILURE_CODES = frozenset(
    {
        "targon_prepare_failed",
        "native_profile_python31214_linux_required",
        "wangp_runtime_source_mismatch",
        "wangp_runtime_source_modified",
        "wangp_runtime_requirements_mismatch",
        "wangp_runtime_untracked_code",
        "wangp_runtime_dependency_mismatch",
        "wangp_profile_cache_lock_timeout",
        "wangp_profile_manifest_mismatch",
        "InternalSetupFailure",
        "SubprocessTimeout",
        "SubprocessFailed",
        "CUDAUnavailable",
        "ExistingComfyDirectoryIsNotCheckout",
        "ExistingComfyCheckoutModified",
        "ComfyRevisionMismatch",
        "RequiredComfyFlagMissing",
        "ImageTorchMissing",
        "ImageTorchChanged",
        "XetRequiredForLargeWeights",
        "InsufficientCacheDiskSpace",
        "DownloadedFileSizeMismatch",
        "ExistingModelPathConflict",
        "ModelDownloadFailed",
        "ExistingOwnedComfyNotReady",
        "Port8188AlreadyInUse",
        "ComfyExitedBeforeReady",
        "ComfyStartupTimeout",
        "bootstrap_operation_failed",
        "source_bundle_mismatch",
        "resolved_environment_manifest_required",
        "dependency_artifact_mismatch",
        "environment_binding_mismatch",
        "system_package_mismatch",
        "requirements_lock_mismatch",
        "wheel_mismatch",
        "model_disk_headroom",
        "model_size_mismatch",
        "model_download_manifest_invalid",
        "model_download_manifest_changed",
        "model_download_path_invalid",
        "model_download_size_mismatch",
        "model_download_disk_headroom",
        "model_download_failed",
        "model_download_timeout",
        "model_download_cache_limit",
        "model_download_progress_invalid",
        "model_download_stop_unconfirmed",
        "model_download_state_exists",
        "model_download_owner_unconfirmed",
        "verification_receipt_mismatch",
        "gpu_observation_invalid",
        "single_gpu_recipe_required",
        "private_token_invalid",
        "runtime_process_exited",
        "runtime_readiness_timeout",
        "python31114_linux_required",
        "python311_linux_required",
        "bootstrap_configuration_invalid",
        "system_deb_mismatch",
        "runtime_import_probe_failed",
        "runtime_import_receipt_mismatch",
        "system_restore_manifest_invalid",
        "system_restore_package_invalid",
        "system_restore_lock_conflict",
        "system_restore_package_mismatch",
        "system_restore_extra_file",
        "system_restore_unrecognized_drift",
        "system_restore_deb_metadata_mismatch",
        "system_restore_verification_failed",
        "system_restore_package_audit_failed",
    }
)


BOOT_FAILURE_TYPES = frozenset(
    {
        "SetupError",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "OSError",
        "FileNotFoundError",
        "PermissionError",
        "ImportError",
        "ModuleNotFoundError",
        "TimeoutError",
        "ConnectionError",
        "CalledProcessError",
        "TimeoutExpired",
        "HTTPError",
        "HTTPStatusError",
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectError",
        "SSLError",
        "HfHubHTTPError",
        "LocalEntryNotFoundError",
        "EntryNotFoundError",
        "RepositoryNotFoundError",
        "RevisionNotFoundError",
        "XetDownloadError",
        "XetError",
        "JSONDecodeError",
    }
)


BOOT_PHASES = frozenset(
    {
        "model_download_waiting_for_shared_cache",
        "preflight",
        "clone_comfy",
        "fetch_comfy",
        "pin_comfy",
        "install_dependencies",
        "download_preflight",
        "download_file",
        "weights_ready",
        "start_comfy",
        "comfy_ready",
        "failed",
        "download",
        "checking_package",
        "dependency_download",
        "dependency_unpack",
        "dependency_install",
        "model_download",
        "runtime_verification",
        "runtime_start",
        "runtime_ready",
        "runtime_start_unknown",
        "setup_failed",
        "system_package_install",
        "system_package_restore",
        "system_package_verification",
        "runtime_imports",
    }
)


BOOT_FAILURE_CODES |= STARTUP_CODES


BOOT_FAILURE_TYPES |= STARTUP_TYPES | {"RuntimeStartupError"}


BOOT_PHASES |= STARTUP_PHASES


def _static(value, allowed, fallback):
    return value if isinstance(value, str) and value in allowed else fallback


def safe_bootstrap_diagnosis(value):
    """Static errors and bounded package versions; never logs, paths or URLs."""
    value = value if isinstance(value, dict) else {}
    diagnosis = {
        "error_code": _static(
            value.get("error_code"), BOOT_FAILURE_CODES, "UnclassifiedBootstrapFailure"
        ),
        "error_type": _static(
            value.get("error_type"), BOOT_FAILURE_TYPES, "UnknownSetupError"
        ),
        "phase": _static(value.get("phase"), BOOT_PHASES, "unknown"),
        "failure_phase": _static(
            value.get("failure_phase"), BOOT_PHASES - {"failed"}, "unknown"
        ),
    }
    details = value.get("failure_details", [])
    if isinstance(details, list):
        bounded = []
        for event in details[-5:]:
            if not isinstance(event, dict):
                continue
            entry = {"phase": _static(event.get("phase"), BOOT_PHASES, "unknown")}
            for name, allowed, fallback in (
                ("error_code", BOOT_FAILURE_CODES, "UnclassifiedBootstrapFailure"),
                ("error_type", BOOT_FAILURE_TYPES, "UnknownSetupError"),
            ):
                if name in event:
                    entry[name] = _static(event[name], allowed, fallback)
            bounded.append(entry)
        if bounded:
            diagnosis["failure_details"] = bounded
    if diagnosis["error_code"] in {
        "system_package_mismatch",
        "system_restore_unrecognized_drift",
        "system_restore_verification_failed",
    }:
        from .runtime_hosts.wangp_environment import safe_system_package_diagnostics

        packages = safe_system_package_diagnostics(
            value.get("system_package_diagnostics")
        )
        if packages is not None:
            diagnosis["system_package_diagnostics"] = packages
    if diagnosis["failure_phase"] == "model_download":
        from .runtime_hosts.wangp_download import SAFE_ERRORS, safe_download_failure

        download = safe_download_failure(value.get("download_failure"))
        if diagnosis["error_code"] in SAFE_ERRORS and download is not None:
            diagnosis["download_failure"] = download
    return diagnosis


class BootError(Conflict):
    pass


class SSHHost:
    """Identity-bound SSH and local tunnels; no provider lifecycle operations."""

    remote_port = 8188

    def __init__(self, config, coordinates):
        self.config, self.coordinates = config, dict(coordinates)
        self.tunnel = None
        self._connection_lock = threading.Lock()
        self._ever_connected = False
        self.client = None
        self.ensure_connected()

    def ensure_connected(self):
        """Reconnect the same endpoint with its already pinned key; never replay a command."""
        with self._connection_lock:
            transport = self.client.get_transport() if self.client is not None else None
            if (
                transport is not None
                and transport.is_active()
                and transport.is_authenticated()
            ):
                return
            self._connect()

    def _connect(self):
        import paramiko

        config, coordinates = self.config, self.coordinates
        expected_user = (
            'ubuntu' if getattr(config, 'provider', 'lium') == 'targon' else 'root'
        )
        username = coordinates.get('username', 'root')
        if username != expected_user:
            raise BootError('bootstrap_ssh_user_mismatch')
        if self.client is not None:
            self.client.close()
        self.client = paramiko.SSHClient()
        config.known_hosts_file.parent.mkdir(parents=True, exist_ok=True)
        if config.known_hosts_file.exists():
            self.client.load_host_keys(str(config.known_hosts_file))
        else:
            config.known_hosts_file.touch(mode=0o600)
            self.client.load_host_keys(str(config.known_hosts_file))
        # Initial explicit TOFU is allowed once. Transport recovery cannot trust
        # a different host key if the endpoint was replaced or the pin was lost.
        self.client.set_missing_host_key_policy(
            paramiko.AutoAddPolicy()
            if config.trust_first_host_key and not self._ever_connected
            else paramiko.RejectPolicy()
        )
        try:
            self.client.connect(
                coordinates["host"],
                port=coordinates["port"],
                username=username,
                key_filename=str(config.ssh_key_file),
                look_for_keys=False,
                allow_agent=False,
                timeout=15,
                banner_timeout=15,
                auth_timeout=15,
            )
            self._ever_connected = True
        except Exception:
            self.client.close()
            raise BootError("bootstrap_ssh_unavailable_or_host_key_untrusted") from None

    def run(self, script, *, limit=4 * 1024 * 1024, timeout=25):
        channel = None
        timer = None
        deadline = None
        expired = threading.Event()

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0 or expired.is_set():
                raise BootError("bootstrap_ssh_command_timeout")
            return value

        def expire():
            expired.set()
            # exec_command waits for a server ACK without consulting the
            # channel I/O timeout. Closing this channel wakes that wait. The
            # remote command outcome stays unknown; never replay it here.
            try:
                channel.close()
            except Exception:
                pass

        try:
            if type(timeout) not in (int, float) or not 0 < timeout < float('inf'):
                raise BootError("bootstrap_ssh_command_timeout_invalid")
            self.ensure_connected()
            deadline = time.monotonic() + timeout
            channel = self.client.get_transport().open_session(
                timeout=min(15, timeout, remaining())
            )
            channel.settimeout(min(timeout, remaining()))
            timer = threading.Timer(min(timeout, remaining()), expire)
            timer.daemon = True
            timer.start()
            prefix = (
                'sudo -n python3 -c '
                if getattr(getattr(self, 'config', None), 'provider', 'lium')
                == 'targon'
                else 'python3 -c '
            )
            channel.exec_command(prefix + shlex.quote(script))
            remaining()
            output = bytearray()
            while (
                not channel.exit_status_ready()
                or channel.recv_ready()
                or channel.recv_stderr_ready()
            ):
                remaining()
                if channel.recv_ready():
                    output.extend(channel.recv(65536))
                    if len(output) > limit:
                        raise BootError("bootstrap_report_too_large")
                if channel.recv_stderr_ready():
                    channel.recv_stderr(65536)  # Never retain upstream diagnostics.
                time.sleep(0.02)
            if channel.recv_exit_status() != 0:
                raise BootError("bootstrap_remote_command_failed")
            remaining()
            return json.loads(output)
        except BootError:
            raise
        except Exception:
            if (
                expired.is_set()
                or deadline is not None
                and time.monotonic() >= deadline
            ):
                raise BootError("bootstrap_ssh_command_timeout") from None
            raise BootError("bootstrap_remote_response_unconfirmed") from None
        finally:
            try:
                if channel is not None:
                    channel.close()
            finally:
                if timer is not None:
                    timer.cancel()

    def open_tunnel(self, port):
        self.ensure_connected()
        if self.tunnel:
            return
        gate = threading.BoundedSemaphore(8)

        class Handler(socketserver.BaseRequestHandler):
            def handle(inner):
                if not gate.acquire(blocking=False):
                    return
                channel = None
                try:
                    # Keep the listening socket stable for existing workers;
                    # each new request uses the current authenticated transport.
                    transport = self.client.get_transport()
                    if (
                        transport is None
                        or not transport.is_active()
                        or not transport.is_authenticated()
                    ):
                        return
                    channel = transport.open_channel(
                        "direct-tcpip",
                        ("127.0.0.1", self.remote_port),
                        inner.request.getpeername(),
                        timeout=15,
                    )
                    while transport.is_active():
                        readable, _, _ = io_select.select(
                            [inner.request, channel], [], [], 10
                        )
                        for source, destination in (
                            (inner.request, channel),
                            (channel, inner.request),
                        ):
                            if source in readable:
                                chunk = source.recv(65536)
                                if not chunk:
                                    return
                                destination.sendall(chunk)
                except Exception:
                    pass
                finally:
                    if channel is not None:
                        channel.close()
                    gate.release()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        try:
            self.tunnel = Server(("127.0.0.1", port), Handler)
        except OSError:
            raise BootError("bootstrap_tunnel_port_already_in_use") from None
        threading.Thread(target=self.tunnel.serve_forever, daemon=True).start()

    def close(self):
        if self.tunnel:
            self.tunnel.shutdown()
            self.tunnel.server_close()
        self.client.close()


def provider_instance_id(provider, value):
    """Validate retained provider IDs without configuring a provider client."""
    if provider == "lium":
        try:
            if not isinstance(value, str) or str(UUID(value)) != value.lower():
                raise ValueError
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise BootError("bootstrap_provider_instance_invalid") from None
    if (
        provider != "targon"
        or not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
    ):
        raise BootError("bootstrap_provider_instance_invalid")
    return value
