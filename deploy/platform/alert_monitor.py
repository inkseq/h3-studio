#!/usr/bin/python3
"""One finite, read-only host observation and bounded SNS delivery pass.

The local file contains notification deduplication only, never task/accounting
authority. No provider APIs, service restarts or business writes are performed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

REGION = 'ap-southeast-1'
ACCOUNT = '829135631045'
NAMESPACE = 'Inkseq/OperatorAlerts'
CONFIG = Path('/etc/inkseq/alert-monitor.json')
STATE = Path('/var/lib/inkseq-alert-monitor/state.json')
SERVICES = ('app', 'db', 'caddy', 'dstack', 'controller')
MAX_BYTES = 1024 * 1024
MAX_PENDING = 128
BACKUP_STATE = Path('/var/lib/inkseq-business-backup')
# Daily timer: 16:30 UTC + <=5m jitter + <=95m service + 20m grace.
# About 26h since the previous scheduled start, not a restore/freshness claim.
BACKUP_DEADLINE_MINUTE = 18 * 60 + 30
THRESHOLD_FIELDS = frozenset({'gpu_max_seconds', 'stuck_seconds', 'wake_count', 'cpu_percent',
    'memory_percent', 'host_sustain_seconds', 'health_down_seconds', 'http_5xx_min',
    'http_5xx_ratio', 'max_sample_gap_seconds', 'terminal_reservation_seconds',
    'payment_fulfillment_seconds', 'payment_processing_seconds', 'payment_window_seconds',
    'payment_failure_count'})
HTTP_PROBE = """import json,urllib.request
result={'health_ok':False,'http_requests':None,'http_5xx':None}
try:
 with urllib.request.urlopen('http://127.0.0.1:8845/healthz',timeout=3) as r:
  h=json.loads(r.read(16384));result['health_ok']=r.status==200 and h.get('auth_ready') is True
except Exception: pass
try:
 with urllib.request.urlopen('http://127.0.0.1:8845/internal/alert-counters',timeout=3) as r:
  c=json.loads(r.read(16384))
 if set(c)=={'epoch','window_seconds','requests','server_errors'} and c['window_seconds']==300 and type(c['requests']) is int and type(c['server_errors']) is int and 0<=c['server_errors']<=c['requests']:
  result.update(http_requests=c['requests'],http_5xx=c['server_errors'])
except Exception: pass
print(json.dumps(result))
"""
PAYMENT_HTTP_PROBE = """import json,urllib.request,sys
result={'payment_signature_failures':None,'payment_receipt_failures':None}
try:
 w=int(sys.argv[1])
 with urllib.request.urlopen('http://127.0.0.1:8845/internal/payment-alert-counters?window_seconds='+str(w),timeout=3) as r:
  c=json.loads(r.read(16384))
 if set(c)=={'epoch','window_seconds','signature_failures','processing_failures'} and c['window_seconds']==w and all(type(c[k]) is int and c[k]>=0 for k in ('signature_failures','processing_failures')):
  result.update(payment_signature_failures=c['signature_failures'],payment_receipt_failures=c['processing_failures'])
