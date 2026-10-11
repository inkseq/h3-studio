#!/usr/bin/python3
"""Root-owned release controller, installed once under /opt/sixnine-release.

Only accepts a commit ID. Never execute an incoming release script through sudo.
No AWS, DNS, GPU provisioning or credential generation occurs here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tarfile
import time

from check_config import validate

ROOT = Path("/srv/sixnine")
DOCKER = "/usr/bin/docker"
FILES = {"image.tar.gz", "compose.yaml", "Caddyfile", "init_database.py", "check_config.py"}
ENV_FIELDS = {"SIXNINE_IMAGE", "SIXNINE_POSTGRES_IMAGE", "SIXNINE_CADDY_IMAGE",
              "SIXNINE_DB_ADMIN_SECRET_FILE", "SIXNINE_APP_DSN_SECRET_FILE"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ReleaseError(code)


def regular(path, *, root_owned=False, maximum=None):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "release_file_not_regular")
    if root_owned and os.name != "nt":
        require(info.st_uid == 0 and not info.st_mode & 0o022, "release_file_not_root_controlled")
    if maximum is not None:
        require(info.st_size <= maximum, "release_file_size_exceeded")
    return info


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024*1024), b""):
            result.update(chunk)
    return result.hexdigest()


def manifest(directory, commit):
    require(bool(SHA.fullmatch(commit)), "invalid_commit")
    regular(directory / "release-manifest.json", maximum=16384)
    try:
        value = json.loads((directory / "release-manifest.json").read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise ReleaseError("invalid_release_manifest") from None
    require(isinstance(value, dict) and set(value) in ({"commit", "image", "image_id", "files"},
            {"commit", "image", "image_id", "files", "contracts"})
            and value["commit"] == commit and value["image"] == "sixnine-platform:"+commit
            and isinstance(value["image_id"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_id"])
            and isinstance(value["files"], dict) and set(value["files"]) == FILES, "invalid_release_manifest")
    if "contracts" in value:
        validate_contracts(value["contracts"])
    for name, expected in value["files"].items():
        require(isinstance(expected, str) and bool(DIGEST.fullmatch(expected)), "invalid_release_checksum")
        regular(directory / name, maximum=2*1024**3 if name == "image.tar.gz" else 1024**2)
        require(checksum(directory / name) == expected, "release_checksum_mismatch")
    return value


def validate_contracts(value):
    require(isinstance(value, dict) and set(value) == {
        "version", "api_compatibility", "worker_compatibility", "frontend_contract"}
        and type(value["version"]) is int and value["version"] == 1
        and all(isinstance(value[key], str) and DIGEST.fullmatch(value[key])
                for key in ("api_compatibility", "worker_compatibility"))
        and value["frontend_contract"] == "sixnine-web-v1", "invalid_release_contracts")
    return value


def approved_manifest(root, directory, commit):
    """Independent host approval, never written by the incoming/deploy identity.

    A self-declared checksum/revision is integrity metadata, not provenance.
    The operator approves the manifest hash from the reviewed CI artifact via
    a separate root channel. Private GitHub attestation support is not assumed.
    """
    path = root / "approved-releases" / (commit+".sha256")
    try:
        parent = path.parent.lstat()
        regular(path, root_owned=True, maximum=128)
    except FileNotFoundError:
        raise ReleaseError("independent_release_approval_missing") from None
    require(stat.S_ISDIR(parent.st_mode) and (os.name == "nt" or parent.st_uid == 0 and not parent.st_mode & 0o022),
            "release_approval_directory_not_protected")
    digest = path.read_text(encoding="ascii").strip()
    require(bool(DIGEST.fullmatch(digest)) and digest == checksum(directory / "release-manifest.json"),
            "release_has_no_matching_independent_approval")


def validate_image_archive(path, expected):
    """Bind classic/config and containerd/descriptor IDs to one archive image.

    Moby classic save writes an OCI manifest whose config digest is inspect.Id;
    containerd inspect.Id can instead be its manifest/index digest. Resolve the
    hashed descriptor chain, never simply accept an arbitrary second digest.
    """
    entries, headers, metadata, sizes, total, metadata_bytes = set(), {}, {}, {}, 0, 0
    with tarfile.open(path, mode="r|gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            require(name and name not in entries and "\\" not in name and not name.startswith("/")
                    and all(part not in {"", ".", ".."} for part in name.split("/")), "unsafe_image_archive_path")
            entries.add(name)
            require(member.isfile() or member.isdir(), "image_archive_links_forbidden")
            if member.isfile():
                sizes[name] = member.size
            total += member.size
            require(len(entries) <= 10000 and total <= 4*1024**3 and member.size <= 2*1024**3,
                    "image_archive_expansion_limit")
            if name in {"manifest.json", "index.json"}:
                require(member.isfile() and member.size <= 1024**2, "invalid_image_archive_metadata")
                headers[name] = json.load(archive.extractfile(member))
            elif member.isfile() and member.size <= 1024**2 and (
                    re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name) or re.fullmatch(r"[0-9a-f]{64}\.json", name)):
                raw = archive.extractfile(member).read()
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue  # Small layer data, not a JSON descriptor/config.
                metadata_bytes += len(raw)
                require(metadata_bytes <= 16*1024**2, "image_archive_metadata_limit")
                metadata[name] = ("sha256:"+hashlib.sha256(raw).hexdigest(), value)
    manifests = headers.get("manifest.json")
    require(isinstance(manifests, list) and len(manifests) == 1 and isinstance(manifests[0], dict),
            "image_archive_requires_one_image")
    value = manifests[0]
    require(value.get("RepoTags") == [expected["image"]]
            and isinstance(value.get("Layers"), list) and all(x in entries for x in value["Layers"])
            and all(isinstance(x, str) and x in sizes for x in value["Layers"])
            and value.get("Config") in metadata, "image_archive_identity_mismatch")
    config_digest, config = metadata[value["Config"]]
    require(value["Config"] in {config_digest.removeprefix("sha256:")+".json",
                               "blobs/sha256/"+config_digest.removeprefix("sha256:")}
            and isinstance(config, dict) and config.get("os") == "linux" and config.get("architecture") == "amd64"
            and config.get("config", {}).get("Labels", {}).get("org.opencontainers.image.revision") == expected["commit"],
            "image_config_identity_mismatch")
    identities = {config_digest}
    if "index.json" in headers:
        index = headers["index.json"]
        require(isinstance(index, dict) and index.get("schemaVersion") == 2
                and isinstance(index.get("manifests"), list) and len(index["manifests"]) == 1,
                "image_archive_requires_one_oci_reference")
        reference = index["manifests"][0]
        require(isinstance(reference, dict), "oci_image_reference_mismatch")
        annotations = reference.get("annotations", {})
        require(isinstance(annotations, dict)
                and annotations.get("io.containerd.image.name") in {expected["image"], "docker.io/library/"+expected["image"]}
                and annotations.get("org.opencontainers.image.ref.name") == expected["commit"],
                "oci_image_reference_mismatch")
        runnable, attestations, visited = [], [], set()

        def descriptor(item, depth=0):
            require(isinstance(item, dict) and depth <= 4 and len(visited) < 16, "oci_descriptor_graph_invalid")
            digest = item.get("digest")
            require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                    and digest not in visited, "oci_descriptor_graph_invalid")
            visited.add(digest)
            filename = "blobs/sha256/"+digest.removeprefix("sha256:")
            require(filename in metadata and metadata[filename][0] == digest
                    and type(item.get("size")) is int and item["size"] == sizes[filename], "oci_descriptor_hash_or_size_mismatch")
            document = metadata[filename][1]
            require(isinstance(document, dict) and document.get("schemaVersion") == 2
                    and document.get("mediaType") == item.get("mediaType"), "oci_descriptor_type_mismatch")
            if item["mediaType"] == "application/vnd.oci.image.index.v1+json":
                children = document.get("manifests")
                require(isinstance(children, list) and 1 <= len(children) <= 8, "oci_descriptor_graph_invalid")
                for child in children:
                    descriptor(child, depth+1)
            else:
                require(item["mediaType"] == "application/vnd.oci.image.manifest.v1+json", "oci_descriptor_type_mismatch")
                notes = item.get("annotations", {})
                if isinstance(notes, dict) and notes.get("vnd.docker.reference.type") == "attestation-manifest":
                    require(item.get("platform") == {"architecture": "unknown", "os": "unknown"}, "oci_attestation_invalid")
                    attestations.append((item, document))
                else:
                    runnable.append((item, document))

        descriptor(reference)
        identities.add(reference["digest"])
        require(len(runnable) == 1, "image_archive_requires_one_runnable_image")
        item, image_manifest = runnable[0]
        identities.add(item["digest"])
        cfg = image_manifest.get("config", {})
        require(isinstance(cfg, dict) and cfg.get("digest") == config_digest
                and cfg.get("size") == sizes[value["Config"]], "oci_config_binding_mismatch")
        layers = image_manifest.get("layers")
        diff_ids = config.get("rootfs", {}).get("diff_ids")
        require(isinstance(layers, list) and isinstance(diff_ids, list)
                and len(layers) == len(diff_ids) == len(value["Layers"]), "oci_layer_binding_mismatch")
        for layer, diff_id, filename in zip(layers, diff_ids, value["Layers"]):
            # Classic save can retain uncompressed diff-id files while the OCI
            # descriptor records source-layer digests. Containerd saves blobs.
            require(isinstance(layer, dict) and isinstance(layer.get("digest"), str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", layer["digest"])
                    and isinstance(diff_id, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", diff_id)
                    and filename in {"blobs/sha256/"+layer["digest"][7:], "blobs/sha256/"+diff_id[7:]},
                    "oci_layer_binding_mismatch")
        for attestation, document in attestations:
            require(attestation["annotations"].get("vnd.docker.reference.digest") == item["digest"]
                    and document.get("subject", {}).get("digest", item["digest"]) == item["digest"]
                    and isinstance(document.get("layers"), list)
                    and all(isinstance(layer, dict) and layer.get("mediaType") == "application/vnd.in-toto+json"
                            for layer in document["layers"]), "oci_attestation_invalid")
    else:
        require(value["Config"] == config_digest.removeprefix("sha256:")+".json",
                "legacy_image_id_mismatch")
    require(expected["image_id"] in identities, "approved_image_identity_not_in_archive")
    return tuple(sorted(identities))


def load_approved_image(root, directory, commit, environment):
    expected = manifest(directory, commit)
    approved_manifest(root, directory, commit)
    identities = validate_image_archive(directory / "image.tar.gz", expected)
    command(["load", "--input", str(directory / "image.tar.gz")], environment=environment, timeout=300)
    image = json.loads(command(["image", "inspect", expected["image"]], environment=environment))[0]
    require(image.get("Id") in identities
            and image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") == commit,
            "loaded_image_does_not_match_approved_release")
    return {**expected, "archive_image_ids": identities}


def deployment_environment(path, commit):
    regular(path, root_owned=True, maximum=16384)
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        require(separator == "=" and key in ENV_FIELDS and key not in values, "invalid_nonsecret_site_config")
        require(value == value.strip() and "\x00" not in value, "invalid_nonsecret_site_config")
        values[key] = value
    require(set(values) == ENV_FIELDS, "incomplete_site_config")
    for key in ("SIXNINE_POSTGRES_IMAGE", "SIXNINE_CADDY_IMAGE"):
        require(bool(re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", values[key])), "unapproved_dependency_image")
    for key, name in (("SIXNINE_DB_ADMIN_SECRET_FILE", "db_admin_password"), ("SIXNINE_APP_DSN_SECRET_FILE", "app_database_url")):
        require(values[key] == "/run/sixnine-secrets/"+name, "unexpected_runtime_secret_path")
    values["SIXNINE_IMAGE"] = "sixnine-platform:"+commit
    # No shell expansion/dotenv inheritance and no cloud credentials in children.
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
            "DOCKER_CONFIG": "/opt/sixnine-release/docker-config", **values}


def command(arguments, *, environment, timeout=180, input_data=None):
    try:
        result = subprocess.run([DOCKER, "--host", "unix:///var/run/docker.sock", *arguments],
            input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=timeout, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("container_operation_failed_no_details_logged") from None


def compose(directory, environment, *arguments, timeout=180):
    return command(["compose", "--project-directory", str(directory), "-f", str(directory / "compose.yaml"), *arguments],
                   environment=environment, timeout=timeout)


def approved_configuration(directory, environment):
    # Serialization changed across Compose versions. Trust only the installed
    # root-controlled binary's version, never a field supplied by the bundle.
    version = command(["compose", "version", "--short"], environment=environment).decode("ascii").strip()
    config = json.loads(compose(directory, environment, "config", "--format", "json"))
    validate(config, deployment_directory=directory, compose_version=version)
    require(config["services"]["app"]["image"] == environment["SIXNINE_IMAGE"]
            and config["services"]["db"]["image"] == environment["SIXNINE_POSTGRES_IMAGE"]
            and config["services"]["caddy"]["image"] == environment["SIXNINE_CADDY_IMAGE"],
            "release_images_differ_from_trusted_site_config")
    return config


def _protected_json(path, *, maximum=65536):
    regular(path, root_owned=True, maximum=maximum)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate_host_record_field")
            value[key] = item
        return value
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    require(isinstance(value, dict), "invalid_host_record")
    return value


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def protected_directory(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and not path.is_symlink()
        and (os.name == "nt" or info.st_uid == 0 and not info.st_mode & 0o022), "host_directory_not_protected")


def app_admission_overlay(root):
    policy = (root / "gpu-scaler" / "operator" / "execution-policy.json").as_posix()
    target = "/control-config/execution-policy.json"
    operator = root / 'gpu-scaler' / 'operator' / 'scaler.json'
    config = _protected_json(operator) if operator.exists() else {}
    backend = config.get('execution_backend', 'comfy-worker')
    require(backend in ('comfy-worker', 'wangp-worker'), 'gpu_execution_backend_invalid')
    health = "import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8845/healthz',timeout=5)); assert h['auth_ready'] and h['generation_enabled'] and not h['render_enabled'] and not h['cloud_creation_enabled'] and h['execution_backend']=='comfy-worker'"
    health = health.replace("=='comfy-worker'", "=="+repr(backend))
    return {"services": {"app": {"environment": {"SIXNINE_GENERATION_ENABLED": "1",
        "SIXNINE_EXECUTION_BACKEND": backend, "SIXNINE_EXECUTION_POLICY_FILE": target},
        "volumes": [{"type": "bind", "source": policy, "target": target,
            "read_only": True, "bind": {"create_host_path": False}}],
        "healthcheck": {"test": ["CMD", "python", "-c", health]}}}}


def operator_release_fence(root):
    """No platform replacement during a pinned or uncertain operator session.

