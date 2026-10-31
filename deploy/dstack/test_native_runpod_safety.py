"""RunPod boundary tests use synthetic responses only; no provider clients."""

import argparse
import ast
import asyncio
import copy
from enum import Enum
import hashlib
import importlib.util
import json
from pathlib import Path
from types import MethodType, SimpleNamespace as NS
import sys
import unittest
from unittest.mock import Mock

HERE = Path(__file__).parent
SOURCE_CACHE = None
INSTALLED_ROOT = None


def load(name, path=None):
    spec = importlib.util.spec_from_file_location(name, path or HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


safety, installer = load("native_vast_safety"), load("patch_native_vast")


def response(status, value):
    return NS(status_code=status, json=Mock(return_value=value), close=Mock())


def sources():
    provenance = json.loads((HERE / "native-vast-provenance.json").read_text())
    selected = {}
    for path in (
        "dstack/_internal/core/backends/runpod/api_client.py",
        "dstack/_internal/core/backends/runpod/compute.py",
        "dstack/_internal/server/background/pipeline_tasks/jobs_submitted.py",
    ):
        if INSTALLED_ROOT:
            selected[path] = (INSTALLED_ROOT / path).read_text()
        elif SOURCE_CACHE:
            raw = (SOURCE_CACHE / path.replace("/", "__")).read_bytes()
            assert (
                hashlib.sha256(raw).hexdigest() == provenance["sources"][path]["sha256"]
            )
            selected[path] = installer.transform(path, raw.decode())
        else:
            raise unittest.SkipTest(
                "exact source checked separately in the native image job"
            )
    return selected


def native_loop():
    """Execute the shipped methods and real allocator catch/return branches."""
    import __future__

    selected = sources()

    class BackendError(Exception):
        pass

    class SkipOffer(Exception):
        pass

    class BackendType(Enum):
        RUNPOD = "runpod"

    class Terminated:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    scope = {
        **vars(safety),
        "BackendError": BackendError,
        "ComputeError": BackendError,
        "SkipOffer": SkipOffer,
        "logger": NS(debug=Mock(), info=Mock(), warning=Mock(), exception=Mock()),
        "fmt": lambda x: "synthetic",
        "settings": NS(MAX_OFFERS_TRIED=25),
        "_OfferAttemptError": NS,
        "_NewCapacityAttempts": NS,
        "_FailedNewCapacityProvisioning": NS,
        "_TerminateSubmittedJobResult": Terminated,
        "JobTerminationReason": NS(TERMINATED_BY_SERVER="server"),
        "_ProvisionNewCapacityResult": NS,
        "_build_placement_group_cleanup": lambda **kw: None,
        "placement_group_model_to_placement_group_optional": lambda x: None,
        "_get_offer_volumes": lambda volumes, offer: volumes,
        "JobConfiguration": NS,
        "JobProvisioningData": NS,
        "filter_non_placeholder_instances": lambda x: [],
        "is_cloud_cluster": lambda x: False,
        "BACKENDS_WITH_GROUP_PROVISIONING_SUPPORT": set(),
        "InstanceConfiguration": NS,
        "get_job_instance_name": lambda *args: "original",
        "generate_unique_instance_name": lambda *args, **kw: "owned-original-pod",
        "MAX_RESOURCE_NAME_LEN": 63,
        "DSTACK_RUNNER_SSH_PORT": 10022,
        "build_authorized_keys": lambda *args: [],
        "get_docker_commands": lambda *args: ["true"],
        "_is_secure_cloud": lambda region: region != "COMMUNITY",
        "_get_cpu_type_id": lambda x: "cpu",
        "_get_docker_args": lambda keys: "true",
        "RunpodProvider": NS(MIN_CUDA_VERSION="12.8"),
        "_build_v2_mounts": lambda *args: None,
        "API_V2_URL": "https://api.runpod.io/v2",
        "API_GRAPHQL_URL": "https://api.runpod.io/graphql",
        "REQUEST_TIMEOUT": 120,
        "_generate_pod_deployment_mutation": lambda **kwargs: (
            "mutation synthetic public image"
        ),
        "master_job_provisioning_data": None,
        "_MAX_REPORTED_OFFER_ERRORS": 3,
        "_PROVISIONING_TROUBLESHOOTING_URL": "https://dstack.ai/docs/guides/troubleshooting/#provisioning-fails",
    }
    functions = []
    for path, names in (
        (list(selected)[0], {"create_pod", "create_gpu_pod_graphql"}),
        (list(selected)[1], {"run_job", "run_jobs"}),
        (
            list(selected)[2],
            {
                "_get_offer_attempt_error",
                "_get_new_capacity_failure_message",
                "_format_offer_errors",
                "_format_reported_reasons",
            },
        ),
    ):
        functions.extend(
            copy.deepcopy(n)
            for n in ast.walk(ast.parse(selected[path]))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name in names
        )
    parent = next(
        n
        for n in ast.walk(ast.parse(selected[list(selected)[2]]))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_provision_new_capacity"
    )
    index = (
        next(
            i
            for i, n in enumerate(parent.body)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "offers_iter" for t in n.targets
            )
        )
        - 1
    )
    tail = ast.parse("async def actual_loop(offers):\n pass").body[0]
    tail.body = ast.parse("placement_group_model = None").body + copy.deepcopy(
        parent.body[index:]
    )
    functions.append(tail)
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
            "exact_native_runpod_boundary",
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        scope,
    )

    async def run_async(method, *args):
        return method(*args)

    scope["run_async"] = run_async

    def run(
        session,
        regions=("SECURE", "SECURE"),
        *,
        spots=None,
        registry=None,
        volumes=None,
        provisioning_failure=False,
    ):
        api = NS(
            _session=session,
            _make_v2_request=Mock(side_effect=AssertionError("unreviewed request")),
            _make_graphql_request=Mock(
                side_effect=AssertionError("unreviewed request")
            ),
        )
        api.create_pod = MethodType(scope["create_pod"], api)
        api.create_gpu_pod_graphql = MethodType(scope["create_gpu_pod_graphql"], api)
        compute = NS(
            api_client=api,
            _last_cleanup_time=None,
            _generate_container_registry_credentials_id=Mock(
                side_effect=AssertionError("registry write")
            ),
            _clean_stale_container_registry_credentials=Mock(
                side_effect=AssertionError("registry sweep")
            ),
        )
        compute.run_job = MethodType(scope["run_job"], compute)
        offers = []
        for i, region in enumerate(regions):
            resources = NS(
                spot=bool(spots and spots[i]),
                gpus=[NS()],
                cpus=4,
                memory_mib=16384,
                disk=NS(size_mib=32768),
            )
            offers.append(
                (
                    NS(compute=lambda: compute),
                    NS(
                        backend=BackendType.RUNPOD,
                        region=region,
                        price=0.5,
                        instance=NS(name="gpu", resources=resources),
                    ),
                )
            )
        job = NS(
            job_spec=NS(image_name="public/synthetic:fixed", registry_auth=registry)
        )
        scope.update(
            job=job,
            jobs=[job],
            run=NS(
                project_name="staging",
                user=NS(),
                run_spec=NS(configuration=NS(volumes=[NS(path="/data")])),
            ),
            profile=NS(instance_types=None),
            job_model=NS(),
            fleet_model=NS(instances=[], name="original"),
            project_ssh_public_key="synthetic",
            project_ssh_private_key="synthetic",
            volumes=volumes or [],
            requirements=NS(),
            extra_authorized_keys=[],
            new_placement_group_models=[],
        )
        scope["JobProvisioningData"] = (
            Mock(side_effect=ValueError("SECRET after allocation"))
            if provisioning_failure
            else NS
        )
        result = asyncio.run(scope["actual_loop"](offers))
        compute._generate_container_registry_credentials_id.assert_not_called()
        compute._clean_stale_container_registry_credentials.assert_not_called()
        return result, api, scope

    return run, Terminated