except Exception: pass
print(json.dumps(result))
"""


def require(value, code):
    if not value:
        raise ValueError(code)


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def protected_directory(path):
    for item in (Path(path), *Path(path).parents):
        info = item.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'alert_directory_unprotected')


def read_protected_bytes(path, *, missing=False, private=True, max_bytes=MAX_BYTES):
    path = Path(path)
    protected_directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if missing:
            return None
        raise
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & (0o077 if private else 0o022)
                and info.st_size <= max_bytes, 'alert_file_unprotected')
        raw = stream.read(max_bytes + 1)
        require(len(raw) <= max_bytes, 'alert_file_too_large')
    return raw


def read_protected(path, *, missing=False):
    raw = read_protected_bytes(path, missing=missing)
    return None if raw is None else json.loads(raw)


def read_transport_manifest(operation):
    # Transport owns this read-only copy (UID10001), behind a root-only parent.
    # The caller binds these bytes to the original root receipt before use.
    protected_directory(operation)
    fd = os.open(operation / 'snapshot' / 'manifest.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_uid in {0, 10001} and not info.st_mode & 0o022
            and info.st_size <= 16 * MAX_BYTES, 'alert_manifest_unprotected')
        raw = stream.read(16 * MAX_BYTES + 1)
        require(len(raw) <= 16 * MAX_BYTES, 'alert_manifest_too_large')
        return raw


def atomic(path, value):
    path = Path(path)
    protected_directory(path.parent)
    raw = encode(value)
    require(len(raw) <= MAX_BYTES, 'alert_state_too_large')
    if path.exists() or path.is_symlink():
        read_protected(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.alert-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def locked(path):
    import fcntl
    protected_directory(Path(path).parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o077, 'alert_lock_unprotected')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def config(value, target):
    require(type(value) is dict and set(value) == {'version', 'environment', 'instance_id', 'topic_arn', 'thresholds'}
            and value['version'] == 1 and type(value['version']) is int
            and value['environment'] in ('staging', 'production')
            and target['name'] == value['environment'] and target['instance_id'] == value['instance_id']
            and re.fullmatch(r'i-[0-9a-f]{17}', value['instance_id']), 'alert_config_invalid')
    topic = f"arn:aws:sns:{REGION}:{ACCOUNT}:sixnine-{value['environment']}-operator-alerts"
    require(value['topic_arn'] == topic and type(value['thresholds']) is dict, 'alert_destination_invalid')
    thresholds = value['thresholds']
    require(not set(thresholds) - THRESHOLD_FIELDS and all(type(item) in (int, float)
        and math.isfinite(item) and item > 0 for item in thresholds.values())
        and all(thresholds.get(key, 1) <= limit for key, limit in
                (('cpu_percent', 100), ('memory_percent', 100), ('http_5xx_ratio', 1))),
        'alert_thresholds_invalid')
    window = thresholds.get('payment_window_seconds', 300)
    require(type(window) is int and 60 <= window <= 3600, 'alert_thresholds_invalid')
    return value


def clients():
    # Systemd supplies an empty credential/config environment. Refuse a process
    # that resolved a static/profile/container identity instead of the host role.
    import boto3
    from botocore.config import Config
    session = boto3.Session(region_name=REGION)
    identity = session.get_credentials()
    require(identity is not None and identity.method == 'iam-role', 'alert_instance_role_required')
    options = Config(connect_timeout=3, read_timeout=5, retries={'mode': 'standard', 'total_max_attempts': 1},
                     max_pool_connections=2, proxies={})
    return tuple(session.client(service, region_name=REGION,
        endpoint_url=f'https://{"monitoring" if service == "cloudwatch" else service}.{REGION}.amazonaws.com', config=options)
        for service in ('sns', 'cloudwatch'))


def docker(arguments, *, runner=subprocess.run):
    result = runner(['/usr/bin/docker', *arguments], stdin=subprocess.DEVNULL, capture_output=True,
        timeout=10, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
    require(result.returncode == 0 and len(result.stdout) <= MAX_BYTES, 'alert_docker_observation_failed')
    return result.stdout.decode('utf-8')


def containers(*, runner=subprocess.run):
    values = {}
    for service in SERVICES:
        names = docker(['ps', '--all', '--quiet', '--filter', 'label=com.docker.compose.project=sixnine-platform',
            '--filter', 'label=com.docker.compose.service=' + service], runner=runner).split()
        require(len(names) <= 1 and all(re.fullmatch(r'[0-9a-f]{12,64}', name) for name in names),
                'alert_container_identity_ambiguous')
        if not names:
            values[service] = None
            continue
        # Render only identity and health; never fetch Config.Env or mount paths.
        template = '{"project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"running":{{json .State.Running}},"restarting":{{json .State.Restarting}},"health":{{with (index .State "Health")}}{{json (index . "Status")}}{{else}}null{{end}}}'
        row = json.loads(docker(['inspect', '--format', template, names[0]], runner=runner))
        require(row['project'] == 'sixnine-platform' and row['service'] == service, 'alert_container_identity_changed')
        running = row['running'] is True and row['restarting'] is not True
        healthy = row['health'] == 'healthy'
        values[service] = {'id': names[0], 'ok': running and (healthy if service in ('app', 'db', 'dstack') else True)}
    return values


def proc_sample(*, proc=Path('/proc')):
    fields = (proc / 'stat').read_text().splitlines()[0].split()
    require(fields[0] == 'cpu' and len(fields) >= 9, 'alert_cpu_sample_invalid')
    counters = [int(value) for value in fields[1:9]]  # guest already counted in user/nice
    memory = {line.split(':')[0]: int(line.split()[1]) for line in (proc / 'meminfo').read_text().splitlines()}
    require(memory['MemTotal'] > 0 and 0 <= memory['MemAvailable'] <= memory['MemTotal'], 'alert_memory_sample_invalid')
    return {'total': sum(counters), 'idle': counters[3] + counters[4],
            'memory_percent': 100 * (1 - memory['MemAvailable'] / memory['MemTotal'])}


def disk_sample(*, path='/srv/sixnine', statvfs=None):
    """The real host data filesystem, using df's non-root available capacity."""
    value = (statvfs or os.statvfs)(path)
    require(all(type(item) is int for item in (value.f_blocks, value.f_bfree, value.f_bavail, value.f_frsize))
        and value.f_frsize > 0 and 0 <= value.f_bavail <= value.f_bfree <= value.f_blocks,
        'alert_disk_sample_invalid')
    used = value.f_blocks - value.f_bfree
    require(used + value.f_bavail > 0, 'alert_disk_sample_invalid')
    return 100 * used / (used + value.f_bavail)


