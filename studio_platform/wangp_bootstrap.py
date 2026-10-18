"""WanGP strategy for the existing identity-bound boot controller.

Only an approved initial boot installs or starts. Reconnect reads the original
marker, token and runtime incarnation. No cloud credentials cross this boundary.
"""

from dataclasses import asdict
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from .lium_bootstrap import (
    BootError,
    SSHHost,
    REMOTE_ROOT,
    safe_bootstrap_diagnosis,
    BOOT_PHASES,
)
from .control import WorkerSpec
from .fleet import SlotConfig
from .inference.wangp_contract import EngineManifest, HostReadiness
from .inference.wangp_factory import create_backend, read_document
from .inference.wangp_http import HTTPWanGPTransport
from .runtime_hosts.wangp_http import private_token_file

SOURCE_NAMES = {
    "wangp-bootstrap.py",
    "wangp-manifest.json",
    "wangp-runtime.json",
    "wangp-package.tar.gz",
}
DEPENDENCY_NAME = 'wangp-dependencies.tar.gz'
MAX_DEPENDENCY_BYTES = 32 * 1024**3
DEPENDENCY_TRANSFER_SECONDS = 1800
SFTP_OPEN_SECONDS = 30
SFTP_IO_SECONDS = 60


def safe_preparation_report(report):
    """Static phases and numeric source evidence only, including older receipts.

    Completed-file bytes are not an in-flight network counter: Xet can preallocate
    incomplete files, so stat sizes must never manufacture transfer progress.
    """
    if not isinstance(report, dict):
        return None
    phase = report.get("phase")
    value = {
        "phase": phase if type(phase) is str and phase in BOOT_PHASES else "unknown"
    }
    for source, target in (
        ("started_unix", "started_at"),
        ("phase_started_unix", "phase_started_at"),
        ("updated_unix", "updated_at"),
    ):
        item = report.get(source)
        value[target] = (
            item
            if type(item) in (int, float)
            and math.isfinite(item)
            and 0 <= item <= 4_102_444_800
            else None
        )
    counters = {
        key: report.get(key)
        for key in ("bytes_complete", "bytes_total", "files_complete", "files_total")
    }
    if (
        all(type(item) is int for item in counters.values())
        and 0 <= counters["bytes_complete"] <= counters["bytes_total"] <= 256 * 1024**3
        and 0 <= counters["files_complete"] <= counters["files_total"] <= 64
    ):
        value["download"] = {**counters, "basis": "completed_files"}
    else:
        value["download"] = None
    diagnosis = safe_bootstrap_diagnosis(report)
    if type(report.get("state")) is str and report["state"] in {
        "failed",
        "unknown",
        "reconcile_required",
    }:
        value["reason_code"] = diagnosis["error_code"]
        value["failure_phase"] = diagnosis["failure_phase"]
    return value


def _transfer_remaining(deadline, maximum):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BootError('wangp_dependency_transfer_timeout')
    return min(maximum, remaining)


def dependency_source(config, runtime):
    """Large immutable dependency bytes stay outside the in-memory source map."""
    if not runtime.get('dependency_artifact_path'):
        return None
    digest = runtime.get('dependency_artifact_sha256')
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r'[0-9a-f]{64}', digest)
        or runtime['dependency_artifact_path']
        != '/root/sixnine-cache/' + DEPENDENCY_NAME
    ):
        raise BootError('wangp_dependency_binding_invalid')
    path = config.source_dir / DEPENDENCY_NAME
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not 0 < info.st_size <= MAX_DEPENDENCY_BYTES
    ):
        raise BootError('wangp_dependency_source_untrusted')
    return path, digest, info.st_size


