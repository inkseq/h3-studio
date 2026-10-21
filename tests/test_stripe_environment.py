"""Stripe mode/target settings refuse cross-environment activation."""

from dataclasses import replace
from pathlib import Path
import unittest

from studio_platform.settings import Settings


class StripeEnvironmentTests(unittest.TestCase):
    def test_live_requires_canonical_production_and_explicit_config(self):
        settings = Settings(
            data_dir=Path('.'),
            stripe_mode='live',
            deployment_target='production',
            public_origin='https://inkseq.com',
            stripe_config_file=Path.cwd() / 'protected-stripe',
            public_origin_aliases=('https://sixnine.art', 'https://www.sixnine.art'),
        )
        for changes in (
            {'deployment_target': 'staging'},
            {'public_origin': 'https://staging.inkseq.com'},
            {'public_origin_aliases': ('https://untrusted.example',)},
            {'stripe_config_file': None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(settings, **changes)

    def test_test_stays_staging_and_live_off_does_not_enable_checkout(self):
        settings = Settings(
            data_dir=Path('.'),
            stripe_mode='test',
            deployment_target='staging',
            public_origin='https://staging.inkseq.com',
            stripe_config_file=Path.cwd() / 'protected-stripe',
        )
        with self.assertRaises(ValueError):
            replace(settings, deployment_target='production')
        self.assertEqual(replace(settings, stripe_mode='off').stripe_mode, 'off')
