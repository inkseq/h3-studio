"""Print a machine's capacity timeline and where it got stuck (read-only).

    python tools/capacity_timeline.py <intent_id|demand>
    python tools/capacity_timeline.py --recent 10

The logic lives in studio_platform.capacity_timeline_cli, so the same command
runs inside the app or controller container without this checkout:
``python -m studio_platform.capacity_timeline_cli ...``.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_platform.capacity_timeline_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
