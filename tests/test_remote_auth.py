"""Unit tests for remote authentication primitives (no networking)."""

import unittest

from windows_gui.remote import auth


class FakeEntry:
    def __init__(self, store, username):
        self._store = store
        self._username = username

    def set_secret(self, secret):
        self._store.secrets[self._username] = secret

    def get_secret(self):
        return self._store.secrets.get(self._username)

    def delete_secret(self):
        return self._store.secrets.pop(self._username, None) is not None


class FakeStoreBackend:
    def __init__(self):
        self.secrets = {}

    def factory(self, service, username):
        return FakeEntry(self, username)


def make_authenticator():
    backend = FakeStoreBackend()
    clock = {'now': 1_000.0, 'wall': 1_700_000_000.0}
    authenticator = auth.RemoteAuthenticator(
        secret_store=auth.DeviceSecretStore(backend.factory),
        now_factory=lambda: clock['now'],
        wall_factory=lambda: clock['wall'],
    )
    return authenticator, backend, clock


def enroll(authenticator, backend, name='test device'):
    code = authenticator.start_pairing()
    return authenticator.claim_pairing(code, name)


class PairingTests(unittest.TestCase):
    def test_pairing_code_shape_and_single_active_code(self):
        authenticator, _, _ = make_authenticator()
        first = authenticator.start_pairing()
        second = authenticator.start_pairing()
        self.assertEqual(8, len(first))
        self.assertTrue(set(first) <= set(auth.PAIRING_CODE_ALPHABET))
        with self.assertRaises(auth.PairingCodeInvalidError):
            authenticator.claim_pairing(first, 'late device')
        device_id, secret = authenticator.claim_pairing(second, 'device')
        self.assertEqual(16, len(device_id))
        self.assertTrue(all(char in '0123456789abcdef' for char in device_id))
        self.assertTrue(secret)

    def test_claim_stores_device_secret_and_registers_device(self):
        authenticator, backend, _ = make_authenticator()
        device_id, secret = enroll(authenticator, backend, 'phone')
        self.assertEqual(secret, backend.secrets[f'device_{device_id}'])
        devices = authenticator.list_devices()
        self.assertEqual(1, len(devices))
        self.assertEqual('phone', devices[0].name)

    def test_expired_pairing_code_is_rejected(self):
        authenticator, _, clock = make_authenticator()
        code = authenticator.start_pairing()
        clock['now'] += auth.PAIRING_CODE_TTL_SECONDS + 1
        with self.assertRaises(auth.PairingCodeExpiredError):
            authenticator.claim_pairing(code, 'device')

    def test_revoke_device_removes_sessions_and_secret(self):
        authenticator, backend, _ = make_authenticator()
        device_id, _ = enroll(authenticator, backend)
        token = authenticator.create_session(device_id)
        self.assertTrue(authenticator.revoke_device(device_id))
        self.assertNotIn(f'device_{device_id}', backend.secrets)
        with self.assertRaises(auth.SessionError):
            authenticator.validate_session(token)
        self.assertFalse(authenticator.revoke_device(device_id))


class RequestAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.authenticator, self.backend, self.clock = make_authenticator()
        self.device_id, self.secret = enroll(self.authenticator, self.backend)

    def _signed_authenticate(
        self, *, secret=None, sign_method='POST', send_method='POST',
        sign_path='/command', send_path='/command', sign_body=b'{}',
        send_body=None, nonce='n1', sign_timestamp=None,
        send_timestamp=None, signature=None, device_id=None,
    ):
        body = sign_body if send_body is None else send_body
        stamp = self.clock['wall'] if sign_timestamp is None else sign_timestamp
        body_hash = auth.body_fingerprint(sign_body)
        if signature is not None:
            sig = signature
        else:
            try:
                sig = auth.sign_request(
                    secret or self.secret, sign_method, sign_path,
                    body_hash, nonce, stamp,
                )
            except (TypeError, ValueError):
                sig = 'unsigned'
        send_stamp = stamp if send_timestamp is None else send_timestamp
        self.authenticator.authenticate(
            device_id or self.device_id, nonce, send_stamp, sig,
            send_method, send_path, body,
        )

    def test_valid_signature_authenticates(self):
        self._signed_authenticate()

    def test_unknown_device_is_rejected(self):
        with self.assertRaises(auth.UnknownDeviceError):
            self._signed_authenticate(device_id='ffffffffffffffff')

    def test_missing_device_secret_fails_closed(self):
        self.backend.secrets.pop(f'device_{self.device_id}')
        with self.assertRaises(auth.UnknownDeviceError):
            self._signed_authenticate()

    def test_malformed_timestamp_is_rejected(self):
        with self.assertRaises(auth.TimestampError):
            self._signed_authenticate(send_timestamp='not-a-number')

    def test_timestamp_outside_window_is_rejected(self):
        stale = self.clock['wall'] - auth.TIMESTAMP_WINDOW_SECONDS - 1
        with self.assertRaises(auth.TimestampError):
            self._signed_authenticate(send_timestamp=stale)

    def test_replayed_nonce_is_rejected(self):
        self._signed_authenticate()
        with self.assertRaises(auth.ReplayError):
            self._signed_authenticate()

    def test_tampered_body_is_rejected(self):
        with self.assertRaises(auth.SignatureError):
            self._signed_authenticate(send_body=b'{"tampered": true}')

    def test_wrong_secret_is_rejected(self):
        with self.assertRaises(auth.SignatureError):
            self._signed_authenticate(secret='wrong-secret-value')

    def test_method_and_path_are_bound_to_signature(self):
        with self.assertRaises(auth.SignatureError):
            self._signed_authenticate(send_method='GET')
        with self.assertRaises(auth.SignatureError):
            self._signed_authenticate(send_path='/other')


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.authenticator, self.backend, self.clock = make_authenticator()
        self.device_id, _ = enroll(self.authenticator, self.backend)

    def test_create_and_validate_session(self):
        token = self.authenticator.create_session(self.device_id)
        self.assertEqual(self.device_id, self.authenticator.validate_session(token))

    def test_unknown_session_is_rejected(self):
        with self.assertRaises(auth.SessionError):
            self.authenticator.validate_session('missing-token')

    def test_idle_expiry(self):
        token = self.authenticator.create_session(self.device_id)
        self.clock['now'] += auth.SESSION_IDLE_TTL_SECONDS + 1
        with self.assertRaises(auth.SessionError):
            self.authenticator.validate_session(token)

    def test_absolute_expiry_even_with_activity(self):
        token = self.authenticator.create_session(self.device_id)
        deadline = 1_000 + auth.SESSION_ABSOLUTE_TTL_SECONDS
        step = auth.SESSION_IDLE_TTL_SECONDS - 1
        while self.clock['now'] + step < deadline:
            self.clock['now'] += step
            self.assertEqual(
                self.device_id, self.authenticator.validate_session(token)
            )
        self.clock['now'] = deadline + 1
        with self.assertRaises(auth.SessionError):
            self.authenticator.validate_session(token)

    def test_session_cap_evicts_oldest_per_device(self):
        first = self.authenticator.create_session(self.device_id)
        second = self.authenticator.create_session(self.device_id)
        third = self.authenticator.create_session(self.device_id)
        with self.assertRaises(auth.SessionError):
            self.authenticator.validate_session(first)
        self.assertEqual(self.device_id, self.authenticator.validate_session(second))
        self.assertEqual(self.device_id, self.authenticator.validate_session(third))

    def test_revoke_session_and_device_sessions(self):
        first = self.authenticator.create_session(self.device_id)
        second = self.authenticator.create_session(self.device_id)
        self.assertTrue(self.authenticator.revoke_session(first))
        with self.assertRaises(auth.SessionError):
            self.authenticator.validate_session(first)
        self.assertEqual(1, self.authenticator.revoke_device_sessions(self.device_id))


class AuditHashTests(unittest.TestCase):
    def test_audit_hash_is_stable_opaque_and_short(self):
        first = auth.audit_device_hash('device-1', 'pepper')
        second = auth.audit_device_hash('device-1', 'pepper')
        other = auth.audit_device_hash('device-2', 'pepper')
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(16, len(first))
        self.assertNotIn('device-1', first)


if __name__ == '__main__':
    unittest.main()
