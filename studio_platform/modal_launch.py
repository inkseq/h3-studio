"""Private initialization of an already journaled Modal host, never a rental."""

import json

from .modal_entry import validate_launch


# The qualified image has no `python` on PATH; its CMD uses this venv (build_modal_sources.py).
PYTHON = '/venv/main/bin/python'

# The controller installs right after create, while the image CMD may still be
# importing; wait (inside the sandbox) for the entrypoint's private directory.
INSTALL = '''
import json,sys,time
from studio_platform.modal_entry import install_launch,STATE
try:
 raw=sys.stdin.buffer.read(4097)
 if len(raw)>4096: raise ValueError()
 deadline=time.monotonic()+40
 while not STATE.is_dir() and time.monotonic()<deadline: time.sleep(0.25)
 print(install_launch(json.loads(raw)),flush=True)
except Exception:
 sys.exit(1)
'''

OBSERVE = '''
import json,sys
from studio_platform.modal_entry import launch_document,STATE
try:
 raw=sys.stdin.buffer.read(4097)
 if len(raw)>4096: raise ValueError()
 print('observed' if launch_document(STATE/'launch'/'value.json')==json.loads(raw) else 'unconfirmed',flush=True)
except Exception:
 print('unconfirmed',flush=True)
'''


def observe(sandbox, value, *, stderr_sink):
    """Read the original file after an unknown install; never install or restart."""
    validate_launch(value)
    try:
        process = sandbox.exec(
            PYTHON, '-c', OBSERVE, text=False, timeout=15, stderr=stderr_sink
        )
        process.stdin.write(json.dumps(value, separators=(',', ':')).encode())
        process.stdin.write_eof()
        process.stdin.drain()
        result = process.stdout.read()
        return process.wait() == 0 and result == b'observed\n'
    except Exception:
        return False


def initialize(sandbox, value, *, stderr_sink):
    """No retry: a missing response requires original-host read-back."""
    validate_launch(value)
    raw = json.dumps(value, separators=(',', ':')).encode()
    if len(raw) > 4096:
        raise ValueError('modal_launch_document_too_large')
    try:
        process = sandbox.exec(
            PYTHON, '-c', INSTALL, text=False, timeout=50, stderr=stderr_sink
        )
        process.stdin.write(raw)
        process.stdin.write_eof()
        process.stdin.drain()
        result = process.stdout.read()
        if process.wait() != 0 or result not in {b'installed\n', b'observed\n'}:
            raise ValueError()
        return result.decode().strip()
    except Exception:
        raise ValueError('modal_launch_result_unknown') from None
