"""#816 off-host stopper: acts only on the heartbeat alarm plus a dead site."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "platform"))
import host_loss_lambda as stopper  # noqa: E402


def alarm(name="Inkseq-production-MonitorHeartbeat", state="ALARM"):
    message = json.dumps({"AlarmName": name, "NewStateValue": state})
    return {"Records": [{"Sns": {"Message": message}}]}


class StopperTests(unittest.TestCase):
    def setUp(self):
        self.ssm = Mock()
        self.ssm.get_parameter.return_value = {
            "Parameter": {
                "Value": json.dumps(
                    {
                        "version": 1,
                        "updated_at": 1.0,
                        "instances": [
                            {"instance_id": "100"},
                            {"instance_id": "bad id"},
                        ],
                    }
                )
            }
        }
        self.secrets = Mock()
        self.secrets.get_secret_value.return_value = {
            "SecretString": json.dumps({"api_key": "k"})
        }
        self.sns, self.stop = Mock(), Mock(return_value=200)

    def run_with(self, event, fetch):
        return stopper.run(
            event,
            ssm=self.ssm,
            secrets=self.secrets,
            sns=self.sns,
            fetch=fetch,
            stop=self.stop,
            sleep=lambda _: None,
        )

    def test_other_alarms_and_recoveries_do_nothing(self):
        for event in (
            alarm(name="Inkseq-production-DeliveryHealthy"),
            alarm(state="OK"),
        ):
            self.assertEqual(self.run_with(event, Mock())["state"], "ignored")
        self.stop.assert_not_called()

    def test_one_healthy_answer_means_the_host_lives(self):
        fetch = Mock(side_effect=[OSError("down"), 200, 200])
        self.assertEqual(self.run_with(alarm(), fetch)["state"], "site_alive")
        self.stop.assert_not_called()
        self.secrets.get_secret_value.assert_not_called()

    def test_dead_site_stops_only_listed_numeric_instances(self):
        result = self.run_with(alarm(), Mock(side_effect=OSError("down")))
        self.assertEqual(result["state"], "stopped")
        self.stop.assert_called_once_with("k", "100")
        self.assertEqual(result["results"], {"100": 200})


if __name__ == "__main__":
    unittest.main()