Only the protected host helper marks a session inactive after its exact local
controller has exited and the existing database has no execution obligations.
An absent marker does not authorize replacing an independently running process.
"""
    path = root/'operator-capacity'/'active.json'
    if path.exists() or path.is_symlink():
        protected_directory(path.parent)
        value = _protected_json(path,maximum=16384)
        require(type(value.get('version')) is int and value['version']==1
            and value.get('active') is False and value.get('state')=='restored'
            and value.get('admission')=='closed', 'operator_capacity_requires_safe_restore')
    environment = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8',
        'DOCKER_CONFIG':'/opt/sixnine-release/docker-config'}
    running = command(['ps','--quiet','--filter','label=com.docker.compose.project=sixnine-platform',
        '--filter','label=com.docker.compose.service=operator-controller'],environment=environment,timeout=20)
    require(not running.strip(),'operator_controller_still_running')


def operator_google_title_configuration(root, execution, candidate):
    """Compare trusted Compose renders, allowing only the reviewed title wiring.

    The installed Docker parser and validator, not code from either bundle,
    interpret YAML. Secret contents are never read. Image tags and the two
    immutable release-relative binds are the only per-release normalization.
    """
    import copy
    configurations=[]
    code='operator_host_configuration_change_requires_drain'
    for value in (execution,candidate):
        directory=root/'releases'/value['commit']
        protected_directory(directory)
        for name in ('compose.yaml','check_config.py'):
            regular(directory/name,root_owned=True,maximum=1024**2)
            require(checksum(directory/name)==value.get('files',{}).get(name),code)
        environment=deployment_environment(root/'site.env',value['commit'])
        config=copy.deepcopy(approved_configuration(directory,environment))
        services=config['services']
        for name in ('app','db-init'):
            require(services[name]['image']==environment['SIXNINE_IMAGE'],code)
            services[name]['image']='reviewed-application-image'
        for name,filename,target in (('db-init','init_database.py','/bootstrap/init_database.py'),
                                     ('caddy','Caddyfile','/etc/caddy/Caddyfile')):
            mounts=[item for item in services[name].get('volumes',[])
                if item.get('target')==target]
            require(len(mounts)==1 and mounts[0].get('source')==str(directory/filename),code)
            mounts[0]['source']='reviewed-release-file/'+filename
        configurations.append(config)
    original,target=configurations
    before,after=original['services']['app'],target['services']['app']
    require('SIXNINE_TITLE_CONFIG_FILE' not in before.get('environment',{})
        and after.get('environment',{}).pop('SIXNINE_TITLE_CONFIG_FILE',None)=='/run/secrets/google_titles',code)
    require('google_titles' not in original.get('secrets',{})
        and target.get('secrets',{}).pop('google_titles',None)=={
            'name':'sixnine-platform_google_titles','file':'/run/sixnine-secrets/google_titles'},code)
    title_secret={'source':'google_titles','target':'/run/secrets/google_titles'}
    require(isinstance(after.get('secrets'),list) and after['secrets'].count(title_secret)==1
        and all(not isinstance(item,dict) or item.get('source')!='google_titles'
            for item in before.get('secrets',[])),code)
    after['secrets'].remove(title_secret)
    require('edge' not in before.get('networks',{})
        and after.get('networks',{}).pop('edge',None)=={},code)
    require(canonical_hash(original)==canonical_hash(target),code)


def operator_app_compatibility(root, pin, execution, candidate):
    """Exact independent review, not a weakening of source compatibility hashes.

    The protected receipt attests the *complete* execution-to-app source delta,
    including create_app/create_schema startup effects. It cannot authorize an
    execution image, provider config, command protocol or schema transition.
    V2 also attests the exact optional Google title configuration addition;
    every other host configuration change still requires a drain.
    """
    original=validate_contracts(execution.get('contracts'))
    target=validate_contracts(candidate.get('contracts'))
    require(original['frontend_contract']==target['frontend_contract'],
        'operator_frontend_contract_changed')
    unchanged=all(candidate.get('files',{}).get(name)==execution.get('files',{}).get(name)
        and isinstance(execution.get('files',{}).get(name),str)
        for name in ('compose.yaml','Caddyfile','init_database.py','check_config.py'))
    require(unchanged or all(candidate.get('files',{}).get(name)==execution.get('files',{}).get(name)
        and isinstance(execution.get('files',{}).get(name),str)
        for name in ('Caddyfile','init_database.py')),
        'operator_host_configuration_change_requires_drain')
    if candidate['commit']==execution['commit']:
        require(unchanged and all(candidate.get(key)==value for key,value in execution.items()),
            'operator_execution_manifest_changed')
        return
    directory=root/'approved-app-compatibility'
    protected_directory(directory)
    path=directory/(execution['commit']+'--'+candidate['commit']+'.json')
    value=_protected_json(path,maximum=16384)
    expected={'schema_version':1,'execution_commit':execution['commit'],
        'execution_image_id':pin['image_id'],'prepared_sha256':pin['prepared_hash'],
        'execution_manifest_sha256':checksum(root/'releases'/execution['commit']/'release-manifest.json'),
        'app_commit':candidate['commit'],
        'app_manifest_sha256':checksum(root/'releases'/candidate['commit']/'release-manifest.json'),
        'review':{'database_schema_and_startup_migrations':'unchanged',
            'generation_admission':'reviewed_compatible_safety_tightening','operator_commands':'unchanged',
            'accepted_jobs_and_attempts':'unchanged','controller_and_guardian':'preserved'}}
    if not unchanged:
        require(type(value.get('schema_version')) is int and value['schema_version']==2,
            'operator_host_configuration_change_requires_drain')
        expected['schema_version']=2
        expected['review']['app_configuration']='reviewed_google_titles_only'
    require(type(value.get('schema_version')) is int and value==expected,
        'operator_app_compatibility_review_mismatch')
    if not unchanged:
        operator_google_title_configuration(root,execution,candidate)


def operator_execution_context(root):
    # Import only the installed protected helper, never the incoming bundle.
    import operator_capacity as operator
    require(operator.ROOT==root/'operator-capacity','operator_host_root_mismatch')
    runtime,prepared,directory,environment=operator.prepared()
    pin=operator.checked_pin(prepared)
    execution=manifest(directory,prepared['commit'])
    return operator,runtime,pin,execution,environment


def operator_deployment_context(root, target_manifest=None):
    path=root/'operator-capacity'/'active.json'
    if not path.exists() and not path.is_symlink(): return None
    protected_directory(path.parent)
    marker=_protected_json(path,maximum=16384)
    if marker.get('active') is not True: return None
    operator,runtime,pin,execution,environment=operator_execution_context(root)
    require(pin.get('version')==1 and pin.get('active') is True and pin.get('state')=='running'
        and isinstance(pin.get('controller_id'),str) and bool(pin['controller_id']),
        'operator_execution_state_unknown')
    state=operator.inspect_controller(environment,pin)
    require(state.get('Running') is True and state.get('Restarting') is False
        and state.get('OOMKilled') is False,'operator_controller_not_running')
    raw=command(['ps','--quiet','--filter','label=com.docker.compose.project=sixnine-platform',
        '--filter','label=com.docker.compose.service=operator-controller'],environment=environment,timeout=20).decode().strip()
    require(bool(re.fullmatch(r'[0-9a-f]{12,64}',raw)),'operator_controller_not_uniquely_running')
    # Resolve the listed container as well as the pinned name: an additional or
    # substituted controller must not pass merely because the pin still exists.
    observed=json.loads(command(['inspect',raw],environment=environment,timeout=20))
    require(isinstance(observed,list) and len(observed)==1
        and observed[0].get('Name')=='/'+pin['container_name']
        and observed[0].get('Image')==pin['image_id'],'operator_controller_identity_changed')
    proof=operator.receipt(pin,fresh=True)
    require(proof.get('state') in ('running','degraded','draining','shutdown_waiting'),
        'operator_controller_status_unknown')
    if target_manifest is not None: operator_app_compatibility(root,pin,execution,target_manifest)
    # Original overlay and preparation remain immutable. This derived file is
    # app-only and only written while holding the existing release lock.
    overlay=operator.app_overlay(admission=pin['admission'])
    overlay_path=root/'operator-capacity'/'app-release-overlay.json'
    if overlay_path.exists() or overlay_path.is_symlink(): regular(overlay_path,root_owned=True,maximum=16384)
    operator.atomic(overlay_path,overlay)
    return {**pin,'kind':'operator','root':root,'contracts':execution['contracts'],
        'execution_manifest':execution,'app_overlay':overlay,'overlay_path':overlay_path}


def gpu_deployment_context(root, target_manifest=None):
    """Validate a pinned v2 controller without changing its lifecycle or ledger.

    Caller holds release.lock. Old/unknown barriers remain strict; compatibility
    permits an app replacement, never a controller restart or policy change.
    """
    context = operator_deployment_context(root,target_manifest)
    if context is None: operator_release_fence(root)
    for folder in ("gpu-acceptance", "gpu-scaler"):
        path = root / folder / "active.json"
        if not path.exists() and not path.is_symlink():
            continue
        protected_directory(root / folder)
        value = _protected_json(path, maximum=16384)
        require(type(value.get("active")) is bool and type(value.get("version")) is int
            and value["version"] in (1, 2), "gpu_acceptance_requires_explicit_safe_restore")
        if not value["active"]:
            continue
        require(context is None,'operator_competing_controller')
        require(folder == "gpu-scaler" and value["version"] == 2,
            "gpu_acceptance_requires_explicit_safe_restore")
        required = {"version", "active", "commit", "image_id", "contracts", "cycle_id", "config_hash",
                    "container_name", "admission", "updated_at"}
        require(set(value) == required and isinstance(value["commit"], str) and SHA.fullmatch(value["commit"])
            and isinstance(value["image_id"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_id"])
            and isinstance(value["config_hash"], str) and DIGEST.fullmatch(value["config_hash"])
            and value["admission"] in ("open", "closed"), "gpu_execution_pin_invalid")
        validate_contracts(value["contracts"])
        for parent in (root / folder / "operator", root / folder / "public-source"):
            protected_directory(parent)
        config = _protected_json(root / folder / "operator" / "scaler.json")
        policy = _protected_json(root / folder / "operator" / "execution-policy.json")
        require(value["config_hash"] == canonical_hash(config)
            and value["cycle_id"] == config.get("cycle_id")
            and isinstance(value["cycle_id"], str)
            and value["container_name"] == "sixnine-finite-"+hashlib.sha256(value["cycle_id"].encode()).hexdigest()[:20]
            and config.get("execution_policy_sha256") == canonical_hash(policy), "gpu_execution_config_changed")
        sources = config.get("source_sha256")
        backend = config.get('execution_backend', 'comfy-worker')
        require(backend in ('comfy-worker', 'wangp-worker') and policy.get('backend', 'comfy-worker') == backend
            and policy.get('engine_manifest_digest', '') == config.get('engine_manifest_digest', ''), 'gpu_execution_engine_changed')
        expected_sources = ({'bootstrap_cloud.py', 'model_manifest.json'} if backend == 'comfy-worker' else
            {'wangp-bootstrap.py', 'wangp-manifest.json', 'wangp-runtime.json', 'wangp-package.tar.gz'})
        require(isinstance(sources, dict) and set(sources) == expected_sources,
            "gpu_execution_sources_invalid")
        for filename, digest in sources.items():
            source = root / folder / "public-source" / filename
            regular(source, root_owned=True, maximum=16*1024**2 if filename.endswith('.gz') else 2*1024**2)
            require(isinstance(digest, str) and DIGEST.fullmatch(digest) and checksum(source) == digest,
                "gpu_execution_sources_changed")
        if backend == 'wangp-worker':
            runtime = _protected_json(root/folder/'public-source'/'wangp-runtime.json', maximum=524288)
            require(runtime.get('dependency_artifact_path') == '/root/sixnine-cache/wangp-dependencies.tar.gz'
                and not runtime.get('dependency_artifact_url') and not runtime.get('prepared_root'),
                'gpu_execution_dependency_source_invalid')
            archive = root/folder/'public-source'/'wangp-dependencies.tar.gz'
            regular(archive, root_owned=True, maximum=32*1024**3)
            require(checksum(archive) == runtime.get('dependency_artifact_sha256'),
                'gpu_execution_dependency_changed')
        directory = root / "releases" / value["commit"]
        approved_manifest(root, directory, value["commit"])
        expected = manifest(directory, value["commit"])
        require(expected.get("contracts") == value["contracts"], "gpu_execution_contract_changed")
        identities = validate_image_archive(directory / "image.tar.gz", expected)
        require(value["image_id"] in identities, "gpu_execution_image_unapproved")
        if target_manifest is not None:
            contracts = validate_contracts(target_manifest.get("contracts"))
            require(contracts["worker_compatibility"] == value["contracts"]["worker_compatibility"]
                and contracts["frontend_contract"] == value["contracts"]["frontend_contract"],
                "gpu_runtime_change_requires_drain")
            require(all(target_manifest.get("files", {}).get(name) == expected.get("files", {}).get(name)
                and isinstance(expected.get("files", {}).get(name), str)
                for name in ("compose.yaml", "Caddyfile", "init_database.py", "check_config.py")),
                "gpu_host_configuration_change_requires_drain")
        overlay_path = root / folder / "app-admission.json"
        require(_protected_json(overlay_path) == app_admission_overlay(root), "gpu_app_admission_overlay_changed")
        context = {**value, "root": root, "overlay_path": overlay_path}
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                   "DOCKER_CONFIG": "/opt/sixnine-release/docker-config"}
    for service in ("gpu-worker", "gpu-controller"):
        raw = command(["ps", "--quiet", "--filter", "label=com.docker.compose.project=sixnine-platform",
                       "--filter", "label=com.docker.compose.service="+service], environment=environment, timeout=20).decode().strip()
        if context is None or context.get('kind')=='operator' or service != "gpu-controller":
            require(not raw, "gpu_acceptance_worker_still_running")
            continue
        # A lost/exited/unknown controller is not evidence that admission is safe.
        require(bool(re.fullmatch(r"[0-9a-f]{12,64}", raw)), "gpu_controller_not_uniquely_running")
        values = json.loads(command(["inspect", raw], environment=environment, timeout=20))
        require(isinstance(values, list) and len(values) == 1, "gpu_controller_identity_unknown")
        observed = values[0]
        labels = observed.get("Config", {}).get("Labels", {})
        state = observed.get("State", {})
        require(observed.get("Name") == "/"+context["container_name"] and observed.get("Image") == context["image_id"]
            and labels.get("com.docker.compose.project") == "sixnine-platform"
            and labels.get("com.docker.compose.service") == "gpu-controller"
            and labels.get("com.sixnine.finite.config-hash") == context["config_hash"]
            and state.get("Running") is True and state.get("Restarting") is False and state.get("OOMKilled") is False,
            "gpu_controller_identity_mismatch")
    return context


def application_compose(directory, environment, *arguments, gpu_context=None, timeout=180):
    if gpu_context is None or gpu_context["admission"] == "closed":
        return compose(directory, environment, *arguments, timeout=timeout)
    return command(["compose", "--project-directory", str(directory), "-f", str(directory / "compose.yaml"),
        "-f", str(gpu_context["overlay_path"]), *arguments], environment=environment, timeout=timeout)


def approved_application_configuration(directory, environment, *, gpu_context=None):
    if gpu_context is None or gpu_context["admission"] == "closed":
        return approved_configuration(directory, environment)
    import copy
    version = command(["compose", "version", "--short"], environment=environment).decode("ascii").strip()
    original = json.loads(application_compose(directory, environment, "config", "--format", "json", gpu_context=gpu_context))
    if gpu_context.get('kind')=='operator':
        import operator_capacity as operator
        operator.validate_app_rendered(original,directory,version,environment['SIXNINE_IMAGE'],gpu_context['app_overlay'])
        require(original['services']['db']['image']==environment['SIXNINE_POSTGRES_IMAGE']
            and original['services']['caddy']['image']==environment['SIXNINE_CADDY_IMAGE'],
            'release_images_differ_from_trusted_site_config')
        return original
    config = copy.deepcopy(original)
    app = config.get("services", {}).get("app", {})
    expected = app_admission_overlay(gpu_context["root"])["services"]["app"]
    env = app.get("environment", {})
    require(all(env.get(k) == v for k, v in expected["environment"].items()), "gpu_app_admission_settings_invalid")
    env.pop("SIXNINE_EXECUTION_POLICY_FILE")
    env.update(SIXNINE_GENERATION_ENABLED="0", SIXNINE_EXECUTION_BACKEND="disabled")
    mounts = app.get("volumes", [])
    if version in ("2.38.2", "v2.38.2"):
        for mount in mounts:
            if mount.get("type") == "bind" and mount.get("bind") == {}:
                mount["bind"] = {"create_host_path": False}
    require(mounts.count(expected["volumes"][0]) == 1, "gpu_app_policy_mount_invalid")
    mounts.remove(expected["volumes"][0])
    require(app.get("healthcheck", {}).get("test") == expected["healthcheck"]["test"], "gpu_app_health_invalid")
    app["healthcheck"]["test"][3] = "import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8845/healthz',timeout=5)); assert h['auth_ready'] and not h['generation_enabled'] and not h['render_enabled'] and not h['cloud_creation_enabled'] and h['execution_backend']=='disabled'"
    validate(config, deployment_directory=directory, compose_version=version)
    require(app["image"] == environment["SIXNINE_IMAGE"]
        and config["services"]["db"]["image"] == environment["SIXNINE_POSTGRES_IMAGE"]
        and config["services"]["caddy"]["image"] == environment["SIXNINE_CADDY_IMAGE"], "release_images_differ_from_trusted_site_config")
    return original


def current_application(root=ROOT):
    state = _protected_json(root / "release-state.json", maximum=16384)
    commit = state.get("current")
    require(isinstance(commit, str) and SHA.fullmatch(commit) and state.get("pending") is None
        and state.get("status") in ("app_ready", "rolled_back_app_only"), "current_application_requires_reconciliation")
    directory = root / "releases" / commit
    approved_manifest(root, directory, commit)
    expected = manifest(directory, commit)
    environment = deployment_environment(root / "site.env", commit)
    approved_configuration(directory, environment)
    identities = validate_image_archive(directory / "image.tar.gz", expected)
    images = json.loads(command(["image", "inspect", environment["SIXNINE_IMAGE"]], environment=environment))
    require(isinstance(images, list) and len(images) == 1 and images[0].get("Id") in identities,
        "current_application_image_unapproved")
    return commit, directory, environment


def restore_current_cpu_locked(root=ROOT, *, operator_pin=None):
    """Caller holds release.lock; never reuse a controller's old app directory."""
    if operator_pin is None:
        operator_release_fence(root)
    else:
        # Only the protected helper closes its own admission; compatibility is
        # rechecked against the immutable execution release below.
        protected_directory(root/'operator-capacity')
        require(_protected_json(root/'operator-capacity'/'active.json')==operator_pin
            and operator_pin.get('version')==1 and operator_pin.get('active') is True
            and operator_pin.get('admission')=='closed', 'operator_admission_pin_invalid')
    commit, directory, environment = current_application(root)
    if operator_pin is not None:
        _,_,checked,execution,_=operator_execution_context(root)
        require(checked==operator_pin,'operator_admission_pin_invalid')
        operator_app_compatibility(root,operator_pin,execution,manifest(directory,commit))
    application_compose(directory, environment, "up", "-d", "--no-deps", "app")
    wait_ready(directory, environment)
    expected = manifest(directory, commit)
    expected = {**expected, "archive_image_ids": validate_image_archive(directory / "image.tar.gz", expected)}
    verify_running_app(directory, environment, expected)
    return commit


