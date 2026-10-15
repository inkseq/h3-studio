"""Prepared native-profile setup; no installation, rental, or generated smoke jobs."""
import importlib.metadata
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

LOCK_WAIT_SECONDS = 1800
GPU_UUID = re.compile(r'GPU-[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}')


def prepare(config, manifest, source, status):
    from ..runtime_catalog import validate_manifest
    profile = validate_manifest(manifest)
    if config["deployment_profile_id"] != profile["id"]:
        raise ValueError("wangp_profile_manifest_mismatch")
    runtime = Path(config["prepared_root"]).resolve(strict=True)
    status("runtime_imports")
    revision = subprocess.run(["git", "-C", str(runtime), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    if revision != profile["source_revision"]:
        raise ValueError("wangp_runtime_source_mismatch")
    for args in (["diff", "--quiet", "HEAD", "--", ":(exclude)requirements.txt"],
                 ["diff", "--cached", "--quiet", "HEAD", "--", ":(exclude)requirements.txt"]):
        if subprocess.run(["git", "-C", str(runtime), *args], capture_output=True).returncode:
            raise ValueError("wangp_runtime_source_modified")
    requirements = runtime / 'requirements.txt'
    if (requirements.is_symlink() or hashlib.sha256(requirements.read_bytes()).hexdigest()
            != profile['runtime']['requirements_sha256']):
        raise ValueError('wangp_runtime_requirements_mismatch')
    extras = subprocess.run(['git','-C',str(runtime),'ls-files','--others','--exclude-standard','-z'],
        check=True,capture_output=True).stdout.decode().split('\0')
    if any(Path(name).suffix.lower() in {'.py','.pyd','.so'} for name in extras if name):
        raise ValueError('wangp_runtime_untracked_code')
    for name, expected in profile["runtime"]["core_versions"].items():
        if importlib.metadata.version(name) != expected:
            raise ValueError("wangp_runtime_dependency_mismatch")
    # Private model workers receive no API/cloud/SSH credentials. Keep only the
    # runtime system paths required by a prepared container, not the whole env.
    environment = {name: os.environ[name] for name in ("PATH", "HOME", "LD_LIBRARY_PATH", "TMPDIR") if name in os.environ}
    gpu = selected_devices(config, observed_devices())[0]
    environment.update(PYTHONPATH=str(source), PYTHONUNBUFFERED="1",
        CUDA_VISIBLE_DEVICES=gpu['uuid'], TOKENIZERS_PARALLELISM="false")
    if profile.get('compiler_epoch')=='full-controls-v2':
        from .wangp_media_tools import verify_media_tools
        verify_media_tools(runtime,environment,cwd=source)
    return runtime, sys.executable, environment


def observed_devices():
    raw = subprocess.run(['nvidia-smi','--query-gpu=uuid,memory.total','--format=csv,noheader,nounits'],
        check=True,capture_output=True,text=True,timeout=15).stdout
    if len(raw)>8192:
        raise ValueError('gpu_observation_invalid')
    devices = []
    for line in raw.splitlines():
        values = [value.strip() for value in line.split(',')]
        if len(values)!=2 or not GPU_UUID.fullmatch(values[0]) or not values[1].isdecimal() or int(values[1])<=0:
            raise ValueError('gpu_observation_invalid')
        devices.append({'uuid':values[0],'total_bytes':int(values[1])*1024**2})
    return devices


def selected_devices(config, devices, *, expected_uuid=None):
    if not config.get("deployment_profile_id"):
        return devices
    index, count = config["profile_slot_index"], config["expected_host_gpus"]
    if (not isinstance(devices,list) or type(count) is not int or not 1<=count<=8
            or type(index) is not int or not 0<=index<count or len(devices) != count
            or any(not isinstance(device,dict) or not isinstance(device.get('uuid'),str)
                   or not GPU_UUID.fullmatch(device['uuid']) for device in devices)
            or len({device['uuid'].lower() for device in devices})!=count):
        raise ValueError("gpu_observation_invalid")
    if expected_uuid is not None:
        matches = [device for device in devices if device['uuid']==expected_uuid]
        if len(matches)!=1:
            raise ValueError('gpu_observation_invalid')
        return matches
    return [devices[index]]


def _acquire_cache_lock(stream, status, *, timeout=LOCK_WAIT_SECONDS, clock=None, sleeper=None, locker=None):
    """A contended sibling download cannot block bootstrap indefinitely."""
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=LOCK_WAIT_SECONDS:
        raise ValueError('wangp_profile_cache_lock_timeout_invalid')
    clock, sleeper = clock or time.monotonic, sleeper or time.sleep
    if locker is None:
        import fcntl
        locker = lambda handle:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    deadline = clock()+timeout
    last_status = -float('inf')
    while True:
        try:
            locker(stream)
            return
        except BlockingIOError:
            now = clock()
            if now>=deadline:
                raise ValueError('wangp_profile_cache_lock_timeout') from None
            if now-last_status>=2:
                status('model_download_waiting_for_shared_cache')
                last_status = now
            sleeper(min(.25,deadline-now))


def download_profile(config, python, source, manifest_digest, model_root, state_root, environment, status):
    """Serialize shared cache population across slots; never redownload a valid file."""
    from .wangp_download import run_download
    root = Path(model_root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".sixnine-profile-download.lock"
    if lock.is_symlink():
        raise ValueError("model_download_path_invalid")
    descriptor = os.open(lock,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(descriptor,"a+b") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or hasattr(os,'getuid') and info.st_uid!=os.getuid():
            raise ValueError('model_download_path_invalid')
        _acquire_cache_lock(stream,status)
        return run_download(python, source, config["manifest_path"], manifest_digest,
            root, state_root, environment, status)
