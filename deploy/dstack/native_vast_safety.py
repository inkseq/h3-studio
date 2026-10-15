"""Bounded safety helpers for the SHA-pinned dstack CPU server patch.

The native allocator owns offers, submissions and persistence. This module has
no clients, background threads, provider configuration or import-time effects.
"""
import json
import random
import re
import time
from email.utils import parsedate_to_datetime
import math
import threading

OUTCOME_PREFIX = "sixnine_vast_outcome_v1:"
ATTEMPTS_PREFIX = "sixnine_vast_attempts_v1:"
READ_ATTEMPTS = 3
MAX_ATTEMPTS = 25
OUTCOME_KEYS = {"ask_id", "http_status", "code", "outcome", "contract_id"}
SAFE_CODES = {"no_such_ask", "rate_limited"}
_cooldown_lock = threading.Lock()
_cooldown_until = 0.


class VastReadRateLimited(Exception):
    def __init__(self, retry_after=30):
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__("sixnine_vast_read_rate_limited")


class VastSafeReject(Exception):
    def __init__(self, outcome):
        self.outcome = outcome
        self.sixnine_vast_outcome = outcome
        super().__init__(OUTCOME_PREFIX + json.dumps(outcome, sort_keys=True, separators=(",", ":")))


class VastAllocationUnknown(VastSafeReject):
    """One PUT may have allocated; the native offer loop must terminate."""


def _numeric_id(value):
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,17}", str(value)):
        raise ValueError("sixnine_vast_numeric_identity_required")
    return str(value)


def _retry_hint(response):
    header = response.headers.get("Retry-After", "")
    retry_after = 0
    if header:
        if not isinstance(header, str) or len(header) > 128:
            return None
        if re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,3})?", header):
            retry_after = float(header)
        else:
            try:
                date = parsedate_to_datetime(header)
                if date.tzinfo is None:return None
                retry_after = max(0., date.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None
    return retry_after


def _delay(response, attempt=0):
    # A longer provider hint is deferred, never shortened into an early retry.
    retry_after = _retry_hint(response)
    if retry_after is None or retry_after > 30:
        return None
    return max(retry_after, random.uniform(1., 2. ** (attempt + 1)))


def _remaining_cooldown():
    with _cooldown_lock:
        return max(0., _cooldown_until - time.monotonic())


def _ensure_available():
    remaining = _remaining_cooldown()
    if remaining > 0:
        raise VastReadRateLimited(remaining)


def _record_rate_limit(response, delay):
    global _cooldown_until
    hint = _retry_hint(response)
    seconds = max(hint if hint is not None else 30., delay or 0.)
    with _cooldown_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + seconds)


def read_request(call):
    """Retry only a caller's existing GET or read-only catalog search on 429."""
    for attempt in range(READ_ATTEMPTS):
        _ensure_available()
        response = call()
        if response.status_code != 429:
            return response
        delay = _delay(response, attempt)
        _record_rate_limit(response, delay)
        _close(response)
        if delay is None or attempt + 1 == READ_ATTEMPTS:
            raise VastReadRateLimited(_remaining_cooldown()) from None
        time.sleep(delay)