def retention_service_failed(*, runner=subprocess.run):
    """Observe only the fixed oneshot unit's result; never fetch its journal."""
    return oneshot_failed('inkseq-release-retention.service', runner=runner)


def oneshot_failed(unit, *, runner=subprocess.run):
    require(unit in {'inkseq-release-retention.service', 'inkseq-business-backup.service'}, 'alert_unit_invalid')
    fields = {'LoadState', 'ActiveState', 'SubState', 'Result', 'ExecMainCode', 'ExecMainStatus'}
    try:
        result = runner(['/usr/bin/systemctl', 'show', unit, '--no-pager',
            '--property=' + ','.join(sorted(fields))], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=5, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
        if result.returncode != 0 or len(result.stdout) > 4096:
            return None
        rows = [line.split('=', 1) for line in result.stdout.decode('utf-8').splitlines()]
        if len(rows) != len(fields) or any(len(row) != 2 for row in rows):
            return None
        value = dict(rows)
        if set(value) != fields or value['LoadState'] != 'loaded':
            return None
        if value['ExecMainCode'] not in {'0', '1', '2', '3'} or not re.fullmatch(r'[0-9]{1,3}', value['ExecMainStatus']) \
                or int(value['ExecMainStatus']) > 255:
            return None
        if value['ActiveState'] == 'failed':
            return True
        if value['ActiveState'] == 'inactive' and value['SubState'] == 'dead':
            if value['Result'] == 'success' and value['ExecMainCode'] == '1' and value['ExecMainStatus'] == '0':
                return False
            if value['Result'] in {'exit-code', 'signal', 'core-dump', 'timeout', 'watchdog',
                    'start-limit-hit', 'resources', 'protocol', 'oom-kill'}:
                return True
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        pass
    # A running attempt or unavailable observation cannot resolve an incident.
    return None


def backup_coverage_loss(completed, receipt):
    """Classify only the hash-bound coverage; legacy unclassified misses alert."""
    version = completed['version']
    if version == 1:
        require('coverage' not in completed and 'coverage' not in receipt, 'alert_backup_coverage_invalid')
        return False  # v1 only allowed complete media copies.
    coverage = completed.get('coverage')
    fields = {'status', 'referenced_objects', 'available_objects', 'missing_objects',
        'referenced_bytes', 'available_bytes', 'missing_bytes'}
    if version == 3:
        fields |= {'expected_missing_objects', 'expected_missing_bytes',
            'unexpected_missing_objects', 'unexpected_missing_bytes', 'loss_alert_required'}
    require(type(coverage) is dict and set(coverage) == fields and encode(coverage) == encode(receipt.get('coverage'))
        and all(type(coverage[key]) is int and coverage[key] >= 0
            for key in fields - {'status', 'loss_alert_required'}), 'alert_backup_coverage_invalid')
    require(coverage['status'] == ('incomplete' if coverage['missing_objects'] else 'complete')
        and (coverage['missing_objects'] == 0) == (coverage['missing_bytes'] == 0)
        and all(coverage['referenced_' + suffix] == coverage['available_' + suffix]
            + coverage['missing_' + suffix] for suffix in ('objects', 'bytes')), 'alert_backup_coverage_invalid')
    if version == 2:
        return coverage['missing_objects'] > 0
    require(type(coverage['loss_alert_required']) is bool
        and coverage['loss_alert_required'] == (coverage['unexpected_missing_objects'] > 0)
        and all(coverage['missing_' + suffix] == coverage['expected_missing_' + suffix]
            + coverage['unexpected_missing_' + suffix] for suffix in ('objects', 'bytes'))
        and all((coverage[kind + '_objects'] == 0) == (coverage[kind + '_bytes'] == 0)
            for kind in ('expected_missing', 'unexpected_missing')), 'alert_backup_coverage_invalid')
    return coverage['loss_alert_required']


def backup_copy_observation(*, now, root=BACKUP_STATE):
    """Check at most two receipts, not media/S3; None fields mean unobservable.

    Root-pinned completion plus receipt hash is copy evidence, never restore proof.
    Before today's deadline, yesterday is still the most recent due copy.
    Malformed evidence cannot resolve an incident or become a successful sample.
    """
    try:
        current = datetime.fromtimestamp(now, timezone.utc)
        days = [current.date()]
        if current.hour * 60 + current.minute < BACKUP_DEADLINE_MINUTE:
            days.append(current.date() - timedelta(days=1))
        protected_directory(root)
        for day in days:
            operation = root / day.isoformat()
            try:
                value = read_protected(operation / 'completed.json', missing=True)
            except FileNotFoundError:
                continue  # No operation directory: no confirmed scheduled copy.
            if value is None:
                continue
            require(type(value) is dict and type(value.get('version')) is int
                and value['version'] in (1, 2, 3) and value.get('day') == day.isoformat()
                and all(type(value.get(k)) is str and re.fullmatch('[0-9a-f]{64}', value[k])
                    for k in ('completion_sha256', 'receipt_sha256')),
                'alert_backup_completion_invalid')
            raw = read_protected_bytes(operation / 'receipt.json', private=False)
            require(hashlib.sha256(raw).hexdigest() == value['receipt_sha256'], 'alert_backup_receipt_changed')
            receipt = json.loads(raw)
            require(type(receipt) is dict and receipt.get('phase') == 'copied_not_restored'
                and receipt.get('format') == 'sixnine-private-backup-copy-v' + str(value['version'])
                and receipt.get('completion_sha256') == value['completion_sha256']
                and type(value.get('completion_version_id')) is str
                and 1 <= len(value['completion_version_id']) <= 1024
                and receipt.get('completion_version_id') == value['completion_version_id'],
                'alert_backup_receipt_invalid')
            loss = backup_coverage_loss(value, receipt)
            if value['version'] in (2, 3):
                # Apply today's policy to the hash-bound manifest, not live media.
                try:
                    from backup_loss_policy import original_loss
                except ImportError:
                    from studio_platform.backup_loss_policy import original_loss
                manifest_raw = read_transport_manifest(operation)
                require(hashlib.sha256(manifest_raw).hexdigest() == receipt.get('snapshot_manifest_sha256'),
                    'alert_backup_manifest_changed')
                manifest = json.loads(manifest_raw)
                require(encode(manifest.get('coverage')) == encode(value['coverage']),
                    'alert_backup_manifest_coverage_invalid')
                loss = original_loss(manifest)
            return {'missing': False, 'loss': loss}
        return {'missing': True, 'loss': None}
    except (OSError, ValueError, TypeError, OverflowError, KeyError):
        return {'missing': None, 'loss': None}


def backup_copy_missing(*, now, root=BACKUP_STATE):
    return backup_copy_observation(now=now, root=root)['missing']


def backup_observation(*, now, runner=subprocess.run):
    failed = oneshot_failed('inkseq-business-backup.service', runner=runner)
    observed = backup_copy_observation(now=now)
    missing = observed['missing']
    return {'business_backup_failed': failed, 'business_backup_missing': missing,
        'business_backup_unobservable': missing is None, 'business_backup_loss': observed['loss']}


def observe(previous_cpu, *, runner=subprocess.run, proc=Path('/proc'), payment_window_seconds=300):
    sample = proc_sample(proc=proc)
    cpu = None
    if previous_cpu is not None:
        total, idle = sample['total'] - previous_cpu['total'], sample['idle'] - previous_cpu['idle']
        if total > 0 and 0 <= idle <= total:
            cpu = 100 * (1 - idle / total)
    host = {'cpu_percent': cpu, 'memory_percent': sample['memory_percent'], 'health_ok': None,
            'http_requests': None, 'http_5xx': None, 'processes_ok': None, 'disk_used_percent': None,
            'retention_service_failed': retention_service_failed(runner=runner)}
    host.update(backup_observation(now=time.time(), runner=runner))
    try:
        host['disk_used_percent'] = disk_sample()
    except (OSError, ValueError, AttributeError):
        pass  # An unknown disk observation never resolves an active alarm.
    snapshot = None
    app = None
    try:
        found = containers(runner=runner)
        host['processes_ok'] = all(value is not None and value['ok'] for value in found.values())
        app = found['app']
        if app is None:
            host['health_ok'] = False
        else:
            try:
                probe = json.loads(docker(['exec', app['id'], 'python', '-B', '-c', HTTP_PROBE], runner=runner))
                require(type(probe) is dict and set(probe) == {'health_ok', 'http_requests', 'http_5xx'}
                        and type(probe['health_ok']) is bool, 'alert_http_probe_invalid')
                host.update(probe)
            except (OSError, subprocess.SubprocessError, ValueError):
                host['health_ok'] = False
            snapshot = json.loads(docker(['exec', app['id'], 'python', '-B', '-m',
                'studio_platform.alert_snapshot'], runner=runner))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        # Unknown ledger state never fabricates a healthy observation or recovery.
        snapshot = None
    if isinstance(snapshot, dict):
        # Optional payment calls have independent subprocess deadlines. A slow
        # payment DB/query or unavailable module cannot discard the GPU ledger.
        snapshot['payments'] = {'complete': False}
        try:
            payment = json.loads(docker(['exec', app['id'], 'python', '-B', '-m',
                'studio_platform.payment_alerts', '--window-seconds',
                str(payment_window_seconds)], runner=runner))
            require(type(payment) is dict
                and set(payment) == {'complete', 'fulfillment', 'processing', 'activity', 'window_seconds'}
                and type(payment['complete']) is bool
                and payment['window_seconds'] == payment_window_seconds,
                'payment_snapshot_invalid')
            snapshot['payments'] = payment
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            pass
    if app is not None:
        try:
            payment = json.loads(docker(['exec', app['id'], 'python', '-B', '-c',
                PAYMENT_HTTP_PROBE, str(payment_window_seconds)], runner=runner))
            require(type(payment) is dict and set(payment) == {'payment_signature_failures', 'payment_receipt_failures'}
                and all(value is None or type(value) is int and value >= 0 for value in payment.values()),
                'payment_counter_probe_invalid')
            host.update(payment)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass  # Unknown payment counters must not change legacy host observations.
    return snapshot, host, {'total': sample['total'], 'idle': sample['idle']}


def notification(value, environment):
    require(type(value) is dict and set(value) == {'code', 'environment', 'resource_id', 'duration_seconds', 'console_link', 'transition'},
            'alert_notification_invalid')
    # The core evaluator owns this finite code list. Keep a second boundary at SNS.
    require(value['code'] in {'gpu_runtime_exceeded', 'gpu_idle_stop_overdue', 'test_spend_80', 'test_spend_100',
            'job_stuck', 'job_repeated_wake', 'host_cpu_sustained', 'host_memory_sustained', 'host_disk_80', 'host_disk_90',
            'host_process_unhealthy', 'retention_service_failed', 'business_backup_failed', 'business_backup_loss',
            'payment_fulfillment_stalled', 'payment_processing_stalled', 'payment_failed',
            'payment_refunded', 'payment_disputed', 'payment_monitor_unavailable',
            'payment_webhook_signature_failed', 'payment_webhook_receipt_failed',
            'business_backup_missing', 'business_backup_unobservable', 'health_down', 'http_5xx_rate', 'generation_failure', 'terminal_reservation_stale'}
            and value['environment'] == environment and value['transition'] in {'firing', 'resolved'}
            and (value['resource_id'] is None or type(value['resource_id']) is str
                and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value['resource_id']))
            and type(value['duration_seconds']) is int and 0 <= value['duration_seconds'] <= 315360000,
            'alert_notification_invalid')
    origin = 'https://staging.inkseq.com' if environment == 'staging' else 'https://inkseq.com'
    require(value['console_link'] == origin + '/freestyle', 'alert_notification_link_invalid')
    return value


