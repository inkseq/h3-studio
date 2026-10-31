"""Explicit Modal SDK process-host boundary; no imports authenticate or rent.

Use Sandboxes rather than Functions: the existing native host keeps its local
receipt journal and does not automatically rerun a generation after a crash.
The database admission boundary, not a new Modal queue, enforces two hosts.
"""

import re

from .modal_host import HostSpec


class ModalSandboxClient:
    def __init__(self, sdk, client, spec: HostSpec):
        if sdk.__version__ != '1.6.3':
            raise ValueError('modal_sdk_version_unqualified')
        self.sdk, self.client, self.spec = sdk, client, spec

    def _app(self):
        app = self.sdk.App.lookup(
            self.spec.app_name, create_if_missing=False, client=self.client
        )
        if app.app_id != self.spec.app_id:
            raise ValueError('modal_app_binding_changed')
        return app

    def create(self, request):
        from dataclasses import asdict

        if (
            set(request) != set(asdict(self.spec)) | {'name', 'timeout'}
            or any(request[k] != v for k, v in asdict(self.spec).items())
            or not re.fullmatch(r'inkseq-[0-9a-f-]{36}', request['name'])
            or type(request['timeout']) is not int
            or not 0 < request['timeout'] <= 7200
        ):
            raise ValueError('modal_launch_request_invalid')
        # The qualified image starts the one-slot native host. Never inject
        # database, object-store, Modal or cloud credentials into a GPU host.
        app = self._app()
        image = self.sdk.Image.from_id(self.spec.image_id, client=self.client)
        volume = self.sdk.Volume.from_id(self.spec.volume_id, client=self.client)
        sandbox = self.sdk.Sandbox.create(
            app=app,
            name=request['name'],
            image=image,
            volumes={'/ckpts': volume},
            gpu=self.spec.gpu,
            cpu=(2, self.spec.cpu_limit),
            memory=(self.spec.memory_mib, self.spec.memory_mib),
            timeout=request['timeout'],
            # Provider idle time ignores entrypoint background inference.
            # The qualified native entrypoint owns receipt-aware 60s idle exit.
            idle_timeout=None,
            client=self.client,
            verbose=False,
        )
        return sandbox.object_id

    def lookup(self, name, identity=None):
        if identity is None:
            # Resolve by the SAME original name including terminated hosts;
            # neither an empty listing nor a NotFound grants another create.
            self._app()
            sandbox = self.sdk.Sandbox.from_name(
                self.spec.app_name,
                name,
                include_terminated=True,
                client=self.client,
            )
        else:
            sandbox = self.sdk.Sandbox.from_id(identity, client=self.client)
        return {'id': sandbox.object_id, 'exit_code': sandbox.poll()}

    def terminate(self, identity):
        sandbox = self.sdk.Sandbox.from_id(identity, client=self.client)
        sandbox.terminate()
