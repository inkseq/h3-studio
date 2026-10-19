"""Lazy access to the existing central registry, without provider lifecycle code."""

import importlib
import os
from pathlib import Path
import sys


class RegistryUnavailable(ValueError):
    """Static registry errors; configuration and credentials are never printed."""


def load_api(service, *, profile):
    """Import the existing central implementation lazily, without copying it."""
    configured = os.environ.get("AI_REGISTRY_ROOT", "")
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise RegistryUnavailable("lium_registry_path_invalid")
    elif os.name == "nt":
        root = Path(r"C:\Users\danmo\Desktop\AI-Registry")
    else:
        # This known WSL mount must really exist; an AWS host gets no fallback.
        root = Path("/mnt/c/Users/danmo/Desktop/AI-Registry")
    source = root / "api_registry.py"
    if not source.is_file():
        raise RegistryUnavailable("lium_central_loader_unavailable")
    try:
        existing = sys.modules.get("api_registry")
        if (
            existing is not None
            and Path(existing.__file__).resolve() != source.resolve()
        ):
            raise ValueError
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        module = importlib.import_module("api_registry")
        if Path(module.__file__).resolve() != source.resolve():
            raise ValueError
        return module.load_api(service, profile=profile)
    except Exception:
        raise RegistryUnavailable("lium_central_loader_unavailable") from None
