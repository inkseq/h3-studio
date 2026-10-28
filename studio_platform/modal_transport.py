"""Carry the existing WanGP HTTP protocol through authenticated Modal exec.

No public ports, new queue, provider credentials on the GPU, or HTTP retries.
Tokens, input bytes and presigned delivery targets travel on stdin, never argv.
The CPU WorkerRunner still owns the original claim, fence and settlement.
"""

import json
import math
import time

import httpx

MAX_BODY = 512 * 1024 * 1024
MAX_HEADER = 16384
CHUNK = 64 * 1024

# Static trusted bridge. The child uses only loopback and has no proxy support.
# Stderr is discarded by the explicit SDK caller. Never print raw exceptions.
BRIDGE = r'''
import http.client,json,sys
source=sys.stdin.buffer
target=sys.stdout.buffer
try:
 line=source.readline(16385)
 if len(line)>16384 or not line.endswith(b'\n'): raise ValueError()
 data=json.loads(line)
 if data['method'] not in ('GET','POST','PUT'): raise ValueError()
 path=data['path']
 if not path.startswith('/v1/') or '\r' in path or '\n' in path: raise ValueError()
 size=data['length']
 if type(size) is not int or not 0<=size<=536870912: raise ValueError()
 conn=http.client.HTTPConnection('127.0.0.1',8199,timeout=data['timeout'])
 conn.putrequest(data['method'],path)
 for key,value in data['headers'].items(): conn.putheader(key,value)
 conn.putheader('Content-Length',str(size))
 conn.endheaders()
 while size:
  part=source.read(min(65536,size))
  if not part: raise ValueError()
  conn.send(part);size-=len(part)
 response=conn.getresponse()
 header=json.dumps({'status':response.status}).encode()
 target.write(len(header).to_bytes(4,'big')+header);target.flush()
 total=0
 while True:
  part=response.read(65536)
  if not part: break
  total+=len(part)
  if total>536870912: raise ValueError()
  target.write(part);target.flush()
 conn.close()
except Exception:
 sys.exit(1)
'''


class _Output(httpx.SyncByteStream):
    def __init__(self, source, deadline):
        self.source, self.deadline = iter(source), deadline
        self.buffer = bytearray()
        self.total = 0

    def _next(self):
        if time.monotonic() > self.deadline:
            raise httpx.TransportError('modal_private_transfer_timeout')
        chunk = next(self.source)
        if not isinstance(chunk, bytes):
            raise httpx.TransportError('modal_private_stream_invalid')
        self.total += len(chunk)
        if self.total > MAX_BODY + MAX_HEADER + 4:
            raise httpx.TransportError('modal_private_response_limit')
        return chunk

    def take(self, count):
        while len(self.buffer) < count:
            self.buffer.extend(self._next())
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def __iter__(self):
        if self.buffer:
            yield bytes(self.buffer)
            self.buffer.clear()
        while True:
            try:
                yield self._next()
            except StopIteration:
                return
            except Exception:
                raise httpx.TransportError('modal_private_response_unknown') from None


class ModalHTTPTransport(httpx.BaseTransport):
    def __init__(self, sandbox, *, stderr_sink):
        # sandbox is the already journaled original ID, never looked up/created
        # by a generation request. stderr_sink is modal.stream_type.DEVNULL.
        self.sandbox, self.stderr_sink = sandbox, stderr_sink

    def handle_request(self, request):
        try:
            url = request.url
            if (
                url.scheme != 'http'
                or url.host != '127.0.0.1'
                or url.port != 8199
                or url.userinfo
                or not url.raw_path.startswith(b'/v1/')
                or request.method not in {'GET', 'POST', 'PUT'}
            ):
                raise ValueError()
            length = int(request.headers.get('content-length', '0'))
            if not 0 <= length <= MAX_BODY:
                raise ValueError()
            timeout = request.extensions.get('timeout', {}).get('read', 30)
            if type(timeout) not in (float, int) or not math.isfinite(timeout):
                raise ValueError()
            timeout = max(1, min(900, timeout))
            header = {
                'method': request.method,
                'path': url.raw_path.decode('ascii'),
                'length': length,
                'timeout': timeout,
                'headers': {
                    k: v
                    for k, v in request.headers.items()
                    if k
                    in {
                        'authorization',
                        'content-type',
                        'x-wangp-input',
                        'x-wangp-incarnation',
                    }
                },
            }
            raw = json.dumps(header, separators=(',', ':')).encode() + b'\n'
            if len(raw) > MAX_HEADER:
                raise ValueError()
            deadline = time.monotonic() + timeout
            process = self.sandbox.exec(
                'python',
                '-c',
                BRIDGE,
                text=False,
                timeout=math.ceil(timeout),
                stderr=self.stderr_sink,
            )
            process.stdin.write(raw)
            process.stdin.drain()
            sent = 0
            for chunk in request.stream:
                for offset in range(0, len(chunk), CHUNK):
                    part = chunk[offset : offset + CHUNK]
                    sent += len(part)
                    if sent > length or time.monotonic() > deadline:
                        raise ValueError()
                    process.stdin.write(part)
                    process.stdin.drain()
            if sent != length:
                raise ValueError()
            process.stdin.write_eof()
            process.stdin.drain()
            output = _Output(process.stdout, deadline)
            size = int.from_bytes(output.take(4), 'big')
            if not 0 < size <= MAX_HEADER:
                raise ValueError()
            status = json.loads(output.take(size))['status']
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError()
            return httpx.Response(status, stream=output)
        except Exception:
            # A failure after exec may follow a successful native POST. The
            # existing HTTP adapter turns submission failures into uncertainty.
            raise httpx.TransportError('modal_private_request_unknown') from None
