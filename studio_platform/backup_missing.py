"""Explicit missing-object observations; partial Local objects remain corruption."""

import time

from .storage import LocalObjectStore
from .backup_loss_policy import classification


def missing_object(item, store, evidence):
    # Imported lazily because backup owns the public error contract.
    from .backup import BackupError

    provider = item.get('provider', 'local')
    if provider == 'local':
        if not isinstance(store, LocalObjectStore):
            raise BackupError('backup_local_absence_unconfirmed')
        # stat() may also fail when only meta.json/blob is missing. Only an
        # absent immutable object directory qualifies; links/access errors and
        # existing incomplete directories must still stop publication.
        try:
            store._directory(item['key']).lstat()
        except FileNotFoundError:
            observation = 'local_absent'
        else:
            raise BackupError('backup_local_object_incomplete')
    elif provider == 'r2':
        observation = 'head'
    else:
        raise BackupError('backup_source_provider_not_configured')
    result = dict(
        item,
        reason='source_object_not_found',
        observation=observation,
        observed_at=time.time(),
    )
    if evidence is not None:
        result.update(
            classification=classification(item, evidence),
            deletion_evidence=evidence.get(item['key'], []),
        )
    return result
