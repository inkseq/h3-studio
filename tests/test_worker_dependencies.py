"""Build-time source selection: no imports, providers or installed dependencies."""

import tempfile
import unittest
from pathlib import Path

from tools.worker_dependencies import (
    NATIVE_MANIFESTS,
    dependency_paths,
    literal_files,
    worker_source_paths,
)


class DependencyClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write("studio_platform/__init__.py", "")

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def closure(self):
        return dependency_paths(self.root, ["studio_platform.worker"])

    def test_local_relative_function_try_package_and_dynamic_edges(self):
        self.write(
            "studio_platform/worker.py",
            "import importlib as loader\n"
            "from importlib import import_module as load\n"
            "import builtins as standard\n"
            "from .sub import child\n"
            "def run():\n"
            "    try:\n"
            "        from . import local\n"
            "    except ImportError:\n"
            "        import studio_platform.fallback\n"
            "    loader.import_module('.dynamic', 'studio_platform')\n"
            "    __import__('studio_platform.extra', fromlist=['leaf'])\n"
            "    __import__('studio_platform.extra', None, None, ['other'])\n"
            "    loader.import_module('api_registry')\n",
        )
        self.write("studio_platform/sub/__init__.py", "from .. import initializer\n")
        self.write("studio_platform/sub/child.py", "from ..worker import run\n")
        for name in ("local", "fallback", "dynamic", "initializer"):
            self.write(f"studio_platform/{name}.py", "")
        self.write("studio_platform/extra/__init__.py", "")
        self.write("studio_platform/extra/leaf.py", "")
        self.write("studio_platform/extra/other.py", "")
        with (self.root / "studio_platform/worker.py").open("a") as stream:
            stream.write(
                "    load('studio_platform.dynamic')\n    standard.__import__('studio_platform.local')\n"
            )
        self.assertEqual(
            self.closure(),
            sorted(
                p.relative_to(self.root).as_posix() for p in self.root.rglob("*.py")
            ),
        )
        self.assertEqual(self.closure(), self.closure())

    def test_new_unreachable_module_is_ignored_until_imported(self):
        self.write("studio_platform/worker.py", "")
        before = self.closure()
        self.write(
            "studio_platform/new_api.py", "raise RuntimeError('never execute')\n"
        )
        self.assertEqual(before, self.closure())
        self.write("studio_platform/worker.py", "from . import new_api\n")
        self.assertIn("studio_platform/new_api.py", self.closure())

    def test_required_import_and_syntax_fail_closed(self):
        for source in (
            "from .missing import value\n",
            "from . import missing\n",
            "import studio_platform.missing\n",
            "from .worker import absent\n",
            "from . import *\n",
            "not valid python !\n",
            "import importlib\nimportlib.import_module(name)\n",
            "from importlib import import_module as load\nload(name)\n",
        ):
            with self.subTest(source=source):
                self.write("studio_platform/worker.py", source)
                with self.assertRaises(ValueError):
                    self.closure()

    def test_links_fail_before_source_is_read(self):
        outside = self.root / "outside.py"
        outside.write_text("", encoding="utf-8")
        try:
            (self.root / "studio_platform/worker.py").symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        with self.assertRaisesRegex(ValueError, "linked_source"):
            self.closure()

    def test_escaped_or_reflected_import_loaders_fail_closed(self):
        self.write("studio_platform/hidden.py", "")
        for source in (
            "import importlib\nload = importlib.import_module\nload('studio_platform.hidden')\n",
            "import importlib\ngetattr(importlib, 'import_module')('studio_platform.hidden')\n",
            "import importlib as library\nload = library.import_module\nload('studio_platform.hidden')\n",
            "import importlib\nlibrary = importlib\nlibrary.import_module('studio_platform.hidden')\n",
            "from importlib import import_module\nload = import_module\nload('studio_platform.hidden')\n",
            "load = __import__\nload('studio_platform.hidden')\n",
            "import importlib\nimportlib.__dict__['import_module']('studio_platform.hidden')\n",
            "__import__('importlib').import_module('studio_platform.hidden')\n",
            "import builtins\ngetattr(builtins, '__import__')('studio_platform.hidden')\n",
        ):
            with self.subTest(source=source):
                self.write("studio_platform/worker.py", source)
                with self.assertRaisesRegex(ValueError, "loader_reference"):
                    self.closure()

    def cli(self):
        self.write(
            "studio_platform/dstack_capacity.py",
            "import importlib\nclass DstackCapacity: pass\n"
            "def main(argv=None):\n"
            "    module = argv\n"
            "    return importlib.import_module(module)\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n",
        )

    def test_only_unreferenced_standalone_capacity_cli_has_dynamic_exception(self):
        self.cli()
        self.write(
            "studio_platform/worker.py", "from .dstack_capacity import DstackCapacity\n"
        )
        self.assertIn("studio_platform/dstack_capacity.py", self.closure())
        for source in (
            "from .dstack_capacity import main as start\n",
            "from .dstack_capacity import *\n",
            "from . import dstack_capacity\n",
            "import studio_platform.dstack_capacity as capacity\n",
            "__import__('studio_platform.dstack_capacity')\n",
            "import studio_platform as platform\nplatform.dstack_capacity.main()\n",
        ):
            with self.subTest(source=source):
                self.write("studio_platform/worker.py", source)
                with self.assertRaisesRegex(ValueError, "cli_boundary"):
                    self.closure()

    def test_cli_called_or_reexported_inside_module_fails(self):
        self.write(
            "studio_platform/worker.py", "from .dstack_capacity import DstackCapacity\n"
        )
        for source in ("start = main\n", "def start():\n    return main()\n"):
            with self.subTest(source=source):
                self.cli()
                with (self.root / "studio_platform/dstack_capacity.py").open("a") as f:
                    f.write(source)
                with self.assertRaisesRegex(ValueError, "cli_boundary"):
                    self.closure()

    def test_cli_parent_module_cannot_escape_through_reflection_or_aliases(self):
        self.cli()
        prefix = "from studio_platform.dstack_capacity import DstackCapacity\n"
        for source in (
            "import studio_platform as sp\ngetattr(getattr(sp, 'dstack_capacity'), 'main')('studio_platform.hidden')\n",
            "import studio_platform as sp\nvars(sp)['dstack_capacity'].main()\n",
            "import studio_platform as sp\nother = sp\ngetattr(other, 'dstack_capacity').main()\n",
            "getattr(__import__('studio_platform'), 'dstack_capacity').main()\n",
            "import importlib\ngetattr(importlib.import_module('studio_platform'), 'dstack_capacity').main()\n",
            "getattr(__import__('studio_platform.worker'), 'dstack_capacity').main()\n",
        ):
            with self.subTest(source=source):
                self.write("studio_platform/worker.py", prefix + source)
                with self.assertRaisesRegex(ValueError, "cli_boundary"):
                    self.closure()

    def test_cli_guard_else_is_executed_at_import_and_is_not_exempt(self):
        self.cli()
        self.write(
            "studio_platform/worker.py", "from .dstack_capacity import DstackCapacity\n"
        )
        path = self.root / "studio_platform/dstack_capacity.py"
        with path.open("a") as stream:
            stream.write("else:\n    main()\n")
        with self.assertRaisesRegex(ValueError, "cli_boundary"):
            self.closure()

    def test_cli_exception_does_not_cover_another_function_or_dynamic_expression(self):
        self.write(
            "studio_platform/worker.py", "from .dstack_capacity import DstackCapacity\n"
        )
        for replacement in ("def helper(argv=None):", "def main(argv=None):"):
            self.cli()
            path = self.root / "studio_platform/dstack_capacity.py"
            source = path.read_text().replace("def main(argv=None):", replacement)
            source = source.replace(
                "import_module(module)", "import_module(argv + '.service')"
            )
            path.write_text(source)
            with self.assertRaises(ValueError):
                self.closure()

    def test_cli_default_argument_runs_at_import_and_cannot_use_exception(self):
        self.cli()
        self.write(
            "studio_platform/worker.py", "from .dstack_capacity import DstackCapacity\n"
        )
        path = self.root / "studio_platform/dstack_capacity.py"
        path.write_text(
            path.read_text().replace(
                "def main(argv=None):",
                "def main(argv=importlib.import_module(module)):",
            )
        )
        with self.assertRaisesRegex(ValueError, "dynamic_import_unresolved"):
            self.closure()

    def test_native_manifest_is_literal_data_and_missing_member_fails(self):
        self.write(
            "deploy/package.py",
            "FILES = ('native.py',)\nraise RuntimeError('never execute')\n",
        )
        self.write("native.py", "raise RuntimeError('never execute')\n")
        self.assertEqual(
            {"native.py"}, literal_files(self.root, "deploy/package.py", "FILES")
        )
        (self.root / "native.py").unlink()
        with self.assertRaises(FileNotFoundError):
            literal_files(self.root, "deploy/package.py", "FILES")
        self.write("deploy/package.py", "FILES = list_factory()\n")
        with self.assertRaises(ValueError):
            literal_files(self.root, "deploy/package.py", "FILES")


class RealWorkerSourcesTests(unittest.TestCase):
    def test_real_cpu_and_native_contracts_remain_in_scope(self):
        root = Path(__file__).resolve().parents[1]
        names = set(worker_source_paths(root))
        for manifest, variable in NATIVE_MANIFESTS.items():
            self.assertTrue(literal_files(root, manifest, variable).issubset(names))
        migrations = {
            p.relative_to(root).as_posix()
            for p in (root / "studio_platform/migrations").rglob("*.py")
        }
        self.assertTrue(migrations.issubset(names))
        self.assertTrue(
            {
                "studio_platform/dstack_controller.py",
                "studio_platform/worker.py",
                "studio_platform/api.py",
                "studio_platform/queue.py",
                "studio_platform/repository.py",
                "studio_platform/credits.py",
                "studio_platform/credit_schema.py",
                "studio_platform/payment_schema.py",
                "studio_platform/dialout_boot.py",
                "studio_platform/inference/wangp_compiler.py",
                "deploy/wangp/probe_gpu.py",
                "deploy/wangp/bootstrap.py",
                "deploy/dstack/Dockerfile.wangp",
                "deploy/wangp/requirements.ocr.txt",
            }.issubset(names)
        )


if __name__ == "__main__":
    unittest.main()
