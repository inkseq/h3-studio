"""Retired bootstrap caps; accounting remains in the original ledger (#254)."""

MAX_LEDGER_AMOUNT = 9_000_000_000_000_000
_RETIRED = {
    "demand-gpu-remaining-20261004": (None, None),
    "demand-job-total-20261004": (None, None),
    "demand-job-owner-superdan-20261004": ("superdan", None),
    "demand-job-owner-supervan-20261004": ("supervan", None),
    "prod-h3-20261004-superdan-production-h3-acceptance-20261004":
        ("superdan", "production-h3-acceptance-20261004"),
}


def fixed_cap_retired(account):
    return (account["tenant_id"] == "sixnine" and account["id"] in _RETIRED
        and (account["owner_id"], account["project_id"]) == _RETIRED[account["id"]])


def effective_budget_limit(account):
    # Finite integer ceiling is the ledger's representable range, not a new
    # product allowance. Editable Settings admission and test GPU guards still
    # run independently; every reservation and actual charge is retained.
    return MAX_LEDGER_AMOUNT if fixed_cap_retired(account) else account["limit_microusd"]
