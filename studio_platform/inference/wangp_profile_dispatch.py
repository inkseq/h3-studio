"""Select the immutable compiler epoch bound by the deployment profile.

Retained manifests keep their original compiler and validation semantics. New
native inputs never change a previously accepted request's mapping implicitly.
"""
from ..runtime_catalog import get_profile


def compiler_module(profile_id):
    profile = get_profile(profile_id)
    epoch = profile.get("compiler_epoch", "native-profile-v1")
    if epoch == "native-profile-v1":
        from . import wangp_profile_compiler
        return wangp_profile_compiler
    if epoch == "full-controls-v2":
        from . import wangp_full_compiler
        return wangp_full_compiler
    raise ValueError("wangp_profile_compiler_epoch_unsupported")


def control_schema(profile_id, mode):
    return compiler_module(profile_id).control_schema(profile_id, mode)


def normalize_request(request, metadata, output_spec, profile_id):
    return compiler_module(profile_id).normalize_request(request, metadata, output_spec, profile_id)


def compile_settings(request, metadata, output_spec, handles, profile_id):
    return compiler_module(profile_id).compile_settings(request, metadata, output_spec, handles, profile_id)


def H3ProfileCompiler(manifest, stage_input):
    return compiler_module(manifest.document["deployment_profile_id"]).H3ProfileCompiler(manifest, stage_input)


def validate_prepared(prepared, manifest):
    return compiler_module(manifest.document["deployment_profile_id"]).validate_prepared(prepared, manifest)
