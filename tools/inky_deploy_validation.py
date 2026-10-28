"""Compatibility entry for the shared, root-qualified Inky mount validator."""

from deploy.platform.deployment_target import inky_mounts


def validate_inky(document, *, target='staging'):
    inky_mounts(document, target=target)
