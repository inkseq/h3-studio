"""Launch fixed media tools with Linux address-space limits, without preexec_fn.

The fresh interpreter sets its own limits before exec replaces it with the tool
in the same PID. This bounds one process, not the whole container or descendant
tree. Windows development can run normally, but has no OS memory limit here.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


DEFAULT_ADDRESS_SPACE_BYTES = 9 * 1024**3 // 4  # 2.25 GiB, not an RSS promise.
MAX_CAPTURE_BYTES = 1024 * 1024
_POLICY_EXIT = 78
_TOOLS = frozenset({"ffmpeg", "ffprobe"})


class MediaProcessError(subprocess.SubprocessError):
    """Bounded diagnostics only; never retain a command, path or decoder output."""

    _MESSAGES = {
        "invalid": "媒体处理启动参数无效；原件保持不变",
        "unavailable": "媒体处理工具不可用；原件保持不变",
        "policy": "媒体处理资源限制或启动失败；原件保持不变",
        "timeout": "媒体处理超时，处理进程已结束；原件保持不变",
        "failed": "媒体处理失败或达到资源限制；原件保持不变",
        "output": "媒体检查结果超过安全上限；原件保持不变",
    }

    def __init__(self, code, *, returncode=None):
        self.code = code if code in self._MESSAGES else "failed"
        self.returncode = returncode if type(returncode) is int else None
        super().__init__(self._MESSAGES[self.code])


def _validated_argv(argv):
    if (
        not isinstance(argv, (list, tuple))
        or not argv
        or not isinstance(argv[0], str)
        or argv[0] not in _TOOLS
    ):
        raise MediaProcessError("invalid")
    if any(
        not isinstance(value, (str, os.PathLike))
        or not isinstance(os.fspath(value), str)
        or "\x00" in os.fspath(value)
        for value in argv
    ):
        raise MediaProcessError("invalid")
    try:
        resolved = shutil.which(argv[0])
        executable = str(Path(resolved).resolve()) if resolved else None
        available = executable and Path(executable).is_file()
    except (OSError, ValueError):
        raise MediaProcessError("unavailable") from None
    if not available:
        raise MediaProcessError("unavailable")
    return argv[0], executable, [os.fspath(value) for value in argv[1:]]


def run_media_process(
    argv,
    *,
    timeout,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=True,
    memory_bytes=DEFAULT_ADDRESS_SPACE_BYTES,
    require_linux_limits=False,
):
    """Return a sanitized CompletedProcess for ffmpeg or ffprobe only.

    Linux limits are mandatory: a launcher policy failure never falls back to
    direct execution. `require_linux_limits=True` also rejects non-Linux hosts.
    PIPE output is spooled and bounded before loading it into the API process.
    Decoder stderr is always discarded. No shell and no preexec_fn are used.
    """
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
        or type(memory_bytes) is not int
        or memory_bytes < 16 * 1024**2
        or memory_bytes > DEFAULT_ADDRESS_SPACE_BYTES
        or stdout not in (subprocess.DEVNULL, subprocess.PIPE)
        or stderr != subprocess.DEVNULL
        or type(check) is not bool
        or type(require_linux_limits) is not bool
    ):
        raise MediaProcessError("invalid")
    tool, executable, arguments = _validated_argv(argv)
    linux = sys.platform.startswith("linux")
    if require_linux_limits and not linux:
        raise MediaProcessError("policy")
    command = (
        [
            sys.executable,
            "-I",
            "-S",
            str(Path(__file__).resolve()),
            "--limited-exec",
            str(memory_bytes),
            tool,
            executable,
            *arguments,
        ]
        if linux
        else [executable, *arguments]
    )
    try:
        captured = tempfile.TemporaryFile() if stdout == subprocess.PIPE else None
    except OSError:
        raise MediaProcessError("unavailable") from None
    try:
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=captured if captured is not None else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                shell=False,
                close_fds=True,
            )
        except subprocess.TimeoutExpired:
            # run() kills and waits for the child. Linux exec keeps the PID, so
            # there is no surviving launcher grandchild holding the decoder.
            raise MediaProcessError("timeout") from None
        except (OSError, subprocess.SubprocessError):
            raise MediaProcessError("unavailable") from None
        if linux and result.returncode == _POLICY_EXIT:
            raise MediaProcessError("policy", returncode=result.returncode)
        if check and result.returncode:
            raise MediaProcessError("failed", returncode=result.returncode)
        output = None
        if captured is not None:
            try:
                captured.seek(0)
                output = captured.read(MAX_CAPTURE_BYTES + 1)
            except OSError:
                raise MediaProcessError("output") from None
            if len(output) > MAX_CAPTURE_BYTES:
                raise MediaProcessError("output")
        # Do not return launcher's private paths / args through repr() either.
        return subprocess.CompletedProcess([tool], result.returncode, output, None)
    finally:
        if captured is not None:
            captured.close()


def _limited_exec(arguments):
    """Internal child entry, invoked only in a fresh Python interpreter."""
    try:
        if (
            not sys.platform.startswith("linux")
            or len(arguments) < 4
            or arguments[0] != "--limited-exec"
        ):
            return _POLICY_EXIT
        amount = int(arguments[1])
        tool, executable = arguments[2:4]
        if (
            not 16 * 1024**2 <= amount <= DEFAULT_ADDRESS_SPACE_BYTES
            or tool not in _TOOLS
            or not Path(executable).is_absolute()
            or not Path(executable).is_file()
        ):
            return _POLICY_EXIT
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (amount, amount))
        # Both limits survive exec. The PID remains the subprocess.run child.
        os.execv(executable, [executable, *arguments[4:]])
    except (OSError, ValueError, OverflowError, ImportError):
        return _POLICY_EXIT
    return _POLICY_EXIT


if __name__ == "__main__":
    raise SystemExit(_limited_exec(sys.argv[1:]))
