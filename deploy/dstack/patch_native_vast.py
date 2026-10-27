"""Build-time, exact-source patch for the existing dstack 0.22.5 CPU server.

No package import, dependency resolution, service command or network operation.
Every original source is checked before any write; repeat application refuses.
"""
import argparse
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path

HERE = Path(__file__).parent
HELPER_MODULE = "dstack._internal.core.backends.vastai.sixnine_safety"


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("native_patch_anchor_mismatch")
    return source.replace(before, after, 1)


def transform(path, source):
    if path.endswith("runpod/api_client.py"):
        source = "from " + HELPER_MODULE + " import RunPodPreRequestRejected, runpod_create_once\n" + source
        for before, code in (
            ('raise ValueError("Exactly one of CPU or GPU type must be set")', "cpu_gpu_type"),
            ('raise ValueError("cpu_count is not applicable for GPU pods")', "cpu_count_for_gpu"),
            ('raise ValueError("cpu_count is required for CPU pods")', "cpu_count_required"),
        ):
            source = replace_once(source, before, 'raise RunPodPreRequestRejected("' + code + '")')
        source = replace_once(source,
            '        response = self._make_v2_request("POST", "pods", json=body)\n        return response.json()',
            '        return runpod_create_once(self._session, API_V2_URL + "/pods", body, timeout=REQUEST_TIMEOUT)')
        start = source.index("        resp = self._make_graphql_request(", source.index("    def create_gpu_pod_graphql("))
        end = source.index("    def get_pod(", start)
        original = source[start:end]
        changed = replace_once(original, "        resp = self._make_graphql_request(\n", "        payload = (\n")
        changed = replace_once(changed,
            '        data = resp.json()["data"]\n        return data["podRentInterruptable"] if bid_per_gpu else data["podFindAndDeployOnDemand"]',
            '        if bid_per_gpu is not None or network_volume_id is not None or container_registry_auth_id is not None:\n            raise RunPodPreRequestRejected("network_volume_unsupported")\n        return runpod_create_once(self._session, API_GRAPHQL_URL, payload, timeout=REQUEST_TIMEOUT, graphql=True)')
        source = source[:start] + changed + source[end:]
        return source
    if path.endswith("runpod/compute.py"):
        source = "from " + HELPER_MODULE + " import RunPodPreRequestRejected, RunPodAllocationUnknown\n" + source
        start, end = source.index("    def run_job("), source.index("    def run_jobs(")
        prefix, tail, source = source[:start], source[end:], source[start:end]
        for before, code in (
            ('raise ComputeError("Mounting more than one network volume is not supported in runpod")', "multiple_volumes"),
            ('raise ComputeError("Runpod spot offers are not supported")', "spot_unsupported"),
            ('raise ComputeError("Runpod CPU offers are only supported in secure cloud regions")', "community_cpu"),
        ):
            source = replace_once(source, before, 'raise RunPodPreRequestRejected("' + code + '")')
        source = replace_once(source,
            '        container_registry_credentials_id = self._generate_container_registry_credentials_id(\n            job.job_spec.registry_auth\n        )',
            '        if job.job_spec.registry_auth is not None:\n            raise RunPodPreRequestRejected("private_registry")\n        if network_volume_id is not None:\n            raise RunPodPreRequestRejected("network_volume_unsupported")\n        container_registry_credentials_id = None')
        start = source.index('        if (\n            self._last_cleanup_time is None')
        end = source.index('        return JobProvisioningData(', start)
        source = source[:start] + '        # Public-image slice never lists, creates or deletes registry credentials.\n\n' + source[end:]
        # Preserve the known allocation if local provisioning validation fails.
        start = source.index("        return JobProvisioningData(")
        original = source[start:].rstrip()
        source = source[:start] + "        try:\n" + "\n".join("    " + line for line in original.splitlines()) + "\n        except Exception:\n            raise RunPodAllocationUnknown(pod_id=instance_id) from None\n\n"
        tail = replace_once(tail, '        master_job_configuration = job_configurations[0]',
            '        raise RunPodPreRequestRejected("cluster_unsupported")\n        master_job_configuration = job_configurations[0]')
        return prefix + source + tail
    if path.endswith("vastai/api_client.py"):
        source = "from " + HELPER_MODULE + " import create_instance_once, read_request, VastReadRateLimited\n" + source
        before = '''        resp = self.s.put(f"/v0/asks/{bundle_id}/", json=payload)
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise VastAICreateInstanceError(resp=resp.text)
        if resp.status_code != 200 or not data["success"]:
            raise VastAICreateInstanceError(
                resp=resp.text, created_instance_id=data.get("new_contract")
            )
        return data["new_contract"]'''
        source = replace_once(source, before, "        return create_instance_once(self.s, bundle_id, payload)")
        return replace_once(source, '        resp = self.s.get(f"/v0/instances/{instance_id}/")', '''        try:
            resp = read_request(lambda: self.s.get(f"/v0/instances/{instance_id}/"))
        except VastReadRateLimited:
            # Preserve native provisioning's transient-429 catch and original instance.
            raise VastAIRateLimitError() from None''')
    if path.endswith("vastai/compute.py"):
        source = "from " + HELPER_MODULE + " import VastAllocationUnknown, VastSafeReject\n" + source
        source = replace_once(source, '"inet_down": {"gt": 128}', '"inet_down": {"gte": 1000}')
        start = source.index("        except VastAICreateInstanceError as e:")
        end = source.index("        return JobProvisioningData(", start)
        source = source[:start] + '''        except VastAllocationUnknown:
            raise
        except VastSafeReject as error:
            rejected = NoCapacityError(str(error))
            rejected.sixnine_vast_outcome = error.outcome
            rejected.sixnine_vast_stop_fallback = getattr(error, "sixnine_vast_stop_fallback", False)
            raise rejected from None
''' + source[end:]
        return source
    if path == "gpuhunt/providers/vastai.py":
        source = "from " + HELPER_MODULE + " import read_request\n" + source
        return replace_once(source, "        resp = requests.post(bundles_url, json=filters, timeout=10)", "        resp = read_request(lambda: requests.post(bundles_url, json=filters, timeout=10))")
    if path.endswith("services/backends/__init__.py"):
        source = "from fastapi import HTTPException\nfrom " + HELPER_MODULE + " import VastReadRateLimited\n" + source
        return replace_once(source, "        if isinstance(result, BackendError):", '''        if isinstance(result, VastReadRateLimited):
            raise HTTPException(status_code=429, detail="sixnine_vast_read_rate_limited", headers={"Retry-After": str(result.retry_after)})
        if isinstance(result, BackendError):''')
    if path.endswith("pipeline_tasks/jobs_submitted.py"):
        source = "from " + HELPER_MODULE + " import closed_error, failure_message, safe_for_fallback, order_offers, runpod_closed_error, runpod_safe_for_fallback\n" + source
        source = replace_once(source, "    offers_iter = iter(offers)", "    offers = order_offers(offers, profile.instance_types)\n    offers_iter = iter(offers)")
        source = replace_once(source, '''    if attempts.total == 0:
''', '''    if attempts.errors and all(error.backend == "vastai" for error in attempts.errors):
        return failure_message(attempts)
    if attempts.total == 0:
''')
        before = "            offer_errors.append(_get_offer_attempt_error(offer=offer, error=e))\n"
        if source.count(before) != 2:
            raise ValueError("native_patch_offer_catch_mismatch")
        source = source.replace(before, before + '''            if offer.backend.value == "vastai" and (getattr(e, "sixnine_vast_stop_fallback", False) or not safe_for_fallback(offer_errors[-1].error)):
                break  # Possible allocation: terminate this submission, never try another PUT.
''')
        source = source.replace(before, before + '''            if offer.backend.value == "runpod":
                if runpod_safe_for_fallback(e):
                    continue  # Typed local pre-request rejection only.
                return _TerminateSubmittedJobResult(
                    reason=JobTerminationReason.TERMINATED_BY_SERVER,
                    message=runpod_closed_error(e))
''')
        source = replace_once(source, "    # Backend errors may be multiline and arbitrarily long since they often wrap cloud API errors.\n", '''    if offer.backend.value == "runpod":
        return _OfferAttemptError(backend="runpod", region=offer.region,
            instance=offer.instance.name, error=runpod_closed_error(error))
    # Backend errors may be multiline and arbitrarily long since they often wrap cloud API errors.
''')
        return replace_once(source, "    # Backend errors may be multiline and arbitrarily long since they often wrap cloud API errors.\n", '''    if offer.backend.value == "vastai":
        return _OfferAttemptError(backend="vastai", region=offer.region,
            instance=offer.instance.name, error=closed_error(offer.instance.name, error))
    # Backend errors may be multiline and arbitrarily long since they often wrap cloud API errors.
''')
    if path.endswith("scheduled_tasks/offers_catalog.py"):
        source = "import os\n" + source
        return replace_once(source, '    logger.debug("Pre-loading offers catalog")', '''    if os.environ.get("SIXNINE_DSTACK_PRELOAD_CATALOG", "1") == "0":
        return  # Demand reads remain native; idle provider safety sweep is external.
    logger.debug("Pre-loading offers catalog")''')
    raise ValueError("native_patch_path_not_allowlisted")