def read_sources(config):
    files = {}
    for name in sorted(SOURCE_NAMES):
        path = config.source_dir / name
        maximum = 16 * 1024 * 1024 if name.endswith('.gz') else 512 * 1024
        if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
            raise BootError("wangp_boot_source_untrusted")
        files[name] = path.read_bytes()
    manifest = EngineManifest.from_dict(json.loads(files['wangp-manifest.json']))
    if manifest.digest != config.engine_manifest_digest or manifest.document.get(
        'synthetic'
    ):
        raise BootError('wangp_boot_manifest_mismatch')
    from .inference.wangp_compiler import H3FL2VACompiler
    from .inference.wangp_ref_compiler import H3Ref2VACompiler, RECIPE_ID

    try:
        if getattr(config, 'deployment_profile_id', ''):
            from .runtime_catalog import validate_manifest

            validate_manifest(manifest)
            runtime = json.loads(files['wangp-runtime.json'])
            if (
                manifest.document["deployment_profile_id"]
                != config.deployment_profile_id
                or manifest.document["model_id"] != config.model_id
                or tuple(config.recipe_ids)
                != (manifest.document["generation_recipe_id"],)
                or runtime.get('deployment_profile_id') != config.deployment_profile_id
                or runtime.get('profile_slot_index') != config.profile_slot_index
                or runtime.get('expected_host_gpus') != config.expected_host_gpus
                or runtime.get('port') != 8199 + config.profile_slot_index
            ):
                raise ValueError()
        elif tuple(config.recipe_ids) == (RECIPE_ID,):
            H3Ref2VACompiler(manifest, None)
        elif tuple(config.recipe_ids) == ('h3-base-fl2va-v1',):
            H3FL2VACompiler(manifest, None)
        else:
            raise ValueError()
    except ValueError:
        raise BootError('wangp_boot_recipe_manifest_mismatch') from None
    return files, manifest.document


def validate_report(config, report, manifest):
    if (
        report.get('engine_manifest_digest') != config.engine_manifest_digest
        or report.get('source_revision') != manifest['source_revision']
        or report.get('runtime_verified') is not True
    ):
        raise BootError('wangp_boot_runtime_identity_unconfirmed')
    gpus, runtime = report.get('gpus'), report.get('runtime', {})
    if (
        not isinstance(gpus, list)
        or len(gpus) != 1
        or not isinstance(gpus[0], dict)
        or not re.fullmatch(r'GPU-[A-Za-z0-9-]{8,100}', str(gpus[0].get('uuid', '')))
        or type(runtime.get('gpu_total_bytes')) is not int
        or runtime['gpu_total_bytes'] < config.min_gpu_bytes
    ):
        raise BootError('bootstrap_gpu_identity_or_memory_mismatch')


def make_slot(config, intent, report, directory):
    endpoint = f'http://127.0.0.1:{config.local_port}'
    suffix = (
        '-gpu' + str(config.profile_slot_index) if config.deployment_profile_id else ''
    )
    provider = getattr(config, 'provider', 'lium')
    if intent.get('provider', 'lium') != provider:
        raise BootError('bootstrap_slot_provider_mismatch')
    spec = WorkerSpec(
        provider + '-' + intent['id'].replace('-', '') + suffix,
        intent['pool'],
        provider,
        intent['provider_instance_id'],
        (report['gpus'][0]['uuid'],),
        config.recipe_ids,
        config.model_id,
        config.configuration_id,
        'wangp-worker',
        config.engine_manifest_digest,
        output_delivery=config.output_delivery,
    )
    return SlotConfig(
        spec,
        True,
        endpoint,
        (endpoint,),
        '',
        True,
        runtime_config_file=str(directory / 'wangp-client.json'),
    )


def _write_immutable(path, value):
    if path.exists():
        if read_document(path) != value:
            raise BootError('wangp_boot_client_config_changed')
        return
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
        'w',
        encoding='utf-8',
    ) as f:
        json.dump(value, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())


def connect_backend(boot, intent, directory, state):
    token_path = directory / 'wangp-token'
    token = private_token_file(
        token_path
    )  # Missing credentials never mint a new runtime identity.
    transport = HTTPWanGPTransport(f'http://127.0.0.1:{boot.config.local_port}', token)
    try:
        info = transport.readiness()
    finally:
        transport.close()
    if (
        not isinstance(info, HostReadiness)
        or info.manifest_digest != boot.config.engine_manifest_digest
        or info.slot_key != intent['id']
        or not isinstance(info.incarnation, str)
        or not re.fullmatch(r'[0-9a-f]{32}', info.incarnation)
    ):
        raise BootError('wangp_boot_endpoint_identity_unconfirmed')
    prior = state.get('runtime_incarnation')
    if prior is not None and prior != info.incarnation:
        raise BootError('wangp_boot_incarnation_changed_reconcile_required')
    manifest_path = directory / 'wangp-manifest.json'
    _write_immutable(
        manifest_path,
        json.loads((boot.config.source_dir / 'wangp-manifest.json').read_text()),
    )
    _write_immutable(
        directory / 'wangp-client.json',
        {
            'version': 1,
            'enabled': True,
            'slot_key': intent['id'],
            'configuration_id': boot.config.configuration_id,
            'manifest_file': str(manifest_path),
            'token_file': str(token_path),
            'runtime_incarnation': info.incarnation,
        },
    )
    slot = make_slot(
        boot.config, intent, {'gpus': [state['hardware']['gpu']]}, directory
    )
    backend = create_backend(slot, directory)
    state['runtime_incarnation'] = info.incarnation
    boot._save(directory / 'bootstrap-state.json', state)
    return backend


