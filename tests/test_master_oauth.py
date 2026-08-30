"""Security tests for the one-time Outlook authorization-code login."""

from __future__ import annotations

import base64
import hashlib
import io
from types import SimpleNamespace
import unittest
from unittest import mock

from windows_gui import master_oauth
from windows_gui.mail_digest import MailboxFlowError


class AuthorizationUrlTests(unittest.TestCase):
    def test_builds_pkce_and_loopback_url(self):
        verifier = 'a' * 64
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode('ascii')).digest()
        ).rstrip(b'=').decode('ascii')

        url, flow = master_oauth.build_authorization_url(
            'tenant',
            'client',
            port=8932,
            state='expected-state',
            code_verifier=verifier,
        )

        self.assertIn('https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize', url)
        self.assertIn('redirect_uri=http%3A%2F%2F127.0.0.1%3A8932%2Fcallback', url)
        self.assertIn('code_challenge_method=S256', url)
        self.assertIn(f'code_challenge={challenge}', url)
        self.assertIn('offline_access', url)
        self.assertIn('Mail.ReadWrite', url)
        self.assertIn('Mail.Send', url)
        self.assertEqual('expected-state', flow.state)
        self.assertEqual('http://127.0.0.1:8932/callback', flow.redirect_uri)

    def test_missing_configuration_is_explicit(self):
        with self.assertRaises(MailboxFlowError):
            master_oauth.build_authorization_url('', 'client')


class CallbackValidationTests(unittest.TestCase):
    def test_valid_callback_returns_code_without_exposing_it_in_response(self):
        kind, value, state = master_oauth.parse_callback(
            '/callback',
            '127.0.0.1:8932',
            'code=one-time-code&state=expected-state',
            expected_state='expected-state',
            expected_port=8932,
        )
        self.assertEqual(('code', 'one-time-code', 'expected-state'), (kind, value, state))

    def test_rejects_foreign_host_path_and_state(self):
        cases = [
            (
                master_oauth.parse_callback(
                    '/callback',
                    'attacker.example:8932',
                    'code=x&state=expected',
                    expected_state='expected',
                    expected_port=8932,
                )[0],
                'forbidden',
            ),
            (
                master_oauth.parse_callback(
                    '/other',
                    '127.0.0.1:8932',
                    'code=x&state=expected',
                    expected_state='expected',
                    expected_port=8932,
                )[0],
                'not_found',
            ),
            (
                master_oauth.parse_callback(
                    '/callback',
                    '127.0.0.1:8932',
                    'code=x&state=wrong',
                    expected_state='expected',
                    expected_port=8932,
                )[0],
                'state_mismatch',
            ),
        ]
        for actual, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, actual)

    def test_provider_error_is_reported(self):
        outcome = master_oauth.parse_callback(
            '/callback',
            '127.0.0.1:8932',
            'error=access_denied&error_description=user+declined&state=expected',
            expected_state='expected',
            expected_port=8932,
        )
        self.assertEqual(('error', 'user declined', 'expected'), outcome)

    def test_handler_ignores_invalid_callback_without_cancelling_login(self):
        class FakeLoginServer:
            result = None

        server = FakeLoginServer()
        handler_class = master_oauth._response_handler_factory('expected-state', 8932)

        def callback(path, host, query):
            handler = object.__new__(handler_class)
            handler.server = server
            handler.path = path if query in ('', None) else f'{path}?{query}'
            handler.headers = {'Host': host}
            handler._respond = mock.Mock()
            handler.do_GET()
            return handler._respond

        callback(
            '/callback', 'attacker.example:8932', 'code=x&state=expected-state'
        )
        self.assertIsNone(server.result)
        callback('/callback', '127.0.0.1:8932', 'code=x&state=wrong-state')
        self.assertIsNone(server.result)
        respond = callback(
            '/callback', '127.0.0.1:8932', 'code=one-time&state=expected-state'
        )
        self.assertEqual(('code', 'one-time', 'expected-state'), server.result)
        respond.assert_called_once_with(
            200, '登录完成', '可以关闭此页面并返回终端。'
        )


