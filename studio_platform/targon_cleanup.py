"""Independent VM deadline guardian, not a claim of Targon-native TTL.

The controller can write requests but cannot write guardian acknowledgements.
Run the guardian separately under a restarting host service. Provider credentials
remain in the guardian process; no credential is sent to a GPU. A CPU-host loss
still requires external reconciliation and is not covered by this mechanism.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

UID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
REMOVAL_OBSERVATION_SECONDS = 60


def _read(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("targon_guard_link_forbidden")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not 0 < info.st_size <= 16384
        ):
            raise ValueError("targon_guard_record_invalid")
        raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("targon_guard_record_invalid")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("targon_guard_record_invalid")
        return value


def _write(path, value):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("targon_guard_link_forbidden")
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def _receipt_trust(paths):
    # Windows needs an independently verified ACL implementation; a writable
    # local fixture cannot attest privilege separation on a real controller.
    if os.name == 'nt':
        raise ValueError('targon_guard_receipt_trust_unsupported')
    for path in paths:
        info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or path.is_symlink():
            raise ValueError('targon_guard_receipt_untrusted')


def _retained_request(receipt, instance_id):
    """An armed cleanup obligation belongs to the privileged guardian forever."""
    request = receipt.get('request')
    identity = receipt.get('workload_identity')
    if (
        not isinstance(request, dict)
        or set(request) != {'schema_version', 'instance_id', 'deadline'}
        or type(request.get('schema_version')) is not int
        or request['schema_version'] != 1
        or request.get('instance_id') != instance_id
        or not UID.fullmatch(instance_id)
        or type(request.get('deadline')) not in (int, float)
        or not math.isfinite(request['deadline'])
        or receipt.get('instance_id') != instance_id
        or receipt.get('deadline') != request['deadline']
        or receipt.get('request_hash') != _hash(request)
        or receipt.get('identity_verified') is not True
        or not isinstance(identity, dict)
        or set(identity)
        != {
            'org_slug',
            'instance_id',
            'name',
            'type',
            'image_name',
            'resource_name',
            'created_at',
        }
        or identity.get('instance_id') != instance_id
        or identity.get('type') != 'VM'
        or not isinstance(identity.get('name'), str)
        or not re.fullmatch(r'[0-9a-f]{32}', identity['name'])
        or any(
            not isinstance(identity.get(key), str)
            or not identity[key]
            or len(identity[key]) > 128
            for key in ('org_slug', 'image_name', 'resource_name')
        )
        or type(identity.get('created_at')) not in (int, float)
        or not math.isfinite(identity['created_at'])
    ):
        raise ValueError('targon_guard_retained_obligation_invalid')
    return request


class TargonCleanupGuard:
    """Controller side: require a fresh acknowledgement before first deploy."""

    def __init__(
        self, directory, *, clock=time.time, sleep=time.sleep, wait_seconds=20
    ):
        self.root, self.clock, self.sleep = Path(directory), clock, sleep
        self.wait_seconds = wait_seconds
        if not self.root.is_absolute() or '..' in self.root.parts:
            raise ValueError('targon_guard_directory_invalid')
        if (
            type(wait_seconds) not in (int, float)
            or not math.isfinite(wait_seconds)
            or not 0 <= wait_seconds <= 30
        ):
            raise ValueError('targon_guard_wait_invalid')

    def arm(self, instance_id, deadline):
        if not isinstance(instance_id, str) or not UID.fullmatch(instance_id):
            raise ValueError('targon_guard_identity_invalid')
        if (
            type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or deadline <= self.clock()
        ):
            raise ValueError('targon_guard_deadline_invalid')
        target = self.root / 'requests' / (instance_id + '.json')
        value = {'schema_version': 1, 'instance_id': instance_id, 'deadline': deadline}
        if target.exists():
            old = _read(target)
            if old != value:
                raise ValueError('targon_guard_deadline_immutable')
        else:
            _write(target, value)
        until = time.monotonic() + self.wait_seconds
        while True:
            try:
                proof = self.proof(instance_id)
                if proof['armed']:
                    return
            except (OSError, ValueError, KeyError):
                pass
            if time.monotonic() >= until:
                raise ValueError('targon_guard_ack_pending')
            self.sleep(0.5)

    def proof(self, instance_id):
        if not isinstance(instance_id, str) or not UID.fullmatch(instance_id):
            raise ValueError('targon_guard_identity_invalid')
        request = _read(self.root / 'requests' / (instance_id + '.json'))
        ack_path = self.root / 'receipts' / (instance_id + '.json')
        heartbeat_path = self.root / 'heartbeat.json'
        _receipt_trust((self.root, ack_path.parent, ack_path, heartbeat_path))
        ack, heartbeat = _read(ack_path), _read(heartbeat_path)
        _retained_request(ack, instance_id)
        now = self.clock()
        observed = heartbeat.get('observed_at')
        acknowledged = ack.get('observed_at')
        if (
            set(request) != {'schema_version', 'instance_id', 'deadline'}
            or request.get('schema_version') != 1
            or request.get('instance_id') != instance_id
            or type(request.get('deadline')) not in (int, float)
            or not math.isfinite(request['deadline'])
            or ack.get('instance_id') != instance_id
            or ack.get('request_hash') != _hash(request)
            or ack.get('state') != 'armed'
            or ack.get('guardian_id') != heartbeat.get('guardian_id')
            or heartbeat.get('process_state') != 'running'
            or heartbeat.get('state') not in {'running', 'degraded'}
            or (
                heartbeat.get('state') == 'degraded'
                and heartbeat.get('reason_code') != 'targon_cleanup_pending_or_blocked'
            )
            or type(observed) not in (int, float)
            or not math.isfinite(observed)
            or not 0 <= now - observed <= 45
            or type(acknowledged) not in (int, float)
            or not math.isfinite(acknowledged)
            or not 0 <= now - acknowledged <= 45
            or type(ack.get('deadline')) not in (int, float)
            or not now < ack['deadline'] == request['deadline']
        ):
            raise ValueError('targon_guard_proof_unavailable')
        return {
            'instance_id': instance_id,
            'deadline': ack['deadline'],
            'independent': True,
            'armed': True,
        }

    def removal_proof(self, instance_id):
        """Durable trusted teardown evidence remains valid after guardian restart."""
        if not isinstance(instance_id, str) or not UID.fullmatch(instance_id):
            raise ValueError('targon_guard_identity_invalid')
        ack_path = self.root / 'receipts' / (instance_id + '.json')
        _receipt_trust((self.root, ack_path.parent, ack_path))
        ack = _read(ack_path)
        request = _retained_request(ack, instance_id)
        evidence = ack.get('removal_evidence')
        if (
            set(request) != {'schema_version', 'instance_id', 'deadline'}
            or request.get('schema_version') != 1
            or request.get('instance_id') != instance_id
            or type(request.get('deadline')) not in (int, float)
            or not math.isfinite(request['deadline'])
            or ack.get('instance_id') != instance_id
            or ack.get('request_hash') != _hash(request)
            or ack.get('deadline') != request['deadline']
            or ack.get('state') != 'removed'
            or ack.get('identity_verified') is not True
            or evidence not in {'exact_uid_deleted', 'exact_uid_404_after_delete_ack'}
            or evidence == 'exact_uid_404_after_delete_ack'
            and ack.get('delete_acknowledged') is not True
        ):
            raise ValueError('targon_guard_removal_unconfirmed')
        return {
            'instance_id': instance_id,
            'deadline': request['deadline'],
            'independent': True,
            'removed': True,
            'evidence': evidence,
        }


class TargonDeadlineGuardian:
    """Separate privileged process. Only exact protected requests can be removed."""

    def __init__(
        self,
        directory,
        client,
        *,
        org_slug,
        resource_names,
        image_names,
        approval_start,
        approval_end,
        maximum_seconds=10800,
        clock=time.time,
        manual_review_reader=None,
    ):
        self.root, self.client, self.clock = Path(directory), client, clock
        self.org_slug, self.resources, self.images = (
            org_slug,
            set(resource_names),
            set(image_names),
        )
        self.start, self.end, self.maximum = (
            approval_start,
            approval_end,
            maximum_seconds,
        )
        self.identity = uuid.uuid4().hex
        if manual_review_reader is not None and not callable(manual_review_reader):
            raise ValueError('targon_guard_manual_review_reader_invalid')
        self.manual_review_reader = manual_review_reader
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', org_slug):
            raise ValueError('targon_guard_org_invalid')
        if (
            not self.root.is_absolute()
            or '..' in self.root.parts
            or any(
                type(v) not in (int, float) or not math.isfinite(v)
                for v in (approval_start, approval_end)
            )
            or approval_start >= approval_end
            or type(maximum_seconds) is not int
            or not 120 <= maximum_seconds <= 14400
        ):
            raise ValueError('targon_guard_approval_invalid')

    def _workload(self, instance_id):
        return self.client.get(
            '/tha/v3/orgs/' + self.org_slug + '/workloads/' + instance_id
        )

    def tick(self):
        now = self.clock()
        degraded = False
        # Requests are controller-writable. Once acknowledged, only the protected
        # receipt is authoritative, including after a guardian restart. Removing
        # or rewriting a request cannot cancel or extend an existing obligation.
        receipts = {path.name: path for path in (self.root / 'receipts').glob('*.json')}
        requests = {path.name: path for path in (self.root / 'requests').glob('*.json')}
        for name in sorted(set(receipts) | set(requests)):
            try:
                if name in receipts:
                    path = receipts[name]
                    _receipt_trust((self.root, path.parent, path))
                    old = _read(path)
                    request = _retained_request(old, path.stem)
                    if old.get('state') in {'removed', 'manually_reviewed'}:
                        continue
                    if self.manual_review_reader is not None:
                        try:
                            review = self.manual_review_reader(path.stem)
                            if review is not None:
                                self._acknowledge_review(old, request, review, path)
                                continue
                        except Exception:
                            # DB loss/invalid audits cannot suspend deadline enforcement.
                            degraded = True
                else:
                    path = self.root / 'receipts' / name
                    request, old = _read(requests[name]), None
                degraded = (
                    self._tick_request(request, path, self.clock(), old) is True
                    or degraded
                )
            except Exception:
                # A malformed request or transient provider failure must not
                # prevent cleanup of another already-armed workload.
                # An unrelated malformed file has no armed obligation. A
                # previously acknowledged workload losing verification does.
                degraded = name in receipts or degraded
        # Completion of this sweep proves process health, not successful cleanup
        # of every retained obligation. A distinct UID still needs its own fresh
        # armed receipt; pending/blocked UIDs cannot borrow another UID's proof.
        _write(
            self.root / 'heartbeat.json',
            {
                'schema_version': 1,
                'guardian_id': self.identity,
                'process_state': 'running',
                'state': 'degraded' if degraded else 'running',
                'observed_at': now,
                'reason_code': 'targon_cleanup_pending_or_blocked'
                if degraded
                else None,
            },
        )

    def _acknowledge_review(self, old, request, review, path):
        """Consume an exact committed browser-operator audit through a privileged reader.

        This receipt remains distinct from removed. In particular removal_proof
        cannot consume it and no budget/invoice is settled by human attestation.
        """
        if (
            not isinstance(review, dict)
            or review.get('schema_version') != 1
            or review.get('state') != 'manually_reviewed'
            or review.get('instance_id') != request['instance_id']
            or review.get('deadline') != request['deadline']
            or old['workload_identity']['org_slug'] != self.org_slug
            or review.get('account_absent') is not True
            or review.get('no_continuing_charge') is not True
            or not isinstance(review.get('actor'), str)
            or not review['actor']
            or not isinstance(review.get('operation_id'), str)
            or not re.fullmatch(r'[0-9a-f-]{36}', review['operation_id'])
            or not (
                review.get('source') == 'protected_operator_attestation'
                and review.get('intent_id') is None
                or isinstance(review.get('intent_id'), str)
                and re.fullmatch(r'[0-9a-f-]{36}', review['intent_id'])
            )
            or type(review.get('observed_at')) not in (int, float)
            or not math.isfinite(review['observed_at'])
            or not 0 <= review['observed_at'] <= self.clock()
        ):
            raise ValueError('targon_guard_manual_review_invalid')
        if review.get('source') == 'protected_operator_attestation' and (
            review.get('request_hash') != old['request_hash']
            or review.get('workload_identity_hash') != _hash(old['workload_identity'])
            or old.get('delete_started_at') is None
            or review.get('accepted_delete') is not True
        ):
            raise ValueError('targon_guard_manual_review_identity_invalid')
        _write(
            path,
            {
                **old,
                'state': 'manually_reviewed',
                'manual_review': review,
                'manual_reviewed_at': self.clock(),
                'guardian_id': self.identity,
                'reason_code': 'operator_removal_manually_reviewed',
            },
        )

    def _tick_request(self, request, receipt_path, now, old=None):
        uid, deadline = request.get('instance_id'), request.get('deadline')
        if (
            set(request) != {'schema_version', 'instance_id', 'deadline'}
            or request['schema_version'] != 1
            or not isinstance(uid, str)
            or not UID.fullmatch(uid)
            or receipt_path.name != uid + '.json'
            or type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or not self.start < deadline <= self.end
            or deadline > now + self.maximum
        ):
            return bool(old)
        if old is not None and old['workload_identity']['org_slug'] != self.org_slug:
            return True
        next_removal_observation = None
        stopping_hint = old is not None and old.get('removal_poll_hint') is True
        if old is not None and (
            now >= deadline
            or stopping_hint
            or old.get('state') in {'removal_pending', 'cleanup_blocked'}
        ):
            # Keep the fast heartbeat/armed checks, but persist removal polling
            # before I/O so provider failures and guardian restarts cannot burst.
            # Legacy pending receipts used observed_at for their last check.
            next_removal_observation = old.get('next_removal_observation_at')
            if next_removal_observation is None:
                previous_observation = (
                    old.get('observed_at')
                    if old.get('state') in {'removal_pending', 'cleanup_blocked'}
                    else now - REMOVAL_OBSERVATION_SECONDS
                )
                if type(previous_observation) not in (int, float) or not math.isfinite(
                    previous_observation
                ):
                    raise ValueError('targon_guard_observation_time_invalid')
                next_removal_observation = (
                    previous_observation + REMOVAL_OBSERVATION_SECONDS
                )
            if type(next_removal_observation) not in (int, float) or not math.isfinite(
                next_removal_observation
            ):
                raise ValueError('targon_guard_observation_time_invalid')
            if now < next_removal_observation and not (
                stopping_hint and now >= deadline
            ):
                return True
            next_removal_observation = now + REMOVAL_OBSERVATION_SECONDS
            old = {**old, 'next_removal_observation_at': next_removal_observation}
            if now >= deadline:
                # An earlier stopping hint must not postpone the first deadline
                # check; consume it before I/O so a failure cannot bypass cadence.
                old.pop('removal_poll_hint', None)
            _write(receipt_path, old)
        response = self._workload(uid)
        # Disappearance alone is never stop proof. A known exact DELETE
        # acknowledgement plus its subsequent 404 can confirm teardown.
        if (
            response.status_code == 404
            and old
            and old.get('delete_acknowledged') is True
        ):
            value = {
                **old,
                'state': 'removed',
                'observed_at': now,
                'removal_evidence': 'exact_uid_404_after_delete_ack',
            }
            _write(receipt_path, value)
            return
        if response.status_code != 200:
            return bool(old)
        body = response.json()
        if not isinstance(body, dict):
            return bool(old)
        resource = body.get('resource', {})
        try:
            created = datetime.fromisoformat(body['created_at'].replace('Z', '+00:00'))
            if created.tzinfo is None or not self.start <= created.timestamp() <= min(
                self.end, now + 30
            ):
                return bool(old)
            if deadline > created.timestamp() + self.maximum:
                return bool(old)
        except (ValueError, KeyError, AttributeError, TypeError):
            return bool(old)
        if (
            body.get('uid') != uid
            or body.get('type') != 'VM'
            or not isinstance(body.get('name'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', body['name'])
            or body.get('image') not in self.images
            or (resource.get('name') if isinstance(resource, dict) else None)
            not in self.resources
        ):
            return bool(old)
        identity = {
            'org_slug': self.org_slug,
            'instance_id': uid,
            'name': body['name'],
            'type': 'VM',
            'image_name': body['image'],
            'resource_name': resource['name'],
            'created_at': created.timestamp(),
        }
        if old is not None and old['workload_identity'] != identity:
            return True
        state = body.get('state')
        if not isinstance(state, dict) or not isinstance(state.get('status'), str):
            return bool(old)
        status = state['status']
        value = {
            'instance_id': uid,
            'deadline': deadline,
            'request_hash': _hash(request),
            'request': dict(request),
            'workload_identity': identity,
            'guardian_id': self.identity,
            'identity_verified': True,
            'observed_at': now,
            'state': 'removed' if status == 'deleted' else 'armed',
        }
        if next_removal_observation is not None and now >= deadline:
            value['next_removal_observation_at'] = next_removal_observation
        if now < deadline and any(
            isinstance(state.get(key), str) and state[key].lower() == 'stopping'
            for key in ('status', 'message')
        ):
            # Provider Stopping is only a polling hint, never removal evidence.
            value.update(
                removal_poll_hint=True,
                next_removal_observation_at=now + REMOVAL_OBSERVATION_SECONDS,
            )
        if status == 'deleted':
            value['removal_evidence'] = 'exact_uid_deleted'
        elif now >= deadline:
            # Exact-UID teardown can retry only after this fresh identity-checked
            # GET proves the same VM still exists. Registration/deploy never retry.
            previous = old or {}
            count = previous.get(
                'delete_attempts',
                1 if previous.get('delete_started_at') is not None else 0,
            )
            if type(count) is not int or not 0 <= count <= 12:
                raise ValueError('targon_guard_delete_receipt_invalid')
            if count >= 12:
                _write(
                    receipt_path,
                    {
                        **previous,
                        'guardian_id': self.identity,
                        'observed_at': now,
                        'state': 'cleanup_blocked',
                        'reason_code': 'targon_cleanup_retry_exhausted',
                    },
                )
                return True
            if now < previous.get('next_retry_at', 0):
                return True
            value.update(
                state='removal_pending',
                delete_started_at=previous.get('delete_started_at', now),
                delete_attempts=count + 1,
                delete_acknowledged=previous.get('delete_acknowledged') is True,
                next_retry_at=now + min(300, 30 * (2**count)),
                delete_status=None,
                next_removal_observation_at=now + REMOVAL_OBSERVATION_SECONDS,
            )
            _write(receipt_path, value)
            try:
                deleted = self.client.delete(
                    '/tha/v3/orgs/' + self.org_slug + '/workloads/' + uid
                )
                value['delete_status'] = deleted.status_code
                value['delete_acknowledged'] = (
                    value['delete_acknowledged'] or deleted.status_code == 204
                )
            except Exception:
                pass  # Durable attempt survives; later GETs reconcile.
            # A later exact UID read proves removal, not billing settlement.
        _write(receipt_path, value)
        return value['state'] not in {'armed', 'removed'}