def retire_frontend_pointer(root):
    """A new API image starts with its own UI; keep external assets for rollback.

    Only a root-owned pointer is renamed. No release directory or asset is
    removed, and same-commit retries never enter this path.
    """
    directory = root / "frontend"
    if not directory.exists() and not directory.is_symlink():
        return
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and not directory.is_symlink()
        and (os.name == "nt" or info.st_uid == 0 and not info.st_mode & 0o022), "frontend_directory_not_protected")
    source, previous = directory / "current.json", directory / "previous-platform-pointer.json"
    if not source.exists() and not source.is_symlink():
        return
    regular(source, root_owned=True, maximum=16384)
    if previous.exists() or previous.is_symlink():
        regular(previous, root_owned=True, maximum=16384)
    source.replace(previous)
    sync_directory(directory)


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def prepare_bundle(root, commit):
    incoming, target = root / "incoming" / commit, root / "releases" / commit
    require(not incoming.is_symlink() and incoming.is_dir(), "incoming_release_missing")
    expected = manifest(incoming, commit)
    if target.exists():
        require(not target.is_symlink() and target.is_dir(), "invalid_existing_release")
        require(manifest(target, commit) == expected, "existing_release_differs")
        for filename in FILES | {"release-manifest.json"}:
            regular(target / filename, root_owned=True)
        return target
    # Private staging prevents a interrupted/tampered incoming bundle from
    # publishing partial files or poisoning the final commit directory.
    staging = Path(tempfile.mkdtemp(prefix=".release-", dir=root / "releases"))
    try:
        for name in FILES | {"release-manifest.json"}:
            descriptor = os.open(incoming / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source, (staging / name).open("xb") as destination:
                metadata = os.fstat(source.fileno())
                maximum = 2*1024**3 if name == "image.tar.gz" else 1024**2
                require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
                        and metadata.st_size <= maximum, "incoming_file_changed")
                copied = 0
                while chunk := source.read(1024*1024):
                    copied += len(chunk)
                    require(copied <= maximum, "incoming_file_grew")
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            (staging / name).chmod(0o644)
        require(manifest(staging, commit) == expected, "copied_release_differs")
        # Incoming remains writable by the deploy identity. Bind the private
        # staged copy to independent approval BEFORE publishing its commit
        # directory, so a swap after the initial check cannot poison retries.
        approved_manifest(root, staging, commit)
        staging.rename(target)
        target.chmod(0o755)
        sync_directory(target.parent)
    finally:
        # Only our fresh, flat temporary directory; never traverse a release,
        # persistent data, incoming upload or a symbolic link for cleanup.
        if staging.exists():
            for name in FILES | {"release-manifest.json"}:
                (staging / name).unlink(missing_ok=True)
            staging.rmdir()
    return target


