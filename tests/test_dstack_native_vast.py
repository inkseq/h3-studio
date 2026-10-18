"""Include the offline pinned Vast boundary suite in normal CI discovery."""

import importlib.util
from pathlib import Path


def load_tests(loader, tests, pattern):
    path = (
        Path(__file__).resolve().parents[1] / "deploy/dstack/test_native_vast_safety.py"
    )
    spec = importlib.util.spec_from_file_location(
        "sixnine_native_vast_boundary_tests", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return loader.loadTestsFromModule(module)