class WanGPSSHHost(SSHHost):
    remote_port = 8199
    remote_root = REMOTE_ROOT

    def __init__(self, config, coordinates):
        self.config = config
        self.remote_root = REMOTE_ROOT
        if config.deployment_profile_id:
            self.remote_root += '/profile-slot-' + str(config.profile_slot_index)
            self.remote_port = 8199 + config.profile_slot_index
        super().__init__(config, coordinates)

    def run(self, code, **kwargs):
        # Only trusted, internally authored remote snippets cross this seam.
        # Slot isolation never uses a client-provided path or shell fragment.
        if getattr(self.config, 'deployment_profile_id', ''):
            code = code.replace(REMOTE_ROOT, self.remote_root)
            code = code.replace('==8199 and', '==' + str(self.remote_port) + ' and')
        return super().run(code, **kwargs)

    def upload(self, files, *, progress=None, should_stop=None):
        def check():
            if should_stop is not None and should_stop():
                from .bootstrap_staging import UploadCancelled

                raise UploadCancelled

        check()
        if set(files) != SOURCE_NAMES:
            raise BootError('wangp_boot_source_set_invalid')
        self._capture_system_observation()
        self.run(
            "from pathlib import Path; import json; Path('/workspace/h3-studio').mkdir(parents=True,exist_ok=True); print(json.dumps({'ok':True}))"
        )
        with self._source_sftp() as sftp:
            for name, data in files.items():
                check()
                target = self.remote_root + '/' + name
                try:
                    with sftp.open(target, 'rb') as f:
                        if f.read(len(data) + 1) != data:
                            raise BootError('bootstrap_existing_source_mismatch')
                        # A provider's umask may leave uploaded configuration
                        # group-writable. Tighten the already verified handle,
                        # never weaken the runtime's strict document reader.
                        f.chmod(0o600)
                except FileNotFoundError:
                    with sftp.open(target, 'wx') as f:
                        f.chmod(0o600)
                        f.write(data)
        dependency = dependency_source(
            self.config, json.loads(files['wangp-runtime.json'])
        )
        if dependency is not None:
            self._upload_dependency(
                *dependency, progress=progress, should_stop=should_stop
            )

    @contextmanager
    def _source_sftp(self):
        if getattr(self.config, 'provider', 'lium') == 'targon':
            # Ubuntu's login identity stays unprivileged. This fixed, bounded
            # subsystem keeps trusted bootstrap files/token root-owned without
            # opening a root login or weakening strict runtime permissions.
            with self._transfer_sftp(
                time.monotonic() + SFTP_OPEN_SECONDS + SFTP_IO_SECONDS
            ) as sftp:
                yield sftp
        else:
            with self.client.open_sftp() as sftp:
                yield sftp

    def inspect_system_packages(self):
        """Read-only pre-upload inventory; no package installation or admission."""
        from .runtime_hosts.wangp_environment import (
            PACKAGE_NAME_PATTERN,
            PACKAGE_VERSION_PATTERN,
            MAX_SYSTEM_PACKAGES,
        )

        value = self.run(
            '''import json,subprocess
raw=subprocess.run(['dpkg-query','-W','-f=${binary:Package}\\t${Version}\\n'],check=True,capture_output=True,text=True,timeout=10).stdout
if len(raw)>2097152: raise ValueError('inventory_too_large')
rows=raw.splitlines()
print(json.dumps({'packages':dict(line.split('\\t',1) for line in rows[:10000]),'total':len(rows),'truncated':len(rows)>10000}))
''',
            limit=2 * 1024**2,
            timeout=20,
        )
        packages, total = value.get('packages'), value.get('total')
        if (
            not isinstance(packages, dict)
            or type(total) is not int
            or not 0 <= total <= MAX_SYSTEM_PACKAGES
        ):
            raise BootError('system_package_observation_invalid')
        safe = {
            name: version
            for name, version in list(packages.items())[:MAX_SYSTEM_PACKAGES]
            if isinstance(name, str)
            and re.fullmatch(PACKAGE_NAME_PATTERN, name)
            and isinstance(version, str)
            and re.fullmatch(PACKAGE_VERSION_PATTERN, version)
        }
        if total < len(safe):
            raise BootError('system_package_observation_invalid')
        return {
            'packages': dict(sorted(safe.items())),
            'total': total,
            'truncated': value.get('truncated') is True or total > len(safe),
        }

    def _capture_system_observation(self):
        """Keep the first safe inventory on CPU before transferring dependencies."""
        from .lium_bootstrap import provider_instance_id

        instance_id = self.coordinates.get('instance_id')
        provider_instance_id(getattr(self.config, 'provider', 'lium'), instance_id)
        directory = self.config.work_dir / 'os-observations'
        if directory.is_symlink():
            raise BootError('system_package_observation_path_invalid')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / (instance_id + '.json')
        if target.is_symlink():
            raise BootError('system_package_observation_path_invalid')
        if target.exists():
            return  # Preserve the original pre-upload observation on reconnect.
        record = {'version': 1, 'instance_id': instance_id, 'observed_at': time.time()}
        try:
            record.update(state='observed', **self.inspect_system_packages())
        except Exception:
            # Observation is diagnostic only. Do not turn it into a new
            # readiness criterion or include exception text/SSH coordinates.
            record.update(state='unavailable', code='system_package_observation_failed')
        with target.open('x', encoding='utf-8') as destination:
            target.chmod(0o600)
            json.dump(record, destination, sort_keys=True)
            destination.flush()
            os.fsync(destination.fileno())

    @contextmanager
    def _transfer_sftp(self, deadline):
        """Bound channel opening, subsystem negotiation and all SFTP I/O.

        Paramiko's subsystem request waits on an event without consulting the
        channel I/O timeout. Closing this one channel wakes that wait; no runtime
        or provider operation is retried by the deadline guard.
        """
        import paramiko

        channel = self.client.get_transport().open_session(
            timeout=_transfer_remaining(deadline, SFTP_OPEN_SECONDS)
        )
        if channel is None:
            raise BootError('wangp_dependency_sftp_unavailable')
        expired = threading.Event()
        timer = None
        sftp = None

        def expire():
            expired.set()
            channel.close()

        def guard(seconds):
            value = threading.Timer(seconds, expire)
            value.daemon = True
            value.start()
            return value

        try:
            channel.settimeout(_transfer_remaining(deadline, SFTP_IO_SECONDS))
            timer = guard(_transfer_remaining(deadline, SFTP_OPEN_SECONDS))
            if getattr(getattr(self, 'config', None), 'provider', 'lium') == 'targon':
                channel.exec_command('sudo -n /usr/lib/openssh/sftp-server')
            else:
                channel.invoke_subsystem('sftp')
            sftp = paramiko.SFTPClient(channel)
            timer.cancel()
            if expired.is_set():
                raise BootError('wangp_dependency_transfer_timeout')
            timer = guard(_transfer_remaining(deadline, DEPENDENCY_TRANSFER_SECONDS))
            yield sftp
            if expired.is_set():
                raise BootError('wangp_dependency_transfer_timeout')
            _transfer_remaining(deadline, DEPENDENCY_TRANSFER_SECONDS)
        except Exception:
            if expired.is_set():
                raise BootError('wangp_dependency_transfer_timeout') from None
            raise
        finally:
            try:
                if sftp is not None:
                    sftp.close()
            finally:
                if timer is not None:
                    timer.cancel()
                channel.close()

    def _upload_dependency(
        self, path, expected, size, *, progress=None, should_stop=None
    ):
        # Upload can be resumed before the launch marker exists. Only an exact
        # verified prefix is appended; the final file is published atomically.
        deadline = time.monotonic() + DEPENDENCY_TRANSFER_SECONDS

        def check():
            if should_stop is not None and should_stop():
                from .bootstrap_staging import UploadCancelled

                raise UploadCancelled
            _transfer_remaining(deadline, DEPENDENCY_TRANSFER_SECONDS)

        check()
        self.ensure_connected()
        script = '''import hashlib,json,os,stat
from pathlib import Path
root=Path('/root/sixnine-cache')
if root.is_symlink(): raise ValueError('linked_cache')
root.mkdir(mode=0o700,exist_ok=True)
target=root/'wangp-dependencies.tar.gz'
partial=root/'wangp-dependencies.tar.gz.partial'
p=target if target.exists() else partial
if p.exists():
 info=p.lstat()
 if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise ValueError('untrusted_artifact')
 value=hashlib.sha256()
 with p.open('rb') as source:
  for chunk in iter(lambda:source.read(8388608),b''): value.update(chunk)
 print(json.dumps({'present':True,'complete':p==target,'size':info.st_size,'sha256':value.hexdigest()}))
else: print(json.dumps({'present':False,'complete':False,'size':0,'sha256':hashlib.sha256(b'').hexdigest()}))
'''
        prior = self.run(script, limit=4096, timeout=_transfer_remaining(deadline, 600))
        check()
        if (
            type(prior.get('size')) is not int
            or not 0 <= prior['size'] <= size
            or type(prior.get('complete')) is not bool
            or type(prior.get('present')) is not bool
        ):
            raise BootError('wangp_dependency_existing_untrusted')
        prefix = hashlib.sha256()
        with path.open('rb') as source:
            remaining = prior['size']
            while remaining:
                check()
                chunk = source.read(min(8 * 1024**2, remaining))
                if not chunk:
                    raise BootError('wangp_dependency_local_truncated')
                prefix.update(chunk)
                remaining -= len(chunk)
            if prefix.hexdigest() != prior.get('sha256'):
                raise BootError('wangp_dependency_existing_mismatch')
            check()
            if progress is not None:
                progress(prior['size'], size)
            if prior['complete']:
                if prior['size'] != size or prefix.hexdigest() != expected:
                    raise BootError('wangp_dependency_existing_mismatch')
                return
            target = '/root/sixnine-cache/' + DEPENDENCY_NAME + '.partial'
            with self._transfer_sftp(deadline) as sftp:
                with sftp.open(target, 'ab' if prior['present'] else 'wx') as remote:
                    remote.set_pipelined(True)
                    for chunk in iter(lambda: source.read(8 * 1024**2), b''):
                        check()
                        prefix.update(chunk)
                        remote.write(chunk)
                        if progress is not None:
                            progress(source.tell(), size)
            if source.tell() != size or prefix.hexdigest() != expected:
                raise BootError('wangp_dependency_source_hash_mismatch')
        result = self.run(
            '''import hashlib,json,os,stat
from pathlib import Path
root=Path('/root/sixnine-cache');p=root/'wangp-dependencies.tar.gz.partial';target=root/'wangp-dependencies.tar.gz'
info=p.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=SIZE: raise ValueError('artifact_shape')
value=hashlib.sha256()
with p.open('rb') as source:
 for chunk in iter(lambda:source.read(8388608),b''): value.update(chunk)
if value.hexdigest()!=EXPECTED or target.exists(): raise ValueError('artifact_digest')
os.rename(p,target)
print(json.dumps({'verified':True}))
'''.replace('SIZE', str(size)).replace('EXPECTED', repr(expected)),
            limit=4096,
            timeout=_transfer_remaining(deadline, 600),
        )
        _transfer_remaining(deadline, DEPENDENCY_TRANSFER_SECONDS)
        if result != {'verified': True}:
            raise BootError('wangp_dependency_transfer_unconfirmed')
        check()

    def start(self, identity):
        # Token generation is only in the initial reserved phase, never reconnect.
        token_path = self.config.work_dir / identity['intent_id'] / 'wangp-token'
        if not token_path.exists():
            with os.fdopen(
                os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w'
            ) as f:
                f.write(secrets.token_urlsafe(48))
                f.flush()
                os.fsync(f.fileno())
        token = private_token_file(token_path)
        with self._source_sftp() as sftp:
            target = self.remote_root + '/wangp-token'
            try:
                with sftp.open(target, 'rb') as f:
                    if f.read(256).decode() != token:
                        raise BootError('wangp_boot_token_identity_conflict')
            except FileNotFoundError:
                with sftp.open(target, 'wx') as f:
                    sftp.chmod(target, 0o600)
                    f.write(token.encode())
        del token
        return self.run(
            '''import fcntl,hashlib,json,os,subprocess,sys,tarfile
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
  with marker.open('x') as f:
   json.dump(expected,f);f.flush();os.fsync(f.fileno())
  command=['/opt/conda/bin/python','-u',str(root/'wangp-bootstrap.py'),'--config',str(root/'wangp-runtime.json'),'--slot-key',expected['intent_id'],'--token-file',str(root/'wangp-token')]
  if expected.get('provider')=='targon':
   archive=root/'wangp-package.tar.gz'
   if archive.is_symlink() or not archive.is_file() or archive.stat().st_size>16777216: raise ValueError('source_bundle_mismatch')
   if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected['sources']['wangp-package.tar.gz']: raise ValueError('source_bundle_mismatch')
   with tarfile.open(archive,'r:gz') as source:
    matches=[member for member in source.getmembers() if member.name=='deploy/wangp/targon_prepare.py']
    if len(matches)!=1 or not matches[0].isfile() or not 0<matches[0].size<=262144: raise ValueError('targon_preparer_invalid')
    data=source.extractfile(matches[0]).read(262145)
    if len(data)!=matches[0].size: raise ValueError('targon_preparer_invalid')
   prepare=root/'targon-prepare.py'
   if prepare.is_symlink(): raise ValueError('targon_preparer_invalid')
   if prepare.exists():
    if not prepare.is_file() or prepare.stat().st_nlink!=1 or prepare.read_bytes()!=data: raise ValueError('targon_preparer_invalid')
   else:
    with prepare.open('xb') as f:
     os.fchmod(f.fileno(),0o600);f.write(data);f.flush();os.fsync(f.fileno())
   command=['python3','-u',str(prepare),*command[3:]]
  with (root/'bootstrap-controller.log').open('ab') as log:
   proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  print(json.dumps({'state':'started','pid':proc.pid}))
'''.replace('IDENTITY', repr(identity)).replace(
                "'/opt/conda/bin/python'", repr(self.config.runtime_python)
            )
        )

    def report(self):
        report = self.run('''import json,subprocess
from pathlib import Path
root=Path('/workspace/h3-studio')
def read(name):
 p=root/name
 if not p.exists(): return {}
 if p.is_symlink() or p.stat().st_size>4194304: raise ValueError('untrusted_report')
 return json.loads(p.read_text())
s=read('setup-status.json')
out={k:s.get(k) for k in ('state','phase','error_code','error_type','runtime_verified','engine_manifest_digest','source_revision','runtime','system_package_diagnostics','download_failure','started_unix','phase_started_unix','updated_unix','bytes_complete','bytes_total','files_complete','files_total')}
out['selected_gpus']=s.get('gpus')
out['error_code']=s.get('error_code',s.get('code'))
out['failure_phase']=s.get('failure_phase',s.get('failed_phase',s.get('phase')))
out['identity']=read('sixnine-bootstrap-identity.json')
if out['state']=='ready':
 rows=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,memory.total,name','--format=csv,noheader,nounits'],text=True).strip().splitlines()
 out['gpus']=[{'uuid':x.split(',')[0].strip(),'memory_mib':int(x.split(',')[1].strip()),'name':','.join(x.split(',')[2:]).strip()} for x in rows]
print(json.dumps(out))
''')
        preparation = safe_preparation_report(report)
        for key in (
            'started_unix',
            'phase_started_unix',
            'updated_unix',
            'bytes_complete',
            'bytes_total',
            'files_complete',
            'files_total',
        ):
            report.pop(key, None)  # Only sanitized numeric copies leave this seam.
        if (
            report.get('state') in {'failed', 'unknown', 'reconcile_required'}
            or report.get('phase') == 'runtime_start_unknown'
        ):
            diagnosis = safe_bootstrap_diagnosis(report)
            report = {
                'identity': report.get('identity'),
                'state': report.get('state'),
                **diagnosis,
            }
        else:
            report.pop('download_failure', None)
            report.pop('system_package_diagnostics', None)
            # Nonterminal progress is untrusted remote input too. Only expose
            # known static fields even if a malformed report includes an error.
            diagnosis = safe_bootstrap_diagnosis(report)
            for key in ('phase', 'failure_phase', 'error_code', 'error_type'):
                if key in report:
                    report[key] = diagnosis[key]
        if (
            getattr(self.config, 'deployment_profile_id', '')
            and report.get('state') == 'ready'
        ):
            gpus = report.get('gpus')
            selected = report.pop('selected_gpus', None)
            if (
                not isinstance(gpus, list)
                or len(gpus) != self.config.expected_host_gpus
                or not isinstance(selected, list)
                or len(selected) != 1
                or not any(g.get('uuid') == selected[0].get('uuid') for g in gpus)
            ):
                raise BootError('bootstrap_gpu_identity_or_memory_mismatch')
            report['gpus'] = [g for g in gpus if g['uuid'] == selected[0]['uuid']]
        report.pop('selected_gpus', None)
        report['preparation'] = preparation
        return report

    def preparation_idle_report(self, *, expected_prestart_identity=None):
        if expected_prestart_identity is not None and (
            not isinstance(expected_prestart_identity, dict)
            or expected_prestart_identity.get('backend') != 'wangp-worker'
        ):
            raise BootError('wangp_prestart_identity_required')
        return self.run(
            '''import json,re,time
from pathlib import Path
root=Path('/workspace/h3-studio');proc=Path('/proc')
out={'identity':{},'state':'unknown','process_visibility_complete':False,
 'bootstrap_process_count':None,'runtime_process_count':None,'runtime_port_listening':None}
expected=PRESTART_IDENTITY
markers=('sixnine-bootstrap-identity.json','setup-status.json','sixnine-bootstrap.lock','wangp-token')
def absent():
 for name in markers:
  try: (root/name).lstat()
  except FileNotFoundError: continue
  return False
 return True
def read(name):
 p=root/name
 if p.is_symlink() or not p.is_file() or p.stat().st_size>4194304: raise ValueError('unconfirmed')
 return json.loads(p.read_text())
def pids():
 return {p.name for p in proc.iterdir() if p.name.isdecimal() and p.is_dir()}
try:
 if expected is not None:
  if not absent(): raise ValueError('unconfirmed')
  identity=expected;state='not_started'
 else:
  identity=read('sixnine-bootstrap-identity.json');status=read('setup-status.json');state='failed'
  if identity.get('backend')!='wangp-worker' or status.get('state')!='failed': raise ValueError('unconfirmed')
 before=pids()
 if not before: raise ValueError('unconfirmed')
 bootstrap=runtime=0
 for pid in before:
  with (proc/pid/'cmdline').open('rb') as f: raw=f.read(1048577)
  if len(raw)>1048576: raise ValueError('unconfirmed')
  args=raw.split(b'\\0')
  bootstrap+=int(any(a==b'wangp-bootstrap.py' or a.endswith(b'/wangp-bootstrap.py') for a in args)
                 or any(a==b'targon-prepare.py' or a.endswith(b'/targon-prepare.py') for a in args)
                 or b'studio_platform.runtime_hosts.wangp_download' in args)
  runtime+=int(b'studio_platform.runtime_hosts.wangp_launcher' in args)
 listening=False
 for name in ('tcp','tcp6'):
  with (proc/'net'/name).open(encoding='ascii') as f: raw=f.read(4194305)
  if len(raw)>4194304: raise ValueError('unconfirmed')
  rows=raw.splitlines()
  if not rows or 'local_address' not in rows[0] or 'st' not in rows[0].split(): raise ValueError('unconfirmed')
  for line in rows[1:]:
   fields=line.split()
   if len(fields)<10 or not re.fullmatch(r'[0-9A-Fa-f]+:[0-9A-Fa-f]{4}',fields[1]): raise ValueError('unconfirmed')
   listening=bool(listening or int(fields[1].rsplit(':',1)[1],16)==8199 and fields[3].upper()=='0A')
 if pids()!=before: raise ValueError('unconfirmed')
 if expected is not None and not absent(): raise ValueError('unconfirmed')
 out.update(identity=identity,state=state,setup_markers_absent=expected is not None,
  process_visibility_complete=True,bootstrap_process_count=bootstrap,
  runtime_process_count=runtime,runtime_port_listening=listening)
except Exception:
 pass
out['observed_at']=time.time()
print(json.dumps(out))
'''.replace('PRESTART_IDENTITY', repr(expected_prestart_identity)),
            limit=16384,
        )
