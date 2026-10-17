"""Compatibility entry point for existing API helper installs."""
from pathlib import Path as _Path

# Execute the one implementation in this module's namespace so legacy imports
# and CLI invocation retain the same entry points and sibling connection helper.
_source = _Path(__file__).with_name("inkseq.py")
exec(compile(_source.read_bytes(), str(_source), "exec"), globals())
