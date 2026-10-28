"""Original WanGP HTTP contract over a fake Modal exec stream; no GPU/network."""

import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from studio_platform.modal_transport import BRIDGE, ModalHTTPTransport
from studio_platform.inference.wangp_http import HTTPWanGPTransport
from studio_platform.inference.protocol import SubmissionUncertain


class Input(io.BytesIO):
    def drain(self):
        pass

    def write_eof(self):
        pass


class FakeSandbox:
    def __init__(self):
        self.calls = []
        self.body = b'{"acknowledged":true}'
        self.response_status = 200
        self.break_response = False
        self.input = Input()

    def exec(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        header = json.dumps({'status': self.response_status}).encode()
        output = len(header).to_bytes(4, 'big') + header + self.body
        if self.break_response:
            output = b'broken'
        return SimpleNamespace(
            stdin=self.input,
            stdout=iter([output[:2], output[2:7], output[7:]]),
        )


class ModalTransportTests(unittest.TestCase):
    def setUp(self):
        self.host = FakeSandbox()
        self.transport = ModalHTTPTransport(self.host, stderr_sink=object())
        self.http = HTTPWanGPTransport(
            'http://127.0.0.1:8199',
            'private-token-' + 'x' * 48,
            transport=self.transport,
            expected_incarnation='a' * 32,
        )

    def tearDown(self):
        self.http.close()

    def test_original_cancel_protocol_and_token_only_on_stdin(self):
        self.assertTrue(self.http.cancel('wangp-original'))
        args, kwargs = self.host.calls[0]
        self.assertNotIn('private-token', repr((args, kwargs)))
        header, body = self.host.input.getvalue().split(b'\n', 1)
        value = json.loads(header)
        self.assertEqual(value['path'], '/v1/operations/wangp-original/cancel')
        self.assertEqual(value['method'], 'POST')
        self.assertEqual(body, b'')
        self.assertTrue(value['headers']['authorization'].startswith('Bearer '))
        self.assertIs(kwargs['text'], False)

    def test_streaming_input_preserves_original_bytes_and_metadata(self):
        payload = b'a' * 1100000
        with httpx.Client(transport=self.transport) as client:
            response = client.put(
                'http://127.0.0.1:8199/v1/inputs/original',
                content=payload,
                headers={
                    'Authorization': 'Bearer ' + 'a' * 48,
                    'X-Wangp-Input': '{"sha256":"original"}',
                },
            )
        self.assertEqual(response.status_code, 200)
        header, body = self.host.input.getvalue().split(b'\n', 1)
        self.assertEqual(body, payload)
        self.assertEqual(
            json.loads(header)['headers']['x-wangp-input'], '{"sha256":"original"}'
        )

    def test_lost_submit_response_never_reexecutes(self):
        self.host.break_response = True
        with self.assertRaises(SubmissionUncertain):
            self.http._json(
                'POST', '/v1/operations', value={'id': 'original'}, submission=True
            )
        self.assertEqual(len(self.host.calls), 1)
        header = json.loads(self.host.input.getvalue().split(b'\n', 1)[0])
        self.assertEqual(header['headers']['x-wangp-incarnation'], 'a' * 32)

    def test_external_origin_or_path_never_executes(self):
        for url in ['https://example.com/v1/readiness', 'http://127.0.0.1:8199/admin']:
            with self.assertRaises(httpx.TransportError):
                self.transport.handle_request(httpx.Request('GET', url))
        self.assertEqual(self.host.calls, [])

    def test_remote_error_body_is_not_in_exception(self):
        self.host.response_status = 500
        self.host.body = b'SECRET_RUNTIME_ERROR'
        with self.assertRaises(Exception) as found:
            self.http.cancel('wangp-original')
        self.assertNotIn('SECRET', str(found.exception))

    def test_bridge_relays_exact_loopback_body_without_printing_headers(self):
        header = {
            'method': 'PUT',
            'path': '/v1/inputs/original',
            'length': 3,
            'timeout': 30,
            'headers': {'authorization': 'Bearer SECRET'},
        }
        source = io.BytesIO(json.dumps(header).encode() + b'\nabc')
        output = io.BytesIO()
        response = io.BytesIO(b'{"stored":true}')
        response.status = 200
        connection = Mock()
        connection.getresponse.return_value = response
        with (
            patch('http.client.HTTPConnection', return_value=connection) as factory,
            patch('sys.stdin', SimpleNamespace(buffer=source)),
            patch('sys.stdout', SimpleNamespace(buffer=output)),
        ):
            exec(compile(BRIDGE, '<trusted-modal-bridge>', 'exec'), {})
        factory.assert_called_once_with('127.0.0.1', 8199, timeout=30)
        connection.putrequest.assert_called_once_with('PUT', '/v1/inputs/original')
        connection.send.assert_called_once_with(b'abc')
        self.assertNotIn(b'SECRET', output.getvalue())
        self.assertTrue(output.getvalue().endswith(b'{"stored":true}'))
