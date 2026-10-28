"""Model-call tracing to Langfuse (h3-studio#770).

Off unless the protected Langfuse credential file is configured. Tracing never changes
a request: every tracing failure is swallowed. Inputs are bounded text, never images,
credentials or whole documents; people appear as a stable hash, not a name or email.
"""

import contextlib
import hashlib
import json
import logging

LOG = logging.getLogger(__name__)
TEXT_LIMIT = 6000
_client = None


def configure(settings):
    """Start the Langfuse client for this process, or leave tracing off."""
    global _client
    _client = None
    if settings.langfuse_config_file is None:
        return
    try:
        from langfuse import Langfuse
        from .langfuse_config import runtime_config

        credential = runtime_config(settings.langfuse_config_file)
        _client = Langfuse(
            public_key=credential.public_key,
            secret_key=credential.secret_key,
            base_url=credential.base_url,
            environment=settings.sentry_environment or "development",
            flush_interval=5,
        )
    except Exception:
        LOG.warning("llm_tracing_unavailable")


def enabled():
    return _client is not None


def clip(value, limit=TEXT_LIMIT):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def person(owner):
    """A stable, non-reversible id for filtering by person in Langfuse."""
    if not owner:
        return None
    return "u_" + hashlib.sha256(str(owner).encode()).hexdigest()[:12]


def digest(text):
    return hashlib.sha256(str(text).encode()).hexdigest()[:12]


class _Handle:
    def __init__(self, observation=None):
        self.observation = observation

    def update(self, **fields):
        if self.observation is None:
            return
        try:
            self.observation.update(**fields)
        except Exception:
            LOG.warning("llm_tracing_update_failed")


@contextlib.contextmanager
def observe(name, *, as_type="span", user=None, session=None, tags=None, **fields):
    """One traced step. The body runs the same whether or not tracing works."""
    stack, handle = contextlib.ExitStack(), _Handle()
    if _client is not None:
        try:
            handle = _Handle(
                stack.enter_context(
                    _client.start_as_current_observation(
                        name=name, as_type=as_type, **fields
                    )
                )
            )
            if user or session or tags:
                from langfuse import propagate_attributes

                stack.enter_context(
                    propagate_attributes(
                        user_id=user,
                        session_id=session,
                        tags=list(tags or []),
                        trace_name=name,
                    )
                )
        except Exception:
            LOG.warning("llm_tracing_start_failed")
            _close(stack)
            stack, handle = contextlib.ExitStack(), _Handle()
    try:
        yield handle
    except BaseException as error:
        handle.update(
            level="ERROR",
            status_message=getattr(error, "code", None) or type(error).__name__,
        )
        _close(stack)
        raise
    _close(stack)


def _close(stack):
    try:
        stack.close()
    except Exception:
        LOG.warning("llm_tracing_end_failed")


def generation(name, *, model, **fields):
    return observe(name, as_type="generation", model=model, **fields)
