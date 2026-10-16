"""Pure alert state transitions and notification privacy boundaries."""
import copy
import json
import unittest
import uuid
from studio_platform.alert_conditions import evaluate

JOB = str(uuid.uuid4())
GPU = str(uuid.uuid4())


def snapshot(now, **values):
    return {"version": 1, "observed_at": now, "complete": True, "gpus": [], "jobs": [],
        "test_spend": None, "failures": [], **values}


class AlertConditionsTests(unittest.TestCase):
    def run_check(self, now, data=None, state=None, **kwargs):
        return evaluate(data if data is not None else snapshot(now), state,
            environment="staging", now=now, **kwargs)

    def test_gpu_stuck_wake_and_spend_fire_once_then_clear(self):
        now=10000
        data=snapshot(now, gpus=[{"id":GPU,"running_since":1,"idle_since":1,"idle_limit_seconds":600,"has_obligations":False}],
            jobs=[{"id":JOB,"status":"running","state_since":1,"wake_count":11}],
            test_spend={"day":"2026-10-16","projected_microusd":20_000_000,"limit_microusd":20_000_000})
        first=self.run_check(now,data)
        self.assertEqual({n["code"] for n in first["notifications"]},
            {"gpu_runtime_exceeded","gpu_idle_stop_overdue","job_stuck","job_repeated_wake","test_spend_80","test_spend_100"})
        again=self.run_check(now+60,{**data,"observed_at":now+60},first["state"])
        self.assertEqual(again["notifications"],[])
        clear=self.run_check(now+120,snapshot(now+120,test_spend={"projected_microusd":0,"limit_microusd":20_000_000}),state=again["state"])
        self.assertEqual(len(clear["notifications"]),6)
        self.assertTrue(all(n["transition"]=="resolved" for n in clear["notifications"]))
        self.assertEqual(self.run_check(now+180,state=clear["state"])["notifications"],[])

    def test_host_sustained_intervals_and_recovery(self):
        state=None
        host={"cpu_percent":71,"memory_percent":86,"processes_ok":False,"health_ok":False,
              "http_requests":20,"http_5xx":3}
        messages=[]
        for now in range(0,961,60):
            result=self.run_check(now,state=state,host=host);state=result["state"]
            messages.extend((now,n["code"]) for n in result["notifications"])
        self.assertEqual(messages,[(0,"http_5xx_rate"),(120,"health_down"),(900,"host_cpu_sustained"),
            (900,"host_memory_sustained"),(900,"host_process_unhealthy")])
        result=self.run_check(1020,state=state,host={"cpu_percent":1,"memory_percent":2,
            "processes_ok":True,"health_ok":True,"http_requests":10,"http_5xx":0})
        self.assertEqual(len(result["notifications"]),5)
        self.assertTrue(all(n["duration_seconds"]==1020 for n in result["notifications"]))

    def test_unknown_or_missing_samples_never_clear_active_incident(self):
        data=snapshot(10000,jobs=[{"id":JOB,"status":"running","state_since":1,"wake_count":0}])
        first=self.run_check(10000,data)
        for data in (None,{"version":1,"observed_at":10060,"complete":False},
                     {"version":1,"observed_at":10060,"complete":True}):
            result=evaluate(data,first["state"],environment="staging",now=10060)
            self.assertEqual(result["notifications"],[])
            self.assertTrue(result["state"]["conditions"]["job_stuck:"+JOB]["active"])

    def test_sampling_gap_does_not_invent_continuous_host_high_time(self):
        first=self.run_check(0,host={"cpu_percent":80})
        result=self.run_check(901,state=first["state"],host={"cpu_percent":80})
        self.assertEqual(result["notifications"],[])
        self.assertEqual(result["state"]["conditions"]["host_cpu_sustained:host"]["since"],901)

    def test_missing_history_uses_observed_state_and_resets_on_unknown(self):
        def data(t,status="running"):
            return snapshot(t,jobs=[{"id":JOB,"status":status,"state_since":None,"wake_count":0}])
        state=None
        for now in range(0,1861,60):
            result=self.run_check(now,data(now),state);state=result["state"]
        self.assertEqual([n["code"]for n in result["notifications"]],["job_stuck"])
        changed=self.run_check(1920,data(1920,"collecting"),state)
        self.assertEqual(changed["notifications"][0]["transition"],"resolved")
        unknown=evaluate(None,changed["state"],environment="staging",now=1980)
        resumed=self.run_check(2040,data(2040,"collecting"),unknown["state"])
        self.assertEqual(resumed["state"]["observed_jobs"][JOB]["since"],2040)

    def test_new_failure_kind_not_each_job_and_no_source_content_in_notification(self):
        data=snapshot(10000,failures=[{"job_id":JOB,"kind":"submission_rejected","at":9000,
            "reason_code":"PRIVATE secret prompt"}])
        first=self.run_check(10000,data)
        self.assertEqual(len(first["notifications"]),1)
        other=str(uuid.uuid4())
        data["failures"].append({"job_id":other,"kind":"submission_rejected","at":10000})
        again=self.run_check(10060,{**data,"observed_at":10060},first["state"])
        self.assertEqual(again["notifications"],[])
        data["failures"].append({"job_id":other,"kind":"upload_failed","at":10000})
        new=self.run_check(10120,{**data,"observed_at":10120},again["state"])
        self.assertEqual(len(new["notifications"]),1)
        self.assertNotIn("PRIVATE",json.dumps(first["notifications"]+new["notifications"]))
        self.assertEqual(set(new["notifications"][0]),{"code","environment","resource_id","duration_seconds","console_link","transition"})

    def test_unsafe_attempt_obligation_never_means_idle_and_spend_80_precedes100(self):
        first=self.run_check(10000,snapshot(10000,gpus=[{"id":GPU,"running_since":9000,"idle_since":1,
            "idle_limit_seconds":600,"has_obligations":True}],test_spend={"projected_microusd":16_000_000,"limit_microusd":20_000_000}))
        self.assertEqual([n["code"]for n in first["notifications"]],["test_spend_80"])
        second=self.run_check(10060,snapshot(10060,test_spend={"projected_microusd":20_000_000,"limit_microusd":20_000_000}),first["state"])
        self.assertEqual([n["code"]for n in second["notifications"]],["test_spend_100"])

    def test_override_thresholds_and_environment_are_bounded(self):
        with self.assertRaises(ValueError):
            evaluate(snapshot(1),None,environment="PRIVATE",now=1)
        with self.assertRaises(ValueError):
            self.run_check(1,thresholds={"unexpected":1})
        with self.assertRaises(ValueError):
            self.run_check(1,thresholds={"http_5xx_ratio":2})


