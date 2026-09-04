import hashlib
import http.client
import json
import datetime
import tempfile
import unittest
from pathlib import Path

from windows_gui.remote.adapters import RemoteAdapters
from windows_gui.remote.auth import DeviceSecretStore, RemoteAuthenticator, sign_request, body_fingerprint
from windows_gui.remote.client import RemoteClient, RemoteClientError
from windows_gui.remote.config import LanConfig
from windows_gui.remote.lan_server import LanServer
from windows_gui.remote.local_plane import LocalPlaneServer
from windows_gui.remote.network import InterfaceSnapshot
from windows_gui.remote.pairing import PendingPairingManager
from windows_gui.remote.policy import RateLimiter
from windows_gui.remote.server import RemoteServer
from windows_gui.remote.tls import TlsManager


class FakeEntry:
    def __init__(self, store, username):
        self.store = store
        self.username = username

    def set_secret(self, value):
        self.store.values[self.username] = value

    def get_secret(self):
        return self.store.values.get(self.username)

    def delete_secret(self):
        return self.store.values.pop(self.username, None) is not None


class FakeStore:
    def __init__(self):
        self.values = {}

    def factory(self, service, username):
        _ = service
        return FakeEntry(self, username)


class FakeTlsStore(FakeStore):
    def factory(self, username):
        return FakeEntry(self, username)


