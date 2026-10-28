"""Private initialization of an already journaled Modal host, never a rental."""

import json

from .modal_entry import validate_launch


INSTALL = '''
import json,sys
from studio_platform.modal_entry import install_launch
try:
 raw=sys.stdin.buffer.read(4097)
 if len(raw)>4096: raise ValueError()
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
            'python', '-c', OBSERVE, text=False, timeout=15, stderr=stderr_sink
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
            'python', '-c', INSTALL, text=False, timeout=15, stderr=stderr_sink
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
