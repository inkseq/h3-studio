#!/usr/bin/python3
"""Fixed SSM target: update an initialized site to an independently approved SHA."""
import json
import sys

import fetch_release_s3
import release


def require_no_gpu_acceptance(root):
    for folder in ('gpu-acceptance', 'gpu-scaler'):
        marker = root/folder/'active.json'
        if marker.exists() or marker.is_symlink():
            release.regular(marker, root_owned=True, maximum=16384)
            value = json.loads(marker.read_text())
            release.require(isinstance(value, dict) and type(value.get('version')) is int and value['version'] in (1, 2)
                            and value.get('active') is False, 'gpu_acceptance_requires_explicit_safe_restore')
    # Also catch a service started before its marker was introduced. Never
    # interrupt an uncertain acceptance worker through a routine app release.
    for service in ('gpu-worker', 'gpu-controller'):
        running = release.command(['ps', '--quiet',
            '--filter', 'label=com.docker.compose.project=sixnine-platform',
            '--filter', 'label=com.docker.compose.service='+service],
            environment={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
                         'DOCKER_CONFIG': '/opt/sixnine-release/docker-config'}, timeout=20)
        release.require(not running.strip(), 'gpu_acceptance_worker_still_running')


def deploy(commit, *, root=release.ROOT):
    release.require(bool(release.SHA.fullmatch(commit)), 'invalid_commit')
    release.check_host(root)
    import fcntl
    with (root/'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_file = root/'release-state.json'
        release.regular(state_file, root_owned=True, maximum=16384)
        state = json.loads(state_file.read_text())
        # Routine CI cannot provision passwords or publish an uninitialized site.
        current = state.get('current')
        release.require(isinstance(current, str) and bool(release.SHA.fullmatch(current)),
                        'first_release_requires_independent_operator_bootstrap')
        release.require(state.get('pending') in (None, commit), 'another_release_requires_reconciliation')
        # Legacy/unknown lifecycles still block before downloading. A v2
        # execution pin is rechecked against the approved target in apply_locked.
        migration = release.migration_deployment_enabled(root)
        if not migration:
            release.gpu_deployment_context(root)
        fetch_release_s3.fetch(commit, root=root)
        if migration:
            release.migration_helper().fetch(root, commit)
        # Both download and apply stay under the same host lock. The root
        # approval file is checked again while copying the incoming bundle.
        release.apply_locked(root, commit)
        release.post_release_retention(root)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        release.require(len(argv) == 1, 'one_commit_argument_required')
        deploy(argv[0])
        print(json.dumps({'state': 'approved_application_release_healthy', 'commit': argv[0]}))
        return 0
    except Exception:
        print('Approved deployment incomplete; inspect host release-state and retry the same commit after reconciliation',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
