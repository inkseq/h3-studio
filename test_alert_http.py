"""ASGI counter privacy, actual peer checks and unhandled error coverage."""
import asyncio
import json
import unittest
from studio_platform.alert_http import AlertCountersMiddleware, HTTPAlertCounters, PATH


class AlertHTTPTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.counts=HTTPAlertCounters(clock=lambda:self.now)

    def request(self,path="/v1/jobs",*,peer="127.0.0.1",headers=(),method="GET",status=200,fail=False):
        async def app(scope,receive,send):
            if fail:raise RuntimeError("PRIVATE prompt token")
            await send({"type":"http.response.start","status":status,"headers":[]})
            await send({"type":"http.response.body","body":b"PRIVATE content"})
        middleware=AlertCountersMiddleware(app,counters=self.counts)
        sent=[]
        async def send(message):sent.append(message)
        async def receive():return {"type":"http.request","body":b"PRIVATE input"}
        asyncio.run(middleware({"type":"http","path":path,"client":(peer,1),"method":method,"headers":headers},receive,send))
        return sent

    def test_only_aggregate_counts_and_errors_never_content(self):
        self.request();self.request(status=503)
        with self.assertRaises(RuntimeError):self.request(fail=True)
        value=self.counts.snapshot()
        self.assertEqual((value["requests"],value["server_errors"]),(3,2))
        self.assertNotIn("PRIVATE",repr(vars(self.counts)))
        self.now+=301
        self.assertEqual(self.counts.snapshot()["requests"],0)

    def test_monitor_and_health_are_excluded_and_endpoint_has_closed_fields(self):
        self.request("/healthz",status=503)
        sent=self.request(PATH)
        self.assertEqual(sent[0]["status"],200)
        body=json.loads(sent[1]["body"])
        self.assertEqual(set(body),{"epoch","window_seconds","requests","server_errors"})
        self.assertEqual(body["requests"],0)
        self.assertEqual(self.request(PATH,peer="::1")[0]["status"],200)
        self.assertEqual(self.request(PATH,method="POST")[0]["status"],405)

    def test_forwarded_and_host_headers_cannot_grant_monitor_access(self):
        self.assertEqual(self.request(PATH,peer="203.0.113.1",headers=[(b"host",b"127.0.0.1")])[0]["status"],403)
        for header in (b"x-forwarded-for",b"forwarded",b"x-real-ip"):
            # Even a proxy layer that has rewritten scope.client to loopback
            # cannot turn a forwarded request into a local counter read.
            self.assertEqual(self.request(PATH,headers=[(header,b"127.0.0.1")])[0]["status"],403)
        self.assertEqual(self.counts.snapshot()["requests"],0)

    def test_clock_rollback_drops_future_buckets_and_memory_is_bounded(self):
        for _ in range(1000):
            self.request();self.now+=1
        self.assertLessEqual(len(self.counts.buckets),300)
        self.now=1
        self.assertEqual(self.counts.snapshot()["requests"],0)


class AppCounterIntegrationTests(unittest.TestCase):
    def test_actual_app_counts_unhandled_errors_and_rejects_external_peer(self):
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Settings(Path(directory), auth_mode="local-test"))
            @app.get("/api/alert-test-failure")
            def failure():
                raise RuntimeError("PRIVATE-ERROR-CONTENT")
            with TestClient(app, client=("127.0.0.1", 123), raise_server_exceptions=False) as client:
                self.assertEqual(client.post("/api/auth/login", json={"username":"superdan"}).status_code, 200)
                self.assertEqual(client.get("/api/alert-test-failure").status_code, 500)
                response = client.get(PATH)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["server_errors"], 1)
                self.assertNotIn("PRIVATE", response.text)
                before = response.json()["requests"]
                client.get("/healthz")
                self.assertEqual(client.get(PATH).json()["requests"], before)
            with TestClient(app, client=("203.0.113.1", 123)) as external:
                self.assertEqual(external.get(PATH, headers={"Host":"127.0.0.1"}).status_code, 403)