def create_instance_once(session, bundle_id, payload):
    """Exactly one original PUT; only confirmed non-allocation permits fallback.

    A returned contract is retained as unknown even if the server reports an
    error. This path never deletes it, repeats its PUT or fabricates settlement.
    """
    ask_id = _numeric_id(bundle_id)
    _ensure_available()  # No PUT, fake HTTP status or rejection when throttled.
    unknown = {"ask_id": ask_id, "http_status": None, "code": "mutation_unknown", "outcome": "unknown", "contract_id": None}
    try:
        response = session.put(f"/v0/asks/{ask_id}/", json=payload)
    except Exception:
        raise VastAllocationUnknown(unknown) from None
    try:
        status = response.status_code
        unknown["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
        try:
            data = response.json()
        except Exception:
            raise VastAllocationUnknown(unknown) from None
        if not isinstance(data, dict):
            raise VastAllocationUnknown(unknown) from None
        contract = data.get("new_contract")
        if contract is not None:
            try:
                contract = _numeric_id(contract)
            except ValueError:
                raise VastAllocationUnknown(unknown) from None
            if status == 200 and data.get("success") is True:
                return int(contract)
            unknown.update(code="contract_returned_with_error", contract_id=contract)
            raise VastAllocationUnknown(unknown) from None
        if data.get("success") is False and (status == 429 or status == 404 and data.get("error") == "no_such_ask"):
            outcome = {**unknown, "code": "rate_limited" if status == 429 else "no_such_ask", "outcome": "rejected"}
            if status == 429:
                delay = _delay(response)
                _record_rate_limit(response, delay)
                if delay is None:
                    rejected = VastSafeReject(outcome)
                    rejected.sixnine_vast_stop_fallback = True
                    raise rejected from None
                time.sleep(delay)
            raise VastSafeReject(outcome) from None
        raise VastAllocationUnknown(unknown) from None
    finally:
        _close(response)


def _close(response):
    try:
        response.close()
    except Exception:
        pass  # A local connection-close failure cannot authorize another PUT.


def closed_error(ask_id, error):
    """No native exception text, provider response or URL enters the proof."""
    ask_id = _numeric_id(ask_id)
    value = {"ask_id": ask_id, "http_status": None, "code": "mutation_unknown", "outcome": "unknown", "contract_id": None}
    try:
        # Read typed internal evidence, never parse an arbitrary provider or
        # exception message as a trusted non-allocation receipt.
        value = _validated_outcome(getattr(error, "sixnine_vast_outcome", None), ask_id)
    except (TypeError, ValueError, KeyError):
        pass
    return OUTCOME_PREFIX + json.dumps(value, sort_keys=True, separators=(",", ":"))


def safe_for_fallback(error):
    try:
        value = json.loads(error[len(OUTCOME_PREFIX):]) if error.startswith(OUTCOME_PREFIX) else None
        return _validated_outcome(value, value["ask_id"])["outcome"] == "rejected"
    except (TypeError, ValueError, KeyError):
        return False


def _validated_outcome(value, ask_id):
    if (not isinstance(value, dict) or set(value) != OUTCOME_KEYS
            or _numeric_id(value["ask_id"]) != str(ask_id)
            or value["outcome"] not in {"rejected", "unknown"}
            or value["http_status"] is not None and (type(value["http_status"]) is not int or not 100 <= value["http_status"] <= 599)
            or value["code"] not in SAFE_CODES | {"mutation_unknown", "contract_returned_with_error"}
            or value["contract_id"] is not None and _numeric_id(value["contract_id"]) != value["contract_id"]):
        raise ValueError("sixnine_vast_outcome_invalid")
    if value["outcome"] == "rejected" and not (value["contract_id"] is None and ((value["code"], value["http_status"]) in {("no_such_ask", 404), ("rate_limited", 429)})):
        raise ValueError("sixnine_vast_rejection_unconfirmed")
    return value


def order_offers(offers, instance_types):
    """Preserve the explicit approved Vast order after native qualification."""
    if not instance_types:
        return offers
    rank = {name: index for index, name in enumerate(instance_types)}
    vast = sorted((item for item in offers if item[1].backend.value == "vastai"),
                  key=lambda item: rank.get(item[1].instance.name, len(rank)))
    ordered = iter(vast)
    return [next(ordered) if item[1].backend.value == "vastai" else item for item in offers]


def failure_message(attempts):
    """Complete bounded proof in the original submission's terminal message.

    Missing, legacy, malformed or truncated errors can never become an all-safe
    proof. The consumer still verifies the original run and account absence.
    """
    outcomes = []
    complete = type(attempts.tried) is int and 0 < attempts.tried <= MAX_ATTEMPTS and len(attempts.errors) == attempts.tried
    for attempt in attempts.errors[:MAX_ATTEMPTS]:
        try:
            if attempt.backend != "vastai" or not attempt.error.startswith(OUTCOME_PREFIX):
                raise ValueError()
            value = _validated_outcome(json.loads(attempt.error[len(OUTCOME_PREFIX):]), attempt.instance)
            outcomes.append(value)
        except (TypeError, ValueError, KeyError):
            complete = False
    safe = complete and len(outcomes) == attempts.tried and all(value["outcome"] == "rejected" for value in outcomes)
    return ATTEMPTS_PREFIX + json.dumps({"version": 1, "outcome": "no_allocation" if safe else "unknown", "attempted_count": attempts.tried, "attempts": outcomes}, sort_keys=True, separators=(",", ":"))
