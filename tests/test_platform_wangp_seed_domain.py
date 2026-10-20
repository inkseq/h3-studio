"""CPU-only regression for the pinned WanGP/NumPy seed domain, no GPU calls."""

import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from comfy_workflow import native_output_spec
from studio_platform.api import create_app
from studio_platform.auth import Principal
from studio_platform.capabilities import compile_request
from studio_platform.inference.protocol import BackendError
from studio_platform.inference.wangp_compiler import H3FL2VACompiler, MAX_SEED
from studio_platform.inference.wangp_contract import EngineManifest, canonical_json
from studio_platform.inference.wangp_profile_dispatch import (
    H3ProfileCompiler,
    validate_prepared,
)
from studio_platform.quick_chat import (
    DEFAULT_SETTINGS,
    EMPTY_INPUTS,
    QuickChatError,
    objects,
)
from studio_platform.repository import jobs
from studio_platform.runtime_catalog import PROFILE_IDS, engine_manifest
from studio_platform.runtime_hosts.wangp import WanGPHost
from studio_platform.runtime_hosts.wangp_launcher import resolve_inputs
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_repository import LedgerCase
from test_platform_wangp_profiles import job_for


HISTORICAL_PRO6000_PROFILE = (
    "h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1"
)


class QuickChatSeedDomainTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.settings = Settings(
            self.root / "data",
            auth_mode="local-test",
            database_url=self.url,
            execution_backend="wangp-worker",
            operator_admin_owners=("superdan",),
            operator_capacity_owners=("superdan",),
        )
        self.app = create_app(self.settings, repository=self.repo)
        self.service = self.app.state.quick_chat
        self.principal = Principal("superdan", "browser")
        self.sid = self.service.create_session(self.principal, {}, "session")[
            "session"
        ]["id"]

    def body(self, *, seed=None, profile=None, copies=1):
        value = {
            **copy.deepcopy(DEFAULT_SETTINGS),
            "prompt": "Synthetic seed fixture",
            "inputs": copy.deepcopy(EMPTY_INPUTS),
            "copies": copies,
        }
        value["controls"]["seed"] = seed
        if profile is not None:
            value["deployment_profile_id"] = profile
        return value

    def test_default_new_revision_uses_32_bits_and_replay_does_not_draw_again(self):
        body = self.body(copies=4)
        with patch(
            "studio_platform.quick_chat.secrets.randbits",
            side_effect=lambda bits: (1 << bits) - 1,
        ) as random:
            original = self.service.save_card(self.principal, self.sid, body, "card")
            replay = self.service.save_card(self.principal, self.sid, body, "card")
        self.assertEqual([call.args for call in random.call_args_list], [(32,)] * 4)
        seeds = [int(item["seed"]) for item in original["revision"]["items"]]
        self.assertEqual(seeds[0], MAX_SEED)
        self.assertEqual(len(set(seeds)), 4)
        self.assertTrue(all(0 <= seed <= MAX_SEED for seed in seeds))
        self.assertEqual(replay, original)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])

    def test_explicit_wangp_boundary_seed_only_applies_to_first_take(self):
        for index, profile in enumerate(
            (None, PROFILE_IDS[0], HISTORICAL_PRO6000_PROFILE)
        ):
            body = self.body(seed=str(MAX_SEED), profile=profile, copies=3)
            with patch(
                "studio_platform.quick_chat.secrets.randbits", side_effect=[17, 29]
            ) as random:
                saved = self.service.save_card(
                    self.principal, self.sid, body, "batch-" + str(index)
                )["revision"]
            self.assertEqual(
                [call.args for call in random.call_args_list], [(32,), (32,)]
            )
            self.assertEqual(saved["controls"]["seed"], str(MAX_SEED))
            self.assertEqual(
                [item["seed"] for item in saved["items"]], [str(MAX_SEED), "17", "29"]
            )

    def test_explicit_out_of_domain_seed_rolls_back_card_and_projection(self):
        before = self.service.get_session(self.principal, self.sid)
        for index, profile in enumerate(
            (None, PROFILE_IDS[0], HISTORICAL_PRO6000_PROFILE)
        ):
            with (
                self.subTest(profile=profile),
                self.assertRaisesRegex(QuickChatError, "wangp_invalid_seed"),
            ):
                self.service.save_card(
                    self.principal,
                    self.sid,
                    self.body(seed=str(MAX_SEED + 1), profile=profile),
                    "invalid-" + str(index),
                )
        self.assertEqual(self.service.get_session(self.principal, self.sid), before)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    select(objects.c.id).where(objects.c.kind.in_(("card", "revision")))
                ).all(),
                [],
            )
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])

    def test_comfy_keeps_uint64_and_old_revision_is_not_migrated_after_backend_switch(
        self,
    ):
        self.service.settings = replace(self.settings, execution_backend="comfy-worker")
        maximum = (1 << 64) - 1
        body = self.body(seed=str(maximum), copies=2)
        with patch(
            "studio_platform.quick_chat.secrets.randbits", return_value=maximum - 7
        ) as random:
            original = self.service.save_card(
                self.principal, self.sid, body, "old-card"
            )
        random.assert_called_once_with(64)
        old = copy.deepcopy(original["revision"])
        self.assertEqual(
            [item["seed"] for item in old["items"]], [str(maximum), str(maximum - 7)]
        )
        with patch(
            "studio_platform.quick_chat.secrets.randbits",
            side_effect=lambda bits: (1 << bits) - 1,
        ) as random:
            fresh = self.service.save_card(
                self.principal, self.sid, self.body(), "comfy-random"
            )
        random.assert_called_once_with(64)
        self.assertEqual(fresh["revision"]["items"][0]["seed"], str(maximum))
        self.service.settings = self.settings
        with patch(
            "studio_platform.quick_chat.secrets.randbits",
            side_effect=AssertionError("immutable replay must not redraw"),
        ):
            self.assertEqual(
                self.service.get_revision(self.principal, self.sid, old["id"]), old
            )
            self.assertEqual(
                self.service.save_card(self.principal, self.sid, body, "old-card"),
                original,
            )
        revised = self.service.save_card(
            self.principal,
            self.sid,
            {
                **self.body(seed="42"),
                "expected_card_version": 1,
                "source_revision_id": old["id"],
            },
            "corrected-revision",
            card_id=original["card"]["id"],
        )["revision"]
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["items"][0]["seed"], "42")
        self.assertEqual(
            self.service.get_revision(self.principal, self.sid, old["id"]), old
        )

    def test_selected_wangp_profile_does_not_inherit_disabled_backend_uint64_domain(
        self,
    ):
        self.service.settings = replace(self.settings, execution_backend="disabled")
        with patch(
            "studio_platform.quick_chat.secrets.randbits",
            side_effect=lambda bits: (1 << bits) - 1,
        ) as random:
            value = self.service.save_card(
                self.principal, self.sid, self.body(profile=PROFILE_IDS[0]), "profile"
            )
        random.assert_called_once_with(32)
        self.assertEqual(value["revision"]["items"][0]["seed"], str(MAX_SEED))

    def test_discovery_exposes_actual_wangp_seed_limit_for_recipes_and_profiles(self):
        with TestClient(self.app) as client:
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            catalog = client.get("/v1/capabilities").json()
        for recipe in catalog["recipes"]:
            self.assertEqual(
                recipe["controls"]["seed"]["maximum_decimal"], str(MAX_SEED)
            )
        for profile in catalog["deployment_profiles"]:
            for mode in profile["generation_support"]:
                controls = profile["generation_support"][mode]["controls"]
                self.assertEqual(controls["seed"]["maximum_decimal"], str(MAX_SEED))