def check_host(root):
    require(os.name == "posix" and os.geteuid() == 0, "root_owned_host_controller_required")
    require(root == ROOT and not root.is_symlink(), "unexpected_deployment_root")
    executable = regular(Path(DOCKER), root_owned=True)
    require(bool(executable.st_mode & 0o111), "trusted_docker_executable_missing")
    from preflight_host import check_host as preflight, PreflightError
    try:
        preflight()
    except PreflightError as error:
        raise ReleaseError(str(error)) from None
    for path in (root, root / "incoming", root / "releases", root / "approved-releases"):
        info = path.lstat()
        forbidden = 0o002 if path.name == "incoming" else 0o022
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & forbidden, "deployment_parent_not_protected")
    for name in ("platform-data", "upload-spool", "postgres"):
        path = root / name
        require(path.is_dir() and not path.is_symlink(), "persistent_directory_missing")
    for name in ("db_admin_password", "app_database_url"):
        path = Path("/run/sixnine-secrets") / name
        info = regular(path, root_owned=True, maximum=16384)
        require(not info.st_mode & 0o007, "runtime_secret_world_accessible")
    require(shutil.disk_usage(root).free >= 3*1024**3, "insufficient_release_disk_headroom")


def wait_ready(directory, environment, seconds=120):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        raw = compose(directory, environment, "ps", "--format", "json", "app", timeout=20)
        try:
            parsed = json.loads(raw)
            entries = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            entries = [json.loads(line) for line in raw.splitlines() if line]
        if any(value.get("Service") == "app" and value.get("Health") == "healthy" for value in entries):
            return
        time.sleep(3)
    raise ReleaseError("application_not_ready_existing_data_preserved")


