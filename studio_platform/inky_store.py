"""Scoped advisory cache and durable one-shot claims in existing documents."""

from sqlalchemy import insert, select, update

from .content_lifecycle import _assert_project_available, owner_reference_gate
from .inky_models import KINDS, example_text
from .inky_source import item, sources, suggestion_id
from .repository import Conflict, NotFound, Scope, documents, request_hash

CACHE = "inky_cache"
CLAIM = "inky_evaluation"
IGNORE = "inky_ignore"


class InkyStore:
    def __init__(self, repo, tenant):
        self.repo, self.tenant = repo, tenant

    def where(self, principal, project_id, kind, ident=None):
        return (
            self.repo._scope(
                documents, Scope(self.tenant, principal.owner, project_id)
            ),
            documents.c.kind == kind,
            *(() if ident is None else (documents.c.document_id == ident,)),
        )

    def project(self, conn, principal, project_id, *, write=False):
        if principal.support_viewer or not principal.allows(
            project_id, "projects:write" if write else "projects:read"
        ):
            raise NotFound("project_not_found")
        if write:
            owner_reference_gate(conn, self.tenant, principal.owner)
        query = select(documents).where(
            self.repo._scope(
                documents, Scope(self.tenant, principal.owner, "__projects")
            ),
            documents.c.kind == "project",
            documents.c.document_id == project_id,
        )
        row = (
            self.repo._locked(conn, query)
            if write
            else conn.execute(query).mappings().first()
        )
        if not row or row["payload"].get("content_deleted"):
            raise NotFound("project_not_found")
        _assert_project_available(
            conn, self.tenant, principal.owner, project_id, ("active",)
        )
        return row["payload"]

    def row(self, conn, principal, project_id, kind, ident):
        return (
            conn.execute(
                select(documents).where(*self.where(principal, project_id, kind, ident))
            )
            .mappings()
            .first()
        )

    def put(self, conn, principal, project_id, kind, ident, payload):
        row = self.row(conn, principal, project_id, kind, ident)
        values = {
            "payload": payload,
            "updated_at": self.repo.clock(),
            "version": row["version"] + 1 if row else 1,
        }
        if row:
            conn.execute(
                update(documents)
                .where(*self.where(principal, project_id, kind, ident))
                .values(**values)
            )
        else:
            conn.execute(
                insert(documents).values(
                    tenant_id=self.tenant,
                    owner_id=principal.owner,
                    project_id=project_id,
                    kind=kind,
                    document_id=ident,
                    **values,
                )
            )

    @staticmethod
    def ignore_id(shot_id, kind):
        return request_hash({"shot_id": shot_id, "kind": kind})

    def ignored(self, conn, principal, project_id, shot_id, kind):
        return (
            self.row(conn, principal, project_id, IGNORE, self.ignore_id(shot_id, kind))
            is not None
        )

    def read(self, principal, project_id):
        """Pure reads: no locks, cache writes, evaluations or provider callbacks."""
        with self.repo.engine.connect() as conn:
            current = sources(self.project(conn, principal, project_id))
            ignored = set(
                conn.execute(
                    select(documents.c.document_id).where(
                        *self.where(principal, project_id, IGNORE)
                    )
                ).scalars()
            )
            rows = conn.execute(
                select(documents.c.payload).where(
                    *self.where(principal, project_id, CACHE)
                )
            ).scalars()
            visible = []
            for value in rows:
                source = current.get(value.get("shot_id"))
                if not source or source["source_id"] != value.get("source_id"):
                    continue
                for entry in value.get("items", []):
                    if self.ignore_id(source["shot_id"], entry["kind"]) not in ignored:
                        visible.append(entry)
            visible.sort(key=lambda v: (v["shot_id"], KINDS.index(v["kind"])))
            return {"items": visible[:3]}

    def claim(self, principal, project_id, shot_id):
        """Reserve exactly one evaluation per current source; never retry unknown."""
        with self.repo.transaction() as conn:
            current = sources(self.project(conn, principal, project_id, write=True))
            source = current.get(shot_id)
            if not source or not source["context"]["shot_action"].strip():
                return None
            ident = source["source_id"]
            if self.row(conn, principal, project_id, CLAIM, ident):
                return None
            # Claims contain no creator text. Crash/timeout retains its identity.
            self.put(
                conn,
                principal,
                project_id,
                CLAIM,
                ident,
                {"shot_id": shot_id, "state": "claimed"},
            )
            return source

    def publish(self, principal, project_id, source, findings):
        """Late results and purged/trash sources cannot resurrect advice."""
        if not isinstance(findings, list) or len(findings) > len(KINDS):
            raise ValueError("inky_findings_invalid")
        output = []
        for entry in findings:
            kind = entry.get("kind")
            if (
                kind not in KINDS
                or kind == "upload_card"
                or any(v["kind"] == kind for v in output)
            ):
                raise ValueError("inky_findings_invalid")
            example = entry.get("example")
            if example is not None:
                import json

                example = example_text(
                    json.dumps({"example": example}), source["context"]["lang"]
                )
            output.append(item(source, kind, example))
        with self.repo.transaction() as conn:
            current = sources(
                self.project(conn, principal, project_id, write=True)
            ).get(source["shot_id"])
            if not current or current["source_id"] != source["source_id"]:
                return False
            claim = self.row(conn, principal, project_id, CLAIM, source["source_id"])
            if not claim or claim["payload"].get("state") != "claimed":
                return False
            self.put(
                conn,
                principal,
                project_id,
                CACHE,
                source["shot_id"],
                {
                    "shot_id": source["shot_id"],
                    "source_id": source["source_id"],
                    "items": output,
                },
            )
            self.put(
                conn,
                principal,
                project_id,
                CLAIM,
                source["source_id"],
                {
                    "shot_id": source["shot_id"],
                    "state": "published",
                },
            )
            return True

    def ignore(self, principal, project_id, ident):
        with self.repo.transaction() as conn:
            current = sources(self.project(conn, principal, project_id, write=True))
            # Replay the same ignore even after an edit, without retaining its text.
            saved = self.row(conn, principal, project_id, IGNORE, "id-" + ident)
            if saved:
                return {"id": ident, "ignored": True}
            match = next(
                (
                    (s, k)
                    for s in current.values()
                    for k in KINDS
                    if suggestion_id(s, k) == ident
                ),
                None,
            )
            if not match:
                raise Conflict("inky_suggestion_stale")
            source, kind = match
            cache = self.row(conn, principal, project_id, CACHE, source["shot_id"])
            if not cache or not any(
                v["id"] == ident for v in cache["payload"].get("items", [])
            ):
                raise NotFound("inky_suggestion_not_found")
            value = {"shot_id": source["shot_id"], "kind": kind}
            self.put(
                conn,
                principal,
                project_id,
                IGNORE,
                self.ignore_id(source["shot_id"], kind),
                value,
            )
            self.put(conn, principal, project_id, IGNORE, "id-" + ident, value)
            return {"id": ident, "ignored": True}
