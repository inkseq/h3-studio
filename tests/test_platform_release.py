"""Host release contract tests using inert flat bundles, no service starts."""
from contextlib import contextmanager
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch
import sys

DIRECTORY = Path(__file__).resolve().parents[1] / "deploy" / "platform"
COMMIT = "a"*40


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


validator = module("sixnine_release_test_validator", "check_config.py")
with patch.dict(sys.modules, {"check_config": validator}):
    release = module("sixnine_release_test", "release.py")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Only this synthetic host parent emulates root ownership; retain its
        # real mode/type and all other filesystem checks on unprivileged CI.
        lstat = Path.lstat
        def fixture_lstat(path, *args, **kwargs):
            info = lstat(path, *args, **kwargs)
            if path == self.root:
                fields = list(info); fields[4] = 0
                return os.stat_result(fields)
            return info
        ownership = patch.object(Path, "lstat", fixture_lstat)
        ownership.start(); self.addCleanup(ownership.stop)
        self.incoming = self.root / "incoming" / COMMIT
        self.incoming.mkdir(parents=True)
        (self.root / "releases").mkdir()
        hashes = {}
        for name in release.FILES:
            content = b"INERT TEST BUNDLE - never execute"
            (self.incoming / name).write_bytes(content)
            hashes[name] = hashlib.sha256(content).hexdigest()
        self.value = {"commit": COMMIT, "image": "sixnine-platform:"+COMMIT, "image_id": "sha256:"+"b"*64, "files": hashes}
        self.manifest()

    def manifest(self):
        (self.incoming / "release-manifest.json").write_text(json.dumps(self.value), encoding="utf-8")

    @contextmanager
    def approved_bundle(self):
        approval_dir = self.root / "approved-releases"
        approval_dir.mkdir()
        (approval_dir / (COMMIT+".sha256")).write_text(
            release.checksum(self.incoming / "release-manifest.json"), encoding="ascii")
        original = release.approved_manifest

        def check_approval(*args):
            # Only emulate root ownership during this check, so unprivileged
            # Linux CI still exercises real approval contents/checksums and
            # regular-file/link validation. Production permission policy stays
            # unchanged; all other filesystem operations use the real OS.
            with patch.object(release.os, "name", "nt"):
                return original(*args)

        with patch.object(release, "approved_manifest", side_effect=check_approval):
            yield

    def test_exact_commit_and_file_manifest(self):
        self.assertEqual(release.manifest(self.incoming, COMMIT), self.value)
        for commit in ("../escape", "a"*39, "a"*40+"\n"):
            with self.assertRaises(release.ReleaseError):
                release.manifest(self.incoming, commit)
        self.value["files"]["another-file"] = "a"*64
        self.manifest()
        with self.assertRaises(release.ReleaseError):
            release.manifest(self.incoming, COMMIT)

    def test_optional_contracts_are_strict_and_legacy_manifest_remains_readable(self):
        self.value['contracts'] = {'version': 1, 'api_compatibility': 'c'*64,
            'worker_compatibility': 'd'*64, 'frontend_contract': 'sixnine-web-v1'}
        self.manifest()
        self.assertEqual(release.manifest(self.incoming, COMMIT)['contracts'], self.value['contracts'])
        for key, bad in [('version', True), ('api_compatibility', 'short'),
                         ('worker_compatibility', '0'*63), ('frontend_contract', 'unknown')]:
            old = self.value['contracts'][key]
            self.value['contracts'][key] = bad
            self.manifest()
            with self.assertRaises(release.ReleaseError):
                release.manifest(self.incoming, COMMIT)
            self.value['contracts'][key] = old

    def test_tamper_rejected_before_publishing_release(self):
        (self.incoming / "compose.yaml").write_text("TAMPERED", encoding="utf-8")
        with self.assertRaises(release.ReleaseError):
            release.prepare_bundle(self.root, COMMIT)
        self.assertFalse((self.root / "releases" / COMMIT).exists())

    def test_publish_is_atomic_and_no_incoming_controller_copied(self):
        (self.incoming / "release.py").write_text("never copy", encoding="utf-8")
        with self.approved_bundle():
            result = release.prepare_bundle(self.root, COMMIT)
        self.assertEqual({p.name for p in result.iterdir()}, release.FILES | {"release-manifest.json"})
        self.assertEqual(release.manifest(result, COMMIT), self.value)
        self.assertEqual(list((self.root / "releases").iterdir()), [result])

    def test_interrupted_copy_does_not_poison_retry(self):
        original = release.os.fsync
        with patch.object(release.os, "fsync", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                release.prepare_bundle(self.root, COMMIT)
        self.assertEqual(list((self.root / "releases").iterdir()), [])
        with self.approved_bundle():
            self.assertTrue(release.prepare_bundle(self.root, COMMIT).is_dir())

    def test_swap_after_incoming_approval_does_not_publish_or_poison_retry(self):
        original_files = {path.name: path.read_bytes() for path in self.incoming.iterdir()}
        existing = self.root / "releases" / ("c"*40)
        existing.mkdir()
        preserved = existing / "existing-approved-release-marker"
        preserved.write_bytes(b"preserve existing release")
        original_prepare = release.prepare_bundle
        swap_once = True

        def swap_then_prepare(root, commit):
            nonlocal swap_once
            if swap_once:
                swap_once = False
                # A complete, self-consistent replacement with the same commit
                # arrives AFTER apply_locked checked independent approval.
                replacement = json.loads(original_files["release-manifest.json"])
                for name in release.FILES:
                    body = b"INERT UNAPPROVED REPLACEMENT " + name.encode()
                    (self.incoming / name).write_bytes(body)
                    replacement["files"][name] = hashlib.sha256(body).hexdigest()
                replacement["image_id"] = "sha256:"+"e"*64
                (self.incoming / "release-manifest.json").write_text(json.dumps(replacement), encoding="utf-8")
            return original_prepare(root, commit)

        with self.approved_bundle(), patch.object(release, "prepare_bundle", side_effect=swap_then_prepare), \
             patch.object(release, "command", side_effect=AssertionError("Docker must not be called")) as command:
            with self.assertRaisesRegex(release.ReleaseError, "release_has_no_matching_independent_approval"):
                release.apply_locked(self.root, COMMIT)
            self.assertFalse((self.root / "releases" / COMMIT).exists())
            self.assertEqual(list((self.root / "releases").iterdir()), [existing])
            self.assertFalse((self.root / "release-state.json").exists())
            self.assertEqual(preserved.read_bytes(), b"preserve existing release")
            for name, body in original_files.items():
                (self.incoming / name).write_bytes(body)
            result = release.prepare_bundle(self.root, COMMIT)
            self.assertEqual(release.manifest(result, COMMIT), self.value)
            self.assertEqual(preserved.read_bytes(), b"preserve existing release")
            command.assert_not_called()

    def test_hardlink_input_rejected(self):
        path = self.incoming / "compose.yaml"
        other = self.root / "linked"
        os.link(path, other)
        with self.assertRaises(release.ReleaseError):
            release.manifest(self.incoming, COMMIT)

    def test_nonsecret_site_config_never_inherits_environment(self):
        site = self.root / "site.env"
        site.write_text("\n".join([
            "SIXNINE_IMAGE=old-tag",
            "SIXNINE_POSTGRES_IMAGE=postgres@sha256:"+"1"*64,
            "SIXNINE_CADDY_IMAGE=caddy@sha256:"+"2"*64,
            "SIXNINE_DB_ADMIN_SECRET_FILE=/run/sixnine-secrets/db_admin_password",
            "SIXNINE_APP_DSN_SECRET_FILE=/run/sixnine-secrets/app_database_url"]), encoding="utf-8")
        # Real POSIX ownership checked separately in Linux runtime; test does not
        # need root and never reads any real credential file.
        with patch.object(release, "regular"), patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "synthetic-do-not-inherit"}):
            env = release.deployment_environment(site, COMMIT)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertEqual(env["SIXNINE_IMAGE"], "sixnine-platform:"+COMMIT)
        original = site.read_text(encoding="utf-8")
        for phase in ("0", "1"):
            site.write_text(original + "\nINKSEQ_LEGACY_REDIRECT=" + phase, encoding="utf-8")
            with patch.object(release, "regular"):
                self.assertEqual(release.deployment_environment(site, COMMIT)["INKSEQ_LEGACY_REDIRECT"], phase)
        for phase in ("true", "2", "", "0\nINKSEQ_LEGACY_REDIRECT=1"):
            site.write_text(original + "\nINKSEQ_LEGACY_REDIRECT=" + phase, encoding="utf-8")
            with patch.object(release, "regular"), self.assertRaises(release.ReleaseError):
                release.deployment_environment(site, COMMIT)
        site.write_text(original, encoding="utf-8")
        with site.open("a", encoding="utf-8") as output:
            output.write("\nAWS_SECRET_ACCESS_KEY=synthetic-only")
        with patch.object(release, "regular"), self.assertRaises(release.ReleaseError):
            release.deployment_environment(site, COMMIT)

    def test_ci_uses_platform_and_installed_controller(self):
        text = (DIRECTORY.parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('build_platform_release.py "$GITHUB_SHA"', text)
        self.assertIn('tools/deploy_aws_release.py', text)
        self.assertIn('approved_commit', text)
        self.assertNotIn('sudo -n /opt/sixnine-release/release.py', text)
        self.assertNotIn("sudo -n bash '$target/", text)

    def test_compose_serialization_version_comes_only_from_trusted_docker(self):
        environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "SIXNINE_IMAGE": "fixture-app",
                       "SIXNINE_POSTGRES_IMAGE": "fixture-db", "SIXNINE_CADDY_IMAGE": "fixture-proxy"}
        rendered = {"untrusted_bundle_claim": "5.5.1", "services": {
            name: {"image": environment[key]} for name, key in (
                ("app", "SIXNINE_IMAGE"), ("db", "SIXNINE_POSTGRES_IMAGE"), ("caddy", "SIXNINE_CADDY_IMAGE"))}}
        with patch.object(release, "command", return_value=b"v2.38.2\n") as command, \
             patch.object(release, "compose", return_value=json.dumps(rendered)) as compose, \
             patch.object(release, "validate") as validate:
            release.approved_configuration(self.incoming, environment)
        command.assert_called_once_with(["compose", "version", "--short"], environment=environment)
        compose.assert_called_once_with(self.incoming, environment, "config", "--format", "json")
        validate.assert_called_once_with(rendered, deployment_directory=self.incoming, compose_version="v2.38.2",
                                         target='production', resource_environment=environment)
        rendered["services"]["app"]["environment"] = {"PUBLIC_ORIGIN": "https://inkseq.com"}
        rendered["services"]["caddy"]["environment"] = {"INKSEQ_LEGACY_REDIRECT": "1"}
        with patch.object(release, "command", return_value=b"v2.38.2\n"), \
             patch.object(release, "compose", return_value=json.dumps(rendered)), patch.object(release, "validate"):
            with self.assertRaisesRegex(release.ReleaseError, "domain_redirect_phase_differs_from_site_config"):
                release.approved_configuration(self.incoming, environment)
            release.approved_configuration(self.incoming, {**environment, "INKSEQ_LEGACY_REDIRECT": "1"})

    def archive(self, *, extra=False, unsafe=None):
        filename = self.root / "test-image.tar.gz"
        config_value = {"os": "linux", "architecture": "amd64", "config": {"Labels": {
            "org.opencontainers.image.revision": COMMIT}}}
        self.value["image_id"] = "sha256:"+hashlib.sha256(json.dumps(config_value).encode()).hexdigest()
        config = self.value["image_id"][7:]+".json"
        record = {"Config": config, "RepoTags": [self.value["image"]], "Layers": []}
        with tarfile.open(filename, "w:gz") as output:
            for name, value in ((config, config_value), ("manifest.json", [record, record] if extra else [record])):
                data = json.dumps(value).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                output.addfile(info, io.BytesIO(data))
            if unsafe:
                info = tarfile.TarInfo(unsafe)
                output.addfile(info, io.BytesIO(b""))
        return filename

    def oci_archive(self, *, containerd=False, mutate=None):
        files = {}
        def blob(value, media_type):
            raw = json.dumps(value).encode()
            digest = 'sha256:'+hashlib.sha256(raw).hexdigest()
            files['blobs/sha256/'+digest[7:]] = raw
            return {'mediaType': media_type, 'digest': digest, 'size': len(raw)}
        config = blob({'os': 'linux', 'architecture': 'amd64', 'rootfs': {'diff_ids': []},
            'config': {'Labels': {'org.opencontainers.image.revision': COMMIT}}}, 'application/vnd.oci.image.config.v1+json')
        main = blob({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'config': config, 'layers': []}, 'application/vnd.oci.image.manifest.v1+json')
        if containerd:
            attest = blob({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                          'subject': main, 'layers': []}, 'application/vnd.oci.image.manifest.v1+json')
            attest.update(platform={'os': 'unknown', 'architecture': 'unknown'}, annotations={
                'vnd.docker.reference.type': 'attestation-manifest', 'vnd.docker.reference.digest': main['digest']})
            ref = blob({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
                'manifests': [main, attest]}, 'application/vnd.oci.image.index.v1+json')
        else:
            ref = dict(main)
        ref['annotations'] = {'io.containerd.image.name': 'docker.io/library/'+self.value['image'],
                              'org.opencontainers.image.ref.name': COMMIT}
        index = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': [ref]}
        files['index.json'] = json.dumps(index).encode()
        files['manifest.json'] = json.dumps([{'Config': 'blobs/sha256/'+config['digest'][7:],
            'RepoTags': [self.value['image']], 'Layers': []}]).encode()
        self.value['image_id'] = ref['digest'] if containerd else config['digest']
        if mutate:
            mutate(files, config, main, ref)
        filename = self.root/'test-oci.tar.gz'
        with tarfile.open(filename, 'w:gz') as output:
            for name, raw in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                output.addfile(info, io.BytesIO(raw))
        return filename, {config['digest'], main['digest'], ref['digest']}

    def test_classic_and_containerd_ids_resolve_same_single_config(self):
        for containerd in (False, True):
            filename, identities = self.oci_archive(containerd=containerd)
            self.assertEqual(set(release.validate_image_archive(filename, self.value)), identities)

    def test_oci_identity_cannot_be_replaced_by_arbitrary_digest(self):
        filename, _ = self.oci_archive()
        self.value['image_id'] = 'sha256:'+'d'*64
        with self.assertRaisesRegex(release.ReleaseError, 'approved_image_identity_not_in_archive'):
            release.validate_image_archive(filename, self.value)

    def test_oci_descriptor_tamper_and_additional_runnable_are_rejected(self):
        def corrupt(files, config, main, ref):
            files['blobs/sha256/'+main['digest'][7:]] += b' '
        filename, _ = self.oci_archive(mutate=corrupt)
        with self.assertRaisesRegex(release.ReleaseError, 'oci_descriptor_hash_or_size_mismatch'):
            release.validate_image_archive(filename, self.value)
        def extra(files, config, main, ref):
            value = json.loads(files['index.json'])
            value['manifests'].append(main)
            files['index.json'] = json.dumps(value).encode()
        filename, _ = self.oci_archive(mutate=extra)
        with self.assertRaisesRegex(release.ReleaseError, 'requires_one_oci_reference'):
            release.validate_image_archive(filename, self.value)

    def test_loaded_and_running_id_can_only_use_archive_derived_set(self):
        filename, ids = self.oci_archive()
        expected = {**self.value, 'archive_image_ids': tuple(ids)}
        for ident in ids:
            with patch.object(release, 'inspect_service', return_value={'Image': ident,
                    'State': {'Running': True, 'Health': {'Status': 'healthy'}}}):
                release.verify_running_app(self.incoming, {}, expected)
        with patch.object(release, 'inspect_service', return_value={'Image': 'sha256:'+'e'*64,
                'State': {'Running': True, 'Health': {'Status': 'healthy'}}}), self.assertRaises(release.ReleaseError):
            release.verify_running_app(self.incoming, {}, expected)

    def test_classic_build_loaded_by_containerd_keeps_exact_bound_identity(self):
        filename, identities = self.oci_archive()
        (self.incoming/'image.tar.gz').write_bytes(filename.read_bytes())
        containerd_id = next(value for value in identities if value != self.value['image_id'])
        inspected = [{'Id': containerd_id, 'Config': {'Labels': {'org.opencontainers.image.revision': COMMIT}}}]
        with patch.object(release, 'manifest', return_value=self.value), patch.object(release, 'approved_manifest'), \
             patch.object(release, 'command', side_effect=[b'', json.dumps(inspected).encode()]):
            result = release.load_approved_image(self.root, self.incoming, COMMIT, {})
        self.assertEqual(set(result['archive_image_ids']), identities)
        self.assertEqual(result['image_id'], self.value['image_id'])
        inspected[0]['Config']['Labels']['org.opencontainers.image.revision'] = 'd'*40
        with patch.object(release, 'manifest', return_value=self.value), patch.object(release, 'approved_manifest'), \
             patch.object(release, 'command', side_effect=[b'', json.dumps(inspected).encode()]), \
             self.assertRaisesRegex(release.ReleaseError, 'loaded_image_does_not_match'):
            release.load_approved_image(self.root, self.incoming, COMMIT, {})

    def test_image_archive_rejects_extra_images_before_load(self):
        release.validate_image_archive(self.archive(), self.value)
        with self.assertRaises(release.ReleaseError):
            release.validate_image_archive(self.archive(extra=True), self.value)
        with self.assertRaises(release.ReleaseError):
            release.validate_image_archive(self.archive(unsafe="../other"), self.value)

    def test_incoming_manifest_is_not_its_own_approval(self):
        approval_dir = self.root / "approved-releases"
        approval_dir.mkdir()
        with self.assertRaisesRegex(release.ReleaseError, "independent_release_approval_missing"):
            release.approved_manifest(self.root, self.incoming, COMMIT)
        file = approval_dir / (COMMIT+".sha256")
        file.write_text(release.checksum(self.incoming / "release-manifest.json"), encoding="ascii")
        # CI is unprivileged; emulate the independently provisioned root dir.
        with patch.object(release, "regular"), patch.object(release.os, "name", "nt"):
            release.approved_manifest(self.root, self.incoming, COMMIT)
            self.value["image_id"] = "sha256:"+"c"*64
            self.manifest()
            with self.assertRaises(release.ReleaseError):
                release.approved_manifest(self.root, self.incoming, COMMIT)


class ApplyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old, self.older = "c"*40, "d"*40
        self.calls = []
        self.expected = {"commit": COMMIT, "image": "sixnine-platform:"+COMMIT, "image_id": "sha256:"+"1"*64}
        self.env = {"SIXNINE_IMAGE": "sixnine-platform:"+COMMIT, "SIXNINE_POSTGRES_IMAGE": "postgres@sha256:"+"2"*64,
                    "SIXNINE_CADDY_IMAGE": "caddy@sha256:"+"3"*64}
        def compose(directory, env, *args, **kwargs):
            self.calls.append((str(directory), args))
            if args[:1] == ("config",):
                return json.dumps({"services": {"app": {"image": env["SIXNINE_IMAGE"]},
                    "db": {"image": env["SIXNINE_POSTGRES_IMAGE"]}, "caddy": {"image": env["SIXNINE_CADDY_IMAGE"]}}}).encode()
            return b""
        changes = {
            "approved_manifest": lambda *args: None,
            "prepare_bundle": lambda root, commit: root / "releases" / commit,
            "deployment_environment": lambda path, commit: {**self.env, "SIXNINE_IMAGE": "sixnine-platform:"+commit},
            "manifest": lambda *args: self.expected,
            "load_approved_image": lambda *args: self.calls.append(("load", args[2])) or self.expected,
            "validate_image_archive": lambda *args: (self.expected['image_id'],),
            "regular": lambda *args, **kwargs: None,
            "validate": lambda *args, **kwargs: None,
            "command": lambda *args, **kwargs: b"v5.5.1\n",
            "compose": compose,
            "wait_ready": lambda *args, **kwargs: None,
            "verify_running_app": lambda *args: None,
            "wait_proxy_stable": lambda *args: None,
            "gpu_deployment_context": lambda *args: None,
        }
        for name, replacement in changes.items():
            patched = patch.object(release, name, replacement)
            patched.start()
            self.addCleanup(patched.stop)

    def state(self, value=None):
        path = self.root / "release-state.json"
        if value is not None:
            value.setdefault("dependencies", release.pinned_dependencies(self.env))
            path.write_text(json.dumps(value), encoding="utf-8")
        return json.loads(path.read_text()) if path.exists() else None

    def test_success_and_same_commit_retry_keep_previous(self):
        self.state({"current": self.old, "previous": self.older, "status": "app_ready"})
        release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state()["previous"], self.old)
        previous = self.state()
        count = len(self.calls)
        release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state(), previous)
        self.assertEqual(len(self.calls), count+1)  # Read-only Compose config only.

    def test_proxy_failure_rolls_back_and_reloads_approved_old_image(self):
        self.state({"current": self.old, "previous": self.older, "status": "app_ready"})
        with patch.object(release, "wait_proxy_stable", side_effect=[release.ReleaseError("proxy_failed"), None]):
            with self.assertRaises(release.ReleaseError):
                release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state()["current"], self.old)
        self.assertEqual(self.state()["previous"], self.older)
        self.assertEqual(self.state()["status"], "rolled_back_app_only")
        self.assertEqual([call for call in self.calls if call[0] == "load"], [("load", COMMIT), ("load", self.old)])
        self.assertFalse(any("down" in args for _, args in self.calls))

    def test_first_failure_records_pending_and_requires_reconciliation(self):
        with patch.object(release, "wait_ready", side_effect=release.ReleaseError("first_failed")):
            with self.assertRaises(release.ReleaseError):
                release.apply_locked(self.root, COMMIT)
        self.assertIsNone(self.state()["current"])
        self.assertEqual(self.state()["pending"], COMMIT)
        self.assertEqual(self.state()["status"], "failed_needs_reconciliation")
        with self.assertRaisesRegex(release.ReleaseError, "another_release_requires_reconciliation"):
            release.apply_locked(self.root, self.old)

    def test_rollback_failure_never_claims_ready(self):
        self.state({"current": self.old, "previous": self.older, "status": "app_ready"})
        with patch.object(release, "wait_ready", side_effect=release.ReleaseError("both_failed")):
            with self.assertRaisesRegex(release.ReleaseError, "release_and_rollback_failed"):
                release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state()["status"], "rollback_failed_needs_reconciliation")
        self.assertEqual(self.state()["pending"], COMMIT)

    def test_host_crash_after_pending_can_resume_same_commit(self):
        self.state({"current": self.old, "previous": self.older, "pending": COMMIT, "status": "deploying"})
        release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state()["current"], COMMIT)
        self.assertEqual(self.state()["previous"], self.old)
        self.assertNotIn("pending", self.state())

    def test_dependency_change_cannot_be_smuggled_into_app_release_or_rollback(self):
        self.state({"current": self.old, "previous": self.older, "status": "app_ready"})
        before = self.state()
        with patch.object(release, "deployment_environment", return_value={**self.env,
                "SIXNINE_CADDY_IMAGE": "caddy@sha256:"+"f"*64}):
            with self.assertRaisesRegex(release.ReleaseError, "dependency_change_requires_separate_maintenance"):
                release.apply_locked(self.root, COMMIT)
        self.assertEqual(self.state(), before)
        self.assertFalse(any(call[0] == "load" or "up" in call[1] for call in self.calls))

    def test_compatible_app_release_and_rollback_do_not_restart_database_or_controller(self):
        contracts = {'version': 1, 'api_compatibility': '1'*64,
            'worker_compatibility': '2'*64, 'frontend_contract': 'sixnine-web-v1'}
        self.expected['contracts'] = contracts
        context = {'admission': 'open', 'contracts': contracts}
        for fail in (False, True):
            self.calls.clear()
            self.state({'current': self.old, 'previous': self.older, 'status': 'app_ready'})
            with patch.object(release, 'gpu_deployment_context', return_value=context), \
                    patch.object(release, 'approved_application_configuration'), \
                    patch.object(release, 'application_compose', side_effect=lambda d, e, *a, **kw:
                        self.calls.append(('application', a, kw['gpu_context']))), \
                    patch.object(release, 'wait_proxy_stable', side_effect=[release.ReleaseError('synthetic'), None] if fail else None):
                if fail:
                    with self.assertRaises(release.ReleaseError):
                        release.apply_locked(self.root, COMMIT)
                else:
                    release.apply_locked(self.root, COMMIT)
            operations = [call for call in self.calls if call[0] != 'load']
            self.assertTrue(all('db' not in call[1] and 'db-init' not in call[1]
                and 'gpu-controller' not in call[1] for call in operations))
            applications = [call for call in self.calls if call[0] == 'application']
            self.assertEqual(len(applications), 2 if fail else 1)
            self.assertTrue(all(call[2] is context for call in applications))

    def test_same_release_retry_does_not_retire_external_frontend(self):
        self.state({'current': COMMIT, 'previous': self.old, 'status': 'app_ready'})
        with patch.object(release, 'retire_frontend_pointer') as retire:
            release.apply_locked(self.root, COMMIT)
        retire.assert_not_called()

    def test_operator_app_release_and_rollback_retain_cleanup_and_ledger(self):
        contracts={'version':1,'api_compatibility':'1'*64,'worker_compatibility':'2'*64,
            'frontend_contract':'sixnine-web-v1'}
        self.expected['contracts']={**contracts,'worker_compatibility':'3'*64}
        old={**self.expected,'commit':self.old,'contracts':contracts}
        context={'kind':'operator','admission':'open','contracts':contracts,'execution_manifest':old}
        obligations=self.root/'retained-obligations.json'
        content='{"pending_stop":1,"provider_state":"unknown","reserved":17,"original_deadline":1000}'
        obligations.write_text(content)
        for fail in (False,True):
            self.calls.clear()
            self.state({'current':self.old,'previous':self.older,'status':'app_ready'})
            with patch.object(release,'gpu_deployment_context',return_value=context), \
                 patch.object(release,'approved_application_configuration'), \
                 patch.object(release,'load_approved_image',side_effect=lambda r,d,c,e:old if c==self.old else self.expected), \
                 patch.object(release,'operator_app_compatibility') as compatible, \
                 patch.object(release,'application_compose',side_effect=lambda d,e,*a,**kw:
                     self.calls.append(('application',a,d))), \
                 patch.object(release,'wait_proxy_stable',side_effect=[release.ReleaseError('synthetic'),None] if fail else None):
                if fail:
                    with self.assertRaisesRegex(release.ReleaseError,'release_failed'):release.apply_locked(self.root,COMMIT)
                    compatible.assert_called_once_with(self.root,context,old,old)
                    self.assertEqual(self.state()['status'],'rolled_back_app_only')
                    self.assertEqual(self.calls[-1][2],self.root/'releases'/self.old)
                else:
                    release.apply_locked(self.root,COMMIT)
                    self.assertEqual(self.state()['current'],COMMIT)
            self.assertEqual(len(self.calls),2 if fail else 1)
            self.assertTrue(all(call[1]==('up','-d','--no-deps','app') for call in self.calls))
            self.assertEqual(obligations.read_text(),content)


if __name__ == "__main__":
    unittest.main()
