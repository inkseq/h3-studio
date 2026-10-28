"""Modal's gVisor memory cgroup: limit/usage only, no hierarchy or stat files."""

import os
from pathlib import Path
import tempfile
import unittest

from studio_platform.runtime_hosts import wangp_session as session

GROUP = '/ta-01M627EVM0JDKGY9MVTS6GE67V'
LIMIT = 44023414784


@unittest.skipIf(os.name == 'nt', 'POSIX cgroup paths')
class GvisorRamTests(unittest.TestCase):
    def tree(self, root, *, kernel='Linux version 4.19.0-gvisor', stat_files=False):
        proc, cgroup = Path(root) / 'proc', Path(root) / 'sys/fs/cgroup'
        (proc / 'self').mkdir(parents=True)
        memory = cgroup / 'memory'
        memory.mkdir(parents=True)
        (proc / 'version').write_text(kernel)
        (proc / 'meminfo').write_text(
            'MemTotal:       42991616 kB\nMemAvailable:   42991096 kB\n'
        )
        (proc / 'self/cgroup').write_text(
            f'6:memory:{GROUP}\n4:devices:{GROUP}\n1:cpu:{GROUP}\n'
        )
        (proc / 'self/mountinfo').write_text(
            f'25 21 0:26 / {cgroup.as_posix()} ro,nosuid,noexec - tmpfs none ro\n'
            f'31 25 0:15 {GROUP} {memory.as_posix()} rw - cgroup none rw,memory\n'
        )
        (memory / 'memory.limit_in_bytes').write_text(f'{LIMIT}\n')
        (memory / 'memory.usage_in_bytes').write_text('294912\n')
        if stat_files:
            (memory / 'memory.use_hierarchy').write_text('1\n')
        return proc, cgroup

    def test_gvisor_sandbox_uses_its_reserved_limit(self):
        with tempfile.TemporaryDirectory() as root:
            proc, cgroup = self.tree(root)
            self.assertEqual(session._effective_total_profile_ram(proc, cgroup), LIMIT)
            self.assertEqual(
                session._available_profile_ram(proc, cgroup),
                min(42991096 * 1024, LIMIT - 294912),
            )

    def test_missing_files_still_refuse_outside_gvisor(self):
        with tempfile.TemporaryDirectory() as root:
            proc, cgroup = self.tree(root, kernel='Linux version 6.8.0-generic')
            with self.assertRaises(ValueError):
                session._effective_total_profile_ram(proc, cgroup)

    def test_partial_gvisor_tree_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as root:
            proc, cgroup = self.tree(root, stat_files=True)
            with self.assertRaises(ValueError):
                session._effective_total_profile_ram(proc, cgroup)


if __name__ == '__main__':
    unittest.main()
