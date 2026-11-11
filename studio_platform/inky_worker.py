"""Bounded background orchestration. Provider adapters must be injected explicitly.

No endpoint or module import starts work. Unknown paid calls are never retried.
Real adapters must enforce their quoted worst-case cost before this can activate.
"""

import logging

from .inky_budget import InkyBudget
from .inky_models import (
    CONTINUITY_GATE,
    continuity_note,
    continuity_score,
    example_text,
    finding_applies,
    rewrite_text,
    text_findings,
)
from .repository import Conflict, NotFound


class InkyRewriteUnavailable(Exception):
    """The daily or monthly allowance said no; no provider was called."""


class InkyRewriteFailed(Exception):
    """The provider call or its answer was unusable. Nothing changed."""


LOG = logging.getLogger(__name__)
# Per save or open. The judge answers in under a second; each note takes several.
CONTINUITY_JUDGED = 8
CONTINUITY_NOTES = 2


class InkyWorker:
    def __init__(self, store, *, judge, examples=None, uploads=None):
        self.store, self.judge, self.examples = store, judge, examples
        self.uploads = uploads
        self.budget = InkyBudget(store.repo)

    def refresh(self, principal, project_id):
        """Called after successful save or explicit preflight, never on GET.

        At most three newly claimed shots and three example calls per invocation.
        Failure of optional advice must not fail an already committed save.
        """
        try:
            processed, allowance = 0, 3
            for shot_id in self.store.unclaimed(principal, project_id):
                source = self.store.claim(principal, project_id, shot_id)
                if source is None:
                    continue
                findings, used = self.evaluate(principal, project_id, source, allowance)
                allowance -= used
                self.store.publish(principal, project_id, source, findings)
                processed += 1
                if processed == 3:
                    break
            self.review_continuity(principal, project_id)
        except (Conflict, NotFound):
            LOG.info("inky_source_unavailable")
        except Exception:
            # Never log upstream exception strings, URLs, prompts or credentials.
            LOG.warning("inky_background_unavailable")

    def review_continuity(self, principal, project_id):
        """Earliest unchecked shots first, so an edit is checked before what follows it."""
        if not (
            callable(getattr(self.judge, "continuity", None))
            and callable(getattr(self.examples, "continuity", None))
        ):
            return
        notes = 0
        for _ in range(CONTINUITY_JUDGED):
            if notes == CONTINUITY_NOTES:
                return
            review = self.store.claim_continuity(principal, project_id)
            if review is None:
                return
            finding, called = self.check_continuity(principal, project_id, review)
            notes += called
            self.store.publish_continuity(principal, project_id, review, finding)

    def check_continuity(self, principal, project_id, review):
        """Returns the finding, if any, and whether a note was paid for."""
        try:
            score = continuity_score(self.judge.continuity(review["state"]))
        except Exception:
            # Not silent: a refusal here means the size bound in inky_continuity is wrong.
            LOG.warning("inky_continuity_unavailable")
            return None, 0
        if score < CONTINUITY_GATE:
            return None, 0
        reservation = self.budget.reserve(
            tenant=self.store.tenant,
            owner=principal.owner,
            project=project_id,
            source_id=review["digest"] + ":continuity",
            max_cost_micros=self.examples.max_cost_micros,
            purpose="continuity",
        )
        if not reservation["allowed"]:
            LOG.info(reservation["reason"])
            return None, 0
        lang = review["source"]["context"]["lang"]
        try:
            response = self.examples.continuity(review["state"], lang)
            actual = response.get("actual_micros")
            if type(actual) is int and actual >= 0:
                settled = self.budget.settle(reservation["call_id"], actual)
                LOG.info("inky_continuity_usage", extra={"inky_cost_micros": actual})
                if settled["overrun"]:
                    LOG.error("inky_example_budget")
            else:
                LOG.warning("inky_continuity_usage_unknown")
            return continuity_note(response.get("text"), lang, review["numbers"]), 1
        except Exception:
            LOG.info("inky_continuity_note_failed")
            return None, 1

    def evaluate(self, principal, project_id, source, allowance):
        if not self.store.eligible(principal, project_id, source):
            return [], 0
        try:
            kinds = (
                text_findings(self.judge(source["context"]))
                if source["context"]["shot_action"].strip()
                else []
            )
        except Exception:
            LOG.info("inky_judgement_unavailable")
            kinds = []
        findings, attempted = [], 0
        for kind in kinds:
            if not finding_applies(kind, source["context"]):
                continue
            if kind == "needs_keyframe" and source.get("has_first_frame"):
                continue
            if not self.store.eligible(principal, project_id, source, kind):
                continue
            entry = {"kind": kind}
            if self.examples is not None and attempted < allowance:
                example, called = self.example(principal, project_id, source, kind)
                attempted += called
                if example is not None:
                    entry["example"] = example
            findings.append(entry)
        if (
            self.uploads is not None
            and source.get("upload")
            and self.store.eligible(principal, project_id, source, "upload_card")
        ):
            try:
                entry = self.uploads(principal, project_id, source)
                if entry is not None:
                    findings.append(entry)
            except Exception:
                LOG.info("inky_upload_unavailable")
        return findings, attempted

    def rewrite(self, principal, project_id, source, kind, request_id):
        """A draft of the shot's action text. Nothing is saved: the person decides."""
        reservation = self.budget.reserve(
            tenant=self.store.tenant,
            owner=principal.owner,
            project=project_id,
            source_id=source["source_id"] + ":" + kind + ":rewrite:" + request_id,
            max_cost_micros=self.examples.max_cost_micros,
            purpose="rewrite",
        )
        if not reservation["allowed"]:
            LOG.info(reservation["reason"])
            raise InkyRewriteUnavailable(reservation["reason"])
        try:
            response = self.examples.rewrite(source["context"], kind)
            actual = response.get("actual_micros")
            if type(actual) is int and actual >= 0:
                settled = self.budget.settle(reservation["call_id"], actual)
                LOG.info("inky_rewrite_usage", extra={"inky_cost_micros": actual})
                if settled["overrun"]:
                    LOG.error("inky_example_budget")
            else:
                LOG.warning("inky_rewrite_usage_unknown")
            return rewrite_text(response.get("text"), source["context"]["lang"])
        except Exception:
            LOG.info("inky_rewrite_failed")
            raise InkyRewriteFailed() from None

    def example(self, principal, project_id, source, kind):
        # The trusted adapter, never user input, supplies an enforced ceiling.
        reservation = self.budget.reserve(
            tenant=self.store.tenant,
            owner=principal.owner,
            project=project_id,
            source_id=source["source_id"] + ":" + kind,
            max_cost_micros=self.examples.max_cost_micros,
        )
        if not reservation["allowed"]:
            LOG.info(reservation["reason"])
            return None, 0
        try:
            response = self.examples(source["context"], kind)
            actual = response.get("actual_micros")
            if type(actual) is int and actual >= 0:
                settled = self.budget.settle(reservation["call_id"], actual)
                LOG.info("inky_example_usage", extra={"inky_cost_micros": actual})
                if settled["overrun"]:
                    LOG.error("inky_example_budget")
            else:
                LOG.warning("inky_example_usage_unknown")
            return example_text(response.get("text"), source["context"]["lang"]), 1
        except Exception:
            LOG.info("inky_example_unavailable")
            return None, 1
