"""A mounted, pre-populated model cache for dial-out workers (#924).

Serverless workers (RunPod network volume, Modal volume) set INKSEQ_MODEL_CACHE
to a mount that already holds the manifest's files. When every file is present
at its exact manifest size, the bootstrap uses that directory as the model root
and skips the download; the launcher's verify_runtime still hashes every file
before any load, so a wrong byte fails closed. Anything incomplete, linked or
outside the root falls back to the normal download. dstack hosts never set the
variable, so their path is unchanged.
"""

import os
from pathlib import Path, PurePosixPath

VARIABLE = "INKSEQ_MODEL_CACHE"


def cached_root(document, environ=os.environ):
    """The cache directory when it holds the whole manifest, else None."""
    value = environ.get(VARIABLE)
    if not value:
        return None
    root = Path(value)
    try:
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            return None
        for component in document["components"].values():
            for record in component["files"]:
                relative = PurePosixPath(record["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    return None
                path = root.joinpath(*relative.parts)
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size != record["size_bytes"]
                ):
                    return None
    except (OSError, KeyError, TypeError, AttributeError):
        return None
    return root
