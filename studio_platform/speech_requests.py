"""Durable voice request ownership and settlement on the existing credit ledger."""

import hashlib
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from . import credits, service_credits, speech_credit_proof
from .credit_schema import entries
from .identity_schema import users
from .guided import receipts
from .repository import Conflict, NotFound, request_hash


def fail(code, status=502, **detail):
    raise HTTPException(status, {"code": code, **detail})


def public_input(body, text):
    return {
        "voice_id": body.voice_id,
        "direction": body.direction,
        "language": body.language,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "characters": len(text),
    }


class SpeechRequest:
    def __init__(self, app, principal, project_id, body, text):
        self.app, self.principal, self.project_id = app, principal, project_id
        self.repo, self.settings = app.state.repository, app.state.settings
        self.scope = app.state.scope(principal, project_id)
        self.request_id = body.request_id
        self.entity_id = "speech-" + body.request_id
        self.reference = "speech:" + body.request_id
        self.start_key = "speech-v1:start:" + body.request_id
        self.attach_key = "speech-v1:attach:" + body.request_id
        self.public = public_input(body, text)
        self.binding, self.held, self.charged = None, None, False

    def read(self):
        return self.app.state.authorized_project(
            self.principal, self.project_id, "projects:write"
        )

    def receipt(self, key):
        with self.repo.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(receipts.c.actor, receipts.c.response)
                    .where(
                        receipts.c.tenant == self.settings.tenant_id,
                        receipts.c.owner == self.principal.owner,
                        receipts.c.project_id == self.project_id,
                        receipts.c.key == key,
                    )
                    .limit(2)
                )
                .mappings()
                .all()
            )
        if any(row["actor"] != self.principal.actor_id for row in rows):
            fail("speech_request_conflict", 409)
        if len(rows) > 1:
            fail("speech_outcome_unconfirmed")
        return rows[0]["response"] if rows else None

    def has_hold(self):
        with self.repo.engine.connect() as connection:
            if not credits._ready(connection):
                return False
            return (
                connection.execute(
                    select(entries.c.id)
                    .join(users, users.c.id == entries.c.user_id)
                    .where(
                        entries.c.tenant == self.settings.tenant_id,
                        users.c.tenant == self.settings.tenant_id,
                        users.c.owner == self.principal.owner,
                        entries.c.reference_type == "speech",
                        entries.c.reference_id == self.reference,
                        entries.c.kind == "hold",
                    )
                    .limit(1)
                ).first()
                is not None
            )

    def bind(self, speech):
        value = {
            "tenant": self.settings.tenant_id,
            "owner": self.principal.owner,
            "project_id": self.project_id,
            "card_id": self.entity_id,
            "request_id": self.request_id,
            "actor_id": self.principal.actor_id,
            "speech": speech,
            "input_hash": request_hash(speech),
        }
        self.binding = speech_credit_proof.binding(self.scope, self.reference, value)

    def marker(self, response):
        try:
            marker = response["project"]["journey"]["speechRequests"][self.request_id]
            valid = (
                response["id"] == self.project_id
                and type(response["version"]) is int
                and response["version"] > 0
                and type(marker) is dict
                and set(marker) == {"input_hash", "speech", "charged", "token"}
                and type(marker["charged"]) is bool
                and isinstance(marker["token"], str)
                and re.fullmatch(r"[0-9a-f]{32}", marker["token"])
                and marker["input_hash"] == request_hash(marker["speech"])
            )
        except (KeyError, TypeError):
            valid = False
        if not valid:
            fail("speech_outcome_unconfirmed")
        self.bind(marker["speech"])
        if any(
            marker["speech"].get(key) != value for key, value in self.public.items()
        ):
            fail("speech_request_conflict", 409)
        self.charged = marker["charged"]
        return marker

    def money(self, operation, **kwargs):
        with self.repo.transaction() as connection:
            return operation(
                connection,
                self.repo,
                self.scope,
                self.reference,
                binding=self.binding,
                service="speech",
                **kwargs,
            )

    def replay(self):
        started, attached = self.receipt(self.start_key), self.receipt(self.attach_key)
        if started is None and attached is None:
            if self.has_hold():
                fail("speech_outcome_unconfirmed")
            # Legacy free clips are kept, but lack the frozen language/request proof.
            if any(
                e.get("id") == self.entity_id
                for e in self.read()["payload"]["entities"]
            ):
                fail("speech_outcome_unconfirmed")
            return None
        self.marker(started)
        if self.charged:
            if not self.has_hold():
                # An interrupted admission never creates a new paid hold on replay.
                fail("speech_outcome_unconfirmed")
            self.held = self.money(service_credits.service_hold)
            if self.held is None or self.held["created"]:
                fail("speech_outcome_unconfirmed")
            if self.held["terminal"] and self.held["terminal"]["kind"] == "release":
                fail("speech_released", 409, nothing_charged=True)
        if attached is not None:
            return self.finish()
        fail("speech_in_progress", 409)

    def affordable(self):
        """Refuse before the start fence, so a short balance never strands the request id."""
        need = speech_credit_proof.quote(self.public["characters"])["credits"]
        with self.repo.engine.connect() as connection:
            if not credits._ready(connection):
                return
            try:
                view = credits.account(
                    connection,
                    self.settings.tenant_id,
                    self.principal.owner,
                    now=self.repo.clock(),
                )
            except credits.CreditError:
                return  # No account yet: the hold itself decides, as for pictures.
        if view["available"] < need:
            fail(
                "insufficient_credits",
                402,
                need=need,
                have=view["available"],
                nothing_charged=True,
            )

    def begin(self, voice, *, charged):
        self.bind(
            {
                **self.public,
                **{key: voice[key] for key in ("provider", "model", "provider_voice")},
            }
        )
        self.charged = charged
        if charged:
            self.affordable()
        token = uuid4().hex
        marker = {
            "input_hash": self.binding["input_hash"],
            "speech": self.binding["speech"],
            "charged": charged,
            "token": token,
        }
        # Freeze ownership before reserving: free/paid contenders cannot strand a
        # loser's reservation when only the free request won the durable fence.
        for _ in range(3):
            row = self.read()
            if self.receipt(self.start_key) is not None:
                fail("speech_in_progress", 409)
            journey = row["payload"].get("journey", {})
            requests = journey.get("speechRequests", {})
            if type(requests) is not dict:
                fail("speech_request_conflict", 409)
            if self.request_id in requests:
                # The project CAS also fences a different actor of this owner.
                # Mutable markers may deny a new attempt, never authorize one.
                fail("speech_in_progress", 409)
            try:
                result = self.app.state.guided.mutate(
                    self.principal,
                    self.project_id,
                    {
                        "expected_version": row["version"],
                        "actions": [
                            {
                                "op": "journey.update",
                                "patch": {
                                    "speechRequests": {
                                        **requests,
                                        self.request_id: marker,
                                    }
                                },
                            }
                        ],
                    },
                    self.start_key,
                    receipt_channel="speech",
                )
            except Conflict as error:
                if (
                    str(error) == "document_version_conflict"
                    and self.receipt(self.start_key) is None
                ):
                    continue
                fail("speech_in_progress", 409)
            except Exception:
                fail("speech_outcome_unconfirmed")
            original = self.marker(result)
            if original["token"] != token:
                fail("speech_in_progress", 409)
            break
        else:
            fail("document_version_conflict", 409, nothing_charged=True)
        if charged:
            self.held = self.money(service_credits.service_hold)
            if self.held is None:
                fail("speech_outcome_unconfirmed")
            if not self.held["created"]:
                fail("speech_in_progress", 409)

    def release(self, reason):
        if self.held is None:
            return not self.charged
        if not self.held["created"]:
            return False
        try:
            self.money(service_credits.service_release, reason=reason)
            return True
        except Exception:
            # Unknown attachment/commit outcome is left for original-receipt recovery.
            return False

    def provenance(self, provider_characters):
        return {
            **self.binding["speech"],
            "request_id": self.request_id,
            "input_hash": self.binding["input_hash"],
            "provider_characters": provider_characters,
        }

    def finish(self):
        with self.repo.transaction() as connection:
            if self.charged:
                service_credits.service_capture(
                    connection,
                    self.repo,
                    self.scope,
                    self.reference,
                    binding=self.binding,
                    service="speech",
                )
            result = speech_credit_proof.attachment(connection, self.binding)
            if result is None:
                fail("speech_outcome_unconfirmed")
            return result

    def refused_attachment(self, status=502):
        # Deterministic validation/access refusals did not commit the mutation.
        # Still recover a committed original if the response was lost. A failed
        # receipt read is uncertainty and never authorizes releasing the hold.
        if self.receipt(self.attach_key) is not None:
            return self.finish()
        released = self.release("store_failed")
        fail(
            "speech_store_failed",
            status,
            **({"nothing_charged": True} if released else {}),
        )

    def attach(self, asset_id, title, provenance):
        for _ in range(3):
            try:
                row = self.read()
                self.app.state.guided.mutate(
                    self.principal,
                    self.project_id,
                    {
                        "expected_version": row["version"],
                        "actions": [
                            {
                                "op": "asset.attach",
                                "asset_id": asset_id,
                                "entity_id": self.entity_id,
                                "title": title,
                            },
                            {
                                "op": "entity.update",
                                "entity_id": self.entity_id,
                                "patch": {"data": {"speech": provenance}},
                            },
                        ],
                    },
                    self.attach_key,
                    receipt_channel="speech",
                )
            except (ValueError, NotFound) as error:
                return self.refused_attachment(
                    404 if isinstance(error, NotFound) else 502
                )
            except HTTPException as error:
                if error.status_code in {400, 401, 403, 404, 409, 422}:
                    return self.refused_attachment(error.status_code)
                raise
            except Conflict as error:
                if str(error) == "document_version_conflict":
                    continue
                fail("speech_outcome_unconfirmed")
            except Exception:
                # Do not turn an unknown database commit into permission to refund
                # and then attach later. Retry only settles its immutable receipt.
                fail("speech_outcome_unconfirmed")
            return self.finish()
        released = self.release("version_conflict")
        fail(
            "document_version_conflict",
            409,
            **({"nothing_charged": True} if released else {}),
        )
