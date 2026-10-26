"""Read-only aggregate census; no customer text, identifiers or mutations emitted.

Run inside the existing app container. It uses that app's database configuration
and immutable TaoMate compiler. This is control compatibility, not media readiness.
"""

import collections
import json

from sqlalchemy import create_engine, text

from studio_platform.capabilities import compile_request
from studio_platform.inference.wangp_full_compiler import control_schema
from studio_platform.settings import Settings

PROFILE = "h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7"
CREATOR = {"duration", "seed", "resolution", "aspect_ratio", "generate_audio"}
SQL = """
SELECT owner_id, 'account' AS layer, payload::jsonb->'controls' AS controls
FROM platform_documents WHERE tenant_id=:tenant AND kind='storyboard_preferences'
UNION ALL
SELECT owner_id, 'story', payload::jsonb#>'{journey,storyboard,defaults,controls}'
FROM platform_documents WHERE tenant_id=:tenant AND kind='project'
UNION ALL
SELECT owner_id, 'shot', entity#>'{data,h3,controls}'
FROM platform_documents CROSS JOIN LATERAL jsonb_array_elements(
 CASE WHEN jsonb_typeof(payload::jsonb->'entities')='array'
 THEN payload::jsonb->'entities' ELSE '[]'::jsonb END) AS entity
WHERE tenant_id=:tenant AND kind='project' AND entity->>'type'='shot'
"""


def refused_fields(value):
    """Probe each stored scalar independently, so one error cannot hide others."""
    refused, contextual = [], []
    schema = control_schema(PROFILE, "fl")
    for key, item in value.items():
        if key in CREATOR or item is None or item == "":
            continue  # draft numeric clearing uses profile defaults
        if key not in schema or schema[key].get("available") is False:
            refused.append(key if key in schema else "unknown_control")
            continue
        try:
            compile_request(
                {
                    "recipe_id": "h3-base-fl2va-v1",
                    "deployment_profile_id": PROFILE,
                    "client_ref": {
                        "project_id": "audit",
                        "shot_id": "audit",
                        "shot_version": 1,
                    },
                    "prompt": "Synthetic compatibility probe.",
                    "controls": {"seed": "0", key: item},
                },
                lambda _: None,
                backend="wangp-worker",
            )
        except ValueError as exc:
            code = str(exc)
            # Only input-independent compiler rejection is a definite refusal.
            if code in {"wangp_invalid_" + key, "wangp_unsupported_" + key}:
                refused.append(key)
            else:
                contextual.append(key)
    return refused, contextual


def census(connection, tenant):
    buckets = {
        layer: {
            "records_with_controls": 0,
            "records_with_hidden_controls": 0,
            "records_with_definite_refusal": 0,
            "context_dependent_records": 0,
            "refused_fields": collections.Counter(),
            "records_with_non_default_aspect": 0,
            "non_default_aspects": collections.Counter(),
            "records_with_invalid_aspect": 0,
        }
        for layer in ("account", "story", "shot")
    }
    owners = set()
    aspect_schema = control_schema(PROFILE, "fl")["aspect_ratio"]
    for row in connection.execute(text(SQL), {"tenant": tenant}).mappings():
        value = row["controls"]
        if not isinstance(value, dict) or not value:
            continue
        bucket = buckets[row["layer"]]
        bucket["records_with_controls"] += 1
        bucket["records_with_hidden_controls"] += bool(set(value) - CREATOR)
        aspect = value.get("aspect_ratio")
        if aspect is not None and aspect != "" and aspect != aspect_schema["default"]:
            bucket["records_with_non_default_aspect"] += 1
            if isinstance(aspect, str) and aspect in aspect_schema["enum"]:
                bucket["non_default_aspects"][aspect] += 1
            else:
                # Never emit arbitrary stored strings in an aggregate report.
                bucket["records_with_invalid_aspect"] += 1
        refused, contextual = refused_fields(value)
        if refused:
            owners.add(row["owner_id"])
            bucket["records_with_definite_refusal"] += 1
            bucket["refused_fields"].update(set(refused))
        bucket["context_dependent_records"] += bool(contextual)
    return {
        "profile": PROFILE,
        "scope": "stored layers, including archived/trash documents; not accepted jobs",
        "layers": buckets,
        "owners_with_definite_refusal": len(owners),
        "database_writes": 0,
        "provider_calls": 0,
    }


def main():
    settings = Settings.from_environment()
    engine = create_engine(settings.database_url, echo=False)
    try:
        with engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout = '30s'"))
                print(
                    json.dumps(census(connection, settings.tenant_id), sort_keys=True)
                )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