def install(site_packages, *, check_only=False):
    root = Path(site_packages).resolve(strict=True)
    provenance = json.loads((HERE / "native-vast-provenance.json").read_text(encoding="utf-8"))
    if importlib.metadata.version("dstack") != provenance["dstack_version"]:
        raise ValueError("native_patch_dstack_version_mismatch")
    prepared = []
    for path, expected in provenance["sources"].items():
        target = root / path
        if target.is_symlink() or not target.is_file() or root not in target.resolve().parents:
            raise ValueError("native_patch_source_untrusted")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError("native_patch_source_hash_mismatch")
        changed = transform(path, data.decode("utf-8"))
        ast.parse(changed, filename=path)
        prepared.append((target, changed.encode("utf-8")))
    helper = HERE / "native_vast_safety.py"
    data = helper.read_bytes()
    ast.parse(data, filename=helper.name)
    installed = root / "dstack/_internal/core/backends/vastai/sixnine_safety.py"
    if installed.exists():
        raise ValueError("native_patch_already_applied")
    if check_only:
        return {"checked_sources": len(prepared), "writes": 0}
    for target, changed in prepared:
        target.write_bytes(changed)
    with installed.open("xb") as target:
        target.write(data)
    return {"patched_sources": len(prepared), "dstack_version": provenance["dstack_version"],
        "upstream_commit": provenance["dstack_upstream_commit"],
        "original_sources": {path: record["sha256"] for path, record in provenance["sources"].items()},
        "helper_sha256": hashlib.sha256(data).hexdigest(),
        "sources": {str(target.relative_to(root)): hashlib.sha256(changed).hexdigest() for target, changed in prepared}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--site-packages", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(args.site_packages, check_only=args.check_only), sort_keys=True))


if __name__ == "__main__":
    main()
