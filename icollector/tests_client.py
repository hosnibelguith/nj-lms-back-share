import hashlib
import hmac
import json
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

from .client import DASHBOARD_PATH, REJECT_PATH, OceonClient, OceonError, signed_headers


class SigningTests(SimpleTestCase):
    def test_documented_canonical_string_covers_exact_utf8_bytes_and_query(self):
        raw = '{"name":"José","amount":250.5}'.encode('utf-8')
        path = REJECT_PATH + '?mode=test'
        timestamp, nonce = '2026-09-15T12:00:00Z', 'test-nonce'
        headers = signed_headers('test-api-key', 'test-secret', 'POST', path, raw,
                                 timestamp=timestamp, nonce=nonce)
        expected = hmac.new(b'test-secret', (
            '2026-09-15T12:00:00Z.test-nonce.POST.' + path + '.' + hashlib.sha256(raw).hexdigest()
        ).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Partner-Signature'], 'sha256=' + expected)
        self.assertEqual(headers['Authorization'], 'Bearer test-api-key')
        self.assertEqual(headers['X-Partner-Timestamp'], timestamp)

    def test_get_signs_empty_body_and_every_attempt_gets_a_new_nonce(self):
        first = signed_headers('key', 'secret', 'GET', DASHBOARD_PATH, b'')
        second = signed_headers('key', 'secret', 'GET', DASHBOARD_PATH, b'')
        self.assertNotEqual(first['X-Partner-Nonce'], second['X-Partner-Nonce'])
        self.assertNotEqual(first['X-Partner-Signature'], second['X-Partner-Signature'])


class ClientTests(SimpleTestCase):
    def setUp(self):
        self.session = Mock()
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        self.response = Mock(status_code=200)
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.response.iter_content.return_value = [b'{"row":{"id":123},"idempotent_replay":true}']
        self.session.request.return_value = self.response
        self.mock = patch('icollector.client.requests.Session', return_value=self.session)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_post_uses_same_bytes_for_signature_and_transport(self):
        payload = {'idempotency_key': 'test-1', 'data': {'Client name': 'José', 'Missed payment': 250.5}}
        result = OceonClient().send_reject(payload)
        call = self.session.request.call_args
        raw, headers = call.kwargs['data'], call.kwargs['headers']
        expected = signed_headers('local-test-key', 'local-test-secret', 'POST', REJECT_PATH, raw,
            timestamp=headers['X-Partner-Timestamp'], nonce=headers['X-Partner-Nonce'])
        self.assertEqual(headers, expected)
        self.assertEqual(json.loads(raw), payload)
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertFalse(self.session.trust_env)
        self.assertEqual(result, {'http_status': 200, 'row_id': '123', 'idempotent_replay': True})

    def test_dashboard_get_has_no_body(self):
        self.response.iter_content.return_value = [b'{"dashboard":{"id":257},"widgets":[]}']
        self.assertEqual(OceonClient().dashboard()['dashboard']['id'], 257)
        self.assertEqual(self.session.request.call_args.kwargs['data'], b'')

    def test_authentication_is_not_forwarded_to_redirect(self):
        self.response.status_code = 302
        with self.assertRaises(OceonError):
            OceonClient().dashboard()
        self.session.request.assert_called_once()

    @override_settings(ICOLLECTOR_PROXY_URL='http://test-user:test-password@proxy.example.test:80')
    def test_configured_proxy_is_used_only_in_this_client(self):
        OceonClient().send_reject({'idempotency_key': 'test'})
        self.assertEqual(self.session.proxies, {'https': 'http://test-user:test-password@proxy.example.test:80'})

    def test_network_error_does_not_expose_proxy_credentials(self):
        self.session.request.side_effect = requests.ProxyError('http://user:super-secret@proxy.test') if hasattr(requests, 'ProxyError') else requests.exceptions.ProxyError('http://user:super-secret@proxy.test')
        with self.assertRaises(OceonError) as error:
            OceonClient().dashboard()
        self.assertNotIn('super-secret', str(error.exception))

    def test_400_is_nonretryable_but_service_and_auth_errors_retry(self):
        for status in (400, 401, 403, 409, 429, 500, 503):
            self.response.status_code = status
            with self.assertRaises(OceonError) as error:
                OceonClient().dashboard()
            self.assertEqual(error.exception.retryable, status != 400)
            self.assertEqual(error.exception.http_status, status)

    def test_success_requires_actual_row_acknowledgement(self):
        self.response.iter_content.return_value = [b'{"status":"ok"}']
        with self.assertRaises(OceonError):
            OceonClient().send_reject({'idempotency_key': 'test'})

    @override_settings(ICOLLECTOR_BASE_URL='http://app.icollector.ai')
    def test_plain_http_cannot_receive_credentials(self):
        with self.assertRaises(OceonError):
            OceonClient()
        self.session.request.assert_not_called()

    def test_only_the_two_mohawk_endpoints_are_callable(self):
        with self.assertRaises(OceonError):
            OceonClient().request('POST', '/api/payments/')
        self.session.request.assert_not_called()
