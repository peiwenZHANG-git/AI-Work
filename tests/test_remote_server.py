"""End-to-end tests for the loopback-only remote server (ephemeral port)."""

import http.client
import json
import tempfile
import unittest
from pathlib import Path

from windows_gui.remote import auth, policy
from windows_gui.remote.server import RemoteServer


class FakeStoreBackend:
    def __init__(self):
        self.secrets = {}

    def factory(self, service, username):
        server = self

        class Entry:
            def set_secret(self, secret):
                server.secrets[username] = secret

            def get_secret(self):
                return server.secrets.get(username)

            def delete_secret(self):
                return server.secrets.pop(username, None) is not None

        return Entry()


class RemoteServerTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeStoreBackend()
        self.clock = {'now': 1_000.0, 'wall': 1_700_000_000.0}
        self.health_calls = []
        self.authenticator = auth.RemoteAuthenticator(
            secret_store=auth.DeviceSecretStore(self.backend.factory),
            now_factory=lambda: self.clock['now'],
            wall_factory=lambda: self.clock['wall'],
        )
        self.server = RemoteServer(
            port=0,
            authenticator=self.authenticator,
            limiter=policy.RateLimiter(now_factory=lambda: self.clock['now']),
            pepper_provider=lambda: 'test-pepper',
            health_collector=self._fake_health,
            devices_path=self._devices_path(),
        )
        self.server.start()
        self.addCleanup(self.server.stop)

    def _devices_path(self):
        if not hasattr(self, '_devices_dir'):
            self._devices_dir = tempfile.TemporaryDirectory()
            self.addCleanup(self._devices_dir.cleanup)
        return Path(self._devices_dir.name) / 'devices.json'

    def _fake_health(self):
        self.health_calls.append(1)
        return {
            'overall_status': 'PASS',
            'components': [
                {'component': 'mcp', 'status': 'PASS', 'summary': 'x'},
                {'component': 'remote', 'status': 'PASS', 'summary': 'y'},
            ],
        }

    def enroll(self, name='test device'):
        code = self.authenticator.start_pairing()
        return self.authenticator.claim_pairing(code, name)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.server.port, timeout=5,
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            return response.status, data
        finally:
            connection.close()

    def post_session(self, device_id, secret, *, nonce='n1', timestamp=None,
                     signature=None, host='loopback'):
        stamp = int(self.clock['wall'] if timestamp is None else timestamp)
        sig = signature or auth.sign_request(
            secret, 'POST', '/session', auth.body_fingerprint(b''), nonce, stamp,
        )
        headers = {
            'X-Remote-Device': device_id,
            'X-Remote-Nonce': nonce,
            'X-Remote-Timestamp': str(stamp),
            'X-Signature': sig,
        }
        if host == 'loopback':
            headers['Host'] = f'127.0.0.1:{self.server.port}'
        elif host is not None:
            headers['Host'] = host
        return self.request('POST', '/session', body=b'', headers=headers)

    def authed_headers(self, token):
        return {
            'Authorization': f'Bearer {token}',
            'Host': f'127.0.0.1:{self.server.port}',
        }

    def test_session_round_trip_and_health_read(self):
        device_id, secret = self.enroll()
        status, data = self.post_session(device_id, secret)
        self.assertEqual(200, status)
        token = json.loads(data)['session']
        status, data = self.request(
            'GET', '/health', headers=self.authed_headers(token),
        )
        self.assertEqual(200, status)
        payload = json.loads(data)
        self.assertEqual('PASS', payload['overall_status'])
        self.assertEqual(
            [{'component': 'mcp', 'status': 'PASS'},
             {'component': 'remote', 'status': 'PASS'}],
            payload['components'],
        )

    def test_wrong_host_is_rejected(self):
        device_id, secret = self.enroll()
        status, data = self.post_session(
            device_id, secret, host='localhost:8932',
        )
        self.assertEqual(403, status)
        self.assertEqual({'error': 'forbidden'}, json.loads(data))

    def test_origin_header_is_rejected(self):
        device_id, secret = self.enroll()
        stamp = self.clock['wall']
        sig = auth.sign_request(
            secret, 'POST', '/session',
            auth.body_fingerprint(b''), 'n1', stamp,
        )
        status, data = self.request(
            'POST', '/session', body=b'',
            headers={
                'Host': f'127.0.0.1:{self.server.port}',
                'Origin': 'https://evil.example',
                'X-Remote-Device': device_id,
                'X-Remote-Nonce': 'n1',
                'X-Remote-Timestamp': str(stamp),
                'X-Signature': sig,
            },
        )
        self.assertEqual(403, status)

    def test_bad_signature_maps_to_fixed_auth_failed(self):
        device_id, _ = self.enroll()
        status, data = self.post_session(device_id, 'wrong-secret')
        self.assertEqual(401, status)
        self.assertEqual({'error': 'auth_failed'}, json.loads(data))

    def test_replayed_nonce_maps_to_replay_detected(self):
        device_id, secret = self.enroll()
        self.post_session(device_id, secret, nonce='n1')
        status, data = self.post_session(device_id, secret, nonce='n1')
        self.assertEqual(401, status)
        self.assertEqual({'error': 'replay_detected'}, json.loads(data))

    def test_stale_timestamp_maps_to_timestamp_invalid(self):
        device_id, secret = self.enroll()
        stale = self.clock['wall'] - auth.TIMESTAMP_WINDOW_SECONDS - 5
        status, data = self.post_session(device_id, secret, timestamp=stale)
        self.assertEqual(401, status)
        self.assertEqual({'error': 'timestamp_invalid'}, json.loads(data))

    def test_health_requires_session(self):
        status, data = self.request('GET', '/health', headers={
            'Host': f'127.0.0.1:{self.server.port}',
        })
        self.assertEqual(401, status)
        self.assertEqual({'error': 'auth_failed'}, json.loads(data))

    def test_task_status_returns_empty_until_staging_exists(self):
        device_id, secret = self.enroll()
        _, data = self.post_session(device_id, secret)
        token = json.loads(data)['session']
        body = json.dumps({
            'command': 'task.status', 'request_id': 'a' * 16, 'params': {},
        }).encode('utf-8')
        status, data = self.request(
            'POST', '/command', body=body, headers=self.authed_headers(token),
        )
        self.assertEqual(200, status)
        self.assertEqual({'tasks': []}, json.loads(data))

    def test_staging_command_is_unavailable_in_3b2(self):
        device_id, secret = self.enroll()
        _, data = self.post_session(device_id, secret)
        token = json.loads(data)['session']
        body = json.dumps({
            'command': 'browser.request_click',
            'request_id': 'a' * 16,
            'params': {'text': 'Submit'},
        }).encode('utf-8')
        status, data = self.request(
            'POST', '/command', body=body, headers=self.authed_headers(token),
        )
        self.assertEqual(400, status)
        self.assertEqual({'error': 'unavailable_command'}, json.loads(data))

    def test_unknown_command_is_rejected(self):
        device_id, secret = self.enroll()
        _, data = self.post_session(device_id, secret)
        token = json.loads(data)['session']
        body = json.dumps({
            'command': 'shell.exec', 'request_id': 'a' * 16, 'params': {},
        }).encode('utf-8')
        status, data = self.request(
            'POST', '/command', body=body, headers=self.authed_headers(token),
        )
        self.assertEqual(400, status)
        self.assertEqual({'error': 'unknown_command'}, json.loads(data))

    def test_health_rate_limit_per_device(self):
        device_id, secret = self.enroll()
        _, data = self.post_session(device_id, secret)
        token = json.loads(data)['session']
        headers = self.authed_headers(token)
        statuses = []
        last_payload = None
        for _ in range(31):
            status, data = self.request('GET', '/health', headers=headers)
            statuses.append(status)
            last_payload = json.loads(data)
        self.assertEqual([200] * 30, statuses[:30])
        self.assertEqual(429, statuses[30])
        self.assertEqual({'error': 'rate_limited'}, last_payload)

    def test_oversized_body_is_rejected(self):
        device_id, secret = self.enroll()
        _, data = self.post_session(device_id, secret)
        token = json.loads(data)['session']
        status, data = self.request(
            'POST', '/command',
            body=b'x' * 257 * 1024,
            headers=self.authed_headers(token),
        )
        self.assertEqual(413, status)

    def test_unknown_path_is_404_and_root_page_is_html(self):
        status, _ = self.request('GET', '/nope', headers={
            'Host': f'127.0.0.1:{self.server.port}',
        })
        self.assertEqual(404, status)
        status, data = self.request('GET', '/', headers={
            'Host': f'127.0.0.1:{self.server.port}',
        })
        self.assertEqual(200, status)
        self.assertIn(b'loopback', data)

    def test_restart_invalidates_sessions(self):
        device_id, secret = self.enroll()
        _, data = self.post_session(device_id, secret)
        token = json.loads(data)['session']
        self.server.stop()
        self.server.authenticator = auth.RemoteAuthenticator(
            secret_store=auth.DeviceSecretStore(self.backend.factory),
            now_factory=lambda: self.clock['now'],
            wall_factory=lambda: self.clock['wall'],
        )
        self.server.start()
        status, data = self.request(
            'GET', '/health', headers=self.authed_headers(token),
        )
        self.assertEqual(401, status)


if __name__ == '__main__':
    unittest.main()
