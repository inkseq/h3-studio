"""Synthetic finite production controller; no provider/SSH/paid generation calls.

LedgerCase also runs against explicitly opted-in loopback PostgreSQL, one unique
schema per test. Real cloud acceptance remains separate from these contracts.
"""

from dataclasses import asdict, replace
import contextlib
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import select, update

from studio_platform.autoscale import ScalePolicy
from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.lium_provider import LiumManifest
from studio_platform.production_scaler import (
    FiniteConfig,
    FiniteController,
    MODEL,
    RECIPE,
    ScalerError,
    main,
    read_config,
    stdin_loader,
    validate_settings,
    verify_policy,
    verify_sources,
    job_scope_filter,
    job_scope_allowed,
)
from studio_platform.production_scaler_boot import ProductionBoot
from studio_platform.repository import Scope, capacity_approvals, jobs, request_hash
from studio_platform.scaler import LaunchSpec, ProviderFact
from studio_platform.settings import Settings
from studio_platform.worker import Outcome
from test_platform_api import generation_request
from test_platform_execution_policy import policy
from test_platform_lium_bootstrap import FakeBackend, FakeHost
from test_platform_lium_provider import PUBLIC_KEY
from test_platform_repository import LedgerCase


ARN = "arn:aws:secretsmanager:ap-southeast-1:829135631045:secret:/sixnine/platform/lium-ABC123"
VERSION = "synthetic-version-" + "0" * 20


def configuration(root, now=1000):
    sources = Path(__file__).resolve().parents[1]
    scale = ScalePolicy(
        dry_run=False,
        max_instances=2,
        max_physical_gpus=2,
        queue_target_s=0,
        min_improvement_s=1,
        cold_start_s=60,
        cooldown_s=0,
        idle_before_drain_s=900,
        approved_remaining_microusd=6_000_000,
        instance_reservation_microusd=2_000_000,
        hard_deadline=now + 7200,
    )
    offers = [str(uuid.UUID(int=10)), str(uuid.UUID(int=11))]
    template = str(uuid.UUID(int=12))
    launches = [
        asdict(
            LaunchSpec("lium", "finite-config", MODEL, offer_id=x, image_id=template)
        )
        for x in offers
    ]
    manifests = [
        asdict(
            LiumManifest(
                "finite-config",
                MODEL,
                x,
                template,
                1,
                1_000_000,
                2,
                PUBLIC_KEY,
                now + 7200,
                allow_preflight_only_price_cap=True,
            )
        )
        for x in offers
    ]
    return FiniteConfig(
        1,
        True,
        "synthetic-finite",
        "sixnine",
        "superdan",
        "story-one",
        "finite-pool",
        "finite-config",
        "finite-approval",
        ["finite-budget"],
        now,
        now + 7200,
        1200,
        120,
        root / "control",
        root / "data",
        sources,
        root / "synthetic-key",
        root / "known_hosts",
        False,
        19300,
        {
            name: hashlib.sha256((sources / name).read_bytes()).hexdigest()
            for name in ("bootstrap_cloud.py", "model_manifest.json")
        },
        "0" * 64,
        "synthetic-50-step-evidence",
        ARN,
        VERSION,
        asdict(scale),
        launches,
        manifests,
    )


def as_json(config):
    value = asdict(config)
    if value["allowed_owners"] is None:
        value.pop("allowed_owners")
    if value["qualification_profile"] == "fl50":
        value.pop("qualification_profile")
    if value["authorization_extension_s"] == 0:
        value.pop("authorization_extension_s")
    if value['execution_backend'] == 'comfy-worker':
        value.pop('execution_backend')
        value.pop('engine_manifest_digest')
    if value['service_policy'] is None:
        value.pop('service_policy')
    if not value['output_delivery']:
        value.pop('output_delivery')
    if value['provider_preparation_timeout_s'] is None:
        value.pop('provider_preparation_timeout_s')
    if value['provider_preparation_failure_limit'] == 2:
        value.pop('provider_preparation_failure_limit')
    if not value['execution_recipe_id']:
        value.pop('execution_recipe_id')
    for field in (
        "work_dir",
        "data_dir",
        "source_dir",
        "ssh_key_file",
        "known_hosts_file",
    ):
        value[field] = str(value[field])
    return value


class FakeProvider:
    provider_id = "lium"
    enabled = True

    def __init__(self, clock):
        self.clock, self.creates, self.destroys, self.facts = clock, [], [], {}
        self.uncertain = False
        self.unknown = False

    def create(self, tag, launch, **kwargs):
        self.creates.append((tag, launch))
        fact = ProviderFact("running", str(uuid.uuid4()))
        self.facts[tag] = fact
        if self.uncertain:
            raise TimeoutError("synthetic lost response")
        return fact

    def reconcile(self, tag, instance):
        if self.unknown:
            return ProviderFact("unknown", instance)
        fact = self.facts.get(tag, ProviderFact("unknown", instance))
        return (
            replace(fact, idle_confirmed=True, idle_since=self.clock() - 30)
            if fact.state == "running"
            else fact
        )

    def destroy(self, tag, instance):
        self.destroys.append(tag)
        self.facts[tag] = ProviderFact("destroyed", instance)
        return self.facts[tag]

    def billing(self, *args):
        return None

    def lifetime(self, tag, instance, **kwargs):
        return {"instance_id": instance, "safe_deadline": self.clock() + 6000}


