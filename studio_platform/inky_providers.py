"""One-shot Inky provider clients. Construction/import never loads credentials.

Not wired into the application: server provisioning and the request-cost envelope
must receive the single Tier2 review before activation. No secret-drop importer.
"""

import json

import httpx

from .central_registry import load_api
from .inky_models import example_payload, judgement_payload

TYPE_PROFILE = "typesafe--avatar-user-20261008"
OPEN_PROFILE = "openai--inkseq"
TYPE_ORIGIN = "https://api.typesafe.ai/v1"
OPEN_ORIGIN = "https://api.openai.com/v1"
# OpenAI price for gpt-6.1-terra in micro-USD per token: input is charged at the
# cache-write rate ($1.25/M; caching is on by default), output $5/M. OpenAI returns
# no per-request cost, so cached reads are conservatively overcounted.
TERRA_INPUT_PER_4_TOKENS = 5  # 1.25 micro-USD per input token, kept integer.
TERRA_OUTPUT_PER_TOKEN = 5


class InkyProviderError(ValueError):
    """Static, content-free error. Unknown requests must not be retried."""


def config(service):
    profile = TYPE_PROFILE if service == "typesafe" else OPEN_PROFILE
    if service not in {"typesafe", "openai"}:
        raise InkyProviderError("inky_profile_invalid")
    return load_api(service, profile=profile)


class Client:
    def __init__(self, service, *, loader=config, transport=None):
        if service not in {"typesafe", "openai"}:
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
            # This default is authorized for exactly openai--inkseq.
            configured = cfg.base_url or (
                OPEN_ORIGIN if self.service == "openai" else None
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


def token_cost(usage):
    """Terra list-price cost from reported tokens; missing counts are unknown."""
    if not isinstance(usage, dict):
        return None
    tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not all(type(t) is int and t >= 0 for t in tokens):
        return None
    return (
        -(-tokens[0] * TERRA_INPUT_PER_4_TOKENS // 4)
        + tokens[1] * TERRA_OUTPUT_PER_TOKEN
    )


class Terra:
    # Conservative reservation, not the observed average price. This text-only
    # envelope calls OpenAI directly and caps byte size and total output (incl
    # reasoning): 16KiB of message bytes + 4096 framing tokens at $1/M and 1024
    # output tokens at $5/M => about $0.026, well below this $0.25 reservation.
    # No tools, images or fallback providers are accepted.
    max_cost_micros = 250_000

    def __init__(self, **kwargs):
        self.client = Client("openai", **kwargs)

    def __call__(self, source, kind):
        body = example_payload(source, kind)
        size = sum(
            len(message["content"].encode("utf-8")) for message in body["messages"]
        )
        if size > 16384:
            raise InkyProviderError("inky_example_context_limit")
        body.update(max_completion_tokens=1024, stream=False)
        result = self.client.post("/chat/completions", body)
        actual = token_cost(result.get("usage"))
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