class RunPodSafetyTests(unittest.TestCase):
    def test_create_unknown_and_remote_rejection_never_retry_or_expose_body(self):
        cases = [
            TimeoutError("SECRET"),
            response(500, {"detail": "SECRET"}),
            response(400, {"title": "SECRET", "detail": "no capacity"}),
            *[
                response(status, {"secret": "SECRET"})
                for status in (401, 403, 429, 502)
            ],
            response(200, {}),
            *[
                response(200, {"id": identity})
                for identity in (None, True, 1, " ", "bad/id")
            ],
            response(200, {"id": ""}),
            *[response(status, {"id": "original"}) for status in (302, 307, 308)],
            response(200, []),
            response(200, "SECRET"),
        ]
        malformed = response(200, {})
        malformed.json.side_effect = ValueError("SECRET")
        cases.append(malformed)
        for value in cases:
            with self.subTest(kind=type(value).__name__):
                session = NS(
                    request=Mock(side_effect=[value, response(200, {"id": "second"})])
                )
                with self.assertRaises(safety.RunPodAllocationUnknown) as raised:
                    safety.runpod_create_once(
                        session, "https://api.runpod.io/v2/pods", {}, timeout=120
                    )
                self.assertEqual(session.request.call_count, 1)
                self.assertFalse(safety.runpod_safe_for_fallback(raised.exception))
                self.assertNotIn("SECRET", safety.runpod_closed_error(raised.exception))
                self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    def test_graphql_partial_malformed_or_missing_id_is_unknown(self):
        for doc in (
            {
                "data": {"podFindAndDeployOnDemand": {"id": "original"}},
                "errors": [{"message": "SECRET"}],
            },
            {"data": {"podFindAndDeployOnDemand": None}},
            {"data": {}},
            {"data": {"podRentInterruptable": {"id": "original"}}},
        ):
            with self.subTest(doc=doc):
                session = NS(request=Mock(return_value=response(200, doc)))
                with self.assertRaises(safety.RunPodAllocationUnknown) as raised:
                    safety.runpod_create_once(
                        session,
                        "https://api.runpod.io/graphql",
                        {},
                        timeout=120,
                        graphql=True,
                    )
                session.request.assert_called_once()
                if "errors" in doc:
                    self.assertEqual(raised.exception.pod_id, "original")

    def test_only_typed_local_rejection_can_advance(self):
        original = safety.RunPodPreRequestRejected("spot_unsupported")
        self.assertTrue(safety.runpod_safe_for_fallback(original))
        self.assertFalse(safety.runpod_safe_for_fallback(Exception(str(original))))
        self.assertFalse(safety.runpod_safe_for_fallback(NS(code="spot_unsupported")))
        self.assertEqual(
            json.loads(safety.runpod_closed_error(original).split(":", 1)[1])[
                "outcome"
            ],
            "not_sent",
        )

    def test_success_preserves_original_pod_and_never_follows_redirect(self):
        for graphql in (False, True):
            pod = {"id": "original_123", "status": "PROVISIONING"}
            value = {"data": {"podFindAndDeployOnDemand": pod}} if graphql else pod
            response_value = response(200, value)
            response_value.close.side_effect = OSError("SECRET")
            session = NS(request=Mock(return_value=response_value))
            url = (
                "https://api.runpod.io/graphql"
                if graphql
                else "https://api.runpod.io/v2/pods"
            )
            self.assertEqual(
                safety.runpod_create_once(
                    session, url, {}, timeout=120, graphql=graphql
                ),
                pod,
            )
            session.request.assert_called_once()
            response_value.close.assert_called_once()

    def test_actual_secure_and_community_success_have_zero_registry_operations(self):
        run, _ = native_loop()
        for region, value in (
            ("SECURE", {"id": "original_123"}),
            (
                "COMMUNITY",
                {"data": {"podFindAndDeployOnDemand": {"id": "original_123"}}},
            ),
        ):
            session = NS(request=Mock(return_value=response(200, value)))
            result, _, _ = run(session, (region,))
            self.assertEqual(result.provisioning_data.instance_id, "original_123")
            session.request.assert_called_once()

    def test_actual_loop_returns_terminal_after_one_uncertain_create(self):
        run, terminal = native_loop()
        for region, result in (
            ("SECURE", TimeoutError("allocated_then_lost_SECRET")),
            ("SECURE", ValueError("allocated_then_lost_SECRET")),
            ("SECURE", ConnectionError("allocated_then_lost_SECRET")),
            ("SECURE", response(500, {"id": "original_123", "secret": "SECRET"})),
            ("SECURE", response(400, {"error": "no capacity SECRET"})),
            ("SECURE", response(401, {})),
            ("SECURE", response(200, {})),
            (
                "COMMUNITY",
                response(
                    200,
                    {
                        "data": {"podFindAndDeployOnDemand": {"id": "original_123"}},
                        "errors": [{}],
                    },
                ),
            ),
        ):
            with self.subTest(region=region, kind=type(result).__name__):
                session = NS(
                    request=Mock(side_effect=[result, response(200, {"id": "second"})])
                )
                value, _, _ = run(session, (region, "SECURE"))
                self.assertIsInstance(value, terminal)
                self.assertEqual(value.reason, "server")
                self.assertEqual(session.request.call_count, 1)
                self.assertNotIn("SECRET", value.message)
                self.assertEqual(
                    json.loads(value.message.split(":", 1)[1])["outcome"], "unknown"
                )

    def test_original_session_auth_is_preserved_without_retries_or_redirects(self):
        try:
            import requests
        except ModuleNotFoundError:
            if INSTALLED_ROOT:
                raise
            self.skipTest(
                "requests authentication is checked in the exact native image"
            )
        selected = sources()
        tree = ast.parse(selected[next(iter(selected))])
        api_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "RunpodApiClient"
        )
        constructor = next(
            node
            for node in api_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        scope = {"requests": requests}
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[constructor], type_ignores=[])
                ),
                "exact_native_runpod_session",
                "exec",
            ),
            scope,
        )
        client = NS()
        scope["__init__"](client, "synthetic-token-only")
        session = client._session
        self.assertEqual(
            session.get_adapter("https://api.runpod.io").max_retries.total, 0
        )
        session.trust_env = False
        captured = []

        class RecordingAdapter(requests.adapters.BaseAdapter):
            def send(self, request, **kwargs):
                captured.append(request)
                result = requests.Response()
                result.status_code = 200
                result._content = json.dumps(
                    {"data": {"podFindAndDeployOnDemand": {"id": "original"}}}
                    if request.url.endswith("graphql")
                    else {"id": "original"}
                ).encode()
                result._content_consumed = True
                result.request = request
                return result

            def close(self):
                pass

        session.mount("https://", RecordingAdapter())
        try:
            for graphql in (False, True):
                url = (
                    "https://api.runpod.io/graphql"
                    if graphql
                    else "https://api.runpod.io/v2/pods"
                )
                result = safety.runpod_create_once(
                    session, url, {"synthetic": True}, timeout=120, graphql=graphql
                )
                self.assertEqual(result["id"], "original")
                request = captured[-1]
                self.assertEqual((request.method, request.url), ("POST", url))
                self.assertEqual(
                    request.headers["Authorization"], "Bearer synthetic-token-only"
                )
                self.assertEqual(json.loads(request.body), {"synthetic": True})
            self.assertEqual(len(captured), 2)
        finally:
            session.close()

    def test_actual_post_create_validation_failure_is_terminal(self):
        run, terminal = native_loop()
        for region in ("SECURE", "COMMUNITY"):
            pod = {"id": "original_123"}
            doc = (
                {"data": {"podFindAndDeployOnDemand": pod}}
                if region == "COMMUNITY"
                else pod
            )
            session = NS(request=Mock(return_value=response(200, doc)))
            result, _, _ = run(session, (region, "SECURE"), provisioning_failure=True)
            self.assertIsInstance(result, terminal)
            session.request.assert_called_once()
            self.assertNotIn("SECRET", result.message)
            self.assertEqual(
                json.loads(result.message.split(":", 1)[1])["pod_id"], "original_123"
            )

    def test_actual_cluster_rejection_is_before_any_transport(self):
        run, _ = native_loop()
        session = NS(request=Mock(return_value=response(200, {"id": "original"})))
        _, api, scope = run(session, ("SECURE",))
        session.request.reset_mock()
        function = scope["run_jobs"]
        import inspect

        arguments = {name: None for name in inspect.signature(function).parameters}
        arguments["self"] = NS(api_client=api)
        with self.assertRaises(safety.RunPodPreRequestRejected):
            function(**arguments)
        session.request.assert_not_called()

    def test_actual_cpu_gpu_validation_never_calls_transport(self):
        run, _ = native_loop()
        session = NS(request=Mock(return_value=response(200, {"id": "first"})))
        _, api, _ = run(session, ("SECURE",))
        session.request.reset_mock()
        base = dict(
            name="synthetic",
            cloud="SECURE",
            container_disk_in_gb=10,
            image_name="public/fixed",
            entrypoint=["true"],
            cmd=[],
        )
        for extra in (
            {},
            {"gpu_type_id": "gpu", "cpu_type_id": "cpu"},
            {"gpu_type_id": "gpu", "cpu_count": 4},
            {"cpu_type_id": "cpu"},
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaises(safety.RunPodPreRequestRejected),
            ):
                api.create_pod(**base, **extra)
        session.request.assert_not_called()

    def test_actual_local_rejection_advances_without_a_create(self):
        run, _ = native_loop()
        session = NS(request=Mock(return_value=response(200, {"id": "original_123"})))
        value, _, _ = run(session, spots=[True, False])
        self.assertEqual(value.provisioning_data.instance_id, "original_123")
        session.request.assert_called_once()

    def test_actual_private_registry_and_volume_never_create(self):
        run, _ = native_loop()
        for kwargs in (
            {"registry": NS(username="SECRET", password="SECRET")},
            {"volumes": [NS(volume_id="existing-volume")]},
        ):
            session = NS(request=Mock(side_effect=AssertionError("provider write")))
            value, _, _ = run(session, **kwargs)
            session.request.assert_not_called()
            self.assertNotIn("SECRET", value.message)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source-cache", type=Path)
    parser.add_argument("--installed-root", type=Path)
    args, remaining = parser.parse_known_args()
    SOURCE_CACHE, INSTALLED_ROOT = args.source_cache, args.installed_root
    if INSTALLED_ROOT:
        shipped = INSTALLED_ROOT / Path(
            *installer.HELPER_MODULE.split(".")
        ).with_suffix(".py")
        assert (
            hashlib.sha256(shipped.read_bytes()).digest()
            == hashlib.sha256((HERE / "native_vast_safety.py").read_bytes()).digest()
        )
        safety = load("installed_native_safety", shipped)
    unittest.main(argv=[sys.argv[0], *remaining])