class AuthorizationExchangeTests(unittest.TestCase):
    def test_exchange_posts_pkce_and_persists_refresh_token(self):
        calls = []

        def transport(url, data=None, timeout=None):
            calls.append((url, data, timeout))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    'access_token': 'runtime-access',
                    'refresh_token': 'runtime-rotation',
                },
            )

        _, flow = master_oauth.build_authorization_url(
            'tenant', 'client', state='s', code_verifier='a' * 64
        )
        with mock.patch.object(
            master_oauth, 'write_master_refresh_token'
        ) as write:
            result = master_oauth.exchange_authorization_code(
                'tenant', 'client', 'one-time-code', flow, transport=transport
            )

        self.assertTrue(result['stored_refresh_token'])
        write.assert_called_once_with('runtime-rotation')
        url, data, timeout = calls[0]
        self.assertIn('/oauth2/v2.0/token', url)
        self.assertEqual('authorization_code', data['grant_type'])
        self.assertEqual('one-time-code', data['code'])
        self.assertEqual(flow.code_verifier, data['code_verifier'])
        self.assertEqual(flow.redirect_uri, data['redirect_uri'])
        self.assertEqual(30, timeout)

    def test_invalid_response_does_not_store_token(self):
        response = SimpleNamespace(
            status_code=400, json=lambda: {'error': 'invalid_grant'}
        )
        _, flow = master_oauth.build_authorization_url(
            'tenant', 'client', state='s', code_verifier='a' * 64
        )
        with mock.patch.object(
            master_oauth, 'write_master_refresh_token'
        ) as write:
            with self.assertRaisesRegex(MailboxFlowError, 'invalid_grant'):
                master_oauth.exchange_authorization_code(
                    'tenant',
                    'client',
                    'bad-code',
                    flow,
                    transport=lambda *args, **kwargs: response,
                )

        write.assert_not_called()


class BootstrapLoginTests(unittest.TestCase):
    def test_opens_browser_once_and_stores_rotated_refresh_token(self):
        opened = []
        calls = []
        output = io.StringIO()

        class FakeServer:
            timeout = None

            def __init__(self, address, handler_class):
                self.address = address
                self.handler_class = handler_class
                self.result = ('code', 'one-time-code', 'state')
                self.closed = False

            def handle_request(self):
                return

            def server_close(self):
                self.closed = True

        def transport(url, data=None, timeout=None):
            calls.append((url, data, timeout))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    'access_token': 'runtime-access',
                    'refresh_token': 'runtime-rotation',
                },
            )

        with mock.patch.dict(
            'os.environ',
            {
                'AI_WORK_OUTLOOK_TENANT_ID': 'tenant',
                'AI_WORK_OUTLOOK_CLIENT_ID': 'client',
            },
        ), mock.patch.object(
            master_oauth, '_CallbackHTTPServer', FakeServer
        ), mock.patch.object(
            master_oauth, 'write_master_refresh_token'
        ) as write:
            result = master_oauth.bootstrap_master_login(
                open_browser=True,
                browser_opener=lambda url: opened.append(url),
                output=output.write,
                transport=transport,
            )

        self.assertTrue(result['stored_refresh_token'])
        write.assert_called_once_with('runtime-rotation')
        self.assertEqual(1, len(opened))
        self.assertIn(
            'redirect_uri=http%3A%2F%2F127.0.0.1%3A8932%2Fcallback', opened[0]
        )
        self.assertIn(opened[0], output.getvalue())
        self.assertTrue(calls[0][1]['code'])
        self.assertEqual(1, len(calls))

    def test_no_open_mode_does_not_launch_browser(self):
        class FakeServer:
            timeout = None

            def __init__(self, address, handler_class):
                self.result = ('code', 'one-time-code', 'state')

            def handle_request(self):
                return

            def server_close(self):
                return

        def unexpected_browser(url):
            raise AssertionError('browser must not open in --no-open mode')

        with mock.patch.dict(
            'os.environ',
            {
                'AI_WORK_OUTLOOK_TENANT_ID': 'tenant',
                'AI_WORK_OUTLOOK_CLIENT_ID': 'client',
            },
        ), mock.patch.object(
            master_oauth, '_CallbackHTTPServer', FakeServer
        ), mock.patch.object(
            master_oauth,
            'exchange_authorization_code',
            return_value={'stored_refresh_token': True},
        ) as exchange:
            master_oauth.bootstrap_master_login(
                open_browser=False,
                browser_opener=unexpected_browser,
                output=lambda text: None,
            )

        exchange.assert_called_once()


if __name__ == '__main__':
    unittest.main()
