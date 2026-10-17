"""Local entry point for the canonical downloadable public connection helper."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def main():
    source = Path(__file__).resolve().parent.parent / "skills" / "inkseq" / "scripts" / "connect.py"
    spec = spec_from_file_location("sixnine_public_agent_connect", source)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
