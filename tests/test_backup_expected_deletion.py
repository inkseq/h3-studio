"""Actual archive roundtrips with same-snapshot permanent deletion evidence."""

from contextlib import closing
import copy
import hashlib
import json
import sqlite3
import unittest
from unittest import mock

from studio_platform import backup, backup_remote as remote
from studio_platform.backup_deletion import _token
from studio_platform.backup_reconcile import reconcile_copy
import test_backup_incomplete_coverage as incomplete
import test_platform_backup as fixtures
from test_platform_backup_remote import FakeS3
from test_platform_backup_reconcile import ReadOnlyS3
import test_business_backup_schedule as schedule

host = schedule.host


class ExpectedDeletionTests(unittest.TestCase):
    setUp = fixtures.BackupTests.setUp
    target = incomplete.IncompleteCoverageTests.target

    def source(self, mutation=None, *, failure='missing', classified=True):
        original = backup._export_business_db
        engine_backup = backup.backup_postgres_engine

        def exported(snapshot, destination):
            original(snapshot, destination)
            with closing(sqlite3.connect(destination)) as db:
                row = next(
                    x
                    for x in db.execute('SELECT id,metadata FROM platform_artifacts')
                    if json.loads(x[1])['kind'] == 'audio'
                )
                ident, raw = row
                spec = json.loads(raw)
                self.deleted_id = ident
                spec['content_deleted'] = True
                db.execute(
                    'UPDATE platform_artifacts SET metadata=? WHERE id=?',
                    (json.dumps(spec), ident),
                )
                # Actual purge keeps audit IDs in the redacted job result.
                db.execute(
                    'UPDATE platform_jobs SET request=?,result=? WHERE id=?',
                    (
                        json.dumps({'content_deleted': True}),
                        json.dumps({'content_deleted': True, 'artifact_id': ident}),
                        self.job['id'],
                    ),
                )
                binding = json.loads(
                    db.execute(
                        'SELECT record FROM platform_artifact_write_receipts'
                    ).fetchone()[0]
                )['binding']
                tenant = self.scope.tenant_id
                owner = 'superdan'
                action_id = 'history-proof'
                object_spec = dict(
                    object_key=self.missing_key,
                    size_bytes=spec['size_bytes'],
                    sha256=spec['sha256'],
                    state='deleted',
                    busy=False,
                    error_code=None,
                    checked_at=2,
                    storage_binding=binding,
                    bindings=[binding],
                )
                payload = dict(
                    action='empty_trash',
                    state='completed',
                    completed_at=3,
                    accounting_released=True,
                    plan=dict(
                        artifact_ids=[ident],
                        asset_ids=[],
                        project_ids=[self.scope.project_id],
                        job_ids=[self.job['id']],
                        objects=[object_spec],
                    ),
                )

                def put(i, kind, business, body, who=owner):
                    db.execute(
                        'INSERT INTO platform_quick_chat_objects (id,tenant,owner,session_id,kind,parent_id,business_key,version,payload,created_at,updated_at) VALUES (?,?,?,?,?,NULL,?,1,?,1,3)',
                        (i, tenant, who, action_id, kind, business, json.dumps(body)),
                    )

                put(action_id, 'history_action', 'proof', payload)
                for kind, value, bind in [
                    ('artifact', ident, None),
                    ('key', self.missing_key, binding),
                ]:
                    token = _token(kind, value, bind)
                    tid = (
                        'content-'
                        + hashlib.sha256(
                            (tenant + '\0' + owner + '\0' + token).encode()
                        ).hexdigest()
                    )
                    body = dict(
                        kind=kind,
                        identity=value,
                        action_id=action_id,
                        storage_binding=bind,
                        state='pending',
                    )
                    if kind == 'key':
                        body['object_key'] = value
                    put(tid, 'content_tombstone', token, body)
                if mutation:
                    mutation(db, payload, spec, action_id, ident)
                db.commit()
                return {
                    name: db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[
                        0
                    ]
                    for name in backup._tables()
                }

        def run(*args, **kwargs):
            return engine_backup(
                *args, **kwargs, classify_expected_deletions=classified
            )

        with (
            mock.patch.object(backup, '_export_business_db', side_effect=exported),
            mock.patch.object(backup, 'backup_postgres_engine', side_effect=run),
        ):
            return incomplete.IncompleteCoverageTests.source(self, failure=failure)

    def test_expected_missing_is_not_loss_but_not_full_byte_recovery(self):
        source = self.source()
        manifest = backup.verify_local(source, allow_incomplete=True)
        c = manifest['coverage']
        self.assertEqual(manifest['version'], 3)
        self.assertEqual(c['expected_missing_objects'], 1)
        self.assertEqual(c['unexpected_missing_objects'], 0)
        self.assertFalse(c['loss_alert_required'])
        self.assertEqual(c['status'], 'incomplete')
        with self.assertRaises(backup.BackupError):
            backup.restore_local(source, self.root / 'strict')
        backup.restore_local(source, self.root / 'restored', allow_incomplete=True)
        hold = json.loads((self.root / 'restored' / 'RECOVERY-HOLD.json').read_bytes())
        self.assertFalse(hold['media_recovery_complete'])
        self.assertFalse(hold['execution_enabled'])
        self.assertEqual(hold['coverage'], c)
        with closing(
            sqlite3.connect(self.root / 'restored' / 'platform.sqlite3')
        ) as db:
            self.assertEqual(
                db.execute(
                    "SELECT reserved_microusd,spent_microusd FROM platform_budget_accounts WHERE id='coverage-unknown-reservation'"
                ).fetchone(),
                (146, 17),
            )
        old = {
            **manifest,
            'version': 2,
            'coverage': backup._coverage(
                manifest['objects'], manifest['missing_objects']
            ),
        }
        old['missing_objects'] = [
            {
                k: v
                for k, v in x.items()
                if k not in ('classification', 'deletion_evidence')
            }
            for x in old['missing_objects']
        ]
        (source / 'manifest.json').write_text(json.dumps(old))
        self.assertEqual(
            backup.verify_local(source, allow_incomplete=True)['version'], 2
        )

    def test_false_or_incomplete_proof_stays_unexpected(self):
        def action_change(db, payload, spec, action, ident, change):
            change(payload)
            db.execute(
                'UPDATE platform_quick_chat_objects SET payload=? WHERE id=?',
                (json.dumps(payload), action),
            )

        mutations = {
            'unfinished': lambda *a: action_change(
                *a, lambda p: p.update(state='pending')
            ),
            'wrong_binding': lambda *a: action_change(
                *a, lambda p: p['plan']['objects'][0].update(storage_binding='0' * 64)
            ),
            'wrong_size': lambda *a: action_change(
                *a, lambda p: p['plan']['objects'][0].update(size_bytes=999)
            ),
            'wrong_owner': lambda db, p, s, a, i: db.execute(
                'UPDATE platform_quick_chat_objects SET owner=? WHERE id=?',
                ('supervan', a),
            ),
            'wrong_tenant': lambda db, p, s, a, i: db.execute(
                'UPDATE platform_quick_chat_objects SET tenant=? WHERE id=?',
                ('foreign', a),
            ),
            'missing_tombstone': lambda db, p, s, a, i: db.execute(
                "DELETE FROM platform_quick_chat_objects WHERE kind='content_tombstone'"
            ),
            'active_same_key': lambda db, p, s, a, i: db.execute(
                'UPDATE platform_artifacts SET metadata=? WHERE id=?',
                (json.dumps({**s, 'content_deleted': False}), i),
            ),
            'additional_reference_same_key': lambda db, p, s, a, i: db.execute(
                "INSERT INTO platform_artifacts (id,job_id,attempt_id,created_at,metadata) SELECT '00000000-0000-4000-8000-000000000001',job_id,attempt_id,created_at,? FROM platform_artifacts WHERE id=?",
                (json.dumps({**s, 'content_deleted': False}), i),
            ),
            'active_other_reference': lambda db, p, s, a, i: db.execute(
                "INSERT INTO platform_quick_chat_objects (id,tenant,owner,session_id,kind,parent_id,business_key,version,payload,created_at,updated_at) SELECT 'active-ref',tenant,owner,session_id,'turn',NULL,'active',1,?,1,3 FROM platform_quick_chat_objects WHERE id=?",
                (json.dumps({'saved_artifact': i}), a),
            ),
            'editable_flag_cannot_hide_turn': lambda db, p, s, a, i: db.execute(
                "INSERT INTO platform_quick_chat_objects (id,tenant,owner,session_id,kind,parent_id,business_key,version,payload,created_at,updated_at) SELECT 'active-ref',tenant,owner,session_id,'turn',NULL,'active',1,?,1,3 FROM platform_quick_chat_objects WHERE id=?",
                (json.dumps({'content_deleted': True, 'saved_artifact': i}), a),
            ),
            'editable_flag_cannot_hide_document': lambda db, p, s, a, i: db.execute(
                "UPDATE platform_documents SET payload=?",
                (json.dumps({'content_deleted': True, 'saved_artifact': i}),),
            ),
            'nonredacted_job_cannot_hide_audit': lambda db, p, s, a, i: db.execute(
                "UPDATE platform_jobs SET request=?",
                (json.dumps({'content_deleted': True, 'prompt': 'still-live'}),),
            ),
        }
        # Fresh isolated fixture per case; no modified live or restored source.
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                case = ExpectedDeletionTests()
                case.setUp()
                try:
                    m = backup.verify_local(
                        case.source(mutation), allow_incomplete=True
                    )
                    self.assertEqual(m['coverage']['expected_missing_objects'], 0)
                    self.assertEqual(m['coverage']['unexpected_missing_objects'], 1)
                    self.assertTrue(m['coverage']['loss_alert_required'])
                finally:
                    case.doCleanups()

    def test_offline_recomputes_proof_not_manifest_claim(self):
        source = self.source()
        path = source / 'manifest.json'
        m = json.loads(path.read_bytes())
        for change in ('evidence', 'classification', 'database'):
            with self.subTest(change=change):
                v = copy.deepcopy(m)
                if change == 'evidence':
                    v['missing_objects'][0]['deletion_evidence'][0]['action_id'] = (
                        'forged'
                    )
                elif change == 'classification':
                    v['missing_objects'][0]['classification'] = 'unexpected_missing'
                else:
                    with (
                        closing(sqlite3.connect(source / 'database.sqlite3')) as db,
                        db,
                    ):
                        db.execute(
                            "UPDATE platform_quick_chat_objects SET owner='foreign' WHERE kind='history_action'"
                        )
                    v['database_sha256'] = backup._digest(source / 'database.sqlite3')
                path.write_text(json.dumps(v))
                with self.assertRaises(backup.BackupError):
                    backup.verify_local(source, allow_incomplete=True)

    def test_remote_reconcile_restore_and_daily_keep_expected_summary(self):
        source = self.source()
        target = self.target()
        client = ReadOnlyS3(target)
        directory = self.root / 'receipts'
        receipt = remote.copy_backup(
            source, target, directory, client=client, allow_incomplete=True
        )
        self.assertEqual(receipt['format'], remote.FORMAT_V3)
        intent = json.loads((directory / 'intent.json').read_bytes())
        client.readonly = True
        result = reconcile_copy(target, intent, receipt=receipt, client=client)
        self.assertEqual(result['coverage'], receipt['coverage'])
        restored = remote.restore_copy(
            target,
            receipt,
            self.root / 'download',
            self.root / 'restore',
            client=client,
            allow_incomplete=True,
        )
        self.assertFalse(restored['media_recovery_complete'])
        self.assertFalse(restored['coverage']['loss_alert_required'])
        complete = {'version': 3, 'coverage': receipt['coverage']}
        with mock.patch.object(host, 'read', return_value=complete):
            self.assertEqual(
                host.completed_summary(self.root, '2026-10-17')['coverage'],
                receipt['coverage'],
            )
        for bad in (True, 1):
            c = copy.deepcopy(complete)
            c['coverage']['loss_alert_required'] = bad
            with self.assertRaises(ValueError):
                host.coverage(c)
        cfg = {
            **schedule.ScheduleTests.config(self),
            'version': 3,
            'allow_missing_media': True,
            'classify_expected_deletions': True,
        }
        self.assertEqual(host.validate_config(cfg, cfg['instance_id']), cfg)

    def test_evidence_never_skips_head_or_qualifies_get_errors(self):
        for failure in ('none', 'forbidden', 'get_missing'):
            case = ExpectedDeletionTests()
            case.setUp()
            try:
                if failure == 'none':
                    m = backup.verify_local(case.source(failure=failure))
                    self.assertEqual(m['coverage']['expected_missing_objects'], 0)
                else:
                    with self.assertRaises(backup.BackupError):
                        case.source(failure=failure)
            finally:
                case.doCleanups()


if __name__ == '__main__':
    unittest.main()
