"""Offline cgroup v1/hybrid memory boundaries; no host or provider calls."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from studio_platform.runtime_catalog import get_profile
from studio_platform.runtime_hosts import wangp_session as session

GIB = 1024**3


@unittest.skipUnless(os.name == 'posix', 'cgroup mountinfo paths require Linux')
class V1MemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc, self.groups = self.root / 'proc', self.root / 'cgroups'
        (self.proc / 'self').mkdir(parents=True)
        self.groups.mkdir()
        (self.proc / 'meminfo').write_text(
            f'MemTotal: {60 * GIB // 1024} kB\nMemAvailable: {50 * GIB // 1024} kB\n'
        )
        self.memory = self.groups / 'memory'
        self.mount('/')
        self.group(self.memory, 60 * GIB, used=0)

    def mount(self, root, membership=None):
        membership = root if membership is None else membership
        (self.proc / 'self/cgroup').write_text(
            f'13:memory:{membership}\n0::{membership}\n'
        )
        (self.proc / 'self/mountinfo').write_text(
            f'637 624 0:47 {root} {self.memory} ro,nosuid,nodev,noexec - cgroup cgroup rw,memory\n'
        )

    def group(
        self,
        path,
        limit,
        *,
        used=0,
        hierarchy=None,
        cache=0,
        shmem=0,
        dirty=0,
        writeback=0,
        unevictable=0,
    ):
        path.mkdir(parents=True, exist_ok=True)
        (path / 'memory.limit_in_bytes').write_text(str(limit))
        (path / 'memory.usage_in_bytes').write_text(str(used))
        (path / 'memory.use_hierarchy').write_text('1')
        counters = dict(
            cache=cache,
            shmem=shmem,
            active_file=cache,
            inactive_file=0,
            dirty=dirty,
            writeback=writeback,
            unevictable=unevictable,
        )
        (path / 'memory.stat').write_text(
            'hierarchical_memory_limit '
            + str(limit if hierarchy is None else hierarchy)
            + '\n'
            + ''.join(f'total_{key} {value}\n' for key, value in counters.items())
        )

    def test_real_hybrid_subtree_mount_maps_process_membership_to_root(self):
        self.mount('/docker/instance')
        self.group(self.memory, 29 * GIB, used=5 * GIB)
        self.assertEqual(
            session._effective_total_profile_ram(self.proc, self.groups), 29 * GIB
        )
        # Hidden ancestor usage is not inferred from leaf usage or host free RAM.
        self.assertEqual(session._available_profile_ram(self.proc, self.groups), 0)

    def test_hybrid_v2_other_controllers_do_not_hide_v1_memory(self):
        (self.groups / 'cgroup.controllers').write_text('cpu pids io')
        self.mount('/docker/instance')
        self.group(self.memory, 29 * GIB)
        self.assertEqual(
            session._effective_total_profile_ram(self.proc, self.groups), 29 * GIB
        )
        (self.groups / 'cgroup.controllers').write_text('cpu memory pids')
        with self.assertRaisesRegex(ValueError, 'cgroup_invalid'):
            session._effective_total_profile_ram(self.proc, self.groups)

    def test_unlimited_numeric_controller_still_bounds_capacity_by_host(self):
        self.group(self.memory, 9223372036854771712)
        self.assertEqual(
            session._effective_total_profile_ram(self.proc, self.groups), 60 * GIB
        )
        self.assertEqual(
            session._available_profile_ram(self.proc, self.groups), 50 * GIB
        )

    def test_visible_child_and_ancestor_limits_are_all_applied(self):
        self.mount('/', '/parent/child')
        self.group(self.memory / 'parent', 29 * GIB, used=25 * GIB)
        self.group(self.memory / 'parent/child', 40 * GIB, used=GIB)
        self.assertEqual(
            session._effective_total_profile_ram(self.proc, self.groups), 29 * GIB
        )
        self.assertEqual(
            session._available_profile_ram(self.proc, self.groups), 4 * GIB
        )
        self.group(self.memory / 'parent/child', 28 * GIB, used=GIB)
        self.assertEqual(
            session._effective_total_profile_ram(self.proc, self.groups), 28 * GIB
        )

    def test_hidden_parent_limit_is_not_discarded(self):
        self.mount('/docker/instance')
        self.group(self.memory, 40 * GIB, hierarchy=28 * GIB)
        self.assertEqual(
            session._effective_total_profile_ram(self.proc, self.groups), 28 * GIB
        )
        self.assertEqual(session._available_profile_ram(self.proc, self.groups), 0)

    def test_headroom_uses_only_coherent_clean_hierarchical_cache(self):
        self.group(
            self.memory,
            29 * GIB,
            used=28 * GIB,
            cache=12 * GIB,
            shmem=2 * GIB,
            dirty=GIB,
            writeback=2 * GIB,
            unevictable=GIB,
        )
        self.assertEqual(
            session._available_profile_ram(self.proc, self.groups), 7 * GIB
        )
        with (self.memory / 'memory.stat').open('a') as target:
            target.write(f'cache {60 * GIB}\nactive_file {60 * GIB}\ndirty 0\n')
        self.assertEqual(
            session._available_profile_ram(self.proc, self.groups), 7 * GIB
        )

    def test_missing_shmem_counter_gets_no_reclaimability_credit(self):
        self.group(self.memory, 29 * GIB, used=28 * GIB, cache=12 * GIB)
        path = self.memory / 'memory.stat'
        path.write_text(
            '\n'.join(
                line
                for line in path.read_text().splitlines()
                if not line.startswith('total_shmem ')
            )
        )
        self.assertEqual(session._available_profile_ram(self.proc, self.groups), GIB)

    def test_hybrid_capacity_retains_29_gib_and_vram_admission_floors(self):
        profile = get_profile(
            'h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4'
        )
        self.mount('/docker/instance')
        self.group(self.memory, 29 * GIB, used=28 * GIB)
        total = session._effective_total_profile_ram
        available = session._available_profile_ram
        with (
            patch.object(
                session,
                '_effective_total_profile_ram',
                side_effect=lambda: total(self.proc, self.groups),
            ),
            patch.object(
                session,
                '_available_profile_ram',
                side_effect=lambda: available(self.proc, self.groups),
            ),
            redirect_stdout(io.StringIO()),
        ):
            session._profile_memory_admission(
                NS(cuda=NS(mem_get_info=lambda _: (31 * GIB, 32 * GIB))), profile
            )
            self.group(self.memory, 28 * GIB)
            with self.assertRaisesRegex(ValueError, 'memory_headroom_insufficient'):
                session._profile_memory_admission(
                    NS(cuda=NS(mem_get_info=lambda _: (31 * GIB, 32 * GIB))), profile
                )
            self.group(self.memory, 29 * GIB)
            with self.assertRaisesRegex(ValueError, 'memory_headroom_insufficient'):
                session._profile_memory_admission(
                    NS(cuda=NS(mem_get_info=lambda _: (0, 32 * GIB))), profile
                )

    def test_invalid_or_ambiguous_membership_mounts_are_rejected(self):
        valid = (self.proc / 'self/mountinfo').read_text()
        cases = [
            ('13:memory:/../outside\n', valid),
            ('13:memory:/missing\n', valid),
            ('13:memory:/\n14:memory:/\n', valid),
            ('13:memory:/\n', valid + valid),
            (
                '13:memory:/docker/instance-other\n',
                valid.replace(' / ', ' /docker/instance ', 1),
            ),
        ]
        for membership, mount in cases:
            with self.subTest(membership=membership, mount=mount):
                (self.proc / 'self/cgroup').write_text(membership)
                (self.proc / 'self/mountinfo').write_text(mount)
                with self.assertRaisesRegex(ValueError, 'cgroup_invalid'):
                    session._effective_total_profile_ram(self.proc, self.groups)

    def test_invalid_limit_usage_and_hierarchy_stat_fail_closed(self):
        for name, bad in (
            ('memory.limit_in_bytes', '-1'),
            ('memory.limit_in_bytes', '0'),
            ('memory.limit_in_bytes', 'bad'),
            ('memory.usage_in_bytes', '-1'),
            ('memory.use_hierarchy', '0'),
            ('memory.stat', 'total_cache 1\n'),
            ('memory.stat', 'hierarchical_memory_limit 0\n'),
            ('memory.stat', 'hierarchical_memory_limit 1\ntotal_cache -1\n'),
            (
                'memory.stat',
                'hierarchical_memory_limit 1\nhierarchical_memory_limit 2\n',
            ),
        ):
            p = self.memory / name
            original = p.read_text()
            p.write_text(bad)
            try:
                with (
                    self.subTest(name=name, bad=bad),
                    self.assertRaisesRegex(ValueError, 'cgroup_invalid'),
                ):
                    session._effective_total_profile_ram(self.proc, self.groups)
            finally:
                p.write_text(original)

    def test_missing_unreadable_and_symlink_controller_files_fail_closed(self):
        p = self.memory / 'memory.limit_in_bytes'
        original = p.read_text()
        p.unlink()
        with self.assertRaisesRegex(ValueError, 'cgroup_invalid'):
            session._effective_total_profile_ram(self.proc, self.groups)
        outside = self.root / 'limit'
        outside.write_text(original)
        p.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'cgroup_invalid'):
            session._effective_total_profile_ram(self.proc, self.groups)
        p.unlink()
        p.write_text(original)
        read_text = Path.read_text

        def unreadable(candidate, *args, **kwargs):
            if candidate == p:
                raise PermissionError('private path')
            return read_text(candidate, *args, **kwargs)

        with (
            patch.object(Path, 'read_text', unreadable),
            self.assertRaisesRegex(ValueError, 'cgroup_invalid'),
        ):
            session._effective_total_profile_ram(self.proc, self.groups)
