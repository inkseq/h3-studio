"""Pure prepared-image bootstrap document shared by build and CPU runtime.

No source packaging, filesystem reads, credentials or deployment operations are
needed to construct the exact reviewed native runtime document.
"""

PREPARED_ROOT = "/opt/workspace-internal/Wan2GP"
MODEL_ROOT = "/root/sixnine-cache/models"


def prepared_runtime_document(profile_id, index, count, bundle_hash):
    remote = "/workspace/h3-studio/profile-slot-" + str(index)
    install = "/root/sixnine-cache/operator/profile-slot-" + str(index)
    return {
        "version": 1, "deployment_profile_id": profile_id,
        "profile_slot_index": index, "expected_host_gpus": count,
        "install_root": install, "source_bundle_path": remote + "/wangp-package.tar.gz",
        "source_bundle_sha256": bundle_hash,
        "dependency_artifact_url": "", "dependency_artifact_path": "",
        # Required schema field, unused with prepared_root; zero is deliberately
        # not a claim that a dependency archive has been built or verified.
        "dependency_artifact_sha256": "0" * 64,
        "manifest_path": remote + "/wangp-manifest.json", "model_root": MODEL_ROOT,
        "config_path": install + "/wgp_config.json", "status_path": remote + "/setup-status.json",
        "port": 8199 + index, "prepared_root": PREPARED_ROOT,
    }
