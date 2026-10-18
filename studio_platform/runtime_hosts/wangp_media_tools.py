"""Check the pinned WanGP decoder's selected tools without installing anything.

The caller supplies the exact child environment and working directory. Call
after any upstream FFmpeg setup, before model work/readiness; this check does
not prevent a later caller from changing that environment or those files.
"""

import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

MIN_VERSION = (5, 1)
COMMAND_TIMEOUT = 5
MAX_OUTPUT_BYTES = 2 * 1024**2
MAX_BINARY_BYTES = 512 * 1024**2
_ENV_KEYS = {"ffmpeg": "FFMPEG_BINARY", "ffprobe": "FFPROBE_BINARY"}


def _absolute(path, cwd):
    value = Path(path)
    return str(value if value.is_absolute() else cwd / value)


def resolve_media_binary(binary_name, runtime_root, environment, *, cwd):
    """Mirror shared.utils.video_decode at upstream 0e58385, not its downloader.

    Returned paths are private process inputs, never public diagnostics. PATH
    entries are made absolute against the child's cwd so lookup cannot use the
    bootstrap process's different working directory.
    """
    if binary_name not in _ENV_KEYS:
        raise ValueError("wangp_media_tools_invalid")
    cwd = Path(cwd).absolute()
    root = Path(_absolute(runtime_root, cwd))
    override = environment.get(_ENV_KEYS[binary_name], "")
    if override and os.path.isfile(_absolute(override, cwd)):
        return _absolute(override, cwd)
    name = binary_name + (".exe" if os.name == "nt" else "")
    candidate = root / "ffmpeg_bins" / name
    if candidate.is_file():
        return str(candidate)
    # An absent PATH uses Python's OS default, while PATH='' stays empty.
    path = environment.get("PATH")
    if path is None:
        try:
            path = os.confstr("CS_PATH")
        except (AttributeError, ValueError):
            path = os.defpath
    if path:
        path = os.pathsep.join(_absolute(item, cwd) for item in path.split(os.pathsep))
    selected = shutil.which(name, path=path) or shutil.which(binary_name, path=path)
    return _absolute(selected, cwd) if selected else None


def _output(executable, arguments, environment, cwd):
    # Help output is spooled, then bounded before loading into memory. Tool
    # stderr and exception text never enter readiness/status diagnostics.
    try:
        with tempfile.TemporaryFile() as capture:
            result = subprocess.run(
                [executable, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=capture,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                env=environment,
                shell=False,
                close_fds=True,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
            if result.returncode:
                raise ValueError("wangp_media_tools_probe_failed")
            capture.seek(0)
            output = capture.read(MAX_OUTPUT_BYTES + 1)
            if len(output) > MAX_OUTPUT_BYTES:
                raise ValueError("wangp_media_tools_output_invalid")
            return output.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for its child on timeout.
        raise ValueError("wangp_media_tools_probe_timeout") from None
    except (OSError, subprocess.SubprocessError):
        raise ValueError("wangp_media_tools_probe_failed") from None


def _version(binary_name, output):
    first = output.splitlines()[0] if output else ""
    match = re.match(
        r"^"
        + binary_name
        + r" version n?(\d{1,3})\.(\d{1,3})(?:\.(\d{1,3}))?(?=[\s.+-]|$)",
        first,
    )
    if not match:
        raise ValueError("wangp_media_tools_version_unconfirmed")
    parts = tuple(int(item) for item in match.groups() if item is not None)
    if parts[:2] < MIN_VERSION:
        raise ValueError("wangp_media_tools_version_unsupported")
    return ".".join(str(item) for item in parts)


def _signature(path):
    value = os.stat(path)
    if not stat.S_ISREG(value.st_mode) or not 0 < value.st_size <= MAX_BINARY_BYTES:
        raise ValueError("wangp_media_tools_binary_invalid")
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def verify_media_tools(runtime_root, environment, *, cwd):
    """Return closed evidence for the actual selected files, with no mutation.

    No upstream/model imports, shell, network, downloader, or inherited parent
    environment are used by the tool commands. Version support is required for
    BOTH binaries; the decoder's required fps_mode option is checked separately.
    """
    if not isinstance(environment, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not key
        or "=" in key
        or "\0" in key
        or "\0" in value
        for key, value in environment.items()
    ):
        raise ValueError("wangp_media_tools_invalid")
    environment = dict(environment)
    try:
        child_cwd = str(Path(cwd).resolve(strict=True))
        if not Path(child_cwd).is_dir():
            raise ValueError("wangp_media_tools_invalid")
        evidence = {}
        for name in _ENV_KEYS:
            selected = resolve_media_binary(
                name, runtime_root, environment, cwd=child_cwd
            )
            if selected is None:
                raise ValueError("wangp_media_tools_missing")
            before = _signature(selected)
            version = _version(
                name, _output(selected, ["-version"], environment, child_cwd)
            )
            if name == "ffmpeg":
                help_text = _output(
                    selected, ["-hide_banner", "-h", "full"], environment, child_cwd
                )
                if not re.search(r"(?m)^\s*-fps_mode(?:\s|$)", help_text):
                    raise ValueError("wangp_media_tools_fps_mode_unsupported")
            digest = hashlib.sha256()
            with open(selected, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024**2), b""):
                    digest.update(chunk)
            if _signature(selected) != before:
                raise ValueError("wangp_media_tools_binary_changed")
            evidence[name] = {
                "binary": name,
                "version": version,
                "sha256": digest.hexdigest(),
            }
        evidence["ffmpeg"]["fps_mode"] = "supported"
        return evidence
    except (OSError, TypeError):
        raise ValueError("wangp_media_tools_probe_failed") from None
