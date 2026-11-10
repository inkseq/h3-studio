"""#816 off-host stopper: acts only on the heartbeat alarm plus a dead site."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "platform"))
import host_loss_lambda as stopper  # noqa: E402
import host_loss_publish as publisher  # noqa: E402

POD = "0b9f6a52-4a1e-4c5e-9d0a-0e2f5f1a7c11"
OTHER_POD = "5c3d1e2f-0a1b-4c2d-8e3f-4a5b6c7d8e9f"


def alarm(name="Inkseq-production-MonitorHeartbeat", state="ALARM"):
    message = json.dumps({"AlarmName": name, "NewStateValue": state})
    return {"Records": [{"Sns": {"Message": message}}]}


def stop_list(instances, version=2):
    return {"version": version, "updated_at": 1.0, "instances": instances}


class StopperTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(stopper, "TOPIC", "arn:topic")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ssm = Mock()
        self.listed(
            stop_list([{"instance_id": "100"}, {"instance_id": "bad id"}], version=1)
        )
        self.secrets = Mock()
        self.secrets.get_secret_value.side_effect = lambda SecretId: {
            "SecretString": json.dumps({"api_key": "k-" + SecretId.split("/")[-1]})
        }
        self.sns = Mock()
        self.vast = Mock(side_effect=lambda key, ids: {i: 200 for i in ids})
        self.runpod = Mock(side_effect=lambda key, ids: {i: 200 for i in ids})
        self.lium = Mock(side_effect=lambda key, ids: {i: 204 for i in ids})

    def listed(self, value):
        self.ssm.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps(value)}
        }

    def run_with(self, event, fetch):
        return stopper.run(
            event,
            ssm=self.ssm,
            secrets=self.secrets,
            sns=self.sns,
            fetch=fetch,
            actions={"vast": self.vast, "runpod": self.runpod, "lium": self.lium},
            sleep=lambda _: None,
        )

    def dead(self):
        return self.run_with(alarm(), Mock(side_effect=OSError("down")))

    def test_other_alarms_and_recoveries_do_nothing(self):
        for event in (
            alarm(name="Inkseq-production-DeliveryHealthy"),
            alarm(state="OK"),
        ):
            self.assertEqual(self.run_with(event, Mock())["state"], "ignored")
        self.vast.assert_not_called()

    def test_one_healthy_answer_means_the_host_lives(self):
        fetch = Mock(side_effect=[OSError("down"), 200, 200])
        self.assertEqual(self.run_with(alarm(), fetch)["state"], "site_alive")
        self.vast.assert_not_called()
        self.secrets.get_secret_value.assert_not_called()

    def test_version_1_list_stops_only_listed_numeric_vast_instances(self):
        result = self.dead()
        self.assertEqual(result["state"], "stopped")
        self.vast.assert_called_once_with("k-vast-host-loss", ["100"])
        self.assertEqual(result["results"], {"vast:100": 200})
        self.assertEqual(result["unconfirmed"], [])
        self.assertIn("paid GPUs stopped", self.sns.publish.call_args.kwargs["Subject"])

    def test_version_2_list_acts_per_provider_with_its_own_key(self):
        self.listed(
            stop_list(
                [
                    {"provider": "vast", "instance_id": "100"},
                    {"provider": "runpod", "instance_id": "x7k2pod9"},
                    {"provider": "lium", "instance_id": POD},
                    {"provider": "runpod", "instance_id": "a/b"},
                    {"provider": "modal", "instance_id": "sb-1"},
                    {"instance_id": "200"},  # Version 2 entries must name a provider.
                ]
            )
        )
        result = self.dead()
        self.assertEqual(result["state"], "stopped")
        self.vast.assert_called_once_with("k-vast-host-loss", ["100"])
        self.runpod.assert_called_once_with("k-runpod-host-loss", ["x7k2pod9"])
        self.lium.assert_called_once_with("k-lium-host-loss", [POD])
        self.assertEqual(
            sorted(result["results"]), ["lium:" + POD, "runpod:x7k2pod9", "vast:100"]
        )
        # Only the secrets of listed providers are read.
        self.listed(stop_list([{"provider": "runpod", "instance_id": "x7k2pod9"}]))
        self.secrets.get_secret_value.reset_mock(side_effect=False)
        self.dead()
        self.secrets.get_secret_value.assert_called_once_with(
            SecretId="/inkseq/production/runpod-host-loss"
        )

    def test_a_failed_stop_is_never_reported_as_stopped(self):
        for answer in (
            {"side_effect": TimeoutError("late")},
            {"return_value": {"100": 500}},
            {"return_value": {"100": "TimeoutError"}},
            {"return_value": {}},
        ):
            self.sns.reset_mock()
            self.vast = Mock(**answer)
            with self.assertRaisesRegex(RuntimeError, "unconfirmed vast:100"):
                self.dead()
            published = self.sns.publish.call_args.kwargs
            self.assertIn("NOT confirmed", published["Subject"])
            self.assertEqual(
                json.loads(published["Message"])["unconfirmed"], ["vast:100"]
            )

    def test_a_missing_secret_spares_the_other_providers(self):
        self.listed(
            stop_list(
                [
                    {"provider": "vast", "instance_id": "100"},
                    {"provider": "lium", "instance_id": POD},
                ]
            )
        )

        def secret(SecretId):
            if "lium" in SecretId:
                raise KeyError("ResourceNotFoundException")
            return {"SecretString": json.dumps({"api_key": "k"})}

        self.secrets.get_secret_value.side_effect = secret
        with self.assertRaisesRegex(RuntimeError, "unconfirmed lium:" + POD):
            self.dead()
        self.vast.assert_called_once_with("k", ["100"])
        self.lium.assert_not_called()
        message = json.loads(self.sns.publish.call_args.kwargs["Message"])
        self.assertEqual(message["results"]["vast:100"], 200)
        self.assertEqual(message["results"]["lium:" + POD], "KeyError")

    def test_an_unknown_list_version_alerts(self):
        self.listed(stop_list([{"provider": "vast", "instance_id": "100"}], 3))
        with self.assertRaisesRegex(RuntimeError, "unconfirmed list"):
            self.dead()
        self.vast.assert_not_called()


class ProviderCallTests(unittest.TestCase):
    def test_vast_and_runpod_are_stopped_not_destroyed(self):
        call = Mock(return_value=(200, None))
        self.assertEqual(stopper.stop_vast("k", ["100"], call=call), {"100": 200})
        call.assert_called_once_with(
            "PUT",
            "https://console.vast.ai/api/v0/instances/100/",
            {"Authorization": "Bearer k"},
            {"state": "stopped"},
        )
        call = Mock(side_effect=[OSError("reset"), (200, None)])
        self.assertEqual(
            stopper.stop_runpod("k", ["p1", "p2"], call=call),
            {"p1": "OSError", "p2": 200},
        )
        self.assertEqual(
            call.call_args.args,
            (
                "POST",
                "https://rest.runpod.io/v1/pods/p2/stop",
                {"Authorization": "Bearer k"},
            ),
        )

    def lium(self, pods, *answers):
        replies = iter(answers)

        def call(method, url, headers, body=None):
            self.assertEqual(headers, {"X-API-Key": "k"})
            self.calls.append((method, url))
            if method == "GET":
                return 200, pods
            return next(replies)

        self.calls = []
        return call

    def test_lium_pods_are_deleted_by_id_or_resolved_name(self):
        pods = [
            {"id": POD, "pod_name": "sixnine-a-0-0", "status": "RUNNING"},
            {"id": OTHER_POD, "pod_name": "other", "status": "RUNNING"},
            {"id": "6d4e2f3a-1b2c-4d3e-9f4a-5b6c7d8e9f0a", "status": "DELETING"},
        ]
        call = self.lium(pods, (204, None), (404, None))
        result = stopper.remove_lium(
            "k",
            [
                "sixnine-a-0-0",
                OTHER_POD,
                "6d4e2f3a-1b2c-4d3e-9f4a-5b6c7d8e9f0a",
                "7e5f3a4b-2c3d-4e5f-8a6b-7c8d9e0f1a2b",
                "sixnine-gone-0-0",
            ],
            call=call,
        )
        self.assertEqual(
            result,
            {
                "sixnine-a-0-0": 204,
                OTHER_POD: "absent",
                "6d4e2f3a-1b2c-4d3e-9f4a-5b6c7d8e9f0a": "deleting",
                "7e5f3a4b-2c3d-4e5f-8a6b-7c8d9e0f1a2b": "absent",
                "sixnine-gone-0-0": "unresolved",
            },
        )
        self.assertEqual(
            self.calls,
            [
                ("GET", "https://lium.io/api/pods"),
                ("DELETE", "https://lium.io/api/pods/" + POD),
                ("DELETE", "https://lium.io/api/pods/" + OTHER_POD),
            ],
        )
        self.assertFalse(stopper.confirmed("unresolved"))
        self.assertTrue(stopper.confirmed("absent"))

    def test_lium_without_a_pod_list_or_with_report_setting_deletes_nothing(self):
        call = Mock(return_value=(503, None))
        self.assertEqual(
            stopper.remove_lium("k", [POD], call=call), {POD: "pod_list_unavailable"}
        )
        call.assert_called_once()
        call = Mock()
        with patch.object(stopper, "LIUM_ACTION", "report"):
            self.assertEqual(
                stopper.remove_lium("k", [POD], call=call),
                {POD: "not_deleted_by_setting"},
            )
        call.assert_not_called()


class PublisherTests(unittest.TestCase):
    def test_accepts_both_list_versions_and_rejects_malformed_entries(self):
        self.assertTrue(publisher.valid(stop_list([{"instance_id": "100"}], 1)))
        self.assertTrue(
            publisher.valid(
                stop_list(
                    [
                        {"provider": "vast", "instance_id": "100"},
                        {"provider": "runpod", "instance_id": "x7k2pod9"},
                        {"provider": "lium", "instance_id": POD},
                    ]
                )
            )
        )
        for bad in (
            stop_list([{"instance_id": "100"}]),
            stop_list([{"provider": "modal", "instance_id": "sb1"}]),
            stop_list([{"provider": "runpod", "instance_id": "a/b"}]),
            stop_list([{"instance_id": "x7k2pod9"}], 1),
            stop_list([], 3),
        ):
            self.assertFalse(publisher.valid(bad), bad)

    def test_publishes_a_fresh_version_2_list(self):
        value = stop_list([{"provider": "lium", "instance_id": POD}])
        value["updated_at"] = 1000.0
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "list.json"
            source.write_text(json.dumps(value))
            ssm = Mock()
            with (
                patch.object(publisher, "SOURCE", source),
                patch.object(publisher, "STATE", Path(directory) / "s" / "last.json"),
            ):
                self.assertEqual(
                    publisher.publish("staging", ssm, now=1010.0), "published"
                )
                self.assertEqual(
                    publisher.publish("staging", ssm, now=1020.0), "unchanged"
                )
        ssm.put_parameter.assert_called_once()
        self.assertEqual(
            ssm.put_parameter.call_args.kwargs["Name"],
            "/sixnine/staging/host-loss/stoppable",
        )

    def test_each_provider_key_comes_from_its_own_dstack_backend(self):
        config = json.dumps(
            {
                "projects": [
                    {
                        "name": "p",
                        "backends": [
                            {"type": "vastai", "creds": {"api_key": "v"}},
                            {"type": "runpod", "creds": {"api_key": "r"}},
                            {"type": "lium", "creds": {"api_key": "l"}},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(publisher.provider_key(config, "vast"), "v")
        self.assertEqual(publisher.provider_key(config, "runpod"), "r")
        self.assertEqual(publisher.provider_key(config, "lium"), "l")
        with self.assertRaisesRegex(SystemExit, "lium_key_not_unique"):
            publisher.provider_key('{"projects": []}', "lium")
        self.assertEqual(
            publisher.secret("production", "vast"), "/sixnine/production/vast-host-loss"
        )
        self.assertEqual(
            publisher.secret("production", "lium"), "/inkseq/production/lium-host-loss"
        )


if __name__ == "__main__":
    unittest.main()