def initial_state(environment):
    return {'version': 1, 'environment': environment, 'sequence': 0, 'evaluator': {}, 'cpu': None, 'pending': []}


def validate_state(state, environment):
    require(type(state) is dict and set(state) == {'version', 'environment', 'sequence', 'evaluator', 'cpu', 'pending'}
            and type(state['version']) is int and state['version'] == 1 and state['environment'] == environment
            and type(state['sequence']) is int and state['sequence'] >= 0 and type(state['evaluator']) is dict
            and type(state['pending']) is list and len(state['pending']) <= MAX_PENDING, 'alert_state_invalid')
    require(state['cpu'] is None or type(state['cpu']) is dict and set(state['cpu']) == {'total', 'idle'}
            and all(type(value) is int and value >= 0 for value in state['cpu'].values())
            and state['cpu']['idle'] <= state['cpu']['total'], 'alert_cpu_state_invalid')
    for entry in state['pending']:
        require(type(entry) is dict and set(entry) == {'id', 'notification', 'attempts', 'retry_at'}
                and type(entry['id']) is str and re.fullmatch(r'[0-9a-f]{64}', entry['id'])
                and type(entry['attempts']) is int and entry['attempts'] >= 0
                and type(entry['retry_at']) in (int, float) and math.isfinite(entry['retry_at'])
                and entry['retry_at'] >= 0, 'alert_pending_invalid')
        notification(entry['notification'], environment)
    return state