class FakeBoot:
    def __init__(self, repo, provider, config, intent, port, **kwargs):
        self.repo, self.config, self.intent, self.port = repo, config, intent, port
        self.drained = False
        self.closed = False
        self.worker = "lium-" + intent["id"].replace("-", "")
        self.control = WorkerControl(repo)
        self.control.register(
            WorkerSpec(
                self.worker,
                config.pool,
                "lium",
                intent["provider_instance_id"],
                ("GPU-" + intent["id"],),
                (RECIPE,),
                MODEL,
                config.configuration_id,
            )
        )
        self.control.mark_ready(self.worker, upstream_idle_confirmed=True)

    def tick(self, intent, *, stopping=False):
        if stopping:
            self.request_drain()
        worker = self.control.get(self.worker)
        if worker["expires_at"] > self.repo.clock() and worker["state"] != "retired":
            self.control.heartbeat(self.worker, worker["fence"])
        return {"state": "draining" if self.drained else "fleet_running"}

    def request_drain(self):
        self.drained = True
        self.control.drain(self.worker)

    def children_done(self):
        return self.drained

    def close_if_safe(self, **kwargs):
        self.closed = self.drained
        return self.closed


class FiniteTests(LedgerCase):
    def test_multimodal_policy_requires_explicit_runtime_profile_and_exact_recipe_set(
        self,
    ):
        from studio_platform.qualification_profiles import (
            MULTIMODAL_PROFILE,
            MULTIMODAL_INPUT_LIMITS,
        )

        value = json.loads(json.dumps(self.value))
        config = replace(self.config, qualification_profile=MULTIMODAL_PROFILE)
        value["recipe_ids"] = list(config.recipe_ids)
        value["qualification"].update(
            status="runtime_required", profile=MULTIMODAL_PROFILE
        )
        value["reservation"]["expected_runtime_s"] = 1800
        value["envelope"].update(
            max_reference_files=3,
            max_guides=1,
            allow_first_last=True,
            input_limits=dict(MULTIMODAL_INPUT_LIMITS),
        )

        def verify(changed, configuration=config):
            self.path.write_text(json.dumps(changed))
            return verify_policy(
                replace(configuration, execution_policy_sha256=request_hash(changed)),
                self.settings,
            )

        self.assertEqual(verify(value), value)
        with self.assertRaises(Exception):
            verify(value, self.config)
        for field, replacement in (("status", "accepted"), ("profile", "different")):
            bad = json.loads(json.dumps(value))
            bad["qualification"][field] = replacement
            with self.subTest(field=field), self.assertRaises(Exception):
                verify(bad)
        bad = json.loads(json.dumps(value))
        bad["recipe_ids"] = [RECIPE]
        with self.assertRaises(Exception):
            verify(bad)
        for key, replacement in (
            ("max_image_pixels", 2048 * 2048 + 1),
            ("max_videos", 2),
            ("guide_recipe_ids", [RECIPE]),
            ("max_video_duration_seconds", 5),
        ):
            bad = json.loads(json.dumps(value))
            bad["envelope"]["input_limits"][key] = replacement
            with self.subTest(limit=key), self.assertRaises(Exception):
                verify(bad)
        bad = json.loads(json.dumps(value))
        bad["reservation"]["expected_runtime_s"] = 300
        with self.assertRaises(Exception):
            verify(bad)

    def setUp(self):
        super().setUp()
        self.scope = Scope("sixnine", "superdan", "story-one")
        self.root = Path(self.temp.name)
        self.config = configuration(self.root, self.now)
        self.value = policy(self.now)
        self.value.update(
            pool=self.config.pool,
            configuration_id=self.config.configuration_id,
            recipe_ids=[RECIPE],
            budget_accounts=["job-budget"],
        )
        self.value["qualification"].update(
            evidence_id=self.config.qualification_evidence_id,
            expires_at=self.now + 7000,
        )
        self.value["reservation"].update(
            expected_runtime_s=300, expires_at=self.now + 7000
        )
        self.value["envelope"].update(
            max_duration_seconds=6,
            max_reference_files=0,
            max_guides=0,
            allow_first_last=False,
        )
        self.value["envelope"]["controls"].update(
            video_decode=["tiled"], encoder_device=["cpu"], ref_image_size=["max"]
        )
        self.path = self.root / "policy.json"
        self.path.write_text(json.dumps(self.value))
        self.path.chmod(0o600)
        self.config = replace(
            self.config, execution_policy_sha256=request_hash(self.value)
        )
        self.settings = Settings(
            self.config.data_dir,
            database_url=self.url,
            auth_mode="password",
            public_origin="https://www.sixnine.art",
            generation_enabled=True,
            execution_backend="comfy-worker",
            execution_policy_file=self.path,
        )
        self.repo.configure_capacity(max_instances=2, max_physical_gpus=2)
        self.repo.configure_pool(self.config.pool, max_instances=2, max_physical_gpus=2)
        for name, limit in (("finite-budget", 6_000_000), ("job-budget", 30_000_000)):
            self.repo.configure_budget(name, tenant_id="sixnine", limit_microusd=limit)
        self.provider = FakeProvider(lambda: self.now)
        self.controller = FiniteController(
            self.repo,
            self.settings,
            self.config,
            provider=self.provider,
            boot_factory=FakeBoot,
        )
        self.policies = ExecutionPolicies(self.settings, self.repo)
        self.repo.approve_capacity(
            self.config.capacity_approval_id,
            tenant_id="sixnine",
            pool=self.config.pool,
            model_id=MODEL,
            configuration_id=self.config.configuration_id,
            recipe_ids=[RECIPE],
            policy_hash=request_hash(self.value),
            qualification_evidence_id=self.config.qualification_evidence_id,
            qualification_expires_at=self.now + 7000,
            quote_expires_at=self.now + 7000,
            expires_at=self.now + 7000,
            launch=LaunchSpec(**self.config.launches[0]),
            scale_policy=ScalePolicy(**self.config.scale_policy),
            budget_scope=self.scope,
            budget_account_ids=self.config.budget_account_ids,
            enabled=True,
        )
        self.controller.initialize()

    def waiting(self, key="first", scope=None):
        request = generation_request()
        request["controls"].update(
            duration=5,
            steps=50,
            resolution="768P",
            video_decode="tiled",
            encoder_device="cpu",
        )
        compiled, fingerprint = compile_request(request, lambda _: None)
        scope = scope or self.scope
        admission = self.policies.evaluate(compiled, scope, fingerprint)
        self.assertTrue(admission.execution["enabled"], admission.execution)
        plan = self.repo.create_plan(
            scope,
            compiled,
            admission.execution,
            expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost,
        )
        return self.repo.create_job(
            scope,
            plan["id"],
            key,
            initial_status=admission.execution["admission_state"],
            budget_account_ids=admission.execution["budget_account_ids"],
        )

    def start_one(self):
        job = self.waiting()
        self.controller.tick()
        self.now += 16
        self.controller.tick()
        self.assertEqual(len(self.provider.creates), 1)
        self.now += 16
        self.controller.tick()
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")
        return job

    def test_cold_same_job_then_real_pg_demands_scale_second_and_stable_ports(self):
        job = self.start_one()
        for n in range(8):
            self.waiting("queued-" + str(n))
        for _ in range(3):
            self.now += 16
            self.controller.tick()
        self.assertEqual(len(self.provider.creates), 2)
        ports = {i: self.controller.port_for(i) for i in self.controller.boots}
        self.assertEqual(set(ports.values()), {19300, 19301})
        retry = FiniteController(
            self.repo,
            self.settings,
            self.config,
            provider=self.provider,
            boot_factory=FakeBoot,
        )
        retry.initialize()
        self.assertEqual({i: retry.port_for(i) for i in reversed(ports)}, ports)
        self.assertEqual(retry.tick()["phase"], "not_leader")
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(
            self.repo.get_budget("finite-budget")["reserved_microusd"], 4_000_000
        )

    def test_stop_drains_destroyed_facts_pending_bill_remains_reserved(self):
        self.start_one()
        self.controller.request_drain()
        self.now += 16
        result = self.controller.tick()
        self.assertTrue(result["all_destroyed"])
        self.assertTrue(result["ledger_safe"])
        self.assertTrue(result["drained"])
        self.assertEqual(result["billing_pending"], 1)
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(
            self.repo.get_budget("finite-budget")["reserved_microusd"], 2_000_000
        )
        self.assertTrue(all(b.closed for b in self.controller.boots.values()))

    def test_unknown_create_never_reposts_or_rents_replacement(self):
        self.provider.uncertain = self.provider.unknown = True
        self.waiting()
        self.controller.tick()
        for _ in range(5):
            self.now += 16
            self.controller.tick()
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(
            self.repo.list_instance_intents()[0]["state"], "creation_unknown"
        )
        self.controller.request_drain()
        self.assertFalse(self.controller.tick()["drained"])
        self.assertFalse(self.provider.destroys)

    def test_quarantined_rental_never_constructs_boot_or_activates_waiter(self):
        self.provider.execution_allowed = lambda tag, instance: False
        self.controller.boot_factory = lambda *args, **kwargs: self.fail(
            "Quarantined rental booted"
        )
        job = self.waiting()
        for _ in range(4):
            value = self.controller.tick()
            self.now += 16
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.controller.boots, {})
        self.assertEqual(value["reason"], "creation_needs_reconciliation")
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(
            (current["status"], current["error_code"]),
            ("waiting_capacity", "capacity_rental_reconciliation"),
        )
        self.assertGreater(
            self.repo.get_budget("finite-budget")["reserved_microusd"], 0
        )

    def test_other_owner_cold_waiter_fails_closed_before_provider(self):
        self.waiting(scope=Scope("sixnine", "supervan", "story-one"))
        with self.assertRaisesRegex(ScalerError, "scope_mismatch"):
            self.controller.tick()
        self.assertFalse(self.provider.creates)

    def test_explicit_shared_scope_admits_both_accounts_across_stories_and_cancels_real_scopes(
        self,
    ):
        shared = replace(
            self.config,
            allowed_owners=["superdan", "supervan"],
            work_dir=self.root / "shared-control",
        )
        controller = FiniteController(
            self.repo,
            self.settings,
            shared,
            provider=self.provider,
            boot_factory=FakeBoot,
        )
        controller.initialize()
        scopes = [
            Scope("sixnine", "superdan", "new-story-a"),
            Scope("sixnine", "supervan", "new-story-b"),
        ]
        waiting = [
            self.waiting("shared-" + scope.owner_id, scope=scope) for scope in scopes
        ]
        controller.tick()
        self.now += 16
        controller.tick()
        self.now += 16
        controller.tick()
        self.assertEqual(len(self.provider.creates), 1)
        with self.repo.engine.connect() as conn:
            selected = set(
                conn.execute(
                    select(jobs.c.id).where(job_scope_filter(shared))
                ).scalars()
            )
        self.assertEqual(selected, {job["id"] for job in waiting})
        for scope, job in zip(scopes, waiting):
            current = self.repo.get_job(scope, job["id"])
            self.assertEqual(current["status"], "queued")
            self.assertTrue(controller.job_allowed(current))
        controller.request_drain()
        self.now += 16
        self.assertTrue(controller.tick()["drained"])
        for scope, job in zip(scopes, waiting):
            self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "cancelled")
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"], 0)

    def test_shared_scope_filters_unknown_owner_other_tenant_pool_configuration(self):
        shared = replace(self.config, allowed_owners=["superdan", "supervan"])
        valid = {
            "tenant_id": "sixnine",
            "owner_id": "supervan",
            "project_id": "arbitrary-story",
            "pool": shared.pool,
            "execution_plan": {"configuration_id": shared.configuration_id},
        }
        self.assertTrue(job_scope_allowed(shared, valid))
        self.assertFalse(job_scope_allowed(self.config, valid))
        for change in (
            {"owner_id": "another-user"},
            {"tenant_id": "another-tenant"},
            {"pool": "another-pool"},
            {"execution_plan": {"configuration_id": "another-config"}},
        ):
            self.assertFalse(job_scope_allowed(shared, {**valid, **change}))
        scopes = [
            Scope("sixnine", owner, "arbitrary-story")
            for owner in ("superdan", "supervan", "another-user")
        ]
        inserted = []
        for number, scope in enumerate(
            scopes + [Scope("another-tenant", "superdan", "arbitrary-story")]
        ):
            plan = self.repo.create_plan(
                scope,
                {},
                valid["execution_plan"] | {"pool": shared.pool},
                expires_at=self.now + 1000,
            )
            inserted.append(
                self.repo.create_job(scope, plan["id"], "filter-" + str(number))
            )
        with self.repo.engine.connect() as conn:
            selected = set(
                conn.execute(
                    select(jobs.c.id).where(job_scope_filter(shared))
                ).scalars()
            )
        self.assertEqual(selected, {job["id"] for job in inserted[:2]})

    def test_no_synthetic_demand_or_budget_raise(self):
        before = self.repo.get_budget("finite-budget")
        for _ in range(3):
            self.now += 16
            self.controller.tick()
        self.assertFalse(self.provider.creates)
        self.assertEqual(self.repo.get_budget("finite-budget"), before)

    def test_stop_before_bootstrap_rejects_waiter_releases_only_job_reservation(self):
        job = self.waiting()
        self.assertFalse(self.controller.status()["ledger_safe"])
        self.controller.request_drain()
        result = self.controller.tick()
        self.assertTrue(result["drained"])
        self.assertFalse(self.provider.creates)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "failed")
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"], 0)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(capacity_approvals.c.enabled)).scalar_one(), 0
            )

    def test_qualification_failure_stops_then_closes_waiter_without_attempt(self):
        class FailingBoot(FakeBoot):
            def __init__(self, repo, provider, config, intent, port, **kwargs):
                self.repo, self.config, self.intent, self.port = (
                    repo,
                    config,
                    intent,
                    port,
                )
                self.drained = self.closed = False

            def tick(self, *args, **kwargs):
                return {"state": "qualification_failed"}

            def request_drain(self):
                self.drained = True

        self.controller.boot_factory = FailingBoot
        job = self.waiting()
        self.controller.tick()
        self.now += 16
        self.controller.tick()
        self.assertTrue(self.controller.stopping())
        self.assertFalse(self.controller.status()["drained"])
        self.now += 16
        self.assertTrue(self.controller.tick()["drained"])
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "failed")

    def test_stopped_restart_retains_cleanup_after_policy_revoked(self):
        self.start_one()
        self.repo.set_capacity_approval_enabled(
            self.config.capacity_approval_id, enabled=False
        )
        value = dict(self.value, enabled=False)
        self.path.write_text(json.dumps(value))
        retry = FiniteController(
            self.repo,
            self.settings,
            self.config,
            provider=self.provider,
            boot_factory=FakeBoot,
        )
        retry.initialize()
        self.assertTrue(retry.stopping())
        self.assertEqual(len(self.provider.creates), 1)

    def test_revocation_stops_new_but_reconciles_existing(self):
        self.start_one()
        self.repo.set_capacity_approval_enabled(
            self.config.capacity_approval_id, enabled=False
        )
        self.now += 16
        result = self.controller.tick()
        self.assertTrue(self.controller.stopping())
        self.assertTrue(result["all_destroyed"])
        self.assertEqual(len(self.provider.creates), 1)

    def test_running_attempt_blocks_destruction_and_reservation_release(self):
        job = self.start_one()
        boot = next(iter(self.controller.boots.values()))
        claim = boot.control.claim(boot.worker, self.config.pool)
        self.assertIsNotNone(claim)
        from studio_platform.queue import TaskQueue

        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        self.controller.request_drain()
        self.now += 16
        result = self.controller.tick()
        self.assertFalse(result["drained"])
        self.assertIn(job["id"], result["active_job_ids"])
        self.assertFalse(self.provider.destroys)
        self.assertEqual(
            self.repo.get_budget("finite-budget")["reserved_microusd"], 2_000_000
        )

    def test_terminal_job_with_unresolved_attempt_is_not_safe_restore_or_idle(self):
        job = self.start_one()
        boot = next(iter(self.controller.boots.values()))
        claim = boot.control.claim(boot.worker, self.config.pool)
        from studio_platform.queue import TaskQueue

        TaskQueue(self.repo).begin_submission(claim.lease)
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="failed")
            )
        self.assertIn(
            job["id"], self.controller.status(fresh_ledger_only=True)["active_job_ids"]
        )
        intent = self.repo.list_instance_intents()[0]
        with self.assertRaisesRegex(ScalerError, "attempt_still"):
            self.controller.idle_probe(intent["id"], intent["provider_instance_id"])

    def test_config_budget_ceiling_is_not_new_account_and_sqlite_rejected_by_entry(
        self,
    ):
        if self.repo.engine.dialect.name == "sqlite":
            with self.assertRaisesRegex(ScalerError, "postgres"):
                validate_settings(self.config, self.settings)
        lower = replace(
            self.config,
            scale_policy={
                **self.config.scale_policy,
                "approved_remaining_microusd": 3_000_000,
            },
        )
        self.assertEqual(
            FiniteController(
                self.repo, self.settings, lower, provider=self.provider
            ).remaining_budget(),
            3_000_000,
        )
        self.assertEqual(
            self.repo.get_budget("finite-budget")["limit_microusd"], 6_000_000
        )

    def test_policy_full50_and_source_exactness(self):
        verify_sources(self.config)
        verify_policy(self.config, self.settings)
        value = json.loads(json.dumps(self.value))
        value["envelope"]["max_reference_files"] = 1
        self.path.write_text(json.dumps(value))
        config = replace(self.config, execution_policy_sha256=request_hash(value))
        with self.assertRaisesRegex(ScalerError, "fl50"):
            verify_policy(config, self.settings)
        bad = replace(
            self.config,
            source_sha256={**self.config.source_sha256, "bootstrap_cloud.py": "0" * 64},
        )
        with self.assertRaisesRegex(ScalerError, "source_hash"):
            verify_sources(bad)


class ConfigAndCredentialTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration(self.root)

    def test_shared_approval_window_extends_only_explicit_pair_not_provider_ttl(self):
        deadline = self.config.created_at + 24 * 3600
        change = {
            "hard_deadline": deadline,
            "scale_policy": {**self.config.scale_policy, "hard_deadline": deadline},
        }
        with self.assertRaisesRegex(ScalerError, "deadline"):
            replace(self.config, **change)
        shared = replace(self.config, allowed_owners=["superdan", "supervan"], **change)
        self.assertEqual(shared.hard_deadline, deadline)
        self.assertEqual(shared.scope, self.config.scope)
        self.assertNotEqual(shared.fingerprint(), self.config.fingerprint())
        for owners in (
            [],
            ["superdan"],
            ["supervan", "supervan"],
            ["superdan", "other"],
            "superdan",
        ):
            with self.assertRaisesRegex(ScalerError, "owners"):
                replace(self.config, allowed_owners=owners)
        manifests = [dict(row, termination_hours=24) for row in shared.manifests]
        with self.assertRaisesRegex(ScalerError, "manifest"):
            replace(shared, manifests=manifests)

    def test_legacy_fingerprint_remains_without_optional_shared_field(self):
        legacy = as_json(self.config)
        self.assertNotIn("allowed_owners", legacy)
        self.assertEqual(self.config.fingerprint(), request_hash(legacy))

    def test_provider_preparation_deadline_is_explicit_validated_and_omitted_when_disabled(
        self,
    ):
        self.assertNotIn('provider_preparation_timeout_s', as_json(self.config))
        self.assertNotIn('provider_preparation_failure_limit', as_json(self.config))
        selected = replace(self.config, provider_preparation_timeout_s=1800)
        self.assertNotEqual(selected.fingerprint(), self.config.fingerprint())
        self.assertEqual(selected.scale_policy, self.config.scale_policy)
        for invalid in (True, 0, -1, 119, 7201, 1800.0, float('nan'), '1800'):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ScalerError, 'provider_preparation_timeout'),
            ):
                replace(self.config, provider_preparation_timeout_s=invalid)
        for invalid in (True, 0, -1, 6, 2.0, None, '2'):
            with (
                self.subTest(limit=invalid),
                self.assertRaisesRegex(
                    ScalerError, 'provider_preparation_failure_limit'
                ),
            ):
                replace(selected, provider_preparation_failure_limit=invalid)
        with self.assertRaisesRegex(ScalerError, 'provider_preparation_failure_limit'):
            replace(self.config, provider_preparation_failure_limit=1)
        self.assertNotEqual(
            replace(selected, provider_preparation_failure_limit=1).fingerprint(),
            selected.fingerprint(),
        )

    def envelope(self):
        return {
            "secret_arn": ARN,
            "version_id": VERSION,
            "payload": {
                "schema_version": 1,
                "service": "lium",
                "profile": "lium--rig-root",
                "base_url": "https://lium.io/api",
                "primary_key_variable": "LIUM_API_KEY",
                "api_key": "SYNTHETIC-NEVER-A-REAL-CREDENTIAL",
            },
        }

    def test_default_disabled_does_not_read_stdin_settings_or_provider(self):
        with (
            patch(
                "studio_platform.production_scaler.Settings.from_environment",
                side_effect=AssertionError,
            ),
            patch(
                "studio_platform.production_scaler.LiumProvider",
                side_effect=AssertionError,
            ),
            patch("sys.stdin", None),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(main([]), 0)
        self.assertIn('"phase": "disabled"', out.getvalue())

    def test_credential_once_no_sdk_output_file_or_env(self):
        payload = self.envelope()
        with (
            patch(
                "studio_platform.lium_runtime_aws._client", side_effect=AssertionError
            ),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            loader = stdin_loader(self.config, io.BytesIO(json.dumps(payload).encode()))
            loaded = loader("lium", profile="lium--rig-root")
            self.assertEqual(loaded.api_key, payload["payload"]["api_key"])
            self.assertNotIn(loaded.api_key, repr(loaded))
            self.assertIs(loader("lium", profile="lium--rig-root"), loaded)
            loader.close()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_secret_metadata_unknown_duplicate_and_oversize_fail_static(self):
        payload = self.envelope()
        malformed = [
            b"{SYNTHETIC-NEVER-A-REAL-CREDENTIAL}",
            b"x" * 24577,
            json.dumps(
                {**payload, "extra": "SYNTHETIC-NEVER-A-REAL-CREDENTIAL"}
            ).encode(),
            json.dumps({**payload, "version_id": "other"}).encode(),
            json.dumps(payload)
            .replace('"service": "lium"', '"service":"other","service":"lium"')
            .encode(),
        ]
        for raw in malformed:
            with self.subTest(length=len(raw)), self.assertRaises(ScalerError) as error:
                stdin_loader(self.config, io.BytesIO(raw))
            self.assertEqual(str(error.exception), "finite_credential_envelope_invalid")

    def test_config_unknown_secrets_and_nonfinite_deadline_rejected(self):
        for key, value in (("api_key", "SYNTHETIC"), ("hard_deadline", float("nan"))):
            path = self.root / "config.json"
            path.write_text(json.dumps({**as_json(self.config), key: value}))
            path.chmod(0o600)
            with self.assertRaisesRegex(ScalerError, "config_unavailable"):
                read_config(path)

    def test_disabled_validation_fingerprint_matches_explicit_canonical_config(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(as_json(self.config)))
        path.chmod(0o600)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--config", str(path)]), 0)
        self.assertEqual(
            json.loads(out.getvalue())["config_hash"],
            request_hash(as_json(self.config)),
        )


class ProductionBootTests(LedgerCase):
    def multimodal(self):
        from studio_platform.qualification_profiles import MULTIMODAL_PROFILE
        from studio_platform.lium_multimodal_smoke import (
            FirstLastSmoke,
            BoundedReferenceSmoke,
        )

        self.config = replace(self.config, qualification_profile=MULTIMODAL_PROFILE)
        self.boot.finite = self.config
        self.boot.config = replace(self.boot.config, recipe_ids=self.config.recipe_ids)
        self.stage_outcomes = {
            FirstLastSmoke.name: Outcome("running", "firstlast-task"),
            BoundedReferenceSmoke.name: Outcome("running", "reference-task"),
        }
        self.backend.poll = lambda tag, task: next(
            (v for k, v in self.stage_outcomes.items() if tag.startswith(k)),
            Outcome("succeeded", task),
        )

        def api(method, path, **kwargs):
            if path == "/queue":
                return self.backend.queue
            return {
                "name": kwargs["files"]["image"][0],
                "subfolder": "sixnine-qualification",
                "type": "input",
            }

        self.backend._json = api

        def fetch(job, tag, task, directory, heartbeat):
            self.backend.fetches += 1
            paths = {"video": directory / "raw.mp4", "audio": directory / "raw.flac"}
            for kind, path in paths.items():
                path.write_bytes(("synthetic-" + kind).encode())
            return paths

        self.backend.fetch = fetch
        self.boot.verify_smoke = lambda paths, request: {
            "request": request,
            "outputs": {
                kind: {
                    "filename": p.name,
                    "size_bytes": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                }
                for kind, p in paths.items()
            },
        }
        self.enterContext(
            patch(
                "studio_platform.lium_multimodal_smoke.inspect",
                side_effect=lambda p, kind: {
                    "kind": kind,
                    "width": 2048 if kind == "image" else 832,
                    "height": 2048 if kind == "image" else 480,
                    "duration": 107 / 24
                    if kind == "video"
                    else 4.45
                    if kind == "audio"
                    else None,
                    "has_audio": kind != "image",
                },
            )
        )
        self.enterContext(
            patch(
                "studio_platform.lium_multimodal_smoke.probe",
                return_value={
                    "streams": [
                        {
                            "codec_type": "video",
                            "avg_frame_rate": "24/1",
                            "nb_frames": "107",
                        }
                    ]
                },
            )
        )
        self.enterContext(
            patch(
                "studio_platform.lium_multimodal_smoke.ffmpeg",
                side_effect=lambda args: Path(args[-1]).write_bytes(
                    b"synthetic-normalized"
                ),
            )
        )
        return FirstLastSmoke.name, BoundedReferenceSmoke.name

    def test_multimodal_registers_both_recipes_only_after_all_three_qualifications(
        self,
    ):
        first, ref = self.multimodal()
        result = self.boot.tick(self.intent["id"])
        self.assertEqual(
            (result["state"], result["qualification_stage"]),
            ("qualification_running", first),
        )
        self.assertIsNone(self.boot.fleet)
        self.assertEqual(self.backend.submissions, 2)
        self.stage_outcomes[first] = Outcome("succeeded", "firstlast-task")
        result = self.boot.tick(self.intent["id"])
        self.assertEqual(result["qualification_stage"], ref)
        self.assertIsNone(self.boot.fleet)
        self.assertEqual(self.backend.submissions, 3)
        self.stage_outcomes[ref] = Outcome("succeeded", "reference-task")
        process = SimpleNamespace(
            pid=4242, poll=lambda: None, send_signal=lambda _: None
        )
        with patch.object(self.boot, "_popen_impl", return_value=process):
            self.assertEqual(
                self.boot.tick(self.intent["id"])["state"], "fleet_running"
            )
        self.assertEqual(
            self.boot.fleet.config.slots[0].spec.recipe_ids, self.config.recipe_ids
        )
        self.boot.tick(self.intent["id"])
        self.assertEqual(self.backend.submissions, 3)
        receipt = self.boot.config.work_dir / self.intent["id"] / first / "state.json"
        value = json.loads(receipt.read_text())
        value["phase"] = "pending"
        receipt.write_text(json.dumps(value))
        with self.assertRaisesRegex(Exception, "multimodal_evidence_missing"):
            self.boot.tick(self.intent["id"])
        self.assertEqual(self.backend.submissions, 3)

    def test_multimodal_drain_collects_firstlast_without_starting_reference_or_worker(
        self,
    ):
        first, ref = self.multimodal()
        self.boot.tick(self.intent["id"])
        self.boot.request_drain()
        with self.assertRaisesRegex(Exception, "still_unresolved"):
            self.boot.idle_probe(self.intent["id"], self.intent["provider_instance_id"])
        self.stage_outcomes[first] = Outcome("succeeded", "firstlast-task")
        self.assertEqual(
            self.boot.tick(self.intent["id"], stopping=True)["state"], "draining"
        )
        self.assertFalse(
            (
                self.boot.config.work_dir / self.intent["id"] / ref / "state.json"
            ).exists()
        )
        self.assertIsNone(self.boot.fleet)
        self.assertEqual(self.backend.submissions, 2)
        self.assertTrue(
            self.boot.idle_probe(
                self.intent["id"], self.intent["provider_instance_id"]
            ).idle
        )

    def test_multimodal_restarted_draining_controller_reconnects_only_to_collect_ref(
        self,
    ):
        first, ref = self.multimodal()
        self.stage_outcomes[first] = Outcome("succeeded", "firstlast-task")
        self.boot.tick(self.intent["id"])
        self.assertEqual(self.backend.submissions, 3)
        self.stage_outcomes[ref] = Outcome("succeeded", "reference-task")
        recovered = ProductionBoot(
            self.repo,
            self.provider,
            self.config,
            self.intent,
            19300,
            config_path=Path(self.temp.name) / "config.json",
            ssh_factory=lambda *a: self.host,
            backend_factory=lambda **kw: self.backend,
            verify_smoke=self.boot.verify_smoke,
        )
        self.assertEqual(
            recovered.tick(self.intent["id"], stopping=True)["state"], "draining"
        )
        self.assertIsNone(recovered.fleet)
        self.assertEqual(
            (self.backend.submissions, self.host.starts, self.host.uploads), (3, 1, 1)
        )
        self.assertTrue(
            recovered.idle_probe(
                self.intent["id"], self.intent["provider_instance_id"]
            ).idle
        )

    def test_multimodal_suite_reserves_time_before_first_post_and_fails_closed(self):
        first, ref = self.multimodal()
        with self.repo.transaction() as conn:
            from studio_platform.repository import instance_intents

            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == self.intent["id"])
                .values(hard_deadline=self.now + 2800)
            )
        self.assertEqual(
            self.boot.tick(self.intent["id"])["state"],
            "qualification_deadline_insufficient",
        )
        self.assertEqual(self.backend.submissions, 0)
        self.assertIsNone(self.boot.fleet)

    def test_multimodal_firstlast_failure_prevents_reference_and_registration(self):
        first, ref = self.multimodal()
        self.stage_outcomes[first] = Outcome("failed", "firstlast-task")
        self.assertEqual(
            self.boot.tick(self.intent["id"])["state"], "qualification_failed"
        )
        self.assertIsNone(self.boot.fleet)
        self.assertFalse(
            (
                self.boot.config.work_dir / self.intent["id"] / ref / "state.json"
            ).exists()
        )
        self.assertEqual(self.backend.submissions, 2)

    def test_external_drain_during_upload_stops_next_inference_post(self):
        self.multimodal()
        original = self.backend._json

        def api(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if path == "/upload/image":
                (self.config.work_dir / "drain.flag").touch()
            return value

        self.backend._json = api
        result = self.boot.tick(self.intent["id"])
        self.assertEqual(result["state"], "qualification_not_started_draining")
        self.assertEqual(self.backend.submissions, 1)  # Only previously accepted FL.
        self.assertIsNone(self.boot.fleet)

    def test_fl_collected_output_validation_failure_is_terminal(self):
        from studio_platform.lium_bootstrap import BootError

        self.backend.outcome = Outcome("succeeded", "task-test")
        self.boot.verify_smoke = lambda *args: (_ for _ in ()).throw(
            BootError("qualification_media_shape_mismatch")
        )
        self.assertEqual(
            self.boot.tick(self.intent["id"])["state"], "qualification_failed"
        )
        self.assertEqual(
            self.boot.tick(self.intent["id"])["state"], "qualification_failed"
        )
        self.assertEqual(self.backend.submissions, 1)
        self.assertIsNone(self.boot.fleet)

    def setUp(self):
        super().setUp()
        self.config = configuration(Path(self.temp.name), self.now)
        self.config.work_dir.mkdir()
        self.repo.configure_pool(self.config.pool, max_instances=2, max_physical_gpus=2)
        self.intent = self.repo.reserve_instance_intent(
            self.scope,
            self.config.pool,
            "boot",
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=2_000_000,
            hard_deadline=self.now + 7200,
            budget_account_ids=["owner-budget"],
            dry_run=False,
            provider="lium",
        )
        self.repo.update_instance(self.intent["id"], "creating")
        self.repo.update_instance(
            self.intent["id"], "starting", provider_instance_id=str(uuid.uuid4())
        )
        self.intent = self.repo.list_instance_intents()[0]
        self.host, self.backend = FakeHost(), FakeBackend()
        self.provider = FakeProvider(lambda: self.now)
        self.provider.ssh_connection = lambda *args: {"host": "203.0.113.1", "port": 22}
        self.boot = ProductionBoot(
            self.repo,
            self.provider,
            self.config,
            self.intent,
            19300,
            config_path=Path(self.temp.name) / "config.json",
            ssh_factory=lambda *a: self.host,
            backend_factory=lambda **kw: self.backend,
            verify_smoke=lambda paths, request: {"request": request},
        )

    def test_exact_full_smoke_unknown_submission_is_not_repeated(self):
        self.backend.fail_submit = True
        self.boot.tick(self.intent["id"])
        self.boot.tick(self.intent["id"])
        self.assertEqual(self.backend.submissions, 1)
        self.assertGreaterEqual(self.backend.reconciles, 1)
        self.assertIsNone(self.boot.fleet)
        self.assertFalse(self.boot.close_if_safe())

    def test_drain_keeps_pending_qualification_and_does_not_close_tunnel(self):
        self.boot.tick(self.intent["id"])
        self.boot.request_drain()
        result = self.boot.tick(self.intent["id"], stopping=True)
        self.assertEqual(result["state"], "smoke_running")
        self.assertFalse(self.boot.close_if_safe(destroyed=False))
        self.assertIsNotNone(self.boot.host)
        self.assertEqual(self.backend.submissions, 1)

    def test_late_download_does_not_start_50step_qualification_without_runtime_margin(
        self,
    ):
        self.boot.config = replace(self.boot.config, minimum_remaining_s=120)
        self.boot.finite = replace(self.config, drain_margin_s=120)
        with self.repo.transaction() as conn:
            from studio_platform.repository import instance_intents

            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == self.intent["id"])
                .values(hard_deadline=self.now + 500)
            )
        result = self.boot.tick(self.intent["id"])
        self.assertEqual(result["state"], "qualification_deadline_insufficient")
        self.assertEqual(self.backend.submissions, 0)
        self.assertTrue(self.boot._stopping)

    def test_boot_fleet_command_and_child_factory_are_exact_and_no_stdin_credential(
        self,
    ):
        self.backend.outcome = Outcome("succeeded", "task-test")
        process = SimpleNamespace(
            pid=4242, poll=lambda: None, send_signal=lambda value: None
        )
        with patch.object(self.boot, "_popen_impl", return_value=process) as popen:
            result = self.boot.tick(self.intent["id"])
        self.assertEqual(result["state"], "fleet_running")
        argv = popen.call_args.args[0]
        self.assertIn("studio_platform.production_scaler", argv)
        self.assertNotIn("--credential-stdin", argv)
        self.assertEqual(
            popen.call_args.kwargs["stdin"], __import__("subprocess").DEVNULL
        )
        self.assertEqual(
            self.boot.config.minimum_remaining_s, self.config.drain_margin_s
        )
        from studio_platform.production_scaler_boot import run_child

        self.boot.fleet.config.work_dir.mkdir(exist_ok=True, parents=True)

        def fake_slot(fleet, worker_id, settings, **kw):
            from studio_platform.drain_safe_runner import DrainSafeRunner
            from studio_platform.storage import LocalObjectStore

            runner = kw["runner_factory"](
                self.repo,
                LocalObjectStore(Path(self.temp.name) / "objects"),
                Path(self.temp.name) / "child",
                backend=self.backend,
                control=WorkerControl(self.repo),
            )
            self.assertIsInstance(runner, DrainSafeRunner)
            self.assertEqual(
                runner.collection_lock_dir, self.config.work_dir / "collection-lock"
            )
            return 0

        with (
            patch(
                "studio_platform.production_scaler_boot.Repository",
                return_value=SimpleNamespace(
                    engine=self.repo.engine, close=lambda: None, clock=lambda: self.now
                ),
            ),
            patch(
                "studio_platform.production_scaler_boot.run_slot", side_effect=fake_slot
            ),
        ):
            self.assertEqual(
                run_child(
                    self.config,
                    self.intent["id"],
                    self.boot.fleet.config.fingerprint(),
                    Settings(Path(self.temp.name) / "data"),
                ),
                0,
            )

    def test_remaining_1000_seconds_runs_wrapper_drain_and_retires_fresh_idle_child(
        self,
    ):
        self.backend.outcome = Outcome("succeeded", "task-test")
        process = SimpleNamespace(
            pid=4242, poll=lambda: None, send_signal=lambda value: None
        )
        with patch.object(self.boot, "_popen_impl", return_value=process):
            self.boot.tick(self.intent["id"])
        worker = "lium-" + self.intent["id"].replace("-", "")
        self.now += 5000  # Actual provider safe deadline has 1000 seconds left.
        control = WorkerControl(self.repo)
        control.mark_ready(worker, upstream_idle_confirmed=True)
        process.poll = lambda: 0
        result = self.boot.tick(self.intent["id"])
        self.assertEqual(result["state"], "draining")
        self.assertEqual(control.get(worker)["state"], "retired")

    def test_shared_child_preserves_account_scope_policy_and_actual_instance_deadline(
        self,
    ):
        from studio_platform.production_scaler_boot import run_child
        from studio_platform.storage import LocalObjectStore

        self.backend.outcome = Outcome("succeeded", "task-test")
        process = SimpleNamespace(
            pid=4242, poll=lambda: None, send_signal=lambda value: None
        )
        with patch.object(self.boot, "_popen_impl", return_value=process):
            self.boot.tick(self.intent["id"])
        self.boot.fleet.config.work_dir.mkdir(exist_ok=True, parents=True)
        shared = replace(self.config, allowed_owners=["superdan", "supervan"])

        def fake_slot(fleet, worker_id, settings, **kw):
            runner = kw["runner_factory"](
                self.repo,
                LocalObjectStore(Path(self.temp.name) / "objects"),
                Path(self.temp.name) / "child",
                backend=self.backend,
                control=WorkerControl(self.repo),
            )
            base = {
                "tenant_id": "sixnine",
                "owner_id": "superdan",
                "project_id": "different-story",
                "pool": shared.pool,
                "expected_runtime_s": 100,
                "execution_plan": {
                    "configuration_id": shared.configuration_id,
                    "policy_hash": shared.execution_policy_sha256,
                },
            }
            self.assertTrue(runner._allowed_new_job(base))
            self.assertTrue(runner._allowed_new_job({**base, "owner_id": "supervan"}))
            for delta in (
                {"owner_id": "other"},
                {"tenant_id": "other"},
                {"pool": "other"},
                {"expected_runtime_s": 6000},
                {"execution_plan": {**base["execution_plan"], "policy_hash": "0" * 64}},
            ):
                # Use a genuinely different policy hash when fixture hash is zero.
                if "execution_plan" in delta:
                    delta["execution_plan"]["policy_hash"] = "f" * 64
                self.assertFalse(runner._allowed_new_job({**base, **delta}))
            self.now += 5000
            self.assertTrue(runner.stopped())
            return 0

        with (
            patch(
                "studio_platform.production_scaler_boot.Repository",
                return_value=SimpleNamespace(
                    engine=self.repo.engine, close=lambda: None, clock=lambda: self.now
                ),
            ),
            patch(
                "studio_platform.production_scaler_boot.run_slot", side_effect=fake_slot
            ),
        ):
            self.assertEqual(
                run_child(
                    shared,
                    self.intent["id"],
                    self.boot.fleet.config.fingerprint(),
                    Settings(Path(self.temp.name) / "data"),
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
