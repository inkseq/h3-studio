"""Public card authoring readback shapes; no additional state or side effects."""
from typing import Any

from pydantic import BaseModel, ConfigDict


class Readback(BaseModel):
    # Preserve additive readback fields; never rewrite the service's saved data.
    model_config = ConfigDict(extra="allow")


class RevisionSummary(Readback):
    id: str
    version: int
    created_at: float
    input_hash: str


class CardReadback(Readback):
    id: str
    version: int
    title: str
    turn_id: str | None
    current_revision_id: str
    revisions: list[RevisionSummary]
    web_url: str


class RevisionItem(Readback):
    id: str
    index: int
    seed: str
    shot_id: str
    projection_hash: str


class RevisionReadback(Readback):
    id: str
    card_id: str
    version: int
    prompt: str
    recipe_id: str
    deployment_profile_id: str | None = None
    controls: dict[str, Any]
    inputs: dict[str, Any]
    copies: int
    title: str
    provenance: list[dict[str, Any]]
    turn_id: str | None
    source_revision_id: str | None
    composer_snapshot: dict[str, Any] | None
    input_hash: str
    requested_input_hash: str
    items: list[RevisionItem]
    created_at: float
    web_url: str


class CardAuthoringResponse(Readback):
    card: CardReadback
    revision: RevisionReadback


class FieldError(BaseModel):
    path: list[str | int]
    reason: str


class AuthoringError(BaseModel):
    code: str
    message: str
    retryable: bool
    field_errors: list[FieldError] | None = None


class AccessError(BaseModel):
    detail: str


AUTHORING_ERRORS = {
    401: {"model": AccessError, "description": "Authentication is required."},
    404: {"model": AccessError, "description": "Resource is absent or inaccessible to this owner."},
    409: {"model": AuthoringError, "description": "Stale card version or conflicting idempotency key. Reconcile the current card before a new logical edit."},
    422: {"model": AuthoringError, "description": "Invalid authoring fields. field_errors contains bounded field paths and static reasons, never submitted values."},
}
