"""Strict card authoring shapes and bounded public validation diagnostics.

These describe saved intent, not execution qualification. Validation never
resolves assets, submits jobs, or returns request values in an error.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
)
from pydantic import ValidationError, create_model, field_validator, model_validator
from pydantic_core import PydanticCustomError

from .capabilities import LIMITS, RECIPES
from .generation_draft import control_schema


MAX_FIELD_ERRORS = 12
# Preserve JSON integers as integers. A strict float alone normalizes them and
# would change the fingerprint of an otherwise identical legacy write body.
Number = StrictInt | StrictFloat
AssetId = Annotated[StrictStr, Field(min_length=1, max_length=160)]


class AuthoringObject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SourceRange(AuthoringObject):
    start: Annotated[Number, Field(ge=0)]
    end: Number

    @model_validator(mode="after")
    def valid_duration(self):
        if not 2 <= self.end - self.start <= 15:
            raise PydanticCustomError("invalid_value", "Invalid source range.")
        return self


class FrameInput(AuthoringObject):
    asset_id: AssetId


class ImageInput(AuthoringObject):
    asset_id: AssetId
    purpose: Literal["reference", "identity"] = "reference"
    source_range: SourceRange | None = None


class VideoInput(AuthoringObject):
    asset_id: AssetId
    purpose: Literal["reference", "motion"] = "motion"
    source_range: SourceRange | None = None
    include_audio: StrictBool = False


class AudioInput(AuthoringObject):
    asset_id: AssetId
    purpose: Literal["reference", "audio"] = "audio"
    source_range: SourceRange | None = None


class GuideInput(AuthoringObject):
    media_id: AssetId
    time_seconds: Annotated[Number, Field(ge=0, le=360000)]
    use_audio: StrictBool = False
    source_range: SourceRange | None = None


class ExcerptInput(AuthoringObject):
    time_seconds: Annotated[Number, Field(ge=0, le=362 / 24)]
    duration_seconds: Annotated[Number, Field(ge=2, le=15)]


class CardInputs(AuthoringObject):
    first_frame: FrameInput | None = None
    last_frame: FrameInput | None = None
    images: Annotated[list[ImageInput], Field(max_length=LIMITS["max_images"])] = Field(
        default_factory=list
    )
    videos: Annotated[list[VideoInput], Field(max_length=LIMITS["max_videos"])] = Field(
        default_factory=list
    )
    audios: Annotated[list[AudioInput], Field(max_length=LIMITS["max_audios"])] = Field(
        default_factory=list
    )
    guides: Annotated[list[GuideInput], Field(max_length=8)] = Field(
        default_factory=list
    )

    @field_validator("images", "videos", "audios")
    @classmethod
    def unique_assets(cls, values):
        if len({value.asset_id for value in values}) != len(values):
            raise PydanticCustomError("invalid_value", "Duplicate input asset.")
        return values


_CONTROL_SCHEMA = {
    name: value
    for name, value in control_schema().items()
    if name not in {"guides", "video_audio"} and value.get("available") is not False
}


def _control_fields():
    fields = {}
    for name, schema in _CONTROL_SCHEMA.items():
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        annotation = (
            Literal[tuple(schema["enum"])]
            if "enum" in schema
            else StrictInt
            if "integer" in types
            else Number
            if "number" in types
            else StrictBool
            if "boolean" in types
            else list[ExcerptInput]
            if "array" in types and name in {'video_excerpts', 'audio_excerpts'}
            else StrictStr
        )
        if "null" in types:
            annotation = annotation | None
        constraints = {
            key: schema[source]
            for key, source in (
                ("ge", "minimum"),
                ("le", "maximum"),
                ("pattern", "pattern"),
            )
            if source in schema
        }
        if 'array' in types and 'maxItems' in schema:
            constraints['max_length'] = schema['maxItems']
        fields[name] = (annotation, Field(default=schema.get("default"), **constraints))
    return fields


class _ControlsBase(AuthoringObject):
    @field_validator("seed", check_fields=False)
    @classmethod
    def seed_domain(cls, value):
        if value is not None and int(value) > 18446744073709551615:
            raise PydanticCustomError("invalid_value", "Invalid seed.")
        return value

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler(core_schema)
        # Some authoring controls have no default: omission is permitted, but
        # explicit null is not. Do not advertise an invalid null default.
        for name, value in schema.get("properties", {}).items():
            if name in _CONTROL_SCHEMA and "default" not in _CONTROL_SCHEMA[name]:
                value.pop("default", None)
        return schema


CardControls = create_model("CardControls", __base__=_ControlsBase, **_control_fields())


class CreateCardRequest(AuthoringObject):
    prompt: Annotated[StrictStr, Field(min_length=1, max_length=12000)]
    recipe_id: Literal[tuple(RECIPES) + ("video-fl-v1", "video-ref-v1")] | None = None
    tier: Literal["fast", "standard", "refine"] | None = None
    mode: Literal["fl", "ref"] | None = None
    controls: CardControls
    inputs: CardInputs
    copies: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    title: Annotated[StrictStr, Field(min_length=1, max_length=160)] = "我的视频"
    deployment_profile_id: StrictStr | None = None
    turn_id: StrictStr | None = None
    source_revision_id: StrictStr | None = None
    expected_card_version: Any = Field(
        default=None,
        description="Legacy create-only field; ignored when creating a new card. Revisions require an integer card version.",
    )

    @model_validator(mode="after")
    def generation_selection(self):
        if self.recipe_id is None and (self.tier is None or self.mode is None):
            raise PydanticCustomError("invalid_value", "Choose quality and mode.")
        return self

    @field_validator("prompt", "title")
    @classmethod
    def nonblank_text(cls, value):
        if not value.strip():
            raise PydanticCustomError("invalid_value", "Text must not be blank.")
        return value


class ReviseCardRequest(CreateCardRequest):
    expected_card_version: Annotated[
        StrictInt,
        Field(
            ge=1,
            description="Current card.version read from this card; not session expected_version.",
        ),
    ]


_TOP_FIELDS = frozenset(CreateCardRequest.model_fields)
_SLOTS = frozenset(CardInputs.model_fields)
_ENTRY_FIELDS = {
    "first_frame": frozenset(FrameInput.model_fields),
    "last_frame": frozenset(FrameInput.model_fields),
    "images": frozenset(ImageInput.model_fields),
    "videos": frozenset(VideoInput.model_fields),
    "audios": frozenset(AudioInput.model_fields),
    "guides": frozenset(GuideInput.model_fields),
}


def _safe_path(location):
    """Return only this contract's field names and bounded array indexes."""
    if not isinstance(location, (tuple, list)):
        return ["body"]
    parts = list(location)
    if parts and parts[0] == "body":
        parts = parts[1:]
    path = ["body"]
    if not parts or parts[0] not in _TOP_FIELDS:
        return path
    top = parts.pop(0)
    path.append(top)
    if top == "controls":
        if parts and parts[0] in _CONTROL_SCHEMA:
            path.append(parts[0])
        return path
    if top != "inputs" or not parts or parts[0] not in _SLOTS:
        return path
    slot = parts.pop(0)
    path.append(slot)
    if slot not in {"first_frame", "last_frame"}:
        if (
            not parts
            or type(parts[0]) is not int
            or not 0 <= parts[0] < max(9, LIMITS["max_images"])
        ):
            return path
        path.append(parts.pop(0))
    if not parts or parts[0] not in _ENTRY_FIELDS[slot]:
        return path
    entry = parts.pop(0)
    path.append(entry)
    if entry == "source_range" and parts and parts[0] in {"start", "end"}:
        path.append(parts[0])
    return path