def tick(settings, state, snapshot, host, cpu, *, evaluator, sns, cloudwatch, save, now):
    """Pure collaborators permit fake-clock/fake-network boundary tests."""
    state = json.loads(encode(validate_state(state, settings['environment'])))
    result = evaluator(snapshot, state['evaluator'], environment=settings['environment'], now=now,
                       host=host, thresholds=settings['thresholds'])
    require(type(result) is dict and set(result) == {'state', 'notifications'} and type(result['state']) is dict
            and type(result['notifications']) is list, 'alert_evaluation_invalid')
    deferred = len(state['pending']) + len(result['notifications']) > MAX_PENDING
    if not deferred:
        for item in result['notifications']:
            notification(item, settings['environment'])
            state['sequence'] += 1
            identity = hashlib.sha256(encode({'sequence': state['sequence'], 'notification': item})).hexdigest()
            state['pending'].append({'id': identity, 'notification': item, 'attempts': 0, 'retry_at': now})
        state['evaluator'] = result['state']
    # Keep the original transition checkpoint on overflow, including a proposed
    # batch larger than MAX_PENDING. Reevaluate it next tick while the ordinary
    # bounded delivery path below makes room; never discard pending alerts.
    state['cpu'] = cpu
    save(state)  # Persist transitions before any possibly-ambiguous remote write.
    delivery_healthy = True
    from botocore.exceptions import BotoCoreError, ClientError
    for _ in range(3):
        if not state['pending'] or state['pending'][0]['retry_at'] > now:
            break
        entry = state['pending'][0]
        entry['attempts'] += 1
        entry['retry_at'] = now + min(900, 60 * 2 ** min(entry['attempts'] - 1, 4))
        save(state)
        try:
            response = sns.publish(TopicArn=settings['topic_arn'],
                Subject='Inkseq ' + settings['environment'] + ' ' + entry['notification']['transition'],
                Message=encode(entry['notification']).decode().strip())
            require(type(response.get('MessageId')) is str and bool(response['MessageId']), 'alert_publish_ack_missing')
        except (BotoCoreError, ClientError, ValueError):
            delivery_healthy = False
            break  # The request may have succeeded: retry can duplicate, never claim exactly once.
        state['pending'].pop(0)
        save(state)
    observed = snapshot.get('observed_at') if type(snapshot) is dict else None
    fresh = (type(snapshot) is dict and snapshot.get('version') == 1 and snapshot.get('complete') is True
        and type(observed) in (int, float) and math.isfinite(observed)
        and 0 <= now - observed <= settings['thresholds'].get('max_sample_gap_seconds', 180))
    metric = {'Namespace': NAMESPACE, 'MetricData': [
        {'MetricName': 'MonitorHeartbeat', 'Dimensions': [{'Name': 'Environment', 'Value': settings['environment']},
            {'Name': 'InstanceId', 'Value': settings['instance_id']}], 'Timestamp': datetime.fromtimestamp(now, timezone.utc),
         'Value': 1, 'Unit': 'Count'},
        {'MetricName': 'DeliveryHealthy', 'Dimensions': [{'Name': 'Environment', 'Value': settings['environment']},
            {'Name': 'InstanceId', 'Value': settings['instance_id']}], 'Timestamp': datetime.fromtimestamp(now, timezone.utc),
         'Value': int(delivery_healthy and not state['pending'] and not deferred), 'Unit': 'Count'},
        {'MetricName': 'SnapshotHealthy', 'Dimensions': [{'Name': 'Environment', 'Value': settings['environment']},
            {'Name': 'InstanceId', 'Value': settings['instance_id']}], 'Timestamp': datetime.fromtimestamp(now, timezone.utc),
         'Value': int(fresh), 'Unit': 'Count'}]}
    disk = host.get('disk_used_percent')
    if type(disk) in (int, float) and math.isfinite(disk) and 0 <= disk <= 100:
        metric['MetricData'].append({'MetricName': 'DiskUsedPercent',
            'Dimensions': [{'Name': 'Environment', 'Value': settings['environment']},
                {'Name': 'InstanceId', 'Value': settings['instance_id']}],
            'Timestamp': datetime.fromtimestamp(now, timezone.utc), 'Value': disk, 'Unit': 'Percent'})
    cloudwatch.put_metric_data(**metric)
    return state


def main():
    try:
        require(os.name == 'posix' and os.geteuid() == 0 and len(sys.argv) == 1, 'alert_fixed_host_invocation_required')
        import deployment_target
        from alert_conditions import evaluate
        settings = config(read_protected(CONFIG), deployment_target.host())
        with locked(STATE.with_suffix('.lock')):
            state = read_protected(STATE, missing=True)
            if state is None:
                state = initial_state(settings['environment'])
            validate_state(state, settings['environment'])
            snapshot, host, cpu = observe(state['cpu'],
                payment_window_seconds=settings['thresholds'].get('payment_window_seconds', 300))
            sns, cloudwatch = clients()
            tick(settings, state, snapshot, host, cpu, evaluator=evaluate, sns=sns, cloudwatch=cloudwatch,
                 save=lambda value: atomic(STATE, value), now=time.time())
        print('{"state":"alert_monitor_observed"}')
        return 0
    except Exception:
        # Operational failures must be visible through service exit/missing heartbeat,
        # without leaking endpoint responses, configuration or exception strings.
        print('{"state":"alert_monitor_failed"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
