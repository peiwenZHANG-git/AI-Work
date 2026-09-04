import unittest

from windows_gui.remote.auth import RemoteAuthenticator, DeviceSecretStore
from windows_gui.remote.pairing import PairingError, PendingPairingManager


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


def make_manager():
    backend = FakeStore()
    clock = {'now': 1_000.0, 'wall': 1_700_000_000.0}
    authenticator = RemoteAuthenticator(
        secret_store=DeviceSecretStore(backend.factory),
        now_factory=lambda: clock['now'],
        wall_factory=lambda: clock['wall'],
    )
    events = []
    manager = PendingPairingManager(
        authenticator,
        now_factory=lambda: clock['now'],
        audit=lambda code, **kwargs: events.append((code, kwargs)),
    )
    return manager, authenticator, backend, clock, events


class PendingPairingTests(unittest.TestCase):
    def test_complete_before_local_approval_cannot_enroll(self):
        manager, authenticator, backend, _, _ = make_manager()
        started = manager.start_local()
        pending = manager.create_pending(
            started['pairing_code'], 'Phone', '203.0.113.20',
        )
        with self.assertRaises(PairingError):
            manager.complete(
                pending['request_id'], pending['claim_token'],
                pending['client_nonce'],
            )
        self.assertFalse(backend.values)
        self.assertEqual([], [d for d in authenticator.list_devices()])

    def test_local_approval_is_required_before_credential_delivery(self):
        manager, authenticator, backend, _, _ = make_manager()
        started = manager.start_local()
        pending = manager.create_pending(
            started['pairing_code'], 'Phone', '203.0.113.20',
        )
        self.assertEqual(1, len(manager.list_pending()))
        self.assertTrue(manager.approve(pending['request_id']))
        credential = manager.complete(
            pending['request_id'], pending['claim_token'],
            pending['client_nonce'],
        )
        device_id = credential['device_id']
        self.assertEqual(credential['secret'], backend.values[f'device_{device_id}'])
        session = authenticator.create_session(device_id)
        self.assertEqual(device_id, authenticator.validate_session(session))
        with self.assertRaises(PairingError):
            manager.complete(
                pending['request_id'], pending['claim_token'],
                pending['client_nonce'],
            )

    def test_wrong_code_claim_and_second_pending_are_rejected(self):
        manager, _, _, _, _ = make_manager()
        started = manager.start_local()
        manager.create_pending(started['pairing_code'], 'First', '203.0.113.20')
        with self.assertRaises(PairingError):
            manager.create_pending('BBBBBBBB', 'Second', '203.0.113.21')
        manager2, _, _, _, _ = make_manager()
        manager2.start_local()
        with self.assertRaises(PairingError):
            manager2.create_pending('ZZZZZZZZ', 'Evil', '203.0.113.99')

    def test_expired_pending_request_disappears_and_fails_closed(self):
        manager, _, backend, clock, _ = make_manager()
        started = manager.start_local()
        pending = manager.create_pending(
            started['pairing_code'], 'Phone', '203.0.113.20',
        )
        clock['now'] += 601
        self.assertEqual([], manager.list_pending())
        self.assertFalse(manager.approve(pending['request_id']))
        with self.assertRaises(PairingError):
            manager.complete(
                pending['request_id'], pending['claim_token'],
                pending['client_nonce'],
            )
        self.assertFalse(backend.values)

    def test_deny_clears_request_without_device(self):
        manager, authenticator, backend, _, _ = make_manager()
        started = manager.start_local()
        pending = manager.create_pending(
            started['pairing_code'], 'Unknown', '203.0.113.20',
        )
        self.assertTrue(manager.deny(pending['request_id']))
        self.assertEqual([], manager.list_pending())
        self.assertFalse(backend.values)
        self.assertEqual([], authenticator.list_devices())

    def test_approved_but_unclaimed_request_is_revoked_on_expiry(self):
        manager, authenticator, backend, clock, _ = make_manager()
        started = manager.start_local()
        pending = manager.create_pending(
            started['pairing_code'], 'Phone', '203.0.113.20',
        )
        self.assertTrue(manager.approve(pending['request_id']))
        self.assertTrue(backend.values)
        clock['now'] += 601
        self.assertEqual([], manager.list_pending())
        self.assertFalse(backend.values)
        self.assertEqual('revoked', authenticator.list_devices()[0].status)


if __name__ == '__main__':
    unittest.main()