def inspect_service(directory, environment, service):
    raw = compose(directory, environment, "ps", "--all", "--quiet", service, timeout=20).decode().strip()
    require(bool(re.fullmatch(r"[0-9a-f]{12,64}", raw)), "service_container_not_unique")
    values = json.loads(command(["inspect", raw], environment=environment, timeout=20))
    require(isinstance(values, list) and len(values) == 1, "service_container_not_unique")
    value = values[0]
    labels = value.get("Config", {}).get("Labels", {})
    require(labels.get("com.docker.compose.project") == "sixnine-platform"
            and labels.get("com.docker.compose.service") == service, "service_container_identity_mismatch")
    return value


def verify_running_app(directory, environment, expected):
    value = inspect_service(directory, environment, "app")
    require(value.get("Image") in expected.get("archive_image_ids", (expected["image_id"],))
            and value.get("State", {}).get("Running") is True
            and value.get("State", {}).get("Health", {}).get("Status") == "healthy",
            "running_app_does_not_match_release")


def wait_proxy_stable(directory, environment, *, observations=4):
    expected_id = json.loads(command(["image", "inspect", environment["SIXNINE_CADDY_IMAGE"]], environment=environment))[0]["Id"]
    previous = None
    for index in range(observations):
        current = inspect_service(directory, environment, "caddy")
        require(current.get("Image") == expected_id and current.get("State", {}).get("Running") is True
                and current.get("State", {}).get("Restarting") is not True, "proxy_not_stably_running")
        identity = (current.get("Id"), current.get("RestartCount"))
        require(previous is None or identity == previous, "proxy_restarted_during_release")
        previous = identity
        if index+1 < observations:
            time.sleep(3)


