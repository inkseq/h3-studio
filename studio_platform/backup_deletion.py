"""Read-only same-snapshot proof of expected missing content; never deletes."""

import hashlib
import json
import math
import uuid


def _token(kind, value, binding=None):
    return (
        kind
        + ":"
        + hashlib.sha256((str(binding or "") + "\0" + value).encode()).hexdigest()
    )


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def deletion_evidence(db, references):
    """Return proofs only when every immutable reference has closed evidence.

    Missing, malformed, ambiguous or unfinished proof is UNKNOWN, never expected.
    The caller still must observe a genuine HEAD ObjectNotFound before using it.
    """
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_quick_chat_objects'"
    ).fetchone():
        return {}
    objects = {}
    tombstones = {}
    for ident, tenant, owner, session, kind, business, payload in db.execute(
        "SELECT id,tenant,owner,session_id,kind,business_key,payload FROM platform_quick_chat_objects"
    ):
        value = json.loads(payload)
        row = dict(
            id=ident,
            tenant=tenant,
            owner=owner,
            session=session,
            business=business,
            kind=kind,
            payload=value,
        )
        objects[ident] = row
        if kind == 'content_tombstone':
            tombstones.setdefault((tenant, owner, business), []).append(row)
    sources = {}

    def account_action(row):
        """A completed ORIGINAL account journal, never a general deleted-owner flag."""
        p = row['payload']
        try:
            user_id = str(uuid.UUID(p['user_id']))
            if str(uuid.UUID(row['id'])) != row['id']:
                return False
            if (
                row['session'] != row['id']
                or row['business'] != user_id
                or p.get('operation_id') != row['id']
                or p.get('owner') != row['owner']
                or p.get('phase') != 'completed'
                or p.get('provider') != {'state': 'deleted'}
            ):
                return False
            for kind, ident, value in (
                ('deletion', row['id'], {'owner': row['owner'], 'user_id': user_id}),
                (
                    'deletion_owner',
                    row['owner'],
                    {'operation_id': row['id'], 'user_id': user_id},
                ),
            ):
                records = list(
                    db.execute(
                        "SELECT payload FROM platform_documents WHERE tenant_id=? AND owner_id='__session_authority' AND project_id='__session_authority' AND kind=? AND document_id=?",
                        (row['tenant'], kind, ident),
                    )
                )
                if len(records) != 1 or json.loads(records[0][0]) != value:
                    return False
            return True
        except (KeyError, TypeError, ValueError):
            return False

    # Reuse the cleanup contract's conservative identity census. A retained
    # active reference anywhere prevents a deleted-content suppression.
    from .content_lifecycle import reference_ids

    active_assets, active_artifacts = set(), set()

    def retain(value):
        a, b = reference_ids(value)
        active_assets.update(a)
        active_artifacts.update(b)

    for row in objects.values():
        if row['kind'] not in (
            'content_tombstone',
            'history_action',
            'history_preview',
            'universal_purge',
            'account_deletion',
        ):
            retain(row['payload'])
    for table, columns in (
        ('platform_documents', ('payload',)),
        ('platform_plans', ('request', 'execution_plan')),
    ):
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            query = (
                'SELECT '
                + ','.join('"' + c + '"' for c in columns)
                + ' FROM "'
                + table
                + '"'
            )
            if table == 'platform_documents':
                query += " WHERE NOT (project_id='__trash' AND (kind='trash_action' OR (kind='trash_item' AND json_extract(payload,'$.state')='purged')))"
            for values in db.execute(query):
                for value in values:
                    if value is not None:
                        retain(json.loads(value))
    for ident, tenant, owner, project, status, payload in db.execute(
        "SELECT id,tenant,owner,project_id,status,record FROM platform_assets"
    ):
        sources['assets', ident] = (tenant, owner, project, status == 'purged', None)
        if status != 'purged':
            retain(json.loads(payload))
    for ident, tenant, owner, project, job, payload in db.execute(
        "SELECT a.id,j.tenant_id,j.owner_id,j.project_id,a.job_id,a.metadata FROM platform_artifacts a JOIN platform_jobs j ON a.job_id=j.id"
    ):
        sources['artifacts', ident] = (
            tenant,
            owner,
            project,
            json.loads(payload).get('content_deleted') is True,
            job,
        )
        if json.loads(payload).get('content_deleted') is not True:
            retain(json.loads(payload))

    def tombstone(tenant, owner, kind, identity, binding=None):
        token = _token(kind, identity, binding)
        rows = tombstones.get((tenant, owner, token), [])
        if len(rows) != 1:
            return None
        row = rows[0]
        p = row['payload']
        expected_id = (
            'content-'
            + hashlib.sha256(
                (tenant + '\0' + owner + '\0' + token).encode()
            ).hexdigest()
        )
        if (
            row['id'] != expected_id
            or p.get('kind') != kind
            or p.get('identity') != identity
            or p.get('storage_binding') != binding
            or row['session'] != p.get('action_id')
            or kind == 'key'
            and p.get('object_key') != identity
        ):
            return None
        return row

    # Index each action's immutable object plan once, rather than scan it for
    # every reference in large archives. Duplicate keys remain ambiguous.
    plans = {}
    for ident, row in objects.items():
        if row['kind'] not in ('history_action', 'universal_purge', 'account_deletion'):
            continue
        try:
            plan = row['payload']['plan']
            index = {}
            for spec in plan['objects']:
                index.setdefault(spec['object_key'], []).append(spec)
            plans[ident] = (
                index,
                {
                    k: set(plan[k])
                    for k in ('artifact_ids', 'asset_ids', 'project_ids', 'job_ids')
                },
            )
        except (KeyError, TypeError, ValueError):
            continue
    result = {}
    for key, item in references.items():
        proofs = []
        try:
            binding = item.get('storage_binding')
            if not binding or item.get('provider') != 'r2' or not item['references']:
                continue
            for ref in item['references']:
                tenant, owner, project, deleted, job = sources[ref['table'], ref['id']]
                if (
                    not deleted
                    or owner != item['owner']
                    or project != ref['project_id']
                ):
                    break
                kind = {'assets': 'asset', 'artifacts': 'artifact'}[ref['table']]
                identity = tombstone(tenant, owner, kind, ref['id'])
                key_proof = tombstone(tenant, owner, 'key', key, binding)
                if (
                    not identity
                    or not key_proof
                    or identity['session'] != key_proof['session']
                ):
                    break
                action = objects.get(identity['session'])
                if (
                    not action
                    or (action['tenant'], action['owner']) != (tenant, owner)
                    or action['kind']
                    not in ('history_action', 'universal_purge', 'account_deletion')
                ):
                    break
                p = action['payload']
                object_index, plan = plans[action['id']]
                if (
                    p.get('state') != 'completed'
                    or (
                        p.get('action') != 'account_delete'
                        or not account_action(action)
                        if action['kind'] == 'account_deletion'
                        else p.get('action') not in ('purge', 'empty_trash')
                    )
                    or p.get('accounting_released') is not True
                    or not _time(p.get('completed_at'))
                    or ref['id'] not in plan[kind + '_ids']
                    or project not in plan['project_ids']
                    or job is not None
                    and job not in plan['job_ids']
                ):
                    break
                specs = object_index.get(key, [])
                if len(specs) != 1:
                    break
                spec = specs[0]
                if (
                    spec.get('state') != 'deleted'
                    or spec.get('busy') is not False
                    or spec.get('error_code') is not None
                    or binding not in spec.get('bindings', [])
                    or spec.get('storage_binding') != binding
                    or any(spec.get(k) != item[k] for k in ('size_bytes', 'sha256'))
                    or not _time(spec.get('checked_at'))
                    or spec['checked_at'] > p['completed_at']
                ):
                    break
                proofs.append(
                    dict(
                        table=ref['table'],
                        id=ref['id'],
                        action_id=action['id'],
                        identity_tombstone_id=identity['id'],
                        key_tombstone_id=key_proof['id'],
                        completed_at=p['completed_at'],
                    )
                )
            else:
                result[key] = proofs
        except (KeyError, TypeError, ValueError):
            continue
    # Historical authoring replay receipts remain unchanged. Only a positive
    # universal purge for the SAME owner may classify their stale output links
    # as audit associations; current drafts and immutable inputs still retain.
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_edit_receipts'"
    ).fetchone():
        proven = {}
        for proofs in result.values():
            for proof in proofs:
                action = objects[proof['action_id']]
                if action['kind'] in ('universal_purge', 'account_deletion'):
                    proven.setdefault(
                        (action['tenant'], action['owner'], proof['table']), set()
                    ).add(proof['id'])
        for tenant, owner, raw in db.execute(
            'SELECT tenant,owner,response FROM platform_edit_receipts'
        ):
            a, b = reference_ids(json.loads(raw))
            active_assets.update(a - proven.get((tenant, owner, 'assets'), set()))
            active_artifacts.update(b - proven.get((tenant, owner, 'artifacts'), set()))

    # Only the server-redacted original job may retain its own proven-deleted
    # artifact IDs as audit evidence. User-editable content_deleted flags in
    # documents, turns or edit receipts never bypass the active census above.
    audit = {}
    for proofs in result.values():
        for proof in proofs:
            tenant, owner, project, _, job = sources[proof['table'], proof['id']]
            if job is not None:
                audit.setdefault((tenant, owner, project, job), set()).add(proof['id'])
    for ident, tenant, owner, project, request, execution, response in db.execute(
        'SELECT id,tenant_id,owner_id,project_id,request,execution_plan,result FROM platform_jobs'
    ):
        request, execution, response = (
            json.loads(v) if v is not None else None
            for v in (request, execution, response)
        )
        proven = audit.get((tenant, owner, project, ident), set())
        allowed = proven if request == {'content_deleted': True} else set()
        universal = {
            p['id']
            for proofs in result.values()
            for p in proofs
            if p['id'] in proven
            and objects[p['action_id']]['kind']
            in ('universal_purge', 'account_deletion')
        }
        retain(request)
        for value, eligible in (
            (execution, True),
            (
                response,
                isinstance(response, dict) and response.get('content_deleted') is True,
            ),
        ):
            a, b = reference_ids(value)
            active_assets.update(a)
            active_artifacts.update(
                (b - allowed if eligible else b)
                - (universal if value is response else set())
            )
    return {
        key: proofs
        for key, proofs in result.items()
        if all(
            p['id']
            not in (active_assets if p['table'] == 'assets' else active_artifacts)
            for p in proofs
        )
    }
