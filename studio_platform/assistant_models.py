"""Stable public assistant identities; provider selection remains server owned.

Aliases apply only at known assistant input/output fields. Saved requests,
idempotency fingerprints and provider payloads retain their original identities.
"""

MODELS = {
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "gemma-4-31b-it": "Gemma 4 31B IT",
}
DEFAULT_MODEL = "gemini-3.8-flash"
ALIASES = {
    "assistant:text-image-v1": "gemma-4-31b-it",
    "assistant:multimodal-v1": "gemini-3.8-flash",
}
LABELS = {
    "assistant:text-image-v1": "Text and image assistant",
    "assistant:multimodal-v1": "Multimodal assistant",
    "assistant:unavailable": "Assistant unavailable",
}


def resolve(value):
    """Two exact aliases plus known legacy input IDs, never a default guess."""
    if not isinstance(value, str):
        return None
    return ALIASES.get(value, value if value in MODELS else None)


def public_id(value):
    original = resolve(value)
    return next(
        (alias for alias, model in ALIASES.items() if model == original),
        "assistant:unavailable",
    )


def public_label(value):
    return LABELS[public_id(value)]
