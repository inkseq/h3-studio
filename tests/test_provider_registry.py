"""The provider registry reproduces every set it replaced (#931 step 1)."""

import unittest

from studio_platform.providers.registry import (
    ADMISSION_PAIRS,
    BACKEND_PROVIDERS,
    DSTACK_BACKENDS,
    DSTACK_V1_PROVIDERS,
)


class ProviderRegistryTests(unittest.TestCase):
    def test_derived_sets_match_the_literals_they_replaced(self):
        self.assertEqual(
            DSTACK_V1_PROVIDERS,
            {"vast", "runpod", "lium", "cloudrift", "modal-dialout"},
        )
        self.assertEqual(
            DSTACK_BACKENDS,
            {
                "vastai",
                "runpod",
                "lium",
                "cloudrift",
                "runpod-serverless",
                "modal-dialout",
            },
        )
        self.assertEqual(BACKEND_PROVIDERS["runpod-serverless"], "runpod")
        self.assertEqual(
            ADMISSION_PAIRS,
            {("dstack-v1", p) for p in DSTACK_V1_PROVIDERS} | {("modal-v1", "modal")},
        )

    def test_callers_use_the_registry(self):
        from studio_platform import dstack_inventory, dstack_operator, dstack_runtime
        from studio_platform import telemetry

        self.assertIs(dstack_operator.PROVIDERS, BACKEND_PROVIDERS)
        self.assertIs(dstack_runtime.PROVIDERS, BACKEND_PROVIDERS)
        self.assertIs(dstack_inventory.SUPPORTED, DSTACK_BACKENDS)
        self.assertEqual(
            telemetry.PROVIDERS,
            DSTACK_V1_PROVIDERS | {"targon", "modal", "local", "unknown"},
        )


if __name__ == "__main__":
    unittest.main()