def fingerprint(command, params):
    return hashlib.sha256(json.dumps(
        {'command': command, 'params': params},
        sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')).hexdigest()


class LanIsolationTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeStore()
        self.clock = {'now': 1_000.0, 'wall': 1_700_000_000.0}
        self.clock['wall_datetime'] = datetime.datetime.now(datetime.timezone.utc)
        self.authenticator = RemoteAuthenticator(
            secret_store=DeviceSecretStore(self.backend.factory),
            now_factory=lambda: self.clock['now'],
            wall_factory=lambda: self.clock['wall'],
        )
        self.remote = RemoteServer(
            port=0,
            authenticator=self.authenticator,
            limiter=RateLimiter(now_factory=lambda: self.clock['now']),
            pepper_provider=lambda: 'test-pepper',
            health_collector=lambda: {'overall_status': 'PASS', 'components': []},
            devices_path=self._temporary_path('devices.json'),
            adapters=RemoteAdapters(audit=lambda *args, **kwargs: None),
        )
        self.remote.start()
        self.addCleanup(self.remote.stop)
        self.pairing = PendingPairingManager(
            self.authenticator,
            now_factory=lambda: self.clock['now'],
            audit=lambda *args, **kwargs: None,
        )
        self.tls = TlsManager(
            store_factory=FakeTlsStore().factory,
            now_factory=lambda: self.clock['wall_datetime'],
        )
        config = LanConfig(
            enabled=True,
            interface_id='test-loopback',
            bind_ip='127.0.0.1',
            port=0,
            allowed_remote_subnet='127.0.0.0/8',
        )
        self.lan = LanServer(
            config=config,
            remote=self.remote,
            pairing=self.pairing,
            tls_manager=self.tls,
            collector=lambda _config: InterfaceSnapshot(
                interface_id='test-loopback', ip_address='127.0.0.1',
                status='Preferred', profile='Private',
                hardware_interface=True, interface_description='Test Ethernet Adapter',
            ),
            monitor_interval=None,
            validate_config=False,
        )
        self.lan.start()
        self.addCleanup(self.lan.stop)
        self.client = RemoteClient(
            host='127.0.0.1',
            port=self.lan.port,
            spki_sha256=self.lan.tls_material.spki_sha256,
            wall_factory=lambda: self.clock['wall'],
        )

    def _temporary_path(self, name):
        if not hasattr(self, '_temp_dir'):
            self._temp_dir = tempfile.TemporaryDirectory()
            self.addCleanup(self._temp_dir.cleanup)
        return Path(self._temp_dir.name) / name

    def enroll(self, name='device'):
        return self.authenticator.claim_pairing(
            self.authenticator.start_pairing(), name,
        )

    def raw_http(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.lan.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_lan_handler_has_no_local_routes(self):
        status, _ = self.client._request(
            'GET', '/local/confirmations',
            headers={'Host': f'127.0.0.1:{self.lan.port}'},
        )
        self.assertEqual(404, status)
        status, _ = self.client._request(
            'POST', '/local/pairing/start', body=b'{}',
            headers={
                'Host': f'127.0.0.1:{self.lan.port}',
                'Content-Type': 'application/json',
            },
        )
        self.assertEqual(404, status)
        status, _ = self.client._request(
            'POST', '/local/confirmations/x/approve', body=b'{}',
            headers={
                'Host': f'127.0.0.1:{self.lan.port}',
                'Content-Type': 'application/json',
            },
        )
        self.assertEqual(404, status)

    def test_plaintext_connection_is_not_served(self):
        with self.assertRaises(Exception):
            self.raw_http('GET', '/health', headers={'Host': 'x'})

    def test_wrong_pinned_fingerprint_fails_before_http(self):
        client = RemoteClient(
            host='127.0.0.1', port=self.lan.port, spki_sha256='0' * 64,
        )
        with self.assertRaises(RemoteClientError):
            client.open_session(device_id='x', secret='y')

    def test_query_string_is_rejected(self):
        device_id, secret = self.enroll()
        self.client.open_session(device_id=device_id, secret=secret)
        with self.assertRaises(RemoteClientError):
            self.client._request(
                'GET', '/health?x=1',
                headers={'Host': f'127.0.0.1:{self.lan.port}'},
            )

    def test_tls_auth_hmac_replay_and_revocation_remain_required(self):
        device_id, secret = self.enroll()
        self.client.open_session(device_id=device_id, secret=secret)
        result = self.client.health()
        self.assertEqual('PASS', result['overall_status'])

        body = json.dumps({
            'command': 'health.read',
            'request_id': 'a' * 16,
            'params': {},
        }, separators=(',', ':')).encode('utf-8')
        nonce = 'replay-nonce'
        stamp = int(self.clock['wall'])
        signature = sign_request(
            secret, 'POST', '/command', body_fingerprint(body), nonce, stamp,
            device_id=device_id,
        )
        headers = {
            'Host': f'127.0.0.1:{self.lan.port}',
            'X-Remote-Device': device_id,
            'X-Remote-Nonce': nonce,
            'X-Remote-Timestamp': str(stamp),
            'X-Signature': signature,
            'Authorization': f'Bearer {self.client.session}',
            'Content-Type': 'application/json',
        }
        status, _ = self.client._request(
            'POST', '/command', body=body, headers=headers,
        )
        self.assertEqual(200, status)
        status, data = self.client._request(
            'POST', '/command', body=body, headers=headers,
        )
        self.assertEqual(401, status)
        self.assertEqual(b'{"error":"replay_detected"}', data)

        wrong_signature = sign_request(
            'wrong-secret', 'POST', '/command', body_fingerprint(body),
            'wrong-nonce', stamp, device_id=device_id,
        )
        headers['X-Remote-Nonce'] = 'wrong-nonce'
        headers['X-Signature'] = wrong_signature
        status, _ = self.client._request(
            'POST', '/command', body=body, headers=headers,
        )
        self.assertEqual(401, status)

        self.assertTrue(self.authenticator.revoke_device(device_id))
        with self.assertRaises(RemoteClientError):
            self.client.health()

    def test_pending_pairing_completes_after_local_plane_approval(self):
        local_plane = LocalPlaneServer(
            remote=self.remote,
            pairing=self.pairing,
            port=0,
            lan_bootstrap=lambda: {
                'endpoint': f'127.0.0.1:{self.lan.port}',
                'spki_sha256': self.lan.tls_material.spki_sha256,
            },
        )
        local_plane.start()
        self.addCleanup(local_plane.stop)
        started = self.pairing.start_local()
        pending = self.client.pairing_pending(
            pairing_code=started['pairing_code'], device_name='Phone',
        )
        connection = http.client.HTTPConnection(
            '127.0.0.1', local_plane.port, timeout=3,
        )
        try:
            connection.request(
                'POST', f"/local/pairing/{pending['request_id']}/approve",
                headers={'Host': f'127.0.0.1:{local_plane.port}'},
            )
            self.assertEqual(200, connection.getresponse().status)
        finally:
            connection.close()
        credential = self.client.pairing_complete(
            request_id=pending['request_id'],
            claim_token=pending['claim_token'],
            client_nonce=pending['client_nonce'],
        )
        self.client.open_session(
            device_id=credential['device_id'], secret=credential['secret'],
        )
        self.assertEqual('PASS', self.client.health()['overall_status'])


if __name__ == '__main__':
    unittest.main()
