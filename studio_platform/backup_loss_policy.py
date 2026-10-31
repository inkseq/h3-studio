"""Backup loss concerns originals; regenerable copies remain disclosed coverage."""

DERIVATIVE_ROLES = frozenset(
    {'model', 'display', 'preview', 'thumb', 'thumbnail', 'poster'}
)
EXPECTED_MISSING = frozenset({'expected_deleted', 'expected_derivative'})


def is_derivative(item):
    """Use reference roles, never a customer's filename or an object-key pattern.

    A physical object also referenced as an original must remain actionable.
    """
    refs = item.get('references')
    return bool(refs) and all(
        isinstance(ref, dict)
        and ref.get('table') in {'assets', 'artifacts'}
        and ref.get('role') in DERIVATIVE_ROLES
        for ref in refs
    )


def classification(item, evidence):
    if is_derivative(item):
        return 'expected_derivative'
    return 'expected_deleted' if item['key'] in evidence else 'unexpected_missing'


def valid_classification(item, evidence):
    expected = classification(item, evidence)
    # Old immutable manifests keep the classification made by their writer.
    legacy = 'expected_deleted' if item['key'] in evidence else 'unexpected_missing'
    return item['classification'] in {expected, legacy}


def original_loss(manifest):
    missing = manifest['missing_objects']
    coverage = manifest['coverage']
    if (
        not isinstance(missing, list)
        or len(missing) != coverage['missing_objects']
        or sum(item['size_bytes'] for item in missing) != coverage['missing_bytes']
    ):
        raise ValueError('alert_backup_manifest_coverage_invalid')
    return any(
        not is_derivative(item) and item.get('classification') != 'expected_deleted'
        for item in missing
    )
