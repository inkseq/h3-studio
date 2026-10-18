"""Host plan guard rejects accidental broad ingress and regional mismatches."""
import copy
import unittest
from tools.check_lightsail_plan import validate


class HostPlanTests(unittest.TestCase):
    def plan(self):
        # 8.8.8.8 is a public format fixture, NOT an address we own or deploy to.
        return {"region": "ap-southeast-1", "InstanceName": "sixnine-fixture", "AvailabilityZone": "ap-southeast-1a",
                "Capacity": "control8gb", "KeyPairName": "fixture-public-key", "AdminIpv4Cidr": "8.8.8.8/32", "DailySnapshot": "disabled"}

    def test_valid_structure_is_explicitly_not_deployment(self):
        value = validate(self.plan())
        self.assertEqual(value["status"], "offline_structure_validated_not_deployed")
        self.assertIn("admin_ip_ownership", value["remaining_checks"])

    def test_public_ingress_cannot_accidentally_include_ssh_cidr_or_private_placeholder(self):
        for address in ("0.0.0.0/0", "8.8.8.0/24", "127.0.0.1/32", "192.168.1.1/32", "203.0.113.1/32", "999.1.1.1/32", "::/0"):
            value = self.plan()
            value["AdminIpv4Cidr"] = address
            with self.assertRaises(ValueError):
                validate(value)

    def test_wrong_region_missing_choice_and_secret_fields_rejected(self):
        for change in ({"AvailabilityZone": "ap-southeast-2a"}, {"Capacity": "cheap-auto"},
                       {"aws_secret_access_key": "synthetic"}, {"KeyPairName": "-----BEGIN PRIVATE KEY-----"}):
            value = self.plan()
            value.update(change)
            with self.assertRaises(ValueError):
                validate(value)
        value = self.plan()
        del value["Capacity"]
        with self.assertRaises(ValueError):
            validate(value)


if __name__ == "__main__":
    unittest.main()
