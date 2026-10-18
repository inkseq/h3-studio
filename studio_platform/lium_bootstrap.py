"""Explicit CPU-side boot/qualification for already-reserved single-GPU pods.

This module NEVER rents, deletes, changes a budget, or copies CPU credentials to
GPU hosts. ScaleCoordinator owns those actions. The controller must run beside
the CPU fleet on one durable host; its lock and receipts live in work_dir.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import select as io_select
import shlex
import socketserver
import threading
import time

from sqlalchemy import select

from .control import WorkerSpec
from .fleet import FleetConfig, FleetSupervisor, SlotConfig
from .lium_provider import InferenceIdleProof, _uuid
from .repository import Conflict, instance_intents
from .qualification_profiles import QUEUED_TASK_PROFILE
from .runtime_hosts.wangp_startup import (
    CODES as STARTUP_CODES,
    PHASES as STARTUP_PHASES,
    TYPES as STARTUP_TYPES,
)
from .worker import ComfyBackend, SubmissionRejected, _slot_lock


def provider_instance_id(provider, value):
    """Local intent UUIDs and remote provider identities are separate domains."""
    if provider == 'lium':
        return _uuid(value)
    if (
        provider != 'targon'
        or not isinstance(value, str)
        or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value)
    ):
        raise BootError('bootstrap_provider_instance_invalid')
    return value


def idle_proof_type(provider):
    if provider == 'lium':
        return InferenceIdleProof
    if provider == 'targon':
        from .targon_provider import TargonIdleProof

        return TargonIdleProof
    raise BootError('bootstrap_provider_unsupported')


REMOTE_ROOT = "/workspace/h3-studio"
MODEL_REVISION = "e5eb578a89295337b8ff433a035929ce0279e0b6"
COMFY_REVISION = "e9027f2b30f37bb3052714eb08fcf479542f4fc0"

# These are literals emitted by the pinned bootstrap, not arbitrary exception
# messages/classes from a remote machine. Unknown values are explicitly masked.
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


@dataclass(frozen=True)
class BootConfig:
    work_dir: Path
    source_dir: Path
    ssh_key_file: Path
    known_hosts_file: Path
    local_port: int
    configuration_id: str
    model_id: str = "MiniMax-H3-Base-BF16"
    min_gpu_bytes: int = 90 * 1024**3
    enabled: bool = False
    trust_first_host_key: bool = False
    smoke_enabled: bool = False
    fleet_enabled: bool = False
    recipe_ids: tuple[str, ...] = ("h3-base-fl2va-v1",)
    minimum_remaining_s: int = 1200
    qualification_profile: str = ""
    execution_backend: str = "comfy-worker"
    engine_manifest_digest: str = ""
    output_delivery: str = ""
    deployment_profile_id: str = ""
    runtime_python: str = "/opt/conda/bin/python"
    profile_slot_index: int = -1
    expected_host_gpus: int = 1
    provider: str = 'lium'

    def __post_init__(self):
        from .inference.outputs import validate_delivery_policy

        validate_delivery_policy(self.execution_backend, self.output_delivery)
        for field in ("work_dir", "source_dir", "ssh_key_file", "known_hosts_file"):
            if not Path(getattr(self, field)).is_absolute():
                raise ValueError("bootstrap_paths_must_be_absolute")
            object.__setattr__(self, field, Path(getattr(self, field)))
        if type(self.local_port) is not int or not 1024 <= self.local_port <= 65535:
            raise ValueError("invalid_bootstrap_local_port")
        for field in (
            "enabled",
            "trust_first_host_key",
            "smoke_enabled",
            "fleet_enabled",
        ):
            if type(getattr(self, field)) is not bool:
                raise ValueError("invalid_bootstrap_switch")
        expected_model = "MiniMax-H3-Base-BF16"
        if self.deployment_profile_id:
            from .runtime_catalog import get_profile

            expected_model = get_profile(self.deployment_profile_id)["model_id"]
            if (
                self.execution_backend != "wangp-worker"
                or self.output_delivery != "native-frames-v1"
            ):
                raise ValueError("bootstrap_profile_requires_native_wangp")
        if self.runtime_python not in {
            "/opt/conda/bin/python",
            "/venv/main/bin/python",
        }:
            raise ValueError("bootstrap_runtime_python_unsupported")
        if (
            self.provider not in {'lium', 'targon'}
            or self.provider == 'targon'
            and (
                not self.deployment_profile_id
                or self.execution_backend != 'wangp-worker'
            )
        ):
            raise ValueError('bootstrap_provider_unsupported')
        if (
            not self.deployment_profile_id
            and self.runtime_python != "/opt/conda/bin/python"
        ):
            raise ValueError("legacy_bootstrap_python_changed")
        if (
            type(self.expected_host_gpus) is not int
            or not 1 <= self.expected_host_gpus <= 8
            or type(self.profile_slot_index) is not int
            or self.deployment_profile_id
            and not 0 <= self.profile_slot_index < self.expected_host_gpus
            or not self.deployment_profile_id
            and (self.profile_slot_index != -1 or self.expected_host_gpus != 1)
        ):
            raise ValueError("bootstrap_explicit_slot_topology_required")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.configuration_id)
            or self.model_id != expected_model
            or type(self.min_gpu_bytes) is not int
            or self.min_gpu_bytes < 30 * 1024**3
            or type(self.minimum_remaining_s) is not int
            or not 120 <= self.minimum_remaining_s <= 7200
            or self.recipe_ids
            not in (("h3-base-fl2va-v1",), ("h3-base-fl2va-v1", "h3-base-ref2va-v1"))
            and not (
                self.execution_backend == "wangp-worker"
                and self.recipe_ids == ("h3-base-ref2va-v1",)
            )
        ):
            raise ValueError(
                "bootstrap_configuration_requires_explicit_fl2va_smoke_envelope"
            )
        if self.qualification_profile not in ("", QUEUED_TASK_PROFILE):
            raise ValueError("bootstrap_qualification_profile_invalid")
        if self.qualification_profile == QUEUED_TASK_PROFILE and self.smoke_enabled:
            raise ValueError("queued_task_profile_cannot_submit_synthetic_smoke")
        if self.execution_backend not in {"comfy-worker", "wangp-worker"}:
            raise ValueError("bootstrap_backend_invalid")
        if self.execution_backend == "wangp-worker":
            if (
                not re.fullmatch(r"[0-9a-f]{64}", self.engine_manifest_digest)
                or self.qualification_profile != QUEUED_TASK_PROFILE
                or self.recipe_ids
                not in (("h3-base-fl2va-v1",), ("h3-base-ref2va-v1",))
                or self.recipe_ids == ("h3-base-ref2va-v1",)
                and self.output_delivery != "native-frames-v1"
            ):
                raise ValueError("bootstrap_wangp_identity_required")
        elif self.engine_manifest_digest:
            raise ValueError("bootstrap_unexpected_engine_manifest")
        if (
            self.fleet_enabled
            and not self.smoke_enabled
            and self.qualification_profile != QUEUED_TASK_PROFILE
        ):
            raise ValueError("fleet_requires_successful_smoke")


class SSHHost:
    """Paramiko private-key use stays inside the library; no secret serialization."""

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

    def upload(self, files):
        self.run(
            "from pathlib import Path; import json; Path('/workspace/h3-studio').mkdir(parents=True,exist_ok=True); print(json.dumps({'ok':True}))"
        )
        with self.client.open_sftp() as sftp:
            for name, content in files.items():
                if name not in {"bootstrap_cloud.py", "model_manifest.json"}:
                    raise BootError("bootstrap_upload_file_not_allowlisted")
                target = REMOTE_ROOT + "/" + name
                try:
                    with sftp.open(target, "rb") as remote:
                        existing = remote.read(len(content) + 1)
                    if existing != content:
                        raise BootError("bootstrap_existing_source_mismatch")
                except FileNotFoundError:
                    with sftp.open(target, "wx") as remote:
                        remote.write(content)

    def start(self, identity):
        # Persistent marker is created before Popen. A lost launch result never
        # causes this method to be called again by BootController.
        script = '''import fcntl,json,os,subprocess,sys
from pathlib import Path
root=Path('/workspace/h3-studio')
with (root/'sixnine-bootstrap.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 marker=root/'sixnine-bootstrap-identity.json'
 expected=IDENTITY
 if marker.exists():
  if json.loads(marker.read_text())!=expected: raise RuntimeError('identity_conflict')
  print(json.dumps({'state':'already_reserved'}))
 else:
  # Provider images may expose PEP 668 managed system Python. Reuse their
  # installed CUDA/Torch via system-site-packages in our dedicated environment.
  python=root/'.venv/bin/python'
  if not python.exists():
   subprocess.check_call([sys.executable,'-m','venv','--system-site-packages',str(root/'.venv')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  with marker.open('x') as out:
   json.dump(expected,out);out.flush();os.fsync(out.fileno())
  with (root/'bootstrap-controller.log').open('ab') as log:
   # Lium image /workspace is its small overlay. The encrypted local volume
   # lives below /root; bootstrap still checks remaining space before download.
   proc=subprocess.Popen([str(python),'-u',str(root/'bootstrap_cloud.py'),'--cache-dir','/root/hf-cache'],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  print(json.dumps({'state':'started','pid':proc.pid}))
'''.replace("IDENTITY", repr(identity))
        return self.run(script)

    def preparation_idle_report(self):
        """Read-only proof for failed setup; never assumes absent files are idle.

        Both complete process visibility and listener inspection are required.
        Race/unreadable/malformed observations are inconclusive, and no command
        lines, environment, socket addresses or error bodies leave the host.
        """
        script = '''import json,re,time,uuid
from pathlib import Path
root=Path('/workspace/h3-studio');proc=Path('/proc')
result={'identity':{},'state':'unknown','process_visibility_complete':False,
 'bootstrap_process_count':None,'comfy_process_count':None,'comfy_port_listening':None}
def read(name,maximum=16384):
 p=root/name
 if not p.is_file() or p.is_symlink() or p.stat().st_size>maximum: raise ValueError('unconfirmed')
 value=json.loads(p.read_text())
 if not isinstance(value,dict): raise ValueError('unconfirmed')
 return value
def pids():
 return {entry.name for entry in proc.iterdir() if entry.name.isdecimal() and entry.is_dir()}
try:
 identity=read('sixnine-bootstrap-identity.json')
 if set(identity)!={'intent_id','instance_id','configuration_id','sources'}: raise ValueError('unconfirmed')
 for field in ('intent_id','instance_id'):
  value=identity[field]
  if not isinstance(value,str) or str(uuid.UUID(value))!=value: raise ValueError('unconfirmed')
 if not isinstance(identity['configuration_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',identity['configuration_id']): raise ValueError('unconfirmed')
 sources=identity['sources']
 if not isinstance(sources,dict) or set(sources)!={'bootstrap_cloud.py','model_manifest.json'} or not all(isinstance(v,str) and re.fullmatch(r'[0-9a-f]{64}',v) for v in sources.values()): raise ValueError('unconfirmed')
 status=read('setup-status.json',4194304)
 result['identity']=identity
 result['state']=status.get('state') if status.get('state') in ('failed','preparing','weights_ready','ready') else 'unknown'
 before=pids()
 if not before: raise ValueError('unconfirmed')
 bootstrap=comfy=0
 for pid in sorted(before):
  with (proc/pid/'cmdline').open('rb') as handle: raw=handle.read(1048577)
  if len(raw)>1048576: raise ValueError('unconfirmed')
  args=raw.split(b'\\0')
  # Match argv paths, not substrings inside this inspection's python -c code.
  bootstrap+=int(any(a==b'bootstrap_cloud.py' or a.endswith(b'/bootstrap_cloud.py') for a in args))
  comfy+=int(any(a==b'ComfyUI/main.py' or a.endswith(b'/ComfyUI/main.py') for a in args))
 listening=False
 for name in ('tcp','tcp6'):
  with (proc/'net'/name).open('r',encoding='ascii') as handle: rows=handle.read(4194305)
  if len(rows)>4194304: raise ValueError('unconfirmed')
  lines=rows.splitlines()
  if not lines or 'local_address' not in lines[0] or 'st' not in lines[0].split(): raise ValueError('unconfirmed')
  for line in lines[1:]:
   fields=line.split()
   if len(fields)<10 or not re.fullmatch(r'[0-9A-Fa-f]+:[0-9A-Fa-f]{4}',fields[1]) or not re.fullmatch(r'[0-9A-Fa-f]{2}',fields[3]): raise ValueError('unconfirmed')
   listening=bool(listening or (int(fields[1].rsplit(':',1)[1],16)==8188 and fields[3].upper()=='0A'))
 if pids()!=before: raise ValueError('unconfirmed')
 result.update(process_visibility_complete=True,bootstrap_process_count=bootstrap,comfy_process_count=comfy,comfy_port_listening=listening)
except Exception:
 pass
result['observed_at']=time.time()
print(json.dumps(result))
'''
        return self.run(script, limit=16384)

    def report(self):
        script = (
            '''import json,subprocess
from pathlib import Path
r=Path('/workspace/h3-studio')
def read(name):
 p=r/name
 return json.loads(p.read_text()) if p.exists() and p.stat().st_size<4194304 else {}
s=read('setup-status.json');runtime=read('runtime-after.json')
codes=SAFE_CODES;types=SAFE_TYPES;phases=SAFE_PHASES
def static(value,allowed,fallback):
 return value if isinstance(value,str) and value in allowed else fallback
events=s.get('events',[]);events=events if isinstance(events,list) else []
prior=next((e.get('phase') for e in reversed(events) if isinstance(e,dict) and e.get('phase') in phases and e.get('phase')!='failed'),'unknown')
details=[]
for event in events:
 if not isinstance(event,dict) or event.get('state')!='failed': continue
 entry={'phase':static(event.get('phase'),phases,'unknown')}
 if 'error_code' in event: entry['error_code']=static(event.get('error_code'),codes,'UnclassifiedBootstrapFailure')
 if 'error_type' in event: entry['error_type']=static(event.get('error_type'),types,'UnknownSetupError')
 details.append(entry)
result={'identity':read('sixnine-bootstrap-identity.json'),'state':s.get('state'),'phase':static(s.get('phase'),phases,'unknown'),'model_revision':s.get('model_revision'),'comfyui_revision':s.get('comfyui_revision'),'files':s.get('files',{}),'runtime':runtime,
 'error_code':static(s.get('error_code'),codes,'UnclassifiedBootstrapFailure'),'error_type':static(s.get('error_type'),types,'UnknownSetupError'),'failure_phase':prior,'failure_details':details[-5:]}
if result['state']=='ready':
 result['actual_comfy_revision']=subprocess.check_output(['git','-C',str(r/'ComfyUI'),'rev-parse','HEAD'],text=True).strip()
 rows=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,memory.total,name','--format=csv,noheader,nounits'],text=True).strip().splitlines()
 result['gpus']=[{'uuid':x.split(',')[0].strip(),'memory_mib':int(x.split(',')[1].strip()),'name':','.join(x.split(',')[2:]).strip()} for x in rows]
print(json.dumps(result))
'''.replace("SAFE_CODES", repr(sorted(BOOT_FAILURE_CODES)))
            .replace("SAFE_TYPES", repr(sorted(BOOT_FAILURE_TYPES)))
            .replace("SAFE_PHASES", repr(sorted(BOOT_PHASES)))
        )
        return self.run(script)

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


class BootController:
    def __init__(
        self,
        repository,
        provider,
        config: BootConfig,
        *,
        ssh_factory=SSHHost,
        backend_factory=ComfyBackend,
        fleet_factory=FleetSupervisor,
        verify_smoke=None,
    ):
        self.repo, self.provider, self.config = repository, provider, config
        self.ssh_factory, self.backend_factory, self.fleet_factory = (
            ssh_factory,
            backend_factory,
            fleet_factory,
        )
        self.verify_smoke = verify_smoke or self._verify_smoke
        self.host, self.backend, self.fleet = None, None, None
        self.bound_intent = None
        self.bound_instance = None
        self.idle_since = None
        self._preparation = None
        self._preparation_identity = None
        self._setup_dispatched = False
        self.start_guard = None

    def enable_pollable_upload(self):
        """Production WanGP preparation only; preserve historical CLI behavior."""
        if self.config.execution_backend != "wangp-worker":
            raise BootError("pollable_staging_requires_wangp")
        if self._preparation is None:
            from .bootstrap_staging import PollableUpload

            self._preparation = PollableUpload()

    def preparation_pending(self):
        return self._preparation is not None and self._preparation.pending()

    def cancel_preparation(self):
        if self._preparation is not None:
            self._preparation.cancel()

    def preparation_status(self):
        return self._preparation.snapshot() if self._preparation is not None else {}

    def preparation_stopped_before_start(self, state):
        """Only the same controller's stopped upload can prove setup was never sent."""
        if (
            self._preparation is None
            or self._setup_dispatched
            or self._preparation_identity is None
            or state.get("identity") != self._preparation_identity
            or state.get("phase")
            not in {"staging", "staged", "staging_failed", "staging_cancelled"}
        ):
            return None
        return self._preparation.stopped_for(self.bound_intent)

    def record_preparation_stop(self, receipt, state):
        proof = self.preparation_stopped_before_start(state)
        if proof is None:
            return False
        phase = proof["state"]
        if phase == "staged" and proof["cancel_requested"]:
            phase = "staging_cancelled"
        if phase not in {"staging_failed", "staging_cancelled"}:
            return False
        if state.get("phase") != phase:
            state.update(phase=phase, staging={**proof, "state": phase})
            self._save(receipt, state)
        return True

    def _start_allowed(self, intent_id):
        if self.start_guard is not None and self.start_guard() is not True:
            return False
        with self.repo.engine.connect() as conn:
            intent = (
                conn.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
        return (
            intent["provider_instance_id"] == self.bound_instance
            and intent["state"] in {"starting", "ready", "busy"}
            and intent["hard_deadline"] - self.repo.clock()
            >= self.config.minimum_remaining_s
        )

    def _save(self, path, state):
        state["updated_at"] = self.repo.clock()
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    def status(self, intent_id=None):
        """CPU-only retained failure lookup, also usable after pod destruction."""
        intent_id = intent_id or self.bound_intent
        _uuid(intent_id)
        path = self.config.work_dir / intent_id / "bootstrap-status.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            return {"state": "bootstrap_diagnosis_unavailable"}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "bootstrap_diagnosis_unavailable"}
        if not isinstance(value, dict) or value.get("state") != "bootstrap_failed":
            return {"state": "bootstrap_diagnosis_unavailable"}
        return {"state": "bootstrap_failed", **safe_bootstrap_diagnosis(value)}

    def _sources(self):
        if self.config.execution_backend == "wangp-worker":
            from .wangp_bootstrap import read_sources

            return read_sources(self.config)
        files = {}
        for name, maximum in (
            ("bootstrap_cloud.py", 512 * 1024),
            ("model_manifest.json", 64 * 1024),
        ):
            path = self.config.source_dir / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
                raise BootError("bootstrap_source_missing_or_untrusted")
            files[name] = path.read_bytes()
        manifest = json.loads(files["model_manifest.json"])
        if (
            manifest.get("revision") != MODEL_REVISION
            or manifest.get("comfyui_revision") != COMFY_REVISION
            or manifest.get("repository") != "Comfy-Org/MiniMax-H3"
            or len(manifest.get("files", [])) != 5
        ):
            raise BootError("bootstrap_manifest_revision_mismatch")
        return files, manifest

    def _identity(self, intent, files):
        value = {
            "intent_id": intent["id"],
            "instance_id": intent["provider_instance_id"],
            "configuration_id": self.config.configuration_id,
            "sources": {
                name: hashlib.sha256(data).hexdigest() for name, data in files.items()
            },
        }
        if self.config.provider != 'lium':
            value['provider'] = self.config.provider
            value['hard_deadline'] = intent['hard_deadline']
        if self.config.execution_backend == "wangp-worker":
            value.update(
                backend="wangp-worker",
                engine_manifest_digest=self.config.engine_manifest_digest,
            )
        if self.config.output_delivery:
            value["output_delivery"] = self.config.output_delivery
        if self.config.deployment_profile_id:
            value["deployment_profile_id"] = self.config.deployment_profile_id
            value["runtime_python"] = self.config.runtime_python
            value["profile_slot_index"] = self.config.profile_slot_index
            value["expected_host_gpus"] = self.config.expected_host_gpus
        return value

    def _connect_backend(self, intent, directory, state):
        self.host.open_tunnel(self.config.local_port)
        if self.backend is None:
            if self.config.execution_backend == "wangp-worker":
                from .wangp_bootstrap import connect_backend

                self.backend = connect_backend(self, intent, directory, state)
            else:
                endpoint = f"http://127.0.0.1:{self.config.local_port}"
                self.backend = self.backend_factory(
                    endpoint=endpoint,
                    enabled=True,
                    allowed_origins=(endpoint,),
                    comfy_revision=COMFY_REVISION,
                )

    def _slot(self, intent, report, directory):
        if self.config.execution_backend == "wangp-worker":
            from .wangp_bootstrap import make_slot

            return make_slot(self.config, intent, report, directory)
        endpoint = f"http://127.0.0.1:{self.config.local_port}"
        spec = WorkerSpec(
            "lium-" + intent["id"].replace("-", ""),
            intent["pool"],
            "lium",
            intent["provider_instance_id"],
            (report["gpus"][0]["uuid"],),
            self.config.recipe_ids,
            self.config.model_id,
            self.config.configuration_id,
        )
        return SlotConfig(spec, True, endpoint, (endpoint,), COMFY_REVISION, True)

    def tick(self, intent_id):
        if not self.config.enabled:
            return {"state": "disabled", "provider_calls_enabled": False}
        _uuid(intent_id)
        if self.bound_intent not in (None, intent_id):
            raise BootError("bootstrap_controller_is_bound_to_one_intent")
        self.bound_intent = intent_id
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        with _slot_lock(self.config.work_dir, "bootstrap-" + intent_id) as acquired:
            if not acquired:
                return {"state": "bootstrap_locked"}
            with self.repo.engine.connect() as conn:
                intent = (
                    conn.execute(
                        select(instance_intents).where(
                            instance_intents.c.id == intent_id
                        )
                    )
                    .mappings()
                    .first()
                )
            if (
                intent is None
                or intent["provider"] != self.config.provider
                or not intent["provider_instance_id"]
                or intent["physical_gpus"] != self.config.expected_host_gpus
                or self.config.profile_slot_index >= intent["slots"]
            ):
                raise BootError(
                    "bootstrap_requires_reserved_single_gpu_lium_intent"
                    if self.config.provider == 'lium'
                    else 'bootstrap_requires_reserved_provider_intent'
                )
            provider_instance_id(self.config.provider, intent['provider_instance_id'])
            self.bound_instance = intent["provider_instance_id"]
            if intent["state"] in {"draining", "destroying", "destroyed"}:
                if self.fleet:
                    self.fleet.drain()
                return {"state": "instance_not_admitting"}
            if intent["state"] not in {"starting", "ready", "busy"}:
                return {"state": "instance_not_confirmed"}
            if (
                intent["hard_deadline"] - self.repo.clock()
                < self.config.minimum_remaining_s
            ):
                if self.fleet:
                    self.fleet.drain()
                return {"state": "bootstrap_deadline_insufficient"}
            files, manifest = self._sources()
            identity = self._identity(intent, files)
            directory = self.config.work_dir / intent_id
            directory.mkdir(exist_ok=True)
            receipt = directory / "bootstrap-state.json"
            state = (
                json.loads(receipt.read_text(encoding="utf-8"))
                if receipt.exists()
                else {
                    "identity": identity,
                    "phase": "reserved",
                    "tag": "boot-" + intent_id.replace("-", ""),
                    "created_at": self.repo.clock(),
                    "local_port": self.config.local_port,
                }
            )
            if (
                state.get("identity") != identity
                or state.get("local_port") != self.config.local_port
            ):
                raise BootError("bootstrap_receipt_identity_conflict")
            queued_task = self.config.qualification_profile == QUEUED_TASK_PROFILE
            if receipt.exists():
                if (
                    queued_task
                    and state.get("qualification_profile") != QUEUED_TASK_PROFILE
                ):
                    raise BootError(
                        "bootstrap_qualification_profile_change_requires_new_configuration"
                    )
                if (
                    not queued_task
                    and state.get("qualification_profile") == QUEUED_TASK_PROFILE
                ):
                    raise BootError(
                        "bootstrap_qualification_profile_change_requires_new_configuration"
                    )
            elif queued_task:
                state["qualification_profile"] = QUEUED_TASK_PROFILE
            if state["phase"] == "bootstrap_failed":
                # A retained failed attempt is terminal. Changing a marker or
                # restarting remote setup requires separate audited recovery.
                return {
                    "state": "bootstrap_failed",
                    **safe_bootstrap_diagnosis(state.get("failure")),
                }
            if state["phase"] in {"staging_failed", "staging_cancelled"}:
                return {"state": state["phase"], "phase": "staging_dependencies"}
            if state["phase"] in {"staging", "staged"} and self._preparation is None:
                return {"state": "staging_recovery_required"}
            if not self.host:
                coordinates = self.provider.ssh_connection(
                    intent_id, intent["provider_instance_id"]
                )
                self.host = self.ssh_factory(self.config, coordinates)
            if self._preparation is not None and state["phase"] in {
                "reserved",
                "staging",
            }:
                allowed = self._start_allowed(intent_id)
                if not allowed:
                    self.cancel_preparation()
                # Persist before the upload thread touches remote source bytes.
                if state["phase"] == "reserved":
                    state.update(phase="staging", staging_started_at=self.repo.clock())
                    self._save(receipt, state)
                self._preparation_identity = identity
                result = self._preparation.poll(
                    directory,
                    intent_id,
                    lambda **options: self.host.upload(files, **options),
                )
                if not allowed:
                    if self.record_preparation_stop(receipt, state):
                        return {
                            "state": state["phase"],
                            "phase": "staging_dependencies",
                        }
                    return {**result, "state": "staging_authority_unavailable"}
                state["staging"] = result
                if self.preparation_pending():
                    # The thread publishes its terminal state just before it
                    # exits; do not dispatch setup or certify stop in that gap.
                    self._save(receipt, state)
                    return {**result, "state": "staging"}
                if result["state"] in {"staged", "staging_failed", "staging_cancelled"}:
                    state["phase"] = result["state"]
                self._save(receipt, state)
                if result["state"] != "staged":
                    return result
            if state["phase"] == "reserved":
                self.host.upload(files)
                state["phase"] = "staged"
            if state["phase"] == "staged":
                # Upload completion is not permission to start. Ownership,
                # revocation, deadline and cancellation may have changed.
                if not self._start_allowed(intent_id):
                    self.cancel_preparation()
                    if self.record_preparation_stop(receipt, state):
                        return {
                            "state": state["phase"],
                            "phase": "staging_dependencies",
                        }
                    self._save(receipt, state)
                    return {"state": "bootstrap_start_not_authorized"}
                state["phase"] = "bootstrap_starting"
                self._save(receipt, state)
                self._setup_dispatched = True
                try:
                    self.host.start(identity)
                    state["phase"] = "booting"
                    self._save(receipt, state)
                except Exception:
                    return {"state": "bootstrap_start_unknown"}
            report = self.host.report()
            if report.get("identity") != identity:
                return {"state": "bootstrap_start_unknown"}
            if (
                report.get("state") in {"unknown", "reconcile_required"}
                or report.get("phase") == "runtime_start_unknown"
            ):
                # A dispatched process may still exist. Surface the hold while
                # retaining the original journal; never relaunch or call this a
                # confirmed failure/stop merely because readiness is unknown.
                return {
                    "state": "bootstrap_reconciliation_required",
                    **safe_bootstrap_diagnosis(report),
                }
            if report.get("state") == "failed":
                state["phase"] = "bootstrap_failed"
                state["failure"] = safe_bootstrap_diagnosis(report)
                self._save(receipt, state)
                result = {"state": "bootstrap_failed", **state["failure"]}
                # Durable, small status survives future draining/aggregate
                # status updates and contains no remote logs or media paths.
                self._save(
                    directory / "bootstrap-status.json",
                    {
                        **result,
                        "intent_id": intent_id,
                        "instance_id": intent["provider_instance_id"],
                        "observed_at": self.repo.clock(),
                    },
                )
                return result
            if report.get("state") != "ready":
                return {
                    "state": "booting",
                    "phase": _static(report.get("phase"), BOOT_PHASES, "unknown"),
                }
            self._validate_report(report, manifest)
            state["hardware"] = {
                "gpu": report["gpus"][0],
                "runtime": report["runtime"],
                "model_revision": MODEL_REVISION,
                "comfy_revision": COMFY_REVISION,
                "weight_verification": "pinned_cache_revision_and_exact_sizes_not_full_rehash",
            }
            if self.config.execution_backend == "wangp-worker":
                state["hardware"] = {
                    "gpu": report["gpus"][0],
                    "runtime": report["runtime"],
                    "engine_manifest_digest": self.config.engine_manifest_digest,
                    "source_revision": manifest["source_revision"],
                    "weight_verification": "pinned_sha256",
                }
            self._connect_backend(intent, directory, state)
            if not self.config.smoke_enabled and not queued_task:
                if state["phase"] not in {
                    "qualified",
                    "fleet_starting",
                    "fleet_started",
                }:
                    state["phase"] = "ready_for_qualification"
                self._save(receipt, state)
                return {
                    "state": "ready_for_qualification",
                    "generation_verified": False,
                }
            if queued_task:
                result = self._queued_task_runtime(directory, receipt, state)
                if result["state"] != "runtime_ready":
                    return result
            else:
                result = self._smoke(directory, receipt, state)
                if result["state"] != "qualified":
                    return result
                additional = self._additional_qualification(directory, state)
                if additional["state"] != "qualified":
                    return additional
                result.update({k: v for k, v in additional.items() if k != "state"})
            if not self.config.fleet_enabled:
                return result
            if self.fleet is None:
                if state["phase"] in {"fleet_starting", "fleet_started"}:
                    return {
                        "state": "fleet_recovery_required",
                        "generation_verified": not queued_task,
                    }
                worker_id = "lium-" + intent_id.replace("-", "")
                endpoint = f"http://127.0.0.1:{self.config.local_port}"
                slot = self._slot(intent, report, directory)
                spec = slot.spec
                config = FleetConfig(directory / "fleet", (slot,), True, 1)
                cfg_path = directory / "fleet.json"
                value = {
                    "version": 1,
                    "work_dir": str(config.work_dir),
                    "enabled": True,
                    "max_children": 1,
                    "shutdown_grace_s": config.shutdown_grace_s,
                    "slots": [
                        {
                            **asdict(spec),
                            "enabled": True,
                            "endpoint": endpoint,
                            "allowed_origins": [endpoint],
                            "comfy_revision": COMFY_REVISION,
                            "confirmed_idle": True,
                        }
                    ],
                }
                if self.config.execution_backend == "wangp-worker":
                    value["version"] = 2
                    value["slots"][0].update(
                        comfy_revision="", runtime_config_file=slot.runtime_config_file
                    )
                if not spec.output_delivery:
                    value["slots"][0].pop("output_delivery")
                if spec.dispatch_backend == "legacy":
                    value["slots"][0].pop("dispatch_backend")
                cfg_path.write_text(json.dumps(value), encoding="utf-8")
                self.fleet = self.fleet_factory(config, self.repo, cfg_path)
                state["fleet_recipe_ids"] = list(self.config.recipe_ids)
                state["phase"] = "fleet_starting"
                self._save(receipt, state)
                self.fleet.start()
                state["phase"] = "fleet_started"
                self._save(receipt, state)
            fleet_status = self.fleet.tick()
            attention = any(
                child.get("state") == "exited"
                for child in fleet_status.get("children", [])
            )
            return {
                **(result if queued_task else {}),
                "state": "fleet_attention_required" if attention else "fleet_running",
                "fleet": fleet_status,
                "generation_verified": not queued_task,
                "qualification_scope": "runtime_ready_awaiting_real_task"
                if queued_task
                else "single_host_fl2va_4s_480p_audio_smoke_only",
            }

    def _queued_task_runtime(self, directory, receipt, state):
        """Prove startup identity and idle endpoint, without an inference POST.

        This contract permits the normal queue runner to validate real work.
        It cannot be used to import or skip a historical synthetic submission.
        """
        if (
            state.get("qualification_profile") != QUEUED_TASK_PROFILE
            or state.get("smoke_submission_started") is not None
            or state.get("smoke_task_id") is not None
            or state.get("evidence") is not None
            or state.get("phase")
            not in {
                "reserved",
                "bootstrap_starting",
                "booting",
                "runtime_ready",
                "fleet_starting",
                "fleet_started",
            }
        ):
            raise BootError("queued_task_runtime_receipt_conflict")
        for name in (
            "reference-smoke",
            "firstlast4-768p-5s-v1",
            "ref4-bounded-768p-5s-v1",
        ):
            if (directory / name / "state.json").exists():
                raise BootError("queued_task_runtime_receipt_conflict")
        existing = state.get("runtime_validation")
        if existing is not None and existing != {
            "profile": QUEUED_TASK_PROFILE,
            "state": "runtime_ready",
            "generation_verified": False,
        }:
            raise BootError("queued_task_runtime_receipt_conflict")
        # Once the fleet owns this endpoint, its normal attempt/fence controls
        # are authoritative. Never mistake its live real task for foreign work.
        if state["phase"] not in ("fleet_starting", "fleet_started"):
            if self.config.execution_backend == "wangp-worker":
                idle = self.backend.is_idle() is True
            else:
                queue = self.backend._json("GET", "/queue")
                idle = (
                    isinstance(queue, dict)
                    and queue.get("queue_running") == []
                    and queue.get("queue_pending") == []
                )
            if not idle:
                return {"state": "runtime_upstream_busy", "generation_verified": False}
            state["phase"] = "runtime_ready"
        elif existing is None or state.get("fleet_recipe_ids") != list(
            self.config.recipe_ids
        ):
            raise BootError("queued_task_runtime_receipt_conflict")
        state["runtime_validation"] = {
            "profile": QUEUED_TASK_PROFILE,
            "state": "runtime_ready",
            "generation_verified": False,
        }
        self._save(receipt, state)
        return {
            "state": "runtime_ready",
            "generation_verified": False,
            "awaiting_real_task": True,
            "qualification_profile": QUEUED_TASK_PROFILE,
            "qualification_scope": "runtime_ready_awaiting_real_task",
        }

    def _additional_qualification(self, directory, state):
        """Historical base smoke remains unchanged; production overrides this."""
        if "h3-base-ref2va-v1" not in self.config.recipe_ids:
            return {"state": "qualified"}
        if state["phase"] in {"fleet_starting", "fleet_started"}:
            if state.get("fleet_recipe_ids", ["h3-base-fl2va-v1"]) != list(
                self.config.recipe_ids
            ):
                raise BootError(
                    "fleet_recipe_change_requires_explicit_drain_and_new_configuration"
                )
            receipt = directory / "reference-smoke" / "state.json"
            if (
                not receipt.exists()
                or json.loads(receipt.read_text()).get("phase") != "qualified"
            ):
                raise BootError(
                    "fleet_reference_evidence_missing_requires_reconciliation"
                )
        from .lium_reference_smoke import ReferenceSmoke

        result = ReferenceSmoke(
            self.backend, self.repo.clock, self._save, self.verify_smoke
        ).tick(directory, state)
        return (
            {"state": "qualified", "reference_evidence": result["evidence"]}
            if result["state"] == "qualified"
            else result
        )

    def _validate_report(self, report, manifest):
        if self.config.execution_backend == "wangp-worker":
            from .wangp_bootstrap import validate_report

            return validate_report(self.config, report, manifest)
        if (
            report.get("model_revision") != MODEL_REVISION
            or report.get("comfyui_revision") != COMFY_REVISION
            or report.get("actual_comfy_revision") != COMFY_REVISION
        ):
            raise BootError("bootstrap_runtime_revision_mismatch")
        for item in manifest["files"]:
            actual = report.get("files", {}).get(item["path"], {})
            if (
                actual.get("state") != "verified_size"
                or actual.get("revision") != MODEL_REVISION
                or actual.get("size_bytes") != item["size_bytes"]
            ):
                raise BootError("bootstrap_weights_not_verified")
        gpus = report.get("gpus", [])
        runtime = report.get("runtime", {})
        if (
            not isinstance(gpus, list)
            or len(gpus) != 1
            or not isinstance(gpus[0], dict)
            or not re.fullmatch(
                r"GPU-[A-Za-z0-9-]{8,100}", str(gpus[0].get("uuid", ""))
            )
            or type(runtime.get("gpu_total_bytes")) is not int
            or runtime["gpu_total_bytes"] < self.config.min_gpu_bytes
        ):
            raise BootError("bootstrap_gpu_identity_or_memory_mismatch")

    def _smoke(self, directory, receipt, state):
        from comfy_workflow import build_workflow

        request = {
            "mode": "fl",
            "prompt": "A red ceramic teapot on a wooden table, slow cinematic camera move, gentle ambient sound.",
            "duration": 4,
            "resolution": "480P",
            "aspect_ratio": "16:9",
            "steps": 4,
            "seed": "12345",
            "generate_audio": True,
            "video_decode": "tiled",
            "encoder_device": "cpu",
            "_job_id": state["tag"],
        }
        if state["phase"] in {"qualified", "fleet_starting", "fleet_started"}:
            return {
                "state": "qualified",
                "generation_verified": True,
                "evidence": state["evidence"],
            }
        if not state.get("smoke_submission_started"):
            queue = self.backend._json("GET", "/queue")
            if queue.get("queue_running") != [] or queue.get("queue_pending") != []:
                return {"state": "qualification_upstream_busy"}
            graph = build_workflow(request, {}, {})
            state.update(
                smoke_submission_started=self.repo.clock(), phase="smoke_submitting"
            )
            self._save(receipt, state)
            try:
                state["smoke_task_id"] = self.backend.submit(graph, state["tag"])
                state["phase"] = "smoke_running"
                self._save(receipt, state)
            except SubmissionRejected:
                state["phase"] = "qualification_failed"
                self._save(receipt, state)
                return {"state": "qualification_failed"}
            except Exception:
                return {"state": "smoke_submission_unknown"}
        if state["phase"] == "qualification_failed":
            return {"state": "qualification_failed"}
        task_id = state.get("smoke_task_id")
        outcome = (
            self.backend.poll(state["tag"], task_id)
            if task_id
            else self.backend.reconcile(state["tag"])
        )
        if outcome.task_id and not task_id:
            state["smoke_task_id"] = task_id = outcome.task_id
            self._save(receipt, state)
        if outcome.state in {"failed", "cancelled"}:
            state["phase"] = "qualification_failed"
            self._save(receipt, state)
            return {"state": "qualification_failed"}
        if outcome.state != "succeeded" or not task_id:
            return {
                "state": "smoke_running"
                if outcome.state == "running"
                else "smoke_submission_unknown"
            }
        paths = self.backend.fetch(
            {"request": {"request": request}},
            state["tag"],
            task_id,
            directory,
            lambda: None,
        )
        state["evidence"] = self.verify_smoke(paths, request)
        state["evidence"]["elapsed_wall_seconds"] = (
            self.repo.clock() - state["smoke_submission_started"]
        )
        state["evidence"]["completed_at"] = self.repo.clock()
        state["phase"] = "qualified"
        self._save(receipt, state)
        return {
            "state": "qualified",
            "generation_verified": True,
            "evidence": state["evidence"],
        }

    @staticmethod
    def _verify_smoke(paths, request):
        from .media import inspect
        from .worker import _validate_video, _validate_audio
        from comfy_workflow import native_output_spec

        spec = native_output_spec(request)
        video, audio = (
            inspect(paths["video"], "video"),
            inspect(paths["audio"], "audio"),
        )
        if (
            video["width"] != spec["width"]
            or video["height"] != spec["height"]
            or abs(video["duration"] - spec["actual_duration"]) > 0.1
            or abs(audio["duration"] - spec["actual_duration"]) > 0.1
        ):
            raise BootError("qualification_media_shape_mismatch")
        _validate_video(
            paths["video"], spec["width"], spec["height"], video["duration"], True
        )
        _validate_audio(paths["audio"], audio["duration"], flac=True)
        evidence = {
            "request": {k: v for k, v in request.items() if k != "prompt"},
            "scope": "single_host_fl2va_smoke_not_ref_or_quality_benchmark",
            "outputs": {},
        }
        for kind, path in paths.items():
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            evidence["outputs"][kind] = {
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
                "metadata": video if kind == "video" else audio,
            }
        return evidence

    def idle_probe(self, tag, instance_id):
        if (
            tag != self.bound_intent
            or instance_id != self.bound_instance
            or self.backend is None
        ):
            raise BootError("bootstrap_idle_probe_not_bound")
        now = self.repo.clock()
        if self.config.execution_backend == "wangp-worker":
            idle = self.backend.is_idle() is True
        else:
            queue = self.backend._json("GET", "/queue")
            idle = queue.get("queue_running") == [] and queue.get("queue_pending") == []
        self.idle_since = (
            (self.idle_since if self.idle_since is not None else now) if idle else None
        )
        return idle_proof_type(self.config.provider)(
            instance_id, now, self.idle_since or now, idle
        )

    def close(self):
        # Graceful CPU-side shutdown never terminates a GPU pod or unknown job.
        if self.fleet:
            self.fleet.shutdown()
        if self.backend:
            self.backend.close()
        if self.host:
            self.host.close()


def main(argv=None):
    """An explicit operator entrypoint; the default touches no config or DB."""
    import argparse
    import signal
    from .lium_provider import LiumProvider
    from .repository import Repository
    from .settings import Settings

    parser = argparse.ArgumentParser(
        description="Bootstrap already-reserved Lium pod; never rents or deletes"
    )
    parser.add_argument("--mode", choices=("disabled", "run"), default="disabled")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--intent-id")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args(argv)
    if args.mode == "disabled":
        print(json.dumps({"state": "disabled", "provider_calls_enabled": False}))
        return 0
    controller = repo = provider = None
    stop = threading.Event()
    previous = {}
    try:
        if (
            not args.config
            or not args.config.is_absolute()
            or args.config.stat().st_size > 65536
            or not 1 <= args.interval <= 60
        ):
            raise ValueError("bootstrap_operator_config_required")
        raw = json.loads(args.config.read_text(encoding="utf-8"))
        if "recipe_ids" in raw:
            raw["recipe_ids"] = tuple(raw["recipe_ids"])
        config = BootConfig(**raw)
        if args.once and config.fleet_enabled:
            raise ValueError("fleet_requires_persistent_controller")
        settings = Settings.from_environment()
        repo = Repository(settings.database_url)
        # Existing ledger required: no DDL or budget/instance creation here.
        provider = LiumProvider(enabled=True)
        controller = BootController(repo, provider, config)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stop.set())
        last = None
        while not stop.is_set():
            value = controller.tick(args.intent_id)
            # Print a small non-secret progress state, never config/DB/SSH keys.
            public = {
                k: value[k]
                for k in (
                    "state",
                    "phase",
                    "failure_phase",
                    "error_code",
                    "error_type",
                    "generation_verified",
                )
                if k in value
            }
            if public != last:
                print(json.dumps(public), flush=True)
                last = public
            if args.once:
                break
            stop.wait(args.interval)
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "state": "bootstrap_configuration_or_runtime_error",
                    "cloud_creation_enabled": False,
                }
            )
        )
        return 1
    finally:
        if controller:
            controller.close()
        if provider:
            provider.close()
        if repo:
            repo.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
