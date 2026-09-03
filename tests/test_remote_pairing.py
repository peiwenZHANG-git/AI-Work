"""End-to-end tests for remote pairing, devices, and revocation."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from windows_gui.remote import auth, policy
from windows_gui.remote.server import RemoteServer
from tests.test_remote_server import FakeStoreBackend


class PairingDeviceTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeStoreBackend()
        self.clock = {'now': 1_000.0, 'wall': 1_700_000_000.0}
        self.audit_events = []
        self.devices_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.devices_dir.cleanup)
        self.devices_path = Path(self.devices_dir.name) / 'devices.json'

    def _make_server(self, **overrides):
        authenticator = overrides.pop(
            'authenticator',
            auth.RemoteAuthenticator(
                secret_store=auth.DeviceSecretStore(self.backend.factory),
                now_factory=lambda: self.clock['now'],
                wall_factory=lambda: self.clock['wall'],
            ),
        )
        server = RemoteServer(
            port=0,
            authenticator=authenticator,
            limiter=policy.RateLimiter(now_factory=lambda: self.clock['now']),
            pepper_provider=lambda: 'test-pepper',
            health_collector=lambda: {'overall_status': 'PASS', 'components': []},
            devices_path=self.devices_path,
            **overrides,
        )
        server.start()
        self.addCleanup(server.stop)
        return server

    def request(self, server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            '127.0.0.1', server.port, timeout=5,
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            return response.status, data
        finally:
            connection.close()

    def local(self, server, method, path, body=None):
        headers = {'Host': f'127.0.0.1:{server.port}'}
        return self.request(server, method, path, body=body, headers=headers)

    def _record_audit(self, code, *, outcome=None, device_id=None, pepper=None):
        device_hash = (
            auth.audit_device_hash(device_id, pepper)
            if device_id and pepper else None
        )
        self.audit_events.append((code, outcome, device_hash, pepper))

    def claim(self, server, code, device_name='device', source='203.0.113.9'):
        body = json.dumps({'code': code, 'device_name': device_name}).encode()
        return self.request(server, 'POST', '/pairing/claim', body=body, headers={
            'Host': f'127.0.0.1:{server.port}',
            'X-Test-Source': source,
        })

    def test_pairing_lifecycle_single_use_and_expiry(self):
        server = self._make_server()
        status, data = self.local(server, 'POST', '/local/pairing/start')
        self.assertEqual(200, status)
        code = json.loads(data)['pairing_code']
        self.assertEqual(8, len(code))

        status, data = self.claim(server, code.lower(), 'phone')
        self.assertEqual(200, status)
        enrolled = json.loads(data)
        self.assertEqual(16, len(enrolled['device_id']))
        self.assertTrue(enrolled['secret'])

        status, data = self.claim(server, code)
        self.assertEqual(403, status)
        self.assertEqual({'error': 'pairing_invalid'}, json.loads(data))

    def test_pairing_code_does_not_survive_restart(self):
        server = self._make_server()
        _, data = self.local(server, 'POST', '/local/pairing/start')
        code = json.loads(data)['pairing_code']
        server.stop()
        second = self._make_server()
        status, data = self.claim(second, code)
        self.assertEqual(403, status)
        self.assertEqual({'error': 'pairing_invalid'}, json.loads(data))

    def test_pairing_rate_limit_blocks_brute_force(self):
        server = self._make_server()
        statuses = []
        for index in range(6):
            status, _ = self.claim(server, f'WRONG{index:03}')
            statuses.append(status)
        self.assertEqual([403] * 5, statuses[:5])
        self.assertEqual(429, statuses[5])

    def test_enrolled_device_authenticates_and_persists_across_restart(self):
        server = self._make_server()
        _, data = self.local(server, 'POST', '/local/pairing/start')
        code = json.loads(data)['pairing_code']
        _, data = self.claim(server, code, 'laptop')
        enrolled = json.loads(data)
        device_id, secret = enrolled['device_id'], enrolled['secret']
        server.stop()

        second = self._make_server()
        stamp = int(self.clock['wall'])
        nonce = 'restart-nonce'
        signature = auth.sign_request(
            secret, 'POST', '/session', auth.body_fingerprint(b''), nonce, stamp,
        )
        status, data = self.request(second, 'POST', '/session', body=b'', headers={
            'Host': f'127.0.0.1:{second.port}',
            'X-Remote-Device': device_id,
            'X-Remote-Nonce': nonce,
            'X-Remote-Timestamp': str(stamp),
            'X-Signature': signature,
        })
        self.assertEqual(200, status)

    def test_revocation_survives_restart_and_blocks_authentication(self):
        server = self._make_server()
        _, data = self.local(server, 'POST', '/local/pairing/start')
        code = json.loads(data)['pairing_code']
        _, data = self.claim(server, code, 'tablet')
        device_id = json.loads(data)['device_id']
        status, data = self.local(
            server, 'POST', f'/local/devices/{device_id}/revoke',
        )
        self.assertEqual(200, status)
        status, _ = self.local(
            server, 'POST', f'/local/devices/{device_id}/revoke',
        )
        self.assertEqual(404, status)
        server.stop()

        second = self._make_server()
        stamp = int(self.clock['wall'])
        status, data = self.request(second, 'POST', '/session', body=b'', headers={
            'Host': f'127.0.0.1:{second.port}',
            'X-Remote-Device': device_id,
            'X-Remote-Nonce': 'revived',
            'X-Remote-Timestamp': str(stamp),
            'X-Signature': 'unsigned',
        })
        self.assertEqual(401, status)

    def test_device_list_never_contains_secrets(self):
        server = self._make_server()
        _, data = self.local(server, 'POST', '/local/pairing/start')
        code = json.loads(data)['pairing_code']
        _, data = self.claim(server, code, 'phone')
        secret = json.loads(data)['secret']
        status, data = self.local(server, 'GET', '/local/devices')
        self.assertEqual(200, status)
        payload = json.loads(data)
        self.assertEqual(1, len(payload['devices']))
        self.assertEqual('phone', payload['devices'][0]['name'])
        self.assertEqual('active', payload['devices'][0]['status'])
        self.assertNotIn(secret, repr(payload))
        self.assertNotIn('secret', payload['devices'][0])

    def test_revoke_self_is_session_scoped_and_idempotent(self):
        server = self._make_server(audit_recorder=self._record_audit)
        tokens = {}
        secrets_by_device = {}
        for name in ('device-a', 'device-b'):
            _, data = self.local(server, 'POST', '/local/pairing/start')
            code = json.loads(data)['pairing_code']
            _, data = self.claim(server, code, name)
            enrolled = json.loads(data)
            stamp = int(self.clock['wall'])
            signature = auth.sign_request(
                enrolled['secret'], 'POST', '/session',
                auth.body_fingerprint(b''), f'n-{name}', stamp,
            )
            _, data = self.request(server, 'POST', '/session', body=b'', headers={
                'Host': f'127.0.0.1:{server.port}',
                'X-Remote-Device': enrolled['device_id'],
                'X-Remote-Nonce': f'n-{name}',
                'X-Remote-Timestamp': str(stamp),
                'X-Signature': signature,
            })
            tokens[name] = json.loads(data)['session']
            secrets_by_device[name] = enrolled

        device_a = secrets_by_device['device-a']
        device_b = secrets_by_device['device-b']

        def command(request_id, nonce):
            body = json.dumps({
                'command': 'session.revoke_self',
                'request_id': request_id,
                'params': {},
            }).encode('utf-8')
            stamp = int(self.clock['wall'])
            signature = auth.sign_request(
                device_a['secret'],
                'POST',
                '/command',
                auth.body_fingerprint(body),
                nonce,
                stamp,
                device_id=device_a['device_id'],
            )
            headers = {
                'Host': f'127.0.0.1:{server.port}',
                'Authorization': f"Bearer {tokens['device-a']}",
                'X-Remote-Device': device_a['device_id'],
                'X-Remote-Nonce': nonce,
                'X-Remote-Timestamp': str(stamp),
                'X-Signature': signature,
            }
            return self.request(
                server, 'POST', '/command', body=body, headers=headers,
            )

        status, data = command('a' * 16, 'revoke-nonce-1')
        self.assertEqual(200, status)
        self.assertEqual({'status': 'SESSION_REVOKED'}, json.loads(data))

        status, data = command('a' * 16, 'revoke-nonce-2')
        self.assertEqual(200, status)
        self.assertEqual({'status': 'SESSION_REVOKED'}, json.loads(data))

        status, data = command('b' * 16, 'revoke-nonce-3')
        self.assertEqual(401, status)
        self.assertEqual({'error': 'auth_failed'}, json.loads(data))

        revoked_audit = [
            event for event in self.audit_events
            if event[0] == 'session_revoked'
        ]
        self.assertEqual(1, len(revoked_audit))
        self.assertEqual(16, len(revoked_audit[0][2]))
        self.assertNotIn(device_a['device_id'], repr(self.audit_events))
        self.assertNotIn(tokens['device-a'], repr(self.audit_events))
        self.assertNotIn(device_a['secret'], repr(self.audit_events))

        self.assertIn(f"device_{device_a['device_id']}", self.backend.secrets)
        self.assertIn(f"device_{device_b['device_id']}", self.backend.secrets)
        status, data = self.request(
            server, 'GET', '/health',
            headers={'Authorization': f"Bearer {tokens['device-b']}"},
        )
        self.assertEqual(200, status)

    def test_revoke_all_kills_every_device_and_session(self):
        server = self._make_server()
        tokens = []
        device_ids = []
        for name in ('one', 'two'):
            _, data = self.local(server, 'POST', '/local/pairing/start')
            code = json.loads(data)['pairing_code']
            _, data = self.claim(server, code, name)
            enrolled = json.loads(data)
            device_ids.append(enrolled['device_id'])
            stamp = int(self.clock['wall'])
            signature = auth.sign_request(
                enrolled['secret'], 'POST', '/session',
                auth.body_fingerprint(b''), f'n-{name}', stamp,
            )
            _, data = self.request(server, 'POST', '/session', body=b'', headers={
                'Host': f'127.0.0.1:{server.port}',
                'X-Remote-Device': enrolled['device_id'],
                'X-Remote-Nonce': f'n-{name}',
                'X-Remote-Timestamp': str(stamp),
                'X-Signature': signature,
            })
            tokens.append(json.loads(data)['session'])

        status, data = self.local(server, 'POST', '/local/devices/revoke-all')
        self.assertEqual(200, status)
        self.assertEqual({'revoked': 2}, json.loads(data))
        for token in tokens:
            status, _ = self.request(server, 'GET', '/health', headers={
                'Authorization': f'Bearer {token}',
            })
            self.assertEqual(401, status)
        self.assertEqual({}, self.backend.secrets)

    def test_local_management_endpoints_reject_non_loopback_host(self):
        server = self._make_server()
        status, _ = self.request(server, 'GET', '/local/devices', headers={
            'Host': 'evil.example',
        })
        self.assertEqual(403, status)

    def test_credential_write_failure_fails_pairing_closed(self):
        class BrokenBackend(FakeStoreBackend):
            def factory(self, service, username):
                raise OSError('credential manager unavailable')

        saved_backend = self.backend
        self.backend = BrokenBackend()
        server = self._make_server()
        self.backend = saved_backend
        _, data = self.local(server, 'POST', '/local/pairing/start')
        code = json.loads(data)['pairing_code']
        status, data = self.claim(server, code)
        self.assertEqual(500, status)
        self.assertEqual({'error': 'internal_error'}, json.loads(data))
        status, data = self.local(server, 'GET', '/local/devices')
        self.assertEqual([], json.loads(data)['devices'])

    def test_concurrent_pairing_claims_produce_single_device(self):
        server = self._make_server()
        _, data = self.local(server, 'POST', '/local/pairing/start')
        code = json.loads(data)['pairing_code']
        results = []
        lock = threading.Lock()

        def claim():
            status, data = self.claim(server, code, 'racer')
            with lock:
                results.append((status, data))

        threads = [
            threading.Thread(target=claim) for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        successes = [data for status, data in results if status == 200]
        self.assertEqual(1, len(successes))


if __name__ == '__main__':
    unittest.main()
