"""One-shot Inky provider clients. Construction/import never loads credentials.

Not wired into the application: server provisioning and the request-cost envelope
must receive the single Tier2 review before activation. No secret-drop importer.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json

import httpx

from .central_registry import load_api
from .inky_models import example_payload, judgement_payload

TYPE_PROFILE = "typesafe--avatar-user-20261008"
OPEN_PROFILE = "openrouter--inkseq-chat"
TYPE_ORIGIN = "https://api.typesafe.ai/v1"
OPEN_ORIGIN = "https://openrouter.ai/api/v1"


class InkyProviderError(ValueError):
    """Static, content-free error. Unknown requests must not be retried."""


def config(service):
    profile = TYPE_PROFILE if service == "typesafe" else OPEN_PROFILE
    if service not in {"typesafe", "openrouter"}:
        raise InkyProviderError("inky_profile_invalid")
    return load_api(service, profile=profile)


class Client:
    def __init__(self, service, *, loader=config, transport=None):
        if service not in {"typesafe", "openrouter"}:
            raise InkyProviderError("inky_profile_invalid")
        self.service, self.loader, self.transport = service, loader, transport

    def post(self, path, body, *, images=False):
        from .llm_tracing import clip, generation, usage as traced_usage

        model = body.get("model") if isinstance(body, dict) else None
        with generation(
            "inky-" + ("jev" if self.service == "typesafe" else "terra"),
            model=model or self.service,
            # Upload judgements carry a picture: record that one was sent, never the bytes.
            input={"images": True} if images else clip(body),
        ) as traced:
            value = self._post(path, body, images=images)
            usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
            details = usage.get("prompt_tokens_details")
            traced.update(
                output=clip(value, 4000),
                usage_details=traced_usage(
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    details.get("cached_tokens", 0) if isinstance(details, dict) else 0,
                ),
                cost_details=(
                    {"total": float(usage["cost"])}
                    if isinstance(usage.get("cost"), (int, float))
                    else None
                ),
            )
        return value

    def _post(self, path, body, *, images=False):
        try:
            cfg = self.loader(self.service)
            origin = TYPE_ORIGIN if self.service == "typesafe" else OPEN_ORIGIN
            # This default is authorized for exactly openrouter--inkseq-chat.
            configured = cfg.base_url or (
                OPEN_ORIGIN if self.service == "openrouter" else None
            )
            if (
                configured != origin
                or not isinstance(cfg.api_key, str)
                or not cfg.api_key
            ):
                raise InkyProviderError("inky_profile_invalid")
            with httpx.Client(
                timeout=httpx.Timeout(8, connect=3),
                follow_redirects=False,
                transport=self.transport,
                trust_env=False,
            ) as client:
                with client.stream(
                    "POST",
                    origin + path,
                    json=body,
                    headers={
                        "Authorization": "Bearer " + cfg.api_key,
                        **(
                            {"TypeSafe-Danger-Zone-Images": "true"}
                            if images and self.service == "typesafe"
                            else {}
                        ),
                    },
                ) as response:
                    if response.status_code != 200:
                        raise InkyProviderError("inky_provider_http")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise InkyProviderError("inky_provider_response_limit")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise InkyProviderError("inky_provider_response_invalid")
            return value
        except InkyProviderError:
            raise
        except Exception:
            # Do not expose HTTP response bodies, URLs, keys or creator content.
            raise InkyProviderError("inky_provider_unavailable") from None


class Jev:
    def __init__(self, **kwargs):
        self.client = Client("typesafe", **kwargs)

    def __call__(self, source):
        return self.client.post("/systemone", judgement_payload(source))

    def upload(self, upload, jpeg):
        from .inky_uploads import upload_payload

        return self.client.post("/systemone", upload_payload(upload, jpeg), images=True)


def cost_micros(value):
    """Missing/invalid accounting is unknown, never a manufactured zero charge."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        cost = Decimal(str(value))
        if not cost.is_finite() or cost < 0 or cost > 1_000_000:
            return None
        return int((cost * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError):
        return None


class Terra:
    # Conservative reservation, not the observed average price. This text-only
    # envelope pins OpenAI, caps rate, byte size and total output (incl reasoning).
    # 16KiB of message bytes + 4096 framing-token allowance at $5/M, and1024
    # output tokens at $30/M => $0.13312, below this $0.25 reservation. No tools,
    # plugins, fallback providers, images, or per-request charge are accepted.
    # Qualification must verify this envelope on the exact provider before use.
    max_cost_micros = 250_000

    def __init__(self, **kwargs):
        self.client = Client("openrouter", **kwargs)

    def __call__(self, source, kind):
        body = example_payload(source, kind)
        size = sum(
            len(message["content"].encode("utf-8")) for message in body["messages"]
        )
        if size > 16384:
            raise InkyProviderError("inky_example_context_limit")
        body.update(
            max_tokens=1024,
            stream=False,
            provider={
                "only": ["openai"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "max_price": {"prompt": 5, "completion": 30, "request": 0, "image": 0},
            },
        )
        result = self.client.post("/chat/completions", body)
        actual = cost_micros(result.get("usage", {}).get("cost"))
        choices = result.get("choices")
        text = None
        if (
            isinstance(choices, list)
            and len(choices) == 1
            and isinstance(choices[0], dict)
        ):
            message = choices[0].get("message")
            if isinstance(message, dict) and choices[0].get("finish_reason") == "stop":
                text = message.get("content")
        # Preserve known billed cost even when the optional example is unusable.
        return {"text": text, "actual_micros": actual}