class DirectAndRuntimeSeedDomainTests(unittest.TestCase):
    def test_blank_direct_seed_uses_backend_domain_and_compiles_without_masking(self):
        for backend, profile, bits in (
            ("wangp-worker", None, 32),
            ("wangp-worker", PROFILE_IDS[0], 32),
            ("comfy-worker", None, 64),
        ):
            with self.subTest(backend=backend, profile=profile):
                body = generation_request(
                    controls={"duration": 5, "resolution": "480P", "seed": None}
                )
                if profile is not None:
                    body["deployment_profile_id"] = profile
                with patch(
                    "studio_platform.capabilities.secrets.randbits",
                    side_effect=lambda count: (1 << count) - 1,
                ) as random:
                    compiled, _ = compile_request(body, lambda _: None, backend=backend)
                random.assert_called_once_with(bits)
                self.assertEqual(compiled["request"]["seed"], str((1 << bits) - 1))

    def test_explicit_wangp_seed_outside_domain_rejects_direct_admission_before_asset_lookup(
        self,
    ):
        for profile in (None, *PROFILE_IDS):
            with self.subTest(profile=profile):
                body = generation_request(
                    controls={
                        "duration": 5,
                        "resolution": "480P",
                        "seed": str(MAX_SEED + 1),
                    }
                )
                if profile is not None:
                    body["deployment_profile_id"] = profile
                before = copy.deepcopy(body)
                looked_up = []
                with self.assertRaisesRegex(ValueError, "wangp_invalid_seed"):
                    compile_request(
                        body,
                        lambda identity: looked_up.append(identity),
                        backend="wangp-worker",
                    )
                self.assertEqual(looked_up, [])
                self.assertEqual(body, before)

    def test_native_prepared_invalid_seed_never_enters_journal_or_dispatch(self):
        # Seed validation depends on model support, not historical hardware cases.
        case = {"mode": "fl", "height": 480, "steps": 20, "input_roles": []}
        for profile_id in PROFILE_IDS:
            job, manifest = job_for(profile_id, case)
            prepared = H3ProfileCompiler(manifest, lambda item, *args, **kwargs: item)(
                job, "attempt-1", NS(open=lambda _: io.BytesIO(b"data")), lambda: None
            )
            for invalid in (MAX_SEED + 1, "2114470198786121451", True):
                altered = replace(
                    prepared,
                    settings_json=canonical_json(
                        {**prepared.settings, "seed": invalid}
                    ),
                )
                with self.subTest(profile=profile_id, seed=invalid):
                    with self.assertRaisesRegex(ValueError, "wangp_invalid_seed"):
                        validate_prepared(altered, manifest)
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        output = root / "outputs"
                        output.mkdir()
                        journal = ReceiptJournal(
                            root / "receipt.sqlite",
                            slot_key="slot",
                            manifest_digest=manifest.digest,
                            create=True,
                        )
                        called = []
                        host = WanGPHost(
                            session=NS(
                                is_idle=lambda: True,
                                submit_task=lambda settings: called.append(settings),
                            ),
                            journal=journal,
                            manifest=manifest,
                            output_root=output,
                            sealed_root=root / "sealed",
                            settings_resolver=lambda value: resolve_inputs(
                                value, None, manifest
                            ),
                        )
                        try:
                            with self.assertRaisesRegex(
                                BackendError, "settings_resolution_failed"
                            ):
                                host.submit(altered)
                            self.assertIsNone(journal.get(altered.operation_id))
                            self.assertEqual(called, [])
                        finally:
                            host.close()

    def test_legacy_host_seed_gate_prevents_resolution_and_dispatch(self):
        manifest = EngineManifest.from_dict(
            json.loads(
                (
                    Path(__file__).resolve().parents[1] / "deploy/wangp/manifest.json"
                ).read_text()
            )
        )
        request = {
            "mode": "fl",
            "model": "MiniMax-H3-Base-BF16",
            "prompt": "Synthetic seed fixture",
            "duration": 5,
            "resolution": "480P",
            "seed": str(MAX_SEED),
        }
        job = {
            "id": "job-1",
            "owner_id": "owner",
            "request_hash": "b" * 64,
            "execution_plan": {"engine_manifest_digest": manifest.digest},
            "request": {
                "request": request,
                "output_spec": native_output_spec(request),
                "assets": {},
            },
        }
        prepared = H3FL2VACompiler(manifest, lambda *args: None)(
            job, "attempt-1", None, lambda: None
        )
        self.assertEqual(resolve_inputs(prepared, None, manifest)["seed"], MAX_SEED)
        altered = replace(
            prepared,
            settings_json=canonical_json({**prepared.settings, "seed": MAX_SEED + 1}),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "outputs"
            output.mkdir()
            journal = ReceiptJournal(
                root / "receipt.sqlite",
                slot_key="slot",
                manifest_digest=manifest.digest,
                create=True,
            )
            called = []
            host = WanGPHost(
                session=NS(
                    is_idle=lambda: True,
                    submit_task=lambda settings: called.append(settings),
                ),
                journal=journal,
                manifest=manifest,
                output_root=output,
                sealed_root=root / "sealed",
                settings_resolver=lambda value: resolve_inputs(value, None, manifest),
            )
            try:
                with self.assertRaisesRegex(BackendError, "settings_resolution_failed"):
                    host.submit(altered)
                self.assertIsNone(journal.get(altered.operation_id))
                self.assertEqual(called, [])
            finally:
                host.close()


if __name__ == "__main__":
    unittest.main()
