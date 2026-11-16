"""Where a fatal error was raised, without its message.

Exception text can carry tenant, job or provider details, so service logs only
name the type. The innermost frames (file basename, line, function) are safe to
log and say which step failed, e.g. which controller sub-step raised Conflict.
"""

from __future__ import annotations

import os
import traceback


def frames(error: BaseException, limit: int = 6) -> str:
    try:
        stack = traceback.extract_tb(error.__traceback__)[-limit:]
    except Exception:
        return "unknown"
    return (
        " < ".join(
            f"{os.path.basename(item.filename)}:{item.lineno}:{item.name}"
            for item in reversed(stack)
        )
        or "unknown"
    )
