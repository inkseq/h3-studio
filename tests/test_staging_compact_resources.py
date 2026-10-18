"""Synthetic compact-host guards only; no swap, service, AWS or GPU operations."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from deploy.platform import deployment_target as target
from test_platform_preflight import preflight, valid_host


def compact_environment():
    return {'INKSEQ_DEPLOYMENT_TARGET': 'staging', 'INKSEQ_RESOURCE_PROFILE': target.COMPACT_RESOURCES,
            'INKSEQ_CPU_CGROUP_PARENT': target.COMPACT_PARENT}


def compact_metadata():
    return {'instance_id': target.COMPACT_INSTANCE, 'cgroup_driver': 'systemd', 'cgroup_version': '2',
        'memory_bytes': 1840*target.MIB, 'memory_max': 1280*target.MIB, 'swap_max': 2048*target.MIB,
        'swap_file': {'regular': True, 'uid': 0, 'gid': 0, 'mode': 0o600, 'links': 1,
                     'bytes': 2048*target.MIB, 'allocated_bytes': 2048*target.MIB},
        'swaps': [{'path': str(target.COMPACT_SWAP), 'kind': 'file', 'bytes': 2048*target.MIB-4096, 'priority': 10}],
        'disk_free_bytes': 3*1024**3, 'swappiness': 60, 'unit_hashes': dict(target.COMPACT_UNITS),
        'docker_requires': [target.COMPACT_PARENT, 'srv-sixnine-staging.swap.swap', 'inkseq-staging-secrets.service'],
        'docker_after': [target.COMPACT_PARENT, 'srv-sixnine-staging.swap.swap', 'inkseq-staging-secrets.service']}


class CompactResourceTests(unittest.TestCase):
    def validate(self, metadata=None, memory_bytes=1840*target.MIB):
        return target.validate_resource_metadata(compact_environment(), metadata or compact_metadata(), memory_bytes=memory_bytes)

    def test_closed_profile_preserves_all_caps_and_physical_reserve(self):
        selected = self.validate()
        self.assertEqual(sum(target.COMPACT_SERVICE_MIB.values()), 3296)
        self.assertLessEqual(3296, selected['resident_mib']+selected['swap_mib'])
        self.assertGreaterEqual(selected['minimum_host_mib']-selected['resident_mib'], 512)
        self.assertEqual((target.COMPACT_SERVICE_MIB['dstack'], target.COMPACT_SERVICE_MIB['dispatch'],
                          target.COMPACT_SERVICE_MIB['controller']), (768, 128, 640))

    def test_compact_is_never_a_production_or_arbitrary_parent_override(self):
        for patch_value in ({'INKSEQ_DEPLOYMENT_TARGET': 'production'}, {'INKSEQ_RESOURCE_PROFILE': '2g'},
                            {'INKSEQ_CPU_CGROUP_PARENT': 'unlimited.slice'}):
            with self.subTest(patch_value=patch_value), self.assertRaisesRegex(ValueError, 'resource_profile_invalid'):
                target.resources({**compact_environment(), **patch_value})
        self.assertEqual(target.resources({})['minimum_host_mib'], 3584)

    def test_missing_swap_limits_wrong_driver_or_host_refuse(self):
        for key, value in (('swap_max', 0), ('memory_max', 2048*target.MIB), ('memory_max', 'max'),
                           ('cgroup_version', '1'), ('cgroup_driver', 'cgroupfs'), ('instance_id', 'i-'+'0'*17)):
            value_copy = compact_metadata(); value_copy[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.validate(value_copy)
        with self.assertRaisesRegex(ValueError, 'physical_memory_below_floor'):
            self.validate(memory_bytes=1792*target.MIB-1)

    def test_unprotected_sparse_or_replaced_swap_refuse(self):
        for key, value in (('regular', False), ('uid', 10001), ('gid', 10001), ('mode', 0o644), ('links', 2),
                           ('bytes', 1024*target.MIB), ('allocated_bytes', 0)):
            value_copy = compact_metadata(); value_copy['swap_file'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'swap_file_not_protected'):
                self.validate(value_copy)

    def test_swapon_total_elsewhere_or_priority_does_not_substitute_for_fixed_swap(self):
        for swaps in ([], [{'path': '/tmp/swap', 'kind': 'file', 'bytes': 2048*target.MIB-4096, 'priority': 10}],
                      compact_metadata()['swaps']*2):
            value = compact_metadata(); value['swaps'] = swaps
            with self.subTest(swaps=swaps), self.assertRaisesRegex(ValueError, 'swap_not_active'):
                self.validate(value)

    def test_disk_boot_and_actual_swap_are_required(self):
        for key, value in (('disk_free_bytes', 3*1024**3-1), ('swappiness', 0), ('unit_hashes', {}),
                           ('docker_requires', []), ('docker_after', [])):
            value_copy = compact_metadata(); value_copy[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate(value_copy)

    def test_all_shipped_new_units_match_pinned_linux_bytes(self):
        directory = Path(__file__).resolve().parents[1]/'deploy/platform/ec2'
        for path, expected in target.COMPACT_UNITS.items():
            name = Path(path).name
            if name == 'inkseq-staging-secrets.service':
                continue  # Existing reviewed normal hydrator is not rewritten by this batch.
            self.assertEqual(hashlib.sha256((directory/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest(), expected)

    def test_sealed_host_record_is_explicit_and_only_the_existing_stage_can_opt_in(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); path = root/'deployment-target.json'
            proxy = types.SimpleNamespace(name='nt', O_RDONLY=os.O_RDONLY, O_NOFOLLOW=getattr(os, 'O_NOFOLLOW', 0),
                open=os.open, fstat=os.fstat, read=os.read, close=os.close)
            with patch.object(target, 'os', proxy):
                for instance, kind in ((target.COMPACT_INSTANCE, 'staging'), (target.COMPACT_INSTANCE, 'production'),
                                       ('i-'+'0'*17, 'staging')):
                    path.write_text(json.dumps({'version': 1, 'target': kind, 'instance_id': instance,
                                                'resource_profile': target.COMPACT_RESOURCES}))
                    if (instance, kind) == (target.COMPACT_INSTANCE, 'staging'):
                        self.assertEqual(target.resource_environment(target.host(root)),
                                         {k: v for k, v in compact_environment().items() if k != 'INKSEQ_DEPLOYMENT_TARGET'})
                    else:
                        with self.assertRaisesRegex(ValueError, 'resource_host_invalid'):
                            target.host(root)

    def test_small_host_requires_compact_proof_and_rollback_requires_medium_first(self):
        snapshot = valid_host(); snapshot['docker']['memory_bytes'] = 1840*target.MIB
        with self.assertRaisesRegex(preflight.PreflightError, 'memory_below_reviewed_floor'):
            preflight.validate_snapshot(snapshot)
        snapshot.update(resource_environment=compact_environment(), compact_resources=compact_metadata())
        self.assertEqual(preflight.validate_snapshot(snapshot)['state'], 'host_metadata_ready')
        del snapshot['resource_environment']; del snapshot['compact_resources']
        with self.assertRaises(preflight.PreflightError):
            preflight.validate_snapshot(snapshot)
        snapshot['docker']['memory_bytes'] = 3584*target.MIB
        self.assertEqual(preflight.validate_snapshot(snapshot)['state'], 'host_metadata_ready')


if __name__ == '__main__':
    unittest.main()
