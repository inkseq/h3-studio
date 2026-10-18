"""Offline host-transport bounds; no credential loads or provider requests."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import targon_cleanup_guard as guard


class FakeAlarm:
    SIGALRM, ITIMER_REAL = 14, 0

    def __init__(self):
        self.handler = 'original'
        self.seconds = 0
        self.armed = []

    def getitimer(self, _):
        return self.seconds, 0

    def signal(self, _, handler):
        previous, self.handler = self.handler, handler
        return previous

    def setitimer(self, _, seconds):
        self.seconds = seconds
        self.armed.append(seconds)

    def expire(self):
        self.handler(self.SIGALRM, None)


class GuardHTTPTests(unittest.TestCase):
    route = '/tha/v3/orgs/approved-org/workloads/1-owned-uid'

    def test_wall_timeout_covers_open_and_dripping_response_body(self):
        for phase in ('open', 'body'):
            with self.subTest(phase=phase):
                alarm = FakeAlarm()
                class Response:
                    code = 200
                    closed = False
                    def __enter__(self): return self
                    def __exit__(self, *_): self.closed = True
                    def read(self, limit):
                        self.limit = limit
                        alarm.expire()
                response = Response()
                def open_request(*args, **kwargs):
                    if phase == 'open': alarm.expire()
                    return response
                client = guard.ProviderHTTP('fake-offline-only')
                client._opener = SimpleNamespace(open=open_request)
                with patch.object(guard, 'signal', alarm):
                    with self.assertRaisesRegex(TimeoutError, '^targon_guard_request_timeout$'):
                        client.get(self.route)
                self.assertEqual(alarm.armed, [15, 0])
                self.assertEqual(alarm.handler, 'original')
                if phase == 'body':
                    self.assertTrue(response.closed)
                    self.assertEqual(response.limit, 1024*1024+1)

    def test_success_and_oversized_body_release_alarm(self):
        for raw in (b'{"state":"running"}', b'x'*(1024*1024+1)):
            alarm = FakeAlarm()
            class Response:
                code = 200
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def read(self, limit): return raw
            client = guard.ProviderHTTP('fake-offline-only')
            client._opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
            with patch.object(guard, 'signal', alarm):
                if len(raw) > 1024*1024:
                    with self.assertRaisesRegex(ValueError, 'response_limit'): client.get(self.route)
                else:
                    self.assertEqual(client.get(self.route).json(), {'state':'running'})
            self.assertEqual(alarm.seconds, 0)
            self.assertEqual(alarm.handler, 'original')


if __name__ == '__main__':
    unittest.main()