def safe_field_errors(errors):
    """Sanitize Pydantic errors without ever copying input, message or context."""
    result = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        kind = error.get("type")
        reason = (
            "missing_field"
            if kind == "missing"
            else "unknown_field"
            if kind == "extra_forbidden"
            else "type_mismatch"
            if kind
            in {
                "int_type",
                "float_type",
                "string_type",
                "bool_type",
                "list_type",
                "dict_type",
                "model_type",
                "model_attributes_type",
            }
            else "invalid_value"
        )
        item = {"path": _safe_path(error.get("loc")), "reason": reason}
        if item not in result:
            result.append(item)
        if len(result) >= MAX_FIELD_ERRORS:
            break
    return result or [{"path": ["body"], "reason": "invalid_value"}]


def validate_card_request(body, *, revising=False):
    """Validate in-process writes with the same contract as HTTP; keep raw JSON."""
    model = ReviseCardRequest if revising else CreateCardRequest
    try:
        model.model_validate(body)
    except ValidationError as error:
        # Imported lazily to keep the schema usable by routes without a cycle.
        from .quick_chat import QuickChatError

        raise QuickChatError(
            "invalid_request",
            "请求字段不符合创作契约；请按字段提示修正。",
            422,
            field_errors=safe_field_errors(
                error.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ),
        ) from None
    return body


class TakePreferencesRequest(AuthoringObject):
    expected_version: Annotated[StrictInt, Field(ge=0)]
    copies_max: Annotated[StrictInt, Field(ge=1, le=2147483647)] = None
    takes_fold_count: Annotated[StrictInt, Field(ge=1, le=2147483647)] = None


class TakeStarRequest(AuthoringObject):
    expected_version: Annotated[StrictInt, Field(ge=0)]
    starred: StrictBool = None
    title: StrictStr = None


class TakeAgainRequest(AuthoringObject):
    expected_card_version: Annotated[StrictInt, Field(ge=1)]
    source_revision_id: AssetId
    item_id: AssetId
    copies: Annotated[StrictInt, Field(ge=1, le=2147483647)]


class TakeStoryRequest(AuthoringObject):
    target_project_id: AssetId
    expected_project_version: Annotated[StrictInt, Field(ge=1)]
    parent_id: AssetId
    title: Annotated[StrictStr, Field(min_length=1, max_length=160)] = None
