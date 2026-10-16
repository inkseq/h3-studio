"""Protected local native-runtime wiring; no provider calls or second ledger.

Construction reads reviewed CPU configuration/source inputs only. Tunnel ports
are allocated lazily in the original operator node, under the original global
capacity lock, and are not reused while any retained node reserves them.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from sqlalchemy import select, update

from .dstack_capacity import DstackError
from .dstack_operator import BACKEND_MARKER, PROVIDERS
from .dstack_runtime import DstackNativeRuntime, DstackRuntimeConfig, IDENTITY_FIELDS
from .native_runtime_document import prepared_runtime_document
from .operator_capacity import operator_nodes
from .runtime_catalog import engine_manifest, get_profile
from .wangp_bootstrap import SOURCE_NAMES, read_sources

CONFIG_KEYS = {"version", "work_dir", "source_index_file", "ssh_key_file", "known_hosts_file",
    "first_local_port", "last_local_port", "trust_first_host_key"}
ENTRY_KEYS = {"runtime_profile_id", "mode", "gpu_count", "profile_slot_index", "directory",
    "engine_manifest_digest", "source_sha256"}
PURPOSE = "local-unqualified-dstack-source-set"


def _require(condition, code):
    if not condition:
        raise DstackError(code)


def _path(value):
    _require(isinstance(value, (str, Path)), "dstack_runtime_path_invalid")
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "dstack_runtime_path_invalid")
    for part in (path, *path.parents):
        _require(not part.is_symlink() and not (hasattr(part, "is_junction") and part.is_junction()),
                 "dstack_runtime_link_forbidden")
    return path


def _file(path, *, maximum=262144, read=True):
    path = _path(path)
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= maximum,
                     "dstack_runtime_file_invalid")
            _require(os.name == "nt" or not info.st_mode & (stat.S_IRWXO | stat.S_IWGRP),
                     "dstack_runtime_permissions_invalid")
            if read:
                value = stream.read(maximum + 1)
                _require(len(value) <= maximum, "dstack_runtime_file_invalid")
                return value
    except OSError:
        raise DstackError("dstack_runtime_file_unavailable") from None
    return path


def _json(path):
    try:
        value = json.loads(_file(path))
    except DstackError:
        raise
    except (ValueError, UnicodeError):
        raise DstackError("dstack_runtime_document_invalid") from None
    _require(type(value) is dict, "dstack_runtime_document_invalid")
    return value


def _directory_parent(path):
    """Existing local transport storage cannot be writable by other accounts."""
    directory = path if path.exists() else path.parent
    while not directory.exists():
        directory = directory.parent
    info = directory.stat()
    _require(stat.S_ISDIR(info.st_mode) and (os.name == "nt" or
        not info.st_mode & (stat.S_IWOTH | stat.S_IWGRP)), "dstack_runtime_directory_untrusted")


def coordinates_for_run(binding, run):
    """Pinned 0.22.3 direct provider-container SSH; no SDK host-key bypass."""
    _require(type(binding) is dict and type(run) is dict and run.get("id") == binding.get("run_id")
        and isinstance(binding.get("run_id"), str) and bool(binding["run_id"])
        and run.get("status") == "running" and binding.get("backend") in PROVIDERS
        and isinstance(binding.get("provider_instance_id"), str) and binding["provider_instance_id"],
        "dstack_ssh_identity_unconfirmed")
    latest = run.get("latest_job_submission")
    provisioning = latest.get("job_provisioning_data") if type(latest) is dict else None
    _require(type(provisioning) is dict and provisioning.get("backend") == binding["backend"]
        and provisioning.get("instance_id") == binding["provider_instance_id"],
        "dstack_ssh_allocation_mismatch")
    instance = provisioning.get("instance_type")
    resources = instance.get("resources") if type(instance) is dict else None
    gpus = resources.get("gpus") if type(resources) is dict else None
    _require(type(gpus) is list and len(gpus) == 1 and type(gpus[0]) is dict,
             "dstack_ssh_allocation_mismatch")
    _require(provisioning.get("dockerized") is False and provisioning.get("ssh_proxy") is None,
             "dstack_ssh_topology_unsupported")
    runtime = latest.get("job_runtime_data")
    _require(runtime is None or type(runtime) is dict, "dstack_ssh_coordinates_invalid")
    username = (runtime or {}).get("username") or provisioning.get("username") or "root"
    _require(username == "root" and provisioning.get("username") in (None, "root"),
             "dstack_ssh_username_unsupported")
    host, port = provisioning.get("hostname"), provisioning.get("ssh_port")
    _require(isinstance(host, str) and host and type(port) is int and 1 <= port <= 65535,
             "dstack_ssh_coordinates_unavailable")
    try:
        ipaddress.ip_address(host)
        valid = "%" not in host
    except ValueError:
        valid = len(host) <= 253 and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in host.rstrip(".").split("."))
    _require(valid, "dstack_ssh_coordinates_invalid")
    return {"host": host, "port": port, "username": username,
            "instance_id": binding["provider_instance_id"]}


class RuntimeFactory:
    def __init__(self, repo, store, configuration_path):
        self.repo, self.store = repo, store
        config = _json(configuration_path)
        _require(set(config) in (CONFIG_KEYS, CONFIG_KEYS | {"broker_config_file"}) and type(config.get("version")) is int and config["version"] == 1
            and type(config.get("trust_first_host_key")) is bool, "dstack_runtime_config_invalid")
        first, last = config.get("first_local_port"), config.get("last_local_port")
        _require(type(first) is int and type(last) is int and 1024 <= first <= last <= 65535
            and last - first + 1 <= 128, "dstack_runtime_port_range_invalid")
        self.first_port, self.last_port = first, last
        self.work_dir = _path(config["work_dir"])
        _directory_parent(self.work_dir)
        self.ssh_key_file = _file(config["ssh_key_file"], maximum=65536, read=False)
        self.known_hosts_file = _path(config["known_hosts_file"])
        _directory_parent(self.known_hosts_file.parent)
        self.trust_first_host_key = config["trust_first_host_key"]
        index_file = _path(config["source_index_file"])
        index = _json(index_file)
        _require(set(index) == {"schema_version", "purpose", "production_adapter_verified", "sources"}
            and type(index.get("schema_version")) is int and index["schema_version"] == 1 and index.get("purpose") == PURPOSE
            and index.get("production_adapter_verified") is False and type(index.get("sources")) is list
            and 1 <= len(index["sources"]) <= 128, "dstack_runtime_source_index_invalid")
        self.sources = {}
        for entry in index["sources"]:
            self._source(index_file.parent, entry)

    def _source(self, base, entry):
        _require(type(entry) is dict and set(entry) == ENTRY_KEYS and entry.get("gpu_count") == 1
            and type(entry.get("gpu_count")) is int and entry.get("profile_slot_index") == 0
            and type(entry.get("profile_slot_index")) is int, "dstack_runtime_source_entry_invalid")
        profile, mode = entry["runtime_profile_id"], entry["mode"]
        try:
            manifest = engine_manifest(profile, mode)
        except (ValueError, TypeError):
            raise DstackError("dstack_runtime_source_profile_invalid") from None
        pair = (profile, mode)
        _require(pair not in self.sources and entry["directory"] == f"{profile}/{mode}/gpu-0"
            and entry["engine_manifest_digest"] == manifest.digest,
            "dstack_runtime_source_binding_invalid")
        directory = _path(base / entry["directory"])
        _require(directory.is_dir() and {path.name for path in directory.iterdir()} == SOURCE_NAMES,
                 "dstack_runtime_source_set_invalid")
        config = DstackRuntimeConfig(self.work_dir, directory, self.ssh_key_file, self.known_hosts_file,
            self.first_port, "validation-only", "vast", profile, mode, manifest.document["model_id"],
            "validation-only", manifest.digest, entry["source_sha256"],
            min_gpu_bytes=get_profile(profile).get("hardware_admission", {}).get("minimum_total_vram_bytes",
                (30 if "Pruned" in manifest.document["model_id"] else 90) * 1024**3))
        for name in SOURCE_NAMES:
            _file(directory / name, maximum=16*1024*1024 if name.endswith(".gz") else 512*1024, read=False)
        files, _ = read_sources(config)
        _require({name: hashlib.sha256(data).hexdigest() for name, data in files.items()} == dict(config.source_sha256),
                 "dstack_runtime_source_changed")
        # No URL/env/secret/config injection into the prepared image bootstrap.
        runtime = json.loads(files["wangp-runtime.json"])
        _require(runtime == prepared_runtime_document(profile, 0, 1, config.source_sha256["wangp-package.tar.gz"]),
                 "dstack_runtime_prepared_source_required")
        self.sources[pair] = (directory, dict(config.source_sha256), manifest.digest)

    @staticmethod
    def _ports(payload):
        """Conservatively include retained legacy/topology port reservations."""
        ports = []
        if type(payload) is dict:
            for key, value in payload.items():
                if key == "local_port":
                    _require(type(value) is int and 1024 <= value <= 65535, "dstack_runtime_retained_port_invalid")
                    ports.append(value)
                elif key == "local_ports":
                    _require(type(value) is list and all(type(port) is int and 1024 <= port <= 65535 for port in value),
                             "dstack_runtime_retained_port_invalid")
                    ports.extend(value)
                elif isinstance(value, (dict, list)):
                    ports.extend(RuntimeFactory._ports(value))
        elif type(payload) is list:
            for value in payload:
                ports.extend(RuntimeFactory._ports(value))
        return ports

    def config_for_binding(self, binding):
        _require(type(binding) is dict, "dstack_runtime_binding_invalid")
        try:
            _require(str(UUID(binding.get("intent_id"))) == binding.get("intent_id"), "dstack_runtime_binding_invalid")
        except (ValueError, TypeError, AttributeError):
            raise DstackError("dstack_runtime_binding_invalid") from None
        pair = (binding.get("profile_id"), binding.get("mode"))
        _require(pair in self.sources, "dstack_runtime_source_selection_missing")
        directory, hashes, manifest_digest = self.sources[pair]
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self.store._node(connection, binding["intent_id"])
            payload = copy.deepcopy(node["payload"])
            current = payload["dstack"]
            _require(all(current.get(key) == binding.get(key) for key in IDENTITY_FIELDS)
                and current.get("run_spec") == binding.get("run_spec")
                and current.get("apply_started") is True and current.get("provider_instance_id")
                and payload.get("capacity_backend") == BACKEND_MARKER
                and payload.get("configuration_id") == binding.get("configuration_id")
                and payload.get("selection", {}).get("runtime_profile_id") == pair[0]
                and payload.get("selection", {}).get("mode") == pair[1]
                and intent["physical_gpus"] == intent["slots"] == 1
                and intent["provider"] == PROVIDERS.get(binding.get("backend"))
                and binding.get("manifest_digest") == manifest_digest, "dstack_runtime_binding_changed")
            retained = []
            for other in connection.execute(select(operator_nodes.c.intent_id, operator_nodes.c.payload)).mappings():
                retained.extend((other["intent_id"], port) for port in self._ports(other["payload"]))
            port = current.get("local_port")
            if port is not None:
                _require(type(port) is int and self.first_port <= port <= self.last_port
                    and all(identity == intent["id"] or reserved != port for identity, reserved in retained),
                    "dstack_runtime_port_conflict")
                _require(binding.get("local_port") in (None, port), "dstack_runtime_port_changed")
            else:
                _require(node["desired_state"] == "running", "dstack_runtime_stopped_port_unallocated")
                taken = {reserved for _, reserved in retained}
                port = next((value for value in range(self.first_port, self.last_port + 1) if value not in taken), None)
                _require(port is not None, "dstack_runtime_ports_exhausted")
                current["local_port"] = port
                connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == intent["id"])
                    .values(payload=payload, updated_at=self.repo.clock()))
            pool, gpu_memory_gib = intent["pool"], payload["request"]["gpu_memory_gib"]
        # Original intent/run/instance bindings never change. New leased hosts
        # can reuse an IP, so their host-key pin must not reuse an older pin.
        known_hosts = _path(self.known_hosts_file.with_name(self.known_hosts_file.name + "." + binding["intent_id"]))
        if known_hosts.exists():
            info = known_hosts.stat()
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                (os.name == "nt" or not info.st_mode & (stat.S_IRWXO | stat.S_IWGRP)),
                "dstack_runtime_known_hosts_invalid")
        profile = get_profile(pair[0])
        return DstackRuntimeConfig(self.work_dir, directory, self.ssh_key_file, known_hosts,
            port, pool, PROVIDERS[binding["backend"]], pair[0], pair[1], binding["model_id"],
            binding["configuration_id"], manifest_digest, hashes,
            min_gpu_bytes=max(gpu_memory_gib * 1024**3,
                profile.get("hardware_admission", {}).get("minimum_total_vram_bytes",
                    (30 if "Pruned" in profile["model_id"] else 90) * 1024**3)),
            trust_first_host_key=self.trust_first_host_key)


def create_runtime(repo, store, *, configuration_path=None):
    """CPU controller factory. No construction-time journal, SSH or cloud call."""
    filename = configuration_path if configuration_path is not None else os.environ.get("DSTACK_RUNTIME_CONFIG")
    _require(filename is not None, "dstack_runtime_configuration_missing")
    try:
        factory = RuntimeFactory(repo, store, filename)
        runtime = DstackNativeRuntime(store, factory.config_for_binding, coordinates_for_run,
            max_hosts=factory.last_port - factory.first_port + 1)
        return runtime, factory.work_dir
    except DstackError:
        raise
    except Exception:
        raise DstackError("dstack_runtime_configuration_unavailable") from None
