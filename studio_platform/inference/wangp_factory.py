"""Fixed, operator-configured CPU adapter factory; imports never start a runtime."""

import json
import re
from pathlib import Path
import stat

from .wangp import WanGPBackend
from .wangp_compiler import H3FL2VACompiler, MODEL_ID
from .wangp_ref_compiler import H3Ref2VACompiler, COMPILER_ID as REF_COMPILER_ID
from .wangp_contract import EngineManifest
from .wangp_http import HTTPWanGPTransport
from ..runtime_hosts.wangp_http import private_token_file
from ..runtime_hosts.wangp_receipts import checked_reader


def read_document(path, *, maximum=1024 * 1024):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("wangp_absolute_configuration_required")
    with checked_reader(path, path.parent) as source:
        import os

        info = os.fstat(source.fileno())
        if os.name != "nt" and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("wangp_configuration_permissions")
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("wangp_configuration_limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("wangp_configuration_object_required")
    return value


def create_backend(slot, directory):
    # fmt: off
    """Called only by explicitly enabled fleet execution; no provider access.

The supervisor owns the private tunnel lifetime. Tokens are read directly into
the transport; they are never serialized into fleet/config/manifest identities.
"""
    # fmt: on
    if slot.spec.backend != "wangp-worker":
        raise ValueError("wangp_factory_backend_mismatch")
    value = read_document(slot.runtime_config_file, maximum=16384)
    required = {
        "version",
        "enabled",
        "slot_key",
        "configuration_id",
        "manifest_file",
        "token_file",
        "runtime_incarnation",
    }
    if (
        set(value) != required
        or not isinstance(value.get('runtime_incarnation'), str)
        or not re.fullmatch(r'[0-9a-f]{32}', value['runtime_incarnation'])
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["enabled"] is not True
        or value["configuration_id"] != slot.spec.configuration_id
    ):
        raise ValueError("wangp_runtime_configuration_mismatch")
    manifest = EngineManifest.from_dict(read_document(value["manifest_file"]))
    profile_id = manifest.document.get("deployment_profile_id")
    if profile_id is not None:
        from ..runtime_catalog import validate_manifest

        expected_model_id = validate_manifest(manifest)["model_id"]
        from .wangp_profile_dispatch import H3ProfileCompiler

        compiler = H3ProfileCompiler
    else:
        expected_model_id = MODEL_ID
        compiler = (
            H3Ref2VACompiler
            if manifest.document["compiler_id"] == REF_COMPILER_ID
            else H3FL2VACompiler
        )
    if (
        manifest.digest != slot.spec.engine_manifest_digest
        or slot.spec.model_id != expected_model_id
        or manifest.document.get("synthetic") is True
    ):
        raise ValueError("wangp_manifest_binding_mismatch")
    transport = HTTPWanGPTransport(
        slot.endpoint,
        private_token_file(value["token_file"]),
        expected_incarnation=value['runtime_incarnation'],
    )
    return WanGPBackend(
        enabled=True,
        slot_key=value["slot_key"],
        manifest=manifest,
        transport=transport,
        compiler=compiler(manifest, transport.stage_input),
        expected_incarnation=value.get('runtime_incarnation'),
    )
