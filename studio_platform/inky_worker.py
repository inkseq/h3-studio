"""Bounded background orchestration. Provider adapters must be injected explicitly.

No endpoint or module import starts work. Unknown paid calls are never retried.
Real adapters must enforce their quoted worst-case cost before this can activate.
"""

import logging

from .inky_budget import InkyBudget
from .inky_models import example_text, text_findings
from .inky_source import sources
from .repository import Conflict, NotFound

LOG = logging.getLogger(__name__)


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
            with self.store.repo.engine.connect() as conn:
                ids = list(sources(self.store.project(conn, principal, project_id)))
            processed, allowance = 0, 3
            for shot_id in ids:
                source = self.store.claim(principal, project_id, shot_id)
                if source is None:
                    continue
                findings, used = self.evaluate(principal, project_id, source, allowance)
                allowance -= used
                self.store.publish(principal, project_id, source, findings)
                processed += 1
                if processed == 3:
                    break
        except (Conflict, NotFound):
            LOG.info("inky_source_unavailable")
        except Exception:
            # Never log upstream exception strings, URLs, prompts or credentials.
            LOG.warning("inky_background_unavailable")

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
