import datetime
import unittest

from cryptography.hazmat.primitives import serialization

from windows_gui.remote.tls import (
    TlsManager, certificate_from_pem, client_pinned_spki, generate_material,
    load_material, server_ssl_context, spki_sha256,
)


class FakeEntry:
    def __init__(self, store, username):
        self._store = store
        self._username = username

    def set_secret(self, value):
        self._store.values[self._username] = value

    def get_secret(self):
        return self._store.values.get(self._username)

    def delete_secret(self):
        return self._store.values.pop(self._username, None) is not None


class FakeStore:
    def __init__(self):
        self.values = {}

    def factory(self, username):
        return FakeEntry(self, username)


class TlsTests(unittest.TestCase):
    def test_generated_material_pins_spki_and_matches_key(self):
        material, key = generate_material(
            server_id='test', bind_ip='192.168.10.20',
        )
        self.assertRegex(material.spki_sha256, r'^[0-9a-f]{64}$')
        loaded, loaded_key = load_material(
            material.private_key_pem, material.certificate_pem,
        )
        self.assertEqual(material.spki_sha256, loaded.spki_sha256)
        self.assertEqual(
            key.public_key().public_numbers(),
            loaded_key.public_key().public_numbers(),
        )
        server_ssl_context(material)

    def test_client_rejects_wrong_pin_without_system_trust(self):
        material, _ = generate_material(server_id='test', bind_ip='192.168.10.20')
        der = certificate_from_pem(material.certificate_pem).public_bytes(
            serialization.Encoding.DER,
        )
        client_pinned_spki(der, material.spki_sha256)
        with self.assertRaises(Exception):
            client_pinned_spki(der, '0' * 64)
        with self.assertRaises(Exception):
            client_pinned_spki(
                der, material.spki_sha256,
                now=datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=100),
            )

    def test_manager_creates_and_renews_certificate_with_same_key_pin(self):
        store = FakeStore()
        start = datetime.datetime.now(datetime.timezone.utc)
        clock = {'now': start}
        manager = TlsManager(
            store_factory=store.factory,
            now_factory=lambda: clock['now'],
        )
        first = manager.load_or_create(
            server_id='test', bind_ip='192.168.10.20',
        )
        clock['now'] += datetime.timedelta(days=80)
        second = manager.load_or_create(
            server_id='test', bind_ip='192.168.10.20',
        )
        self.assertEqual(first.spki_sha256, second.spki_sha256)
        self.assertGreater(
            second.certificate_expires_at,
            first.certificate_expires_at,
        )
        self.assertEqual(
            first.private_key_pem, second.private_key_pem,
        )

    def test_manager_rejects_incomplete_material(self):
        store = FakeStore()
        store.values['tls_private_key'] = 'partial'
        manager = TlsManager(
            store_factory=store.factory,
            now_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        )
        with self.assertRaises(ValueError):
            manager.load_or_create(server_id='test', bind_ip='192.168.10.20')

    def test_manager_reissues_certificate_for_changed_approved_address(self):
        store = FakeStore()
        manager = TlsManager(
            store_factory=store.factory,
            now_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        )
        first = manager.load_or_create(
            server_id='test', bind_ip='192.168.10.20',
        )
        second = manager.load_or_create(
            server_id='test', bind_ip='192.168.10.21',
        )
        self.assertEqual(first.spki_sha256, second.spki_sha256)
        self.assertNotEqual(first.certificate_pem, second.certificate_pem)


if __name__ == '__main__':
    unittest.main()