def migration_deployment_enabled(root):
    """A reviewed root activation makes future ordinary deploys keep the overlay."""
    path = root / "migration" / "active.json"
    if not path.exists() and not path.is_symlink():
        return False
    regular(path, root_owned=True, maximum=16384)
    value = json.loads(path.read_text())
    require(type(value) is dict and type(value.get("version")) is int and value["version"] == 1 and type(value.get("active")) is bool,
            "migration_activation_record_invalid")
    # A disabled marker is not a rollback instruction. Retire separately after
    # original accepted jobs/broker state and provider obligations are reviewed.
    require(value["active"] is True, "migration_retirement_requires_explicit_maintenance")
    return True


def migration_helper():
    """Load only the separately installed root trust path, never incoming code."""
    import importlib.util
    path = Path(__file__).with_name("migration_maintenance.py")
    regular(path, root_owned=True, maximum=1024**2)
    spec = importlib.util.spec_from_file_location("sixnine_installed_migration_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_locked(root, commit):
    if migration_deployment_enabled(root):
        return migration_helper().apply_locked(root, commit)
    # Avoid filling root-owned releases with unapproved multi-gigabyte bundles.
    approved_manifest(root, root / "incoming" / commit, commit)
    directory = prepare_bundle(root, commit)
    approved_manifest(root, directory, commit)
    environment = deployment_environment(root / "site.env", commit)
    state, previous = {}, None
    state_file = root / "release-state.json"
    if state_file.exists():
        regular(state_file, root_owned=True, maximum=16384)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        previous = state.get("current")
        require(previous is None or isinstance(previous, str) and SHA.fullmatch(previous), "invalid_previous_release")
        pending = state.get("pending")
        require(pending in (None, commit), "another_release_requires_reconciliation")
    dependencies = pinned_dependencies(environment)
    if previous or state.get("dependencies") is not None:
        require(state.get("dependencies") == dependencies, "dependency_change_requires_separate_maintenance")
    state = {**state, "dependencies": dependencies}
    expected = manifest(directory, commit)
    # This guard belongs to the shared apply path, not just the CD wrapper:
    # a direct root release must not bypass an active lifecycle's contract.
    gpu_context = gpu_deployment_context(root, expected)
    approved_application_configuration(directory, environment, gpu_context=gpu_context)
    if previous == commit and state.get("status") in {"app_ready", "rolled_back_app_only"}:
        # A lost SSH response must not turn previous into a self-reference.
        identities = validate_image_archive(directory / "image.tar.gz", expected)
        verify_running_app(directory, environment, {**expected, "archive_image_ids": identities})
        wait_proxy_stable(directory, environment)
        return
    fallback = state.get("previous") if previous == commit else previous
    require(fallback is None or isinstance(fallback, str) and SHA.fullmatch(fallback), "invalid_fallback_release")
    # 'current' remains last CONFIRMED version while pending names the attempt.
    # A process/host crash can therefore be resumed, never silently called ready.
    write_state(root, {**state, "current": previous, "pending": commit,
                      "status": "deploying", "updated_at": time.time()})
    try:
        expected = load_approved_image(root, directory, commit, environment)
        if gpu_context is None:
            compose(directory, environment, "up", "-d", "db")
            compose(directory, environment, "run", "--rm", "db-init")
        retire_frontend_pointer(root)
        application_compose(directory, environment, "up", "-d", "--no-deps", "app", gpu_context=gpu_context)
        wait_ready(directory, environment)
        verify_running_app(directory, environment, expected)
        if gpu_context is None:
            compose(directory, environment, "up", "-d", "--no-deps", "caddy")
        wait_proxy_stable(directory, environment)
        write_state(root, {"current": commit, "previous": fallback, "dependencies": dependencies,
                          "updated_at": time.time(), "status": "app_ready"})
    except Exception:
        if fallback and fallback != commit:
            try:
                old_directory = root / "releases" / fallback
                old_environment = deployment_environment(root / "site.env", fallback)
                old_expected = load_approved_image(root, old_directory, fallback, old_environment)
                if gpu_context is not None:
                    old_contracts = validate_contracts(old_expected.get("contracts"))
                    if gpu_context.get('kind')=='operator':
                        operator_app_compatibility(root,gpu_context,gpu_context['execution_manifest'],old_expected)
                    else:
                        require(old_contracts["worker_compatibility"] == gpu_context["contracts"]["worker_compatibility"],
                            "gpu_rollback_contract_requires_reconciliation")
                approved_application_configuration(old_directory, old_environment, gpu_context=gpu_context)
                application_compose(old_directory, old_environment, "up", "-d", "--no-deps", "app", gpu_context=gpu_context)
                wait_ready(old_directory, old_environment)
                verify_running_app(old_directory, old_environment, old_expected)
                if gpu_context is None:
                    compose(old_directory, old_environment, "up", "-d", "--no-deps", "caddy")
                wait_proxy_stable(old_directory, old_environment)
                write_state(root, {"current": fallback, "previous": state.get("previous"), "failed_release": commit, "dependencies": dependencies,
                                  "updated_at": time.time(), "status": "rolled_back_app_only"})
            except Exception:
                write_state(root, {**state, "current": previous, "pending": commit,
                                  "updated_at": time.time(), "status": "rollback_failed_needs_reconciliation"})
                raise ReleaseError("release_and_rollback_failed_no_ready_claim") from None
        else:
            write_state(root, {**state, "current": previous, "pending": commit,
                              "updated_at": time.time(), "status": "failed_needs_reconciliation"})
        raise ReleaseError("release_failed_check_readiness_and_previous_release") from None


def pinned_dependencies(environment):
    return {name: environment[name] for name in ("SIXNINE_POSTGRES_IMAGE", "SIXNINE_CADDY_IMAGE")}


def write_state(root, state):
    temporary = root / "release-state.next"
    with temporary.open("w", encoding="utf-8") as destination:
        json.dump(state, destination, indent=2)
        destination.flush()
        os.fsync(destination.fileno())
    temporary.replace(root / "release-state.json")
    sync_directory(root)


def apply(commit):
    import fcntl
    require(bool(SHA.fullmatch(commit)), "invalid_commit")
    check_host(ROOT)
    with (ROOT / "release.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseError("another_release_is_in_progress") from None
        apply_locked(ROOT, commit)


def main():
    try:
        require(len(sys.argv) == 2, "one_commit_argument_required")
        apply(sys.argv[1])
        print("Sixnine application release is healthy; DNS, public TLS and real inference require separate verification")
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ReleaseError) else "release_failed_details_suppressed"
        print(code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
