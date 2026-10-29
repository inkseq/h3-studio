"""Explicit staging native-host hooks; ordinary WanGP launchers are unchanged.

Sandbox SDK idle time ignores its entrypoint's asynchronous GPU work. Use the
original host's receipt-aware idle proof instead. Provider timeout remains the
finite crash fallback; business idle shuts down the entrypoint after 60 seconds.
"""

import copy
import os
import time

V7 = 'h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7'
V8 = 'h3-pruned-rank8-int8-sage2-cu130-p4-29gib-taomate5-v8'
PROFILES = (V7, V8)
# Inside a Modal container the image names the one profile it was built for;
# images built before this variable existed are the v7 runtime.
PROFILE = os.environ.get('INKSEQ_MODAL_PROFILE', V7)
if PROFILE not in PROFILES:
    raise ValueError('modal_profile_unknown')


def selected_profile(selection):
    """The profile an admission receipt bound; receipts before v8 carry none (v7)."""
    profile = selection.get('profile', V7)
    if profile not in PROFILES:
        raise ValueError('modal_profile_unknown')
    return profile


def device_profile(profile):
    """Only explicit Modal launch may qualify these unchanged recipes on L40S."""
    if profile['id'] not in PROFILES or profile.get('hardware_admission') is not None:
        raise ValueError('modal_v7_profile_required')
    result = copy.deepcopy(profile)
    result['hardware_admission'] = {
        'gpu_models': ['L40S'],
        'compute_capability': [8, 9],
        'minimum_total_vram_bytes': 44 * 1024**3,
        'maximum_total_vram_bytes': 49 * 1024**3,
    }
    return result


class IdleWindow:
    """Caller holds the native host lock across proof and drain transition."""

    def __init__(self, host, *, clock=time.monotonic):
        self.host, self.clock = host, clock
        self.last_busy = clock()
        self.requests = 0

    def expired(self):
        now = self.clock()
        try:
            idle = (
                self.requests == 0
                and self.host.readiness().idle is True
                and self.host.journal.has_pending_delivery() is False
            )
        except Exception:
            idle = False
        if not idle:
            self.last_busy = now
        return idle and now - self.last_busy >= 60


def host_factory(**kwargs):
    from .runtime_hosts.wangp import WanGPHost
    from .inference.protocol import NotReady

    class ModalHost(WanGPHost):
        def __init__(self, **options):
            super().__init__(**options)
            self.draining = False
            self.idle_window = IdleWindow(self)

        def _open(self):
            super()._open()
            if self.draining:
                raise NotReady('modal_host_draining')

        def drain_if_idle(self):
            # This is the SAME RLock used by submit; no request may enter
            # between the no-obligations proof and closing admission.
            with self._lock:
                if not self.draining and self.idle_window.expired():
                    self.draining = True
                return self.draining

    return ModalHost(**kwargs)


def serve(app, host, port):
    import asyncio
    import uvicorn

    async def tracked(scope, receive, send):
        # Readiness probes cannot keep an idle paid machine alive. Every other
        # in-flight HTTP request (including input/upload streaming) pauses idle.
        active = scope['type'] == 'http' and scope.get('path') != '/v1/readiness'
        if active:
            with host._lock:
                if host.draining:
                    refused = True
                else:
                    refused = False
                    host.idle_window.requests += 1
            if refused:
                await send(
                    {'type': 'http.response.start', 'status': 503, 'headers': []}
                )
                await send({'type': 'http.response.body', 'body': b''})
                return
        try:
            await app(scope, receive, send)
        finally:
            if active:
                with host._lock:
                    host.idle_window.requests -= 1
                    host.idle_window.last_busy = host.idle_window.clock()

    async def run():
        host.idle_window.last_busy = host.idle_window.clock()
        server = uvicorn.Server(
            uvicorn.Config(
                tracked,
                host='127.0.0.1',
                port=port,
                workers=1,
                access_log=False,
                log_level='critical',
                proxy_headers=False,
            )
        )

        async def idle_stop():
            while not server.should_exit:
                await asyncio.sleep(1)
                if host.drain_if_idle():
                    server.should_exit = True

        watcher = asyncio.create_task(idle_stop())
        try:
            await server.serve()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(run())
