"""Complete account reads for RunPod, Lium and CloudRift, and backend fallback rules.

Vast's paginated read stays in dstack_capacity. Each reader receives the caller's
allowlisted `read(method, url, body=...)` and returns (instance_ids, aliases).
A malformed reply raises InventoryUnconfirmed; it is never read as empty.
"""

import re

RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"
LIUM_PODS_URL = "https://lium.io/api/pods"
CLOUDRIFT_INSTANCES_URL = "https://api.cloudrift.ai/api/v1/instances/list"
# The CloudRift API version pinned by upstream dstack 0.22.5+ (#4380).
CLOUDRIFT_API_VERSION = "2026-10-12"
# Lium and CloudRift authenticate with an API-key header; Vast and RunPod use Bearer.
KEY_HEADER_URLS = frozenset({LIUM_PODS_URL, CLOUDRIFT_INSTANCES_URL})
SUPPORTED = frozenset({"vastai", "runpod", "lium", "cloudrift"})
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,200}")
LIVE_CLOUDRIFT = ["Initializing", "Active", "Deactivating"]


class InventoryUnconfirmed(ValueError):
    pass


def _check(condition):
    if not condition:
        raise InventoryUnconfirmed("dstack_provider_inventory_unconfirmed")


def _ids(rows, exact_keys=None):
    _check(isinstance(rows, list) and len(rows) <= 4096)
    ids = []
    for row in rows:
        _check(
            isinstance(row, dict)
            and (exact_keys is None or set(row) == exact_keys)
            and isinstance(row.get("id"), str)
            and IDENTIFIER.fullmatch(row["id"])
        )
        ids.append(row["id"])
    _check(len(set(ids)) == len(ids))
    return ids


def read_ids(backend, read):
    if backend == "lium":
        rows = read("GET", LIUM_PODS_URL)
        ids = _ids(rows)
        # Before Lium returns a pod ID, dstack records the pod name: keep names
        # only as aliases for pending bindings, never as extra allocations.
        aliases = {}
        for row in rows:
            name = row.get("pod_name") or row.get("name")
            if isinstance(name, str) and IDENTIFIER.fullmatch(name):
                aliases[name] = row["id"]
        return ids, aliases
    if backend == "cloudrift":
        body = {
            "version": CLOUDRIFT_API_VERSION,
            "data": {"selector": {"ByStatus": {"statuses": LIVE_CLOUDRIFT}}},
        }
        document = read("POST", CLOUDRIFT_INSTANCES_URL, body=body)
        data = document.get("data") if isinstance(document, dict) else None
        return _ids(data.get("instances") if isinstance(data, dict) else None), {}
    query = {"query": "query { myself { pods { id } } }"}
    document = read("POST", RUNPOD_GRAPHQL_URL, body=query)
    _check(isinstance(document, dict) and set(document) == {"data"})
    data = document["data"]
    myself = data.get("myself") if isinstance(data, dict) else None
    _check(
        isinstance(data, dict)
        and set(data) == {"myself"}
        and isinstance(myself, dict)
        and set(myself) == {"pods"}
    )
    return _ids(myself["pods"], exact_keys={"id"}), {}


def fallbacks_valid(profile, providers):
    fallback = profile.get("fallback_backends", [])
    return (
        isinstance(fallback, list)
        and len(fallback) == len(set(fallback))
        and all(b in providers and b != profile["backend"] for b in fallback)
    )


def profile_backends(profile, providers):
    """The primary backend first, then the configured fallbacks, all supported."""
    order = [profile["backend"], *profile.get("fallback_backends", [])]
    return [backend for backend in order if backend in providers]


def read_inventories(client, nodes):
    """Complete reads of every configured account plus any backend a node used."""
    used = {node["payload"].get("dstack", {}).get("backend") for node in nodes}
    swept = (set(client.configured_backends()) | used) & SUPPORTED
    return {name: client.provider_inventory(name) for name in sorted(swept)}


def known_allocations(providers, nodes):
    """Provider instance IDs bound to retained nodes, per swept backend."""
    known = {name: set() for name in providers}
    for node in nodes:
        binding = node["payload"].get("dstack", {})
        identity = binding.get("provider_instance_id")
        if binding.get("backend") in known and identity:
            # A pending Lium binding may hold the pod name instead of its pod ID.
            aliases = providers[binding["backend"]].get("aliases", {})
            known[binding["backend"]].add(aliases.get(identity) or identity)
    return known
