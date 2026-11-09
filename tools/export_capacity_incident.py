"""Export one machine's recorded incident as a controller-simulator fixture.

    python tools/export_capacity_incident.py <intent_id> [--since T] [--until T] \\
        > tests/fixtures/capacity_sim/incident_<name>.json

Reads the database read-only. Inside the app or controller container use
``python -m studio_platform.capacity_incident_export ...``. See
docs/CAPACITY-SIMULATOR.md for the incident -> scenario -> fix workflow.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_platform.capacity_incident_export import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
