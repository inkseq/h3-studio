"""Conservative, import-free worker source closure used only while packaging.

This selects source bytes; it never imports the application or changes its job,
native-package or execution identities. External dependencies remain governed by
the release's pinned lock/profile files, not the build machine's environment.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath
import stat

WORKER_ROOTS = (
    "studio_platform.dstack_controller",
    "studio_platform.worker",
    "studio_platform.runtime_hosts.wangp_launcher",
    "studio_platform.modal_entry",
    "studio_platform.dialout_boot",
)
NATIVE_MANIFESTS = {
    "deploy/wangp/package_tool.py": "PRIVATE_FILES",
    "tools/build_modal_sources.py": "EXTRA",
}
WORKER_POLICY_FILES = ("tools/worker_dependencies.py", "deploy/dstack/Dockerfile.wangp")
CLI_MODULE = "studio_platform.dstack_capacity"
CLI_PARENT = CLI_MODULE.rpartition(".")[0]


def read_source(root, name):
    """Do not follow linked source or accept paths escaping the release tree."""
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError("worker_dependency_invalid_path")
    path = root
    for part in relative.parts:
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("worker_dependency_linked_source")
    if not path.is_file() or path.stat().st_size > 16 * 1024**2:
        raise ValueError("worker_dependency_invalid_source")
    return path.read_bytes()


def parse_source(root, name):
    try:
        return ast.parse(read_source(root, name), filename=name)
    except SyntaxError as error:
        raise ValueError(f"worker_dependency_invalid_python: {name}") from error


def literal_files(root, name, variable):
    tree = parse_source(root, name)
    values = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets)
    ]
    if len(values) != 1:
        raise ValueError("worker_dependency_manifest_invalid")
    value = ast.literal_eval(values[0])
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(n, str) for n in value
    ):
        raise ValueError("worker_dependency_manifest_invalid")
    for name in value:
        read_source(root, name)
    return set(value)


class Closure:
    def __init__(self, root):
        self.root = Path(root)
        self.paths = set()
        self.visited = set()
        self.trees = {}
        self.module_paths = {}
        self.indexed = set()
        self.local_roots = {
            p.stem if p.suffix == ".py" else p.name for p in self.root.iterdir()
        }

    def module_path(self, module):
        first = module.split(".")[0]
        if first not in self.indexed:
            self.indexed.add(first)
            folder = self.root / first
            paths = (
                folder.rglob("*.py")
                if folder.is_dir()
                else [self.root / (first + ".py")]
            )
            for path in paths:
                if path.exists() or path.is_symlink():
                    name = path.relative_to(self.root).as_posix()
                    key = (
                        name.removesuffix(".py")
                        .replace("/", ".")
                        .removesuffix(".__init__")
                    )
                    if key in self.module_paths and self.module_paths[key] != name:
                        raise ValueError("worker_dependency_ambiguous_module")
                    self.module_paths[key] = name
        return self.module_paths.get(module)

    def tree(self, name):
        if name not in self.trees:
            self.trees[name] = parse_source(self.root, name)
        return self.trees[name]

    def internal(self, module):
        first = module.split(".")[0]
        return first == "studio_platform" or first in self.local_roots

    def add(self, module, *, module_object=False):
        if not self.internal(module):
            return
        if module == CLI_MODULE and module_object:
            raise ValueError("worker_dependency_cli_boundary_reference")
        name = self.module_path(module)
        if name is None:
            raise ValueError(f"worker_dependency_missing_module: {module}")
        if module in self.visited:
            return
        self.visited.add(module)
        self.paths.add(name)
        for length in range(1, len(module.split("."))):
            parent = ".".join(module.split(".")[:length])
            if self.module_path(parent):
                self.add(parent)
        Imports(self, module, name).visit(self.tree(name))

    def from_import(self, base, names, *, exclude=None):
        if not self.internal(base):
            return
        self.add(base)
        tree = self.tree(self.module_path(base))
        exports = bindings(n for n in tree.body if n is not exclude)
        for imported in names:
            if base == CLI_MODULE and imported in {"main", "*"}:
                raise ValueError("worker_dependency_cli_boundary_reference")
            if imported == "*":
                raise ValueError("worker_dependency_star_import_unresolved")
            child = base + "." + imported
            if imported != "*" and self.module_path(child):
                self.add(child, module_object=True)
            elif imported != "*" and imported not in exports:
                raise ValueError(f"worker_dependency_unresolved_import: {child}")


def bindings(nodes):
    """Known module exports, including conditional definitions, without execution."""
    result = set()
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            result.update(n.asname or n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            result.add(node.id)
        else:
            result.update(bindings(ast.iter_child_nodes(node)))
    return result


class Imports(ast.NodeVisitor):
    def __init__(self, closure, module, name):
        self.closure, self.module = closure, module
        self.package = (
            module if name.endswith("/__init__.py") else module.rpartition(".")[0]
        )
        self.function = None
        self.dynamic_names = {"__import__": "__import__"}
        self.importlib_names = {"importlib"}
        self.import_names = {}
        tree = closure.tree(name)
        self.parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.importlib_names.update(
                    n.asname or n.name for n in node.names if n.name == "importlib"
                )
                self.import_names.update(
                    (
                        n.asname or n.name.split(".")[0],
                        n.name if n.asname else n.name.split(".")[0],
                    )
                    for n in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module in {
                "importlib",
                "builtins",
            }:
                self.dynamic_names.update(
                    (n.asname or n.name, n.name)
                    for n in node.names
                    if n.name in {"import_module", "__import__"}
                )
        if module == CLI_MODULE:
            self.check_cli_boundary(tree)

    @staticmethod
    def check_cli_boundary(tree):
        # This CLI is not a worker entrypoint. Only named non-main exports may
        # be imported. Any new reference makes the exception require review.
        guards = [
            n
            for n in tree.body
            if isinstance(n, ast.If)
            and ast.dump(n.test)
            == ast.dump(ast.parse("__name__ == '__main__'", mode="eval").body)
        ]
        allowed = {
            id(n) for guard in guards for body in guard.body for n in ast.walk(body)
        }
        if any(
            isinstance(n, ast.Name) and n.id == "main" and id(n) not in allowed
            for n in ast.walk(tree)
        ):
            raise ValueError("worker_dependency_cli_boundary_reference")

    def relative(self, module, level, package=None):
        parts = (self.package if package is None else package).split(".")
        if level > len(parts) or not parts[0]:
            raise ValueError("worker_dependency_invalid_relative_import")
        return ".".join(parts[: len(parts) - level + 1] + ([module] if module else []))

    def visit_Import(self, node):
        for alias in node.names:
            self.closure.add(alias.name, module_object=True)

    def visit_ImportFrom(self, node):
        base = self.relative(node.module, node.level) if node.level else node.module
        if base == "importlib" and any(
            n.name not in {"import_module", "metadata"} for n in node.names
        ):
            raise ValueError("worker_dependency_loader_reference_unresolved")
        if base == "builtins" and any(
            n.name.startswith("_") and n.name != "__import__" for n in node.names
        ):
            raise ValueError("worker_dependency_loader_reference_unresolved")
        self.closure.from_import(base, [n.name for n in node.names], exclude=node)

    def direct_call(self, node):
        parent = self.parents.get(node)
        return isinstance(parent, ast.Call) and parent.func is node

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load):
            return
        if node.id in self.dynamic_names and not self.direct_call(node):
            raise ValueError("worker_dependency_loader_reference_unresolved")
        module = self.import_names.get(node.id, node.id)
        if module == CLI_PARENT:
            parent = self.parents.get(node)
            if (
                not isinstance(parent, ast.Attribute)
                or parent.value is not node
                or parent.attr.startswith("_")
            ):
                raise ValueError("worker_dependency_cli_boundary_reference")
        if module in {"importlib", "builtins"}:
            parent = self.parents.get(node)
            if not isinstance(parent, ast.Attribute) or parent.value is not node:
                raise ValueError("worker_dependency_loader_reference_unresolved")
            allowed = (
                {"import_module", "metadata"}
                if module == "importlib"
                else {"__import__"}
            )
            if parent.attr not in allowed:
                raise ValueError("worker_dependency_loader_reference_unresolved")
            if parent.attr in {"import_module", "__import__"} and not self.direct_call(
                parent
            ):
                raise ValueError("worker_dependency_loader_reference_unresolved")

    def visit_FunctionDef(self, node):
        for child in [*node.decorator_list, node.args, node.returns]:
            if child is not None:
                self.visit(child)
        previous = self.function
        self.function = node.name if previous is None else previous + "." + node.name
        for child in node.body:
            self.visit(child)
        self.function = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous = self.function
        self.function = "class:" + node.name
        self.generic_visit(node)
        self.function = previous

    def visit_Attribute(self, node):
        parts, head = [], node
        while isinstance(head, ast.Attribute):
            parts.insert(0, head.attr)
            head = head.value
        if isinstance(head, ast.Name):
            target = ".".join([self.import_names.get(head.id, head.id), *parts])
            if target == CLI_MODULE:
                raise ValueError("worker_dependency_cli_boundary_reference")
        self.generic_visit(node)

    def visit_Call(self, node):
        function = node.func
        kind = (
            self.dynamic_names.get(function.id)
            if isinstance(function, ast.Name)
            else None
        )
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "import_module"
            and isinstance(function.value, ast.Name)
            and function.value.id in self.importlib_names
        ):
            kind = "import_module"
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "__import__"
            and isinstance(function.value, ast.Name)
            and self.import_names.get(function.value.id) == "builtins"
        ):
            kind = "__import__"
        if kind:
            self.dynamic_import(node, kind)
        self.generic_visit(node)

    def dynamic_import(self, node, kind):
        target = (
            node.args[0]
            if node.args
            else next((k.value for k in node.keywords if k.arg == "name"), None)
        )
        if not isinstance(target, ast.Constant) or not isinstance(target.value, str):
            if (
                kind == "import_module"
                and self.module == CLI_MODULE
                and self.function == "main"
                and isinstance(target, ast.Name)
                and target.id == "module"
            ):
                return
            raise ValueError(
                f"worker_dependency_dynamic_import_unresolved: {self.module}"
            )
        module = target.value
        if module in {"importlib", "builtins"}:
            raise ValueError("worker_dependency_loader_reference_unresolved")
        if module.startswith("."):
            package = (
                node.args[1]
                if len(node.args) > 1
                else next((k.value for k in node.keywords if k.arg == "package"), None)
            )
            if not isinstance(package, ast.Constant) or not isinstance(
                package.value, str
            ):
                raise ValueError("worker_dependency_dynamic_package_unresolved")
            level = len(module) - len(module.lstrip("."))
            module = self.relative(module.lstrip("."), level, package.value)
        returned_module = module
        self.closure.add(module, module_object=True)
        if kind == "__import__":
            level = (
                node.args[4]
                if len(node.args) > 4
                else next(
                    (k.value for k in node.keywords if k.arg == "level"),
                    ast.Constant(0),
                )
            )
            if not isinstance(level, ast.Constant) or level.value != 0:
                raise ValueError("worker_dependency_dynamic_level_unresolved")
            fromlist = (
                node.args[3]
                if len(node.args) > 3
                else next((k.value for k in node.keywords if k.arg == "fromlist"), None)
            )
            # __import__ without a nonempty fromlist returns its top-level
            # package, even when the literal target names a safe child module.
            returned_module = module.split(".")[0]
            if fromlist is not None and self.closure.internal(module):
                values = ast.literal_eval(fromlist)
                if not isinstance(values, (list, tuple)) or not all(
                    isinstance(n, str) for n in values
                ):
                    raise ValueError("worker_dependency_dynamic_fromlist_unresolved")
                self.closure.from_import(module, values)
                if values:
                    returned_module = module
        if returned_module == CLI_PARENT and not isinstance(
            self.parents.get(node), ast.Expr
        ):
            raise ValueError("worker_dependency_cli_boundary_reference")


def dependency_paths(root, roots):
    closure = Closure(root)
    for module in roots:
        closure.add(module)
    return sorted(closure.paths)


def worker_source_paths(root):
    root = Path(root)
    explicit = set(WORKER_POLICY_FILES) | set(NATIVE_MANIFESTS)
    for name, variable in NATIVE_MANIFESTS.items():
        explicit.update(literal_files(root, name, variable))
    # Alembic discovers env/revisions by path, not through ordinary imports.
    # Retain the old worker hash's complete Python migration family as well.
    migrations = root / "studio_platform/migrations"
    if (
        not migrations.is_dir()
        or migrations.is_symlink()
        or getattr(migrations.stat(), "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("worker_dependency_invalid_migrations")
    explicit.update(
        p.relative_to(root).as_posix()
        for p in migrations.rglob("*.py")
        if "__pycache__" not in p.parts
    )
    roots = set(WORKER_ROOTS)
    for name in explicit - set(WORKER_POLICY_FILES) - set(NATIVE_MANIFESTS):
        if name.endswith(".py"):
            roots.add(
                name.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            )
    return sorted(explicit | set(dependency_paths(root, roots)))