class AlertStateRetentionTests(unittest.TestCase):
    def test_finished_resources_are_pruned_after_recovery_without_losing_pending_delivery(self):
        data=snapshot(10000,jobs=[{"id":str(uuid.uuid4()),"status":"running","state_since":1,"wake_count":11} for _ in range(3000)])
        first=evaluate(data,None,environment="staging",now=10000)
        self.assertEqual(len(first["notifications"]),6000)
        done=evaluate(snapshot(10060),first["state"],environment="staging",now=10060)
        self.assertEqual(len(done["notifications"]),6000)
        self.assertTrue(all(n["transition"]=="resolved" for n in done["notifications"]))
        self.assertLess(len(json.dumps(done["state"])),3000)
        self.assertEqual(evaluate(snapshot(10120),done["state"],environment="staging",now=10120)["notifications"],[])
        # Returned notifications are independently owned by the publisher's queue.
        self.assertEqual(len(done["notifications"]),6000)

    def test_missing_budget_observation_does_not_resolve_active_alarm(self):
        data=snapshot(10000,test_spend={"projected_microusd":28_000_000,"limit_microusd":20_000_000})
        first=evaluate(data,None,environment="staging",now=10000)
        unknown=evaluate(snapshot(10060),first["state"],environment="staging",now=10060)
        self.assertEqual(unknown["notifications"],[])
        self.assertTrue(unknown["state"]["conditions"]["test_spend_100:host"]["active"])
        recovered=evaluate(snapshot(10120,test_spend={"projected_microusd":0,"limit_microusd":20_000_000}),unknown["state"],environment="staging",now=10120)
        self.assertEqual({n["code"] for n in recovered["notifications"]},{"test_spend_80","test_spend_100"})
